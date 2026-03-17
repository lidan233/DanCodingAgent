"""
DAG-based task scheduler.

Manages task dependencies, determines which tasks are ready to run,
and computes maximum parallelism from the dependency graph.
"""

import json
import os
import fcntl
import glob as glob_mod
import tempfile
import threading
from pathlib import Path
from collections import deque

# In-process lock for thread safety
_task_file_lock = threading.Lock()


def load_tasks(project_dir: Path) -> list[dict]:
    """Load task_list.json from project directory with file locking."""
    task_file = project_dir / "task_list.json"
    if not task_file.exists():
        return []
    with _task_file_lock:
        try:
            with open(task_file) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (json.JSONDecodeError, IOError):
            return []


def save_tasks(project_dir: Path, tasks: list[dict]) -> None:
    """Save tasks back to task_list.json using atomic write with file locking."""
    task_file = project_dir / "task_list.json"
    with _task_file_lock:
        # Write to a temporary file in the same directory, then atomically replace
        fd, tmp_path = tempfile.mkstemp(dir=project_dir, suffix=".tmp",
                                        prefix=".task_list_")
        try:
            with os.fdopen(fd, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(tasks, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            os.replace(tmp_path, task_file)
        except BaseException:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def build_dag(tasks: list[dict]) -> dict:
    """
    Build adjacency structures from task list.

    Returns dict with:
      - task_map: {id -> task}
      - dependents: {id -> [ids that depend on this]}
      - in_degree: {id -> number of unresolved dependencies}
    """
    task_map = {t["id"]: t for t in tasks}
    dependents = {t["id"]: [] for t in tasks}
    in_degree = {t["id"]: 0 for t in tasks}

    for t in tasks:
        deps = t.get("depends_on", [])
        for dep_id in deps:
            if dep_id in task_map:
                dependents[dep_id].append(t["id"])
                in_degree[t["id"]] += 1

    return {
        "task_map": task_map,
        "dependents": dependents,
        "in_degree": in_degree,
    }


def get_ready_tasks(tasks: list[dict]) -> list[dict]:
    """
    Return tasks that are ready to execute:
    - status is 'pending'
    - all dependencies are 'done'
    """
    task_map = {t["id"]: t for t in tasks}
    ready = []

    for t in tasks:
        if t["status"] != "pending":
            continue

        deps = t.get("depends_on", [])
        all_deps_done = all(
            task_map.get(dep_id, {}).get("status") == "done"
            for dep_id in deps
        )

        if all_deps_done:
            ready.append(t)

    return ready


def get_blocked_tasks(tasks: list[dict]) -> list[dict]:
    """Return tasks that have failed/skipped dependencies (permanently blocked)."""
    task_map = {t["id"]: t for t in tasks}
    blocked = []

    for t in tasks:
        if t["status"] != "pending":
            continue

        deps = t.get("depends_on", [])
        any_dep_failed = any(
            task_map.get(dep_id, {}).get("status") in ("failed", "skipped")
            for dep_id in deps
        )

        if any_dep_failed:
            blocked.append(t)

    return blocked


def compute_max_parallelism(tasks: list[dict]) -> int:
    """
    Compute the maximum number of tasks that can run simultaneously
    using BFS level-based scheduling on the DAG.

    This is the width of the widest level in a topological ordering.
    """
    if not tasks:
        return 0

    task_map = {t["id"]: t for t in tasks}
    in_degree = {}
    dependents = {}

    for t in tasks:
        tid = t["id"]
        in_degree[tid] = 0
        dependents[tid] = []

    for t in tasks:
        for dep_id in t.get("depends_on", []):
            if dep_id in task_map:
                dependents[dep_id].append(t["id"])
                in_degree[t["id"]] += 1

    # BFS by levels
    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    max_width = len(queue) if queue else 1

    while queue:
        next_level = []
        for _ in range(len(queue)):
            tid = queue.popleft()
            for dep_tid in dependents[tid]:
                in_degree[dep_tid] -= 1
                if in_degree[dep_tid] == 0:
                    next_level.append(dep_tid)

        if next_level:
            max_width = max(max_width, len(next_level))
            queue.extend(next_level)

    return max_width


def compute_dag_depth(tasks: list[dict]) -> int:
    """Compute DAG depth (number of BFS levels = minimum waves needed)."""
    if not tasks:
        return 0
    task_map = {t["id"]: t for t in tasks}
    in_degree = {t["id"]: 0 for t in tasks}
    dependents = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep_id in t.get("depends_on", []):
            if dep_id in task_map:
                dependents[dep_id].append(t["id"])
                in_degree[t["id"]] += 1
    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    depth = 0
    while queue:
        depth += 1
        next_level = []
        for _ in range(len(queue)):
            tid = queue.popleft()
            for dep_tid in dependents[tid]:
                in_degree[dep_tid] -= 1
                if in_degree[dep_tid] == 0:
                    next_level.append(dep_tid)
        queue = deque(next_level)
    return depth


def mark_blocked_as_skipped(tasks: list[dict]) -> int:
    """
    Mark pending tasks as 'skipped' if any dependency failed/skipped.
    Returns number of tasks skipped.
    """
    task_map = {t["id"]: t for t in tasks}
    skipped_count = 0
    changed = True

    while changed:
        changed = False
        for t in tasks:
            if t["status"] != "pending":
                continue
            for dep_id in t.get("depends_on", []):
                dep = task_map.get(dep_id, {})
                if dep.get("status") in ("failed", "skipped"):
                    t["status"] = "skipped"
                    t["error_log"] = f"Skipped: dependency task #{dep_id} {dep.get('status')}"
                    skipped_count += 1
                    changed = True
                    break

    return skipped_count


def validate_dag(tasks: list[dict]) -> list[str]:
    """
    Validate the task DAG. Returns list of error messages (empty = valid).
    Checks: no missing deps, no cycles.
    """
    errors = []
    task_ids = {t["id"] for t in tasks}

    # Check for missing dependencies
    for t in tasks:
        for dep_id in t.get("depends_on", []):
            if dep_id not in task_ids:
                errors.append(f"Task #{t['id']} depends on non-existent task #{dep_id}")

    # Check for cycles via topological sort
    in_degree = {t["id"]: 0 for t in tasks}
    dependents = {t["id"]: [] for t in tasks}

    for t in tasks:
        for dep_id in t.get("depends_on", []):
            if dep_id in task_ids:
                dependents[dep_id].append(t["id"])
                in_degree[t["id"]] += 1

    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    visited = 0

    while queue:
        tid = queue.popleft()
        visited += 1
        for dep_tid in dependents[tid]:
            in_degree[dep_tid] -= 1
            if in_degree[dep_tid] == 0:
                queue.append(dep_tid)

    if visited < len(tasks):
        errors.append(f"Cycle detected in task dependencies ({len(tasks) - visited} tasks in cycle)")

    return errors


def count_stats(tasks: list[dict]) -> dict:
    """Count tasks by status."""
    stats = {"total": len(tasks), "done": 0, "failed": 0, "pending": 0,
             "in_progress": 0, "skipped": 0}
    for t in tasks:
        s = t.get("status", "pending")
        if s in stats:
            stats[s] += 1
        else:
            stats["pending"] += 1
    return stats


def is_all_done(tasks: list[dict]) -> bool:
    """Check if all tasks are terminal (done/failed/skipped)."""
    if not tasks:
        return False
    return all(t.get("status") in ("done", "failed", "skipped") for t in tasks)


def apply_task_proposals(project_dir: Path, tasks: list[dict]) -> list[dict]:
    """Scan .context/task_{id}_proposals.json files and apply valid proposals.

    Agents can write proposal files with the format:
    {
      "proposals": [
        {"action": "add", "description": "...", "depends_on": [...], "test_command": "..."},
        {"action": "split", "target_id": 5, "into": [
          {"description": "...", "depends_on": [...], "test_command": "..."},
          {"description": "...", "depends_on": [...], "test_command": "..."}
        ]},
        {"action": "cancel", "target_id": 7, "reason": "..."}
      ]
    }

    Performs DAG validation after each proposal. Invalid proposals are skipped.
    Processed proposal files are renamed with a .applied suffix.

    Returns the updated task list.
    """
    context_dir = project_dir / ".context"
    if not context_dir.exists():
        return tasks

    proposal_pattern = str(context_dir / "task_*_proposals.json")
    proposal_files = sorted(glob_mod.glob(proposal_pattern))

    if not proposal_files:
        return tasks

    existing_ids = {t["id"] for t in tasks}
    max_id = max(existing_ids, default=0)
    applied_count = 0

    for pf_path in proposal_files:
        pf = Path(pf_path)
        try:
            data = json.loads(pf.read_text())
        except (json.JSONDecodeError, IOError):
            # Corrupt file — skip and rename
            try:
                pf.rename(pf.with_suffix(".json.invalid"))
            except OSError:
                pass
            continue

        proposals = data.get("proposals", [])
        if not isinstance(proposals, list):
            pf.rename(pf.with_suffix(".json.invalid"))
            continue

        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            action = proposal.get("action", "")

            if action == "add":
                desc = proposal.get("description")
                if not desc:
                    continue
                max_id += 1
                raw_deps = proposal.get("depends_on", [])
                valid_ids = {t["id"] for t in tasks}
                valid_deps = [d for d in raw_deps if d in valid_ids]

                new_task = {
                    "id": max_id,
                    "description": desc,
                    "depends_on": valid_deps,
                    "test_command": proposal.get("test_command", "echo 'no test'"),
                    "status": "pending",
                    "attempts": 0,
                    "error_log": "",
                }
                # Validate DAG with the candidate task
                candidate = tasks + [new_task]
                errors = validate_dag(candidate)
                if errors:
                    max_id -= 1  # roll back ID
                    continue
                tasks.append(new_task)
                existing_ids.add(max_id)
                applied_count += 1

            elif action == "split":
                target_id = proposal.get("target_id")
                into = proposal.get("into", [])
                if target_id is None or not isinstance(into, list) or len(into) < 2:
                    continue
                # Target must exist and be pending
                target = None
                for t in tasks:
                    if t["id"] == target_id and t["status"] == "pending":
                        target = t
                        break
                if not target:
                    continue

                # Build replacement tasks
                split_tasks = []
                rollback_id = max_id
                for sub in into:
                    if not isinstance(sub, dict) or "description" not in sub:
                        continue
                    max_id += 1
                    raw_deps = sub.get("depends_on", target.get("depends_on", []))
                    valid_ids = {t["id"] for t in tasks} | {st["id"] for st in split_tasks}
                    valid_deps = [d for d in raw_deps if d in valid_ids]

                    split_tasks.append({
                        "id": max_id,
                        "description": sub["description"],
                        "depends_on": valid_deps,
                        "test_command": sub.get("test_command", target.get("test_command", "echo 'no test'")),
                        "status": "pending",
                        "attempts": 0,
                        "error_log": "",
                    })

                if len(split_tasks) < 2:
                    max_id = rollback_id
                    continue

                # Rewire dependents: tasks that depended on target now depend on ALL split tasks
                split_ids = [st["id"] for st in split_tasks]
                candidate = [t for t in tasks if t["id"] != target_id] + split_tasks
                for t in candidate:
                    if target_id in t.get("depends_on", []):
                        t["depends_on"] = [
                            d for d in t["depends_on"] if d != target_id
                        ] + split_ids

                errors = validate_dag(candidate)
                if errors:
                    max_id = rollback_id
                    continue

                # Apply: remove target, add split tasks
                tasks = candidate
                existing_ids.discard(target_id)
                existing_ids.update(st["id"] for st in split_tasks)
                applied_count += 1

            elif action == "cancel":
                target_id = proposal.get("target_id")
                if target_id is None:
                    continue
                # Target must exist and be pending
                target = None
                for t in tasks:
                    if t["id"] == target_id and t["status"] == "pending":
                        target = t
                        break
                if not target:
                    continue
                # Check no other pending/in_progress tasks depend on it
                has_dependents = any(
                    target_id in t.get("depends_on", [])
                    and t["status"] in ("pending", "in_progress")
                    for t in tasks if t["id"] != target_id
                )
                if has_dependents:
                    continue
                target["status"] = "skipped"
                target["error_log"] = f"Cancelled by proposal: {proposal.get('reason', 'no reason')}"
                applied_count += 1

        # Rename processed file
        try:
            pf.rename(pf.with_suffix(".json.applied"))
        except OSError:
            pass

    return tasks
