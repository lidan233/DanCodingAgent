#!/usr/bin/env python3
"""
Auto-Coder Multi-Agent: Parallel autonomous coding with Claude Code.

Reads app_spec.txt, generates a task DAG with dependencies,
and executes tasks in parallel waves using multiple Claude agents.

Usage:
    python run.py --project-dir ./my_project
    python run.py --project-dir ./my_project --max-workers 4
    python run.py --project-dir ./my_project --model claude-opus-4-6
"""

import signal
import subprocess
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import argparse
import json
import time
from pathlib import Path
from string import Template
from concurrent.futures import ThreadPoolExecutor, as_completed

# ──────────────────────────────────────────────────────────
# Thread-safe print wrapper (shared module avoids double-import lock split)
# ──────────────────────────────────────────────────────────

from safe_print import safe_print  # noqa: E402

from scheduler import (
    load_tasks, save_tasks, get_ready_tasks,
    compute_max_parallelism, compute_dag_depth, mark_blocked_as_skipped,
    is_all_done, validate_dag, count_stats, apply_task_proposals,
)
from planner import generate_task_dag, review_plan
from worker import run_claude_session, run_droid_session, terminate_all_processes
from progress import print_progress, print_dag_summary, write_progress_state, colored_tag
from git_ops import (
    ensure_git_repo, auto_commit, create_worktree, merge_worktree, cleanup_worktree,
    detect_file_conflicts as detect_file_conflicts_git, get_head_hash,
)

DEFAULT_MODEL = "claude-opus-4-6"
MAX_TASK_ATTEMPTS = 3
WAVE_DELAY = 3
CONTEXT_DIR = ".context"
MAX_SUMMARY_LEN = 3000
MAX_DEPENDENCY_CONTEXT_LEN = 30_000  # max chars for combined dependency context
DEFAULT_TEST_TIMEOUT = 120      # seconds for test_command
DEFAULT_SESSION_TIMEOUT = 600   # seconds for agent session


def plain_task_tag(task_id: int) -> str:
    """Return a plain-text task tag for non-ANSI log consumers."""
    return f"[Task#{task_id}]"


# ──────────────────────────────────────────────────────────
# Legacy file conflict detection (text-based fallback)
# ──────────────────────────────────────────────────────────

def detect_file_conflicts(project_dir: Path, results: list[dict]) -> list[dict]:
    """Detect files modified by multiple tasks from text output.

    Returns list of conflict dicts: {"file": str, "tasks": [task_ids]}.
    Kept for backward compatibility and non-git test scenarios.
    """
    # Map file -> list of task_ids that touched it
    file_owners: dict[str, list[int]] = {}

    for r in results:
        if not r.get("success"):
            continue
        tid = r["task_id"]

        output = r.get("output", "")
        for line in output.split("\n"):
            stripped = line.strip().lower()
            for kw in ("created ", "wrote ", "modified ", "updated ", "edited "):
                if kw in stripped:
                    for token in line.split():
                        if "/" in token or "." in token:
                            clean = token.strip("`,'\")([]{}")
                            if clean and not clean.startswith("#"):
                                file_owners.setdefault(clean, [])
                                if tid not in file_owners[clean]:
                                    file_owners[clean].append(tid)

    conflicts = [
        {"file": f, "tasks": tids}
        for f, tids in file_owners.items()
        if len(tids) > 1
    ]
    return conflicts


# ──────────────────────────────────────────────────────────
# Context sharing between tasks
# ──────────────────────────────────────────────────────────

def save_task_summary(project_dir: Path, task_id: int, all_text: list[str],
                      commit_hash: str | None = None) -> None:
    """Save a structured JSON summary for downstream tasks using git diff.

    Uses git diff --stat (ground truth) instead of fragile keyword-regex
    extraction from agent output text.  Falls back to the agent's last
    message as secondary context when no commit is available.
    """
    ctx_dir = project_dir / CONTEXT_DIR
    ctx_dir.mkdir(exist_ok=True)

    files_created: list[str] = []
    files_modified: list[str] = []
    diff_stat = ""

    # --- Git-based extraction (primary, reliable) ---
    if commit_hash:
        try:
            # Detect if this is a root commit (no parent) by checking commit_hash~1
            parent_check = subprocess.run(
                ["git", "rev-parse", "--verify", f"{commit_hash}~1"],
                cwd=str(project_dir), capture_output=True, text=True, timeout=10,
            )
            is_root = parent_check.returncode != 0

            if is_root:
                # Root commit: use diff-tree --root to compare against empty tree
                stat_cmd = ["git", "diff-tree", "--stat", "--root", commit_hash]
                ns_cmd = ["git", "diff-tree", "--name-status", "--root", "-r", commit_hash]
            else:
                stat_cmd = ["git", "diff", "--stat", f"{commit_hash}~1", commit_hash]
                ns_cmd = ["git", "diff", "--name-status", f"{commit_hash}~1", commit_hash]

            # Get diff stat for this commit
            stat_result = subprocess.run(
                stat_cmd,
                cwd=str(project_dir), capture_output=True, text=True, timeout=30,
            )
            if stat_result.returncode == 0:
                diff_stat = stat_result.stdout.strip()

            # Get list of changed files with their status (A=added, M=modified, etc.)
            name_status = subprocess.run(
                ns_cmd,
                cwd=str(project_dir), capture_output=True, text=True, timeout=30,
            )
            if name_status.returncode == 0:
                for line in name_status.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        status, filepath = parts
                        if status.startswith("A"):
                            files_created.append(filepath)
                        else:
                            files_modified.append(filepath)
        except Exception:
            pass  # Fall through to agent text fallback

    # --- Agent's last message as secondary context ---
    key_changes = ""
    if all_text:
        last_msg = all_text[-1]
        if len(last_msg) > MAX_SUMMARY_LEN:
            last_msg = last_msg[:MAX_SUMMARY_LEN] + "\n... (truncated)"
        key_changes = last_msg

    # --- Build structured JSON summary ---
    summary_json = {
        "task_id": task_id,
        "commit_hash": commit_hash or "",
        "files_created": files_created,
        "files_modified": files_modified,
        "diff_stat": diff_stat,
        "key_changes": key_changes,
    }

    json_file = ctx_dir / f"task_{task_id}_summary.json"
    json_file.write_text(json.dumps(summary_json, indent=2, ensure_ascii=False))

    # Also write a human-readable .md for backward compatibility
    summary_parts = [f"# Task #{task_id} Completion Summary\n"]
    if files_created:
        summary_parts.append("## Files Created")
        for f in files_created[:20]:
            summary_parts.append(f"- {f}")
        summary_parts.append("")
    if files_modified:
        summary_parts.append("## Files Modified")
        for f in files_modified[:20]:
            summary_parts.append(f"- {f}")
        summary_parts.append("")
    if diff_stat:
        summary_parts.append("## Diff Stat")
        summary_parts.append(f"```\n{diff_stat}\n```\n")
    if key_changes:
        summary_parts.append("## Final Status")
        summary_parts.append(key_changes)

    summary = "\n".join(summary_parts)
    md_file = ctx_dir / f"task_{task_id}_summary.md"
    md_file.write_text(summary)

    safe_print(f"  [context] Saved summary for task #{task_id} "
               f"({len(files_created)} created, {len(files_modified)} modified)")


def _load_single_dep_summary(ctx_dir: Path, dep_id: int) -> str:
    """Load a single dependency summary, preferring JSON over markdown."""
    json_file = ctx_dir / f"task_{dep_id}_summary.json"
    md_file = ctx_dir / f"task_{dep_id}_summary.md"

    if json_file.exists():
        try:
            data = json.loads(json_file.read_text())
            parts = [f"# Task #{dep_id} Completion Summary\n"]
            if data.get("files_created"):
                parts.append("## Files Created")
                for fp in data["files_created"]:
                    parts.append(f"- {fp}")
                parts.append("")
            if data.get("files_modified"):
                parts.append("## Files Modified")
                for fp in data["files_modified"]:
                    parts.append(f"- {fp}")
                parts.append("")
            if data.get("diff_stat"):
                parts.append("## Diff Stat")
                parts.append(f"```\n{data['diff_stat']}\n```\n")
            if data.get("key_changes"):
                parts.append("## Final Status")
                parts.append(data["key_changes"])
            return "\n".join(parts)
        except (json.JSONDecodeError, KeyError):
            if md_file.exists():
                return md_file.read_text()
    elif md_file.exists():
        return md_file.read_text()

    return (
        f"# Task #{dep_id} Completion Summary\n"
        f"(Summary not available — task may not have completed yet)\n"
    )


def load_dependency_context(project_dir: Path, task: dict) -> str:
    """Load structured JSON summaries from all dependency tasks.

    Prefers .context/task_{id}_summary.json (structured, git-based).
    Falls back to .context/task_{id}_summary.md (legacy format).

    Sorts dependencies by relevance (direct first) and enforces
    MAX_DEPENDENCY_CONTEXT_LEN to prevent context window overflow.
    """
    deps = task.get("depends_on", [])
    if not deps:
        return ""

    ctx_dir = project_dir / CONTEXT_DIR

    # Sort deps: direct dependencies (listed first in depends_on) are most
    # relevant; preserve original order as a proxy for relevance ranking.
    # Load all summaries with their dep IDs.
    dep_summaries = []
    for dep_id in deps:
        summary = _load_single_dep_summary(ctx_dir, dep_id)
        dep_summaries.append((dep_id, summary))

    # Build sections respecting the total size budget
    header = (
        "## Context from Completed Dependencies\n\n"
        "The following tasks have been completed before yours. "
        "Use this context to understand what was already done and "
        "what files/structures are available:\n\n"
    )
    budget = MAX_DEPENDENCY_CONTEXT_LEN - len(header)
    sections = []
    used = 0
    truncated_deps = []

    for dep_id, summary in dep_summaries:
        separator = "\n---\n\n" if sections else ""
        entry_len = len(separator) + len(summary)

        if used + entry_len <= budget:
            sections.append(summary)
            used += entry_len
        else:
            # Try to include a truncated version if there's space
            remaining = budget - used - len(separator)
            if remaining > 200:
                truncated = summary[:remaining - 50].rsplit("\n", 1)[0]
                truncated += "\n\n... (truncated due to context size limit)"
                sections.append(truncated)
                used += len(separator) + len(truncated)
                truncated_deps.append(str(dep_id))
            else:
                truncated_deps.append(str(dep_id))
            # No more room for additional deps
            for _, later_dep_id in enumerate(dep_summaries[dep_summaries.index((dep_id, summary)) + 1:]):
                truncated_deps.append(str(later_dep_id[0]))
            break

    if not sections:
        return ""

    result = header + "\n---\n\n".join(sections)

    if truncated_deps:
        result += (
            f"\n\n---\n\n> **Note:** Dependency context was truncated to fit within "
            f"{MAX_DEPENDENCY_CONTEXT_LEN:,} chars. Summaries for task(s) "
            f"{', '.join(truncated_deps)} were truncated or omitted. "
            f"Use Glob/Grep to explore their outputs directly."
        )

    return result


# ──────────────────────────────────────────────────────────
# Session log persistence
# ──────────────────────────────────────────────────────────

def save_session_log(
    project_dir: Path, task_id: int, wave: int,
    all_text: list[str], result: dict,
) -> None:
    """Save full session output for debugging and audit."""
    log_dir = project_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"task_{task_id}_wave_{wave}.log"

    parts = [
        f"# Task #{task_id} — Wave {wave}",
        f"Exit code: {result.get('exit_code', '?')}",
        f"Success: {result.get('success', '?')}",
        f"Duration: {result.get('duration_s', 0):.1f}s",
        f"Turns: {result.get('num_turns', '?')}",
        f"Cost: ${result.get('cost_usd', 0):.4f}",
        "",
        "─" * 60,
        "## Assistant Output",
        "",
    ]
    parts.extend(all_text)
    log_file.write_text("\n".join(parts))


# ──────────────────────────────────────────────────────────
# Dynamic task extraction
# ──────────────────────────────────────────────────────────

def extract_new_tasks(results: list[dict], existing_tasks: list[dict]) -> list[dict]:
    """Extract NEW_TASK: markers from agent output to dynamically add tasks.

    Agents can output lines like:
        NEW_TASK: {"description": "...", "depends_on": [3], "test_command": "..."}

    Returns list of new task dicts (with auto-assigned IDs).
    Validates that depends_on IDs exist and no cycles are introduced.
    """
    existing_ids = {t["id"] for t in existing_tasks}
    max_id = max(existing_ids, default=0)
    new_tasks = []

    for r in results:
        output = r.get("output", "")
        for line in output.split("\n"):
            line = line.strip()
            if not line.startswith("NEW_TASK:"):
                continue
            json_part = line[len("NEW_TASK:"):].strip()
            try:
                task_data = json.loads(json_part)
                if not isinstance(task_data, dict) or "description" not in task_data:
                    continue
                max_id += 1
                raw_deps = task_data.get("depends_on", [r["task_id"]])
                # Validate depends_on: all referenced IDs must exist
                valid_ids = existing_ids | {nt["id"] for nt in new_tasks}
                invalid_deps = [d for d in raw_deps if d not in valid_ids]
                if invalid_deps:
                    safe_print(f"  [dynamic] Rejected deps {invalid_deps} for new task #{max_id} "
                               f"(non-existent IDs), removing them")
                    raw_deps = [d for d in raw_deps if d in valid_ids]

                new_task = {
                    "id": max_id,
                    "description": task_data["description"],
                    "depends_on": raw_deps,
                    "test_command": task_data.get("test_command", "echo 'no test'"),
                    "status": "pending",
                    "attempts": 0,
                    "error_log": "",
                }
                # Check that adding this task doesn't create a cycle
                candidate_tasks = existing_tasks + new_tasks + [new_task]
                cycle_errors = [e for e in validate_dag(candidate_tasks) if "ycle" in e]
                if cycle_errors:
                    safe_print(f"  [dynamic] Rejected new task #{max_id}: would create cycle")
                    continue

                new_tasks.append(new_task)
                safe_print(f"  [dynamic] New task #{max_id}: {new_task['description'][:60]}")
            except (json.JSONDecodeError, KeyError):
                continue

    return new_tasks


# ──────────────────────────────────────────────────────────
# Review via Claude Code agent (read-only session)
# ──────────────────────────────────────────────────────────

DEFAULT_REVIEW_MODEL = "custom:gpt-5.3-codex"
DEFAULT_REVIEW_BACKEND = "droid"  # "claude" or "droid"
REVIEW_CONTEXT_DIR = ".review"
MAX_REVIEW_FIXES = 2  # max fix rounds per task

# Review agents only get read-only tools + Bash for running tests
REVIEW_ALLOWED_TOOLS = "Read Glob Grep Bash"


def _build_review_prompt(task: dict, review_type: str = "general") -> str:
    """Build a review prompt from a review template.

    Args:
        task: Task dict with id, description, test_command.
        review_type: One of 'general', 'functional', 'security'.
            'general' uses review_prompt.md (single mode).
            'functional'/'security' use specialized templates (cross mode).
    """
    template_map = {
        "general": "review_prompt.md",
        "functional": "review_functional.md",
        "security": "review_security.md",
    }
    template_name = template_map.get(review_type, "review_prompt.md")
    template_file = Path(SCRIPT_DIR) / "prompts" / template_name
    tmpl = Template(template_file.read_text())
    return tmpl.safe_substitute(
        task_id=task["id"],
        task_description=task.get("description", ""),
        test_command=task.get("test_command", "echo 'no test command'"),
    )


def _parse_review_result(all_text: list[str]) -> dict:
    """Parse REVIEW_RESULT JSON from agent output text blocks.

    Searches all assistant text blocks for a line matching:
        REVIEW_RESULT: {"passed": ..., "issues": ...}

    Returns {"passed": bool, "issues": str}. Defaults to passed on parse failure.
    """
    import re as _re

    full_text = "\n".join(all_text)

    # Look for REVIEW_RESULT: {...} pattern
    pattern = r'REVIEW_RESULT:\s*(\{.*?\})'
    for match in _re.finditer(pattern, full_text):
        try:
            result = json.loads(match.group(1))
            if "passed" in result:
                return result
        except json.JSONDecodeError:
            continue

    # Fallback: look for the JSON on its own line in the final output
    for line in reversed(full_text.split("\n")):
        line = line.strip()
        if line.startswith("{") and "passed" in line:
            try:
                result = json.loads(line)
                if "passed" in result:
                    return result
            except json.JSONDecodeError:
                continue

    safe_print(f"  [review] Could not parse REVIEW_RESULT from agent output, treating as passed")
    return {"passed": True, "issues": "NONE (parse error)"}


def _run_review_session(
    prompt: str,
    project_dir: Path,
    review_model: str,
    task_id: int | None = None,
    review_backend: str = DEFAULT_REVIEW_BACKEND,
) -> dict:
    """Dispatch a review session to the appropriate backend (claude or droid)."""
    if review_backend == "droid":
        return run_droid_session(
            prompt=prompt,
            project_dir=project_dir,
            model=review_model,
            task_id=task_id,
            allowed_tools=REVIEW_ALLOWED_TOOLS,
        )
    return run_claude_session(
        prompt=prompt,
        project_dir=project_dir,
        model=review_model,
        task_id=task_id,
        allowed_tools=REVIEW_ALLOWED_TOOLS,
    )


def _run_single_review(
    task: dict,
    project_dir: Path,
    review_model: str,
    review_type: str = "general",
    review_backend: str = DEFAULT_REVIEW_BACKEND,
) -> dict:
    """Run a single review session and return parsed result.

    Args:
        review_type: 'general', 'functional', or 'security'.
        review_backend: 'claude' or 'droid'.

    Returns:
        dict with 'passed' (bool), 'issues' (str), and 'review_type' (str).
    """
    task_id = task["id"]
    review_prompt = _build_review_prompt(task, review_type=review_type)

    review_session = _run_review_session(
        prompt=review_prompt,
        project_dir=project_dir,
        review_model=review_model,
        task_id=task_id,
        review_backend=review_backend,
    )

    all_text = review_session.get("all_text", [])
    final_output = review_session.get("output", "")
    if final_output:
        all_text = all_text + [final_output]

    result = _parse_review_result(all_text)
    result["review_type"] = review_type
    return result


def _cross_validate_review(
    task: dict,
    project_dir: Path,
    review_model: str,
    review_backend: str = DEFAULT_REVIEW_BACKEND,
) -> dict:
    """Run functional and security reviewers in parallel (cross-validation).

    Launches both review types via ThreadPoolExecutor. Task passes only if
    both reviewers pass. Issues are concatenated if either fails.

    Returns:
        dict with 'passed' (bool), 'issues' (str), and 'details' (list of per-reviewer results).
    """
    task_id = task["id"]
    review_types = ["functional", "security"]

    safe_print(f"  [review-cross] Task #{task_id}: launching functional + security reviewers in parallel...",
               flush=True)

    details = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(
                _run_single_review, task, project_dir, review_model, rt, review_backend
            ): rt
            for rt in review_types
        }
        for future in as_completed(futures):
            rt = futures[future]
            try:
                result = future.result()
                details.append(result)
                status = "PASSED" if result.get("passed", True) else "FAILED"
                safe_print(f"  [review-cross] Task #{task_id} [{rt}]: {status}", flush=True)
            except Exception as e:
                safe_print(f"  [review-cross] Task #{task_id} [{rt}]: ERROR — {e}", flush=True)
                details.append({"passed": True, "issues": f"Review error: {e}", "review_type": rt})

    # Task passes only if ALL reviewers pass
    all_passed = all(d.get("passed", True) for d in details)

    # Concatenate issues from all failed reviewers
    combined_issues = []
    for d in details:
        if not d.get("passed", True):
            rt = d.get("review_type", "unknown")
            issues = d.get("issues", "Unknown issues")
            combined_issues.append(f"[{rt} review] {issues}")

    return {
        "passed": all_passed,
        "issues": "\n\n".join(combined_issues) if combined_issues else "NONE",
        "details": details,
    }


def review_and_fix_task(
    task: dict,
    project_dir: Path,
    model: str,
    review_model: str = DEFAULT_REVIEW_MODEL,
    review_mode: str = "single",
    review_backend: str = DEFAULT_REVIEW_BACKEND,
) -> None:
    """Review a completed task using an agent session with read-only tools.

    The review agent reads files, runs tests, and outputs a REVIEW_RESULT JSON verdict.
    If issues are found, a fix agent (using the coder model) is dispatched.

    Args:
        review_mode: 'single' for one general reviewer (default),
                     'cross' for parallel functional + security cross-validation.
        review_backend: 'claude' or 'droid'.
    """
    task_id = task["id"]
    review_dir = project_dir / REVIEW_CONTEXT_DIR
    review_dir.mkdir(exist_ok=True)

    for fix_round in range(1, MAX_REVIEW_FIXES + 1):
        safe_print(f"  [review] Task #{task_id}: reviewing with {review_model} "
                   f"(backend={review_backend}, mode={review_mode}, round {fix_round})...", flush=True)

        if review_mode == "cross":
            review_result = _cross_validate_review(task, project_dir, review_model, review_backend)
        else:
            # Single mode: one general reviewer
            review_result = _run_single_review(task, project_dir, review_model, review_type="general",
                                               review_backend=review_backend)

        review_out_file = review_dir / f"task_{task_id}_review_r{fix_round}.json"
        review_out_file.write_text(json.dumps(review_result, ensure_ascii=False, indent=2))

        if review_result.get("passed", True):
            safe_print(f"  [review] Task #{task_id}: PASSED review", flush=True)
            return

        issues = review_result.get("issues", "Unknown issues")
        safe_print(f"  [review] Task #{task_id}: ISSUES FOUND — sending to Claude for fix", flush=True)
        safe_print(f"  [review]   {issues[:200]}", flush=True)

        fix_prompt = (
            f"A code reviewer found issues in Task #{task_id}: {task.get('description', '')}\n\n"
            f"Issues found:\n\n"
            f"{issues}\n\n"
            f"Please fix these issues. After fixing, run the test command to verify:\n"
            f"```bash\n{task.get('test_command', 'echo no test')}\n```\n\n"
            f"Rules:\n"
            f"- Only fix the issues found by the reviewer, do not refactor\n"
            f"- Do NOT modify task_list.json\n"
            f"- Do NOT run git commands\n"
        )

        fix_result = run_claude_session(
            prompt=fix_prompt,
            project_dir=project_dir,
            model=model,
            task_id=task_id,
        )

        if fix_result["exit_code"] != 0:
            safe_print(f"  [review] Fix agent exited with code {fix_result['exit_code']}, stopping review", flush=True)
            return

        fix_texts = fix_result.get("all_text", [])
        if fix_texts:
            fix_file = review_dir / f"task_{task_id}_fix_r{fix_round}.md"
            fix_file.write_text("\n".join(fix_texts))

    safe_print(f"  [review] Task #{task_id}: exhausted {MAX_REVIEW_FIXES} fix rounds", flush=True)


def run_integration_review(
    wave_tasks: list[dict],
    project_dir: Path,
    review_model: str = DEFAULT_REVIEW_MODEL,
    review_backend: str = DEFAULT_REVIEW_BACKEND,
) -> dict:
    """Run an integration review after a wave completes.

    Checks cross-task consistency: shared interfaces, import compatibility,
    and data flow between the tasks that completed in this wave.

    Returns:
        dict with 'passed' (bool) and 'issues' (str).
    """
    task_descriptions = "\n".join(
        f"- Task #{t['id']}: {t.get('description', '')}" for t in wave_tasks
    )
    test_commands = "\n".join(
        f"- Task #{t['id']}: `{t.get('test_command', 'echo no test')}`" for t in wave_tasks
    )

    integration_prompt = (
        "You are an INTEGRATION REVIEWER in a multi-agent development system.\n"
        "Multiple tasks were completed in parallel in this wave. Your job is to check\n"
        "that they integrate correctly with each other.\n\n"
        f"## Tasks completed in this wave\n{task_descriptions}\n\n"
        f"## Test commands\n{test_commands}\n\n"
        "## Review Procedure\n"
        "1. Read the implementations of ALL tasks listed above\n"
        "2. Run each test command to verify they still pass\n"
        "3. Check cross-task integration:\n"
        "   - Do shared imports and interfaces match?\n"
        "   - Are data structures used consistently across tasks?\n"
        "   - Are there conflicting changes to the same files?\n"
        "   - Do function signatures match between callers and callees?\n"
        "4. Output your verdict as a single JSON line:\n"
        '   REVIEW_RESULT: {"passed": true/false, "issues": "description or NONE"}\n\n'
        "## Rules\n"
        "- Do NOT modify any files\n"
        "- Do NOT run git commands\n"
        "- You may read any file and run test commands\n"
        "- Focus on integration issues between the tasks, not individual task correctness\n"
    )

    safe_print(f"  [integration] Reviewing {len(wave_tasks)} tasks for cross-task integration...",
               flush=True)

    session = _run_review_session(
        prompt=integration_prompt,
        project_dir=project_dir,
        review_model=review_model,
        task_id=0,  # integration review is not tied to a single task
        review_backend=review_backend,
    )

    all_text = session.get("all_text", [])
    final_output = session.get("output", "")
    if final_output:
        all_text = all_text + [final_output]

    result = _parse_review_result(all_text)

    review_dir = project_dir / REVIEW_CONTEXT_DIR
    review_dir.mkdir(exist_ok=True)
    task_ids = "_".join(str(t["id"]) for t in wave_tasks)
    out_file = review_dir / f"integration_review_{task_ids}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    status = "PASSED" if result.get("passed", True) else "ISSUES FOUND"
    safe_print(f"  [integration] {status}", flush=True)
    if not result.get("passed", True):
        safe_print(f"  [integration]   {result.get('issues', '')[:200]}", flush=True)

    return result


# ──────────────────────────────────────────────────────────
# Task prompt generation
# ──────────────────────────────────────────────────────────

def build_coder_prompt(task: dict, project_dir: Path, test_timeout: int | None = None) -> str:
    """Build the prompt for a coder agent from the template.

    Args:
        test_timeout: If set, wraps the test command with 'timeout {test_timeout}'
            so test commands are killed if they exceed the limit.
    """
    template_file = Path(SCRIPT_DIR) / "prompts" / "coder_prompt.md"
    tmpl = Template(template_file.read_text())

    dependency_context = load_dependency_context(project_dir, task)

    error_context = ""
    if task.get("attempts", 0) > 0 and task.get("error_log"):
        error_context = (
            f"## Previous Attempt Failed (attempt {task['attempts']}/{MAX_TASK_ATTEMPTS})\n\n"
            f"The previous attempt failed with this error:\n```\n{task['error_log']}\n```\n"
            f"Please analyze the error and fix the issue.\n"
        )

    # Wrap test command with timeout if configured
    test_command = task.get("test_command", "echo 'no test command'")
    if isinstance(test_timeout, (int, float)) and test_timeout > 0:
        test_command = f"timeout {int(test_timeout)} {test_command}"

    return tmpl.safe_substitute(
        task_id=task["id"],
        task_description=task["description"],
        test_command=test_command,
        dependency_context=dependency_context,
        error_context=error_context,
    )


def execute_task(
    task: dict,
    project_dir: Path,
    model: str,
    wave: int = 0,
    test_timeout: int | None = None,
    session_timeout: int | None = None,
    sandbox: bool = True,
    worktree_isolation: bool = False,
) -> dict:
    """
    Execute a single task using Claude.

    When worktree_isolation is True, the task runs in a dedicated git worktree
    so parallel tasks cannot interfere with each other's file changes. After
    success the worktree branch is merged back and cleaned up.

    Returns dict with task_id, success (bool), output, duration_s, cost_usd, num_turns.
    """
    task_id = task["id"]
    work_dir = project_dir
    wt_path = None

    # Create an isolated worktree if requested
    if worktree_isolation:
        try:
            wt_path = create_worktree(project_dir, task_id)
            work_dir = wt_path
        except Exception as e:
            safe_print(f"  {colored_tag(task_id)} Worktree creation failed: {e}, falling back to shared dir")
            wt_path = None
            work_dir = project_dir

    prompt = build_coder_prompt(task, project_dir, test_timeout=test_timeout)

    result = run_claude_session(
        prompt=prompt,
        project_dir=work_dir,
        model=model,
        task_id=task_id,
        session_timeout=session_timeout,
        sandbox=sandbox,
    )

    success = result["exit_code"] == 0
    all_text = result.get("all_text", [])

    # Per-task git commit immediately after success (not per-wave)
    commit_hash = None
    if success:
        desc = task.get("description", "")[:50]
        commit_hash = auto_commit(work_dir, f"Task #{task_id}: {desc}")

    # Save context summary for downstream tasks (after commit, so we have the hash)
    if success and all_text:
        save_task_summary(project_dir, task_id, all_text, commit_hash=commit_hash)

    # Merge worktree branch back and clean up
    if wt_path is not None:
        if success:
            merge_worktree(project_dir, task_id)
        cleanup_worktree(project_dir, task_id)

    task_result = {
        "task_id": task_id,
        "success": success,
        "output": result.get("output", ""),
        "duration_s": result["duration_s"],
        "cost_usd": result.get("cost_usd", 0),
        "num_turns": result.get("num_turns", 0),
        "commit_hash": commit_hash,
    }

    # Save session log
    save_session_log(project_dir, task_id, wave, all_text, task_result)

    return task_result


def run_wave(
    ready_tasks: list[dict],
    project_dir: Path,
    model: str,
    max_workers: int,
    wave: int = 0,
    test_timeout: int | None = None,
    session_timeout: int | None = None,
    sandbox: bool = True,
    worktree_isolation: bool = False,
) -> list[dict]:
    """Execute a wave of tasks in parallel. Returns list of result dicts.

    Registers SIGINT/SIGTERM handlers so that on interrupt:
    1. All running agent subprocesses are terminated
    2. Interrupted tasks are marked as 'pending' (not 'failed')
    3. task_list.json is saved before exiting
    """
    batch = ready_tasks[:max_workers]
    results = []
    interrupted = False

    # Save original signal handlers to restore later
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def _graceful_shutdown(signum, frame):
        nonlocal interrupted
        sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        safe_print(f"\n  [{sig_name}] Graceful shutdown initiated — terminating running agents...")
        interrupted = True

        # Terminate all running agent subprocesses
        interrupted_ids = terminate_all_processes()
        if interrupted_ids:
            safe_print(f"  [shutdown] Terminated {len(interrupted_ids)} agent(s): {interrupted_ids}")

        # Mark interrupted tasks as 'pending' so they can be retried
        tasks = load_tasks(project_dir)
        task_map = {t["id"]: t for t in tasks}
        for tid in interrupted_ids:
            task = task_map.get(tid)
            if task and task.get("status") == "in_progress":
                task["status"] = "pending"
        save_tasks(project_dir, tasks)
        safe_print(f"  [shutdown] Saved task state — interrupted tasks reset to 'pending'")
        safe_print(f"  [shutdown] Re-run to resume.\n")

    # Install signal handlers (only from main thread)
    try:
        signal.signal(signal.SIGINT, _graceful_shutdown)
        signal.signal(signal.SIGTERM, _graceful_shutdown)
    except ValueError:
        # signal.signal can only be called from main thread
        pass

    try:
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {}
            for task in batch:
                future = executor.submit(
                    execute_task, task, project_dir, model, wave,
                    test_timeout, session_timeout, sandbox,
                    worktree_isolation,
                )
                futures[future] = task

            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    safe_print(f"  {colored_tag(task['id'])} Exception: {e}", flush=True)
                    results.append({
                        "task_id": task["id"],
                        "success": False,
                        "output": str(e),
                        "duration_s": 0,
                        "cost_usd": 0,
                        "num_turns": 0,
                    })
    finally:
        # Restore original signal handlers
        try:
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)
        except ValueError:
            pass

    if interrupted:
        # Raise KeyboardInterrupt so the outer loop knows to stop
        raise KeyboardInterrupt("Graceful shutdown completed")

    return results


def update_tasks_from_results(
    project_dir: Path,
    results: list[dict],
    cumulative_cost: float = 0.0,
    max_task_cost: float | None = None,
) -> float:
    """Update task_list.json based on execution results.

    Logs cumulative cost after every task. If a single task exceeds
    max_task_cost, it is marked as failed immediately (no retry).

    Returns updated cumulative cost.
    """
    tasks = load_tasks(project_dir)
    task_map = {t["id"]: t for t in tasks}

    for r in results:
        tid = r["task_id"]
        task = task_map.get(tid)
        if not task:
            continue

        task_cost = r.get("cost_usd", 0)
        cumulative_cost += task_cost

        # Log cumulative cost after every task
        safe_print(f"  [cost] Task #{tid}: ${task_cost:.4f} | Cumulative: ${cumulative_cost:.4f}")

        # Per-task cost circuit breaker
        if max_task_cost is not None and task_cost > max_task_cost:
            task["status"] = "failed"
            task["error_log"] = (
                f"Task exceeded per-task cost limit: ${task_cost:.4f} > ${max_task_cost:.4f}. "
                f"Marked as failed (no retry)."
            )
            safe_print(f"  ✗ Task #{tid}: FAILED — exceeded per-task cost limit "
                        f"(${task_cost:.4f} > ${max_task_cost:.4f})")
            continue

        if r["success"]:
            task["status"] = "done"
            task["error_log"] = ""
            # Persist per-task commit hash from execute_task()
            if r.get("commit_hash"):
                task["commit_hash"] = r["commit_hash"]
            safe_print(f"  ✓ Task #{tid}: DONE ({r['duration_s']:.1f}s)")
        else:
            task["attempts"] = task.get("attempts", 0) + 1
            task["error_log"] = r.get("output", "")[:2000]

            if task["attempts"] >= MAX_TASK_ATTEMPTS:
                task["status"] = "failed"
                safe_print(f"  ✗ Task #{tid}: FAILED after {task['attempts']} attempts")
            else:
                task["status"] = "pending"  # will retry in next wave
                safe_print(f"  ↻ Task #{tid}: retry ({task['attempts']}/{MAX_TASK_ATTEMPTS})")

    save_tasks(project_dir, tasks)
    return cumulative_cost


def run_multi_agent(
    project_dir: Path,
    model: str,
    max_workers: int | None = None,
    max_waves: int | None = None,
    test_timeout: int = DEFAULT_TEST_TIMEOUT,
    session_timeout: int = DEFAULT_SESSION_TIMEOUT,
    review: bool = False,
    review_model: str = DEFAULT_REVIEW_MODEL,
    review_mode: str = "single",
    review_backend: str = DEFAULT_REVIEW_BACKEND,
    sandbox: bool = True,
    max_cost: float | None = None,
    max_task_cost: float | None = None,
    worktree_isolation: bool = False,
    dashboard: bool = False,
    review_plan_flag: bool = False,
    replan: bool = False,
) -> None:
    """Main multi-agent orchestration loop."""
    safe_print("\n" + "=" * 60)
    safe_print("  AUTO-CODER MULTI-AGENT: Parallel Coding with Claude Code")
    safe_print("=" * 60)
    safe_print(f"  Project:     {project_dir.resolve()}")
    safe_print(f"  Model:       {model}")
    if max_workers:
        safe_print(f"  Max workers: {max_workers}")
    else:
        safe_print(f"  Max workers: auto (from DAG)")
    if max_waves:
        safe_print(f"  Max waves:   {max_waves}")
    else:
        safe_print(f"  Max waves:   unlimited")
    if review:
        safe_print(f"  Review:      ON (backend: {review_backend}, model: {review_model}, mode: {review_mode})")
    if max_cost is not None:
        safe_print(f"  Max cost:    ${max_cost:.2f}")
    if max_task_cost is not None:
        safe_print(f"  Max task cost: ${max_task_cost:.2f}")
    if worktree_isolation:
        safe_print(f"  Worktree isolation: ON")
    if dashboard:
        safe_print(f"  Dashboard:   ON (.progress/state.json)")
    if review_plan_flag:
        safe_print(f"  Plan review: ON")
    if replan:
        safe_print(f"  Replan:      ON (regenerate task DAG)")
    safe_print(f"  Test timeout:    {test_timeout}s")
    safe_print(f"  Session timeout: {session_timeout}s")
    safe_print()

    project_dir.mkdir(parents=True, exist_ok=True)

    # Ensure git repo + .gitignore exist
    ensure_git_repo(project_dir)

    # Phase 1: Plan (generate task DAG if needed)
    task_file = project_dir / "task_list.json"

    # --replan: delete existing task_list.json and regenerate from scratch
    if replan and task_file.exists():
        safe_print("  --replan: Deleting existing task_list.json to regenerate...\n")
        task_file.unlink()

    if not task_file.exists():
        spec_file = project_dir / "app_spec.txt"
        if not spec_file.exists():
            safe_print(f"  ERROR: {spec_file} not found.")
            safe_print("  Place your project specification in app_spec.txt and re-run.")
            sys.exit(1)

        safe_print("  Phase 1: Planning — generating task DAG...\n")
        tasks = generate_task_dag(project_dir, model)

        # --review-plan: review the generated plan before execution
        if review_plan_flag:
            plan_review_result = review_plan(project_dir, tasks, model)
            if not plan_review_result["passed"]:
                revised = plan_review_result.get("revised_tasks")
                if isinstance(revised, list) and len(revised) > 0:
                    safe_print("  Plan Review: Applying revised task list from reviewer...")
                    from planner import _validate_tasks
                    try:
                        revised = _validate_tasks(revised)
                        rev_errors = validate_dag(revised)
                        if not rev_errors:
                            tasks = revised
                            save_tasks(project_dir, tasks)
                            safe_print(f"  Plan Review: Applied {len(tasks)} revised tasks")
                        else:
                            safe_print("  Plan Review: Revised tasks have DAG errors, keeping original")
                            for e in rev_errors:
                                safe_print(f"    - {e}")
                    except ValueError as exc:
                        safe_print(f"  Plan Review: Revised tasks invalid ({exc}), keeping original")
                else:
                    safe_print("  Plan Review: Plan failed review but no revised tasks provided. "
                               "Continuing with original plan.")
    else:
        safe_print("  Phase 1: Loading existing task DAG...\n")
        tasks = load_tasks(project_dir)

    # Recover stale in_progress tasks (from a previous interrupted run)
    stale = [t for t in tasks if t.get("status") == "in_progress"]
    if stale:
        for t in stale:
            t["status"] = "pending"
        save_tasks(project_dir, tasks)
        safe_print(f"  Recovered {len(stale)} stale in_progress task(s) → pending")

    # Validate
    errors = validate_dag(tasks)
    if errors:
        safe_print("  WARNING: DAG has issues:")
        for e in errors:
            safe_print(f"    - {e}")

    # Compute parallelism and DAG depth
    dag_max_parallel = compute_max_parallelism(tasks)
    dag_depth = compute_dag_depth(tasks)
    effective_workers = max_workers if max_workers else dag_max_parallel

    # Ensure max_waves >= dag_depth + retry headroom
    min_waves = dag_depth + 2
    if max_waves and max_waves < min_waves:
        safe_print(f"\n  WARNING: --max-waves {max_waves} < minimum needed {min_waves} "
              f"(DAG depth {dag_depth} + 2 retry), auto-adjusting")
        max_waves = min_waves

    safe_print(f"\n  DAG max parallelism: {dag_max_parallel}")
    safe_print(f"  DAG depth (levels):  {dag_depth}")
    safe_print(f"  Effective workers:   {effective_workers}")

    print_dag_summary(tasks)
    print_progress(project_dir)

    # Phase 2: Execute waves
    wave = 0
    total_cost = 0.0

    while True:
        wave += 1

        if max_waves and wave > max_waves:
            safe_print(f"\n  Reached max waves ({max_waves}). Re-run to continue.")
            break

        # Reload tasks (may have been updated)
        tasks = load_tasks(project_dir)

        # Recover stale in_progress (in case of mid-wave crash)
        stale = [t for t in tasks if t.get("status") == "in_progress"]
        if stale:
            for t in stale:
                t["status"] = "pending"
            save_tasks(project_dir, tasks)
            safe_print(f"  Recovered {len(stale)} stale in_progress task(s) → pending")

        # Check completion
        if is_all_done(tasks):
            safe_print("\n  All tasks are terminal (done/failed/skipped).")
            break

        # Skip blocked tasks
        skipped = mark_blocked_as_skipped(tasks)
        if skipped:
            safe_print(f"\n  Skipped {skipped} task(s) due to failed dependencies.")
            save_tasks(project_dir, tasks)

        # Get ready tasks
        ready = get_ready_tasks(tasks)

        if not ready:
            stats = count_stats(tasks)
            if stats["in_progress"] > 0:
                safe_print(f"\n  {stats['in_progress']} task(s) still in progress. Waiting...")
                time.sleep(5)
                continue

            if stats["pending"] > 0:
                safe_print(f"\n  {stats['pending']} pending task(s) but none ready (all blocked).")
                mark_blocked_as_skipped(tasks)
                save_tasks(project_dir, tasks)
                continue

            safe_print("\n  No more tasks to execute.")
            break

        # Mark tasks as in_progress
        task_map = {t["id"]: t for t in tasks}
        batch = ready[:effective_workers]
        for t in batch:
            task_map[t["id"]]["status"] = "in_progress"
        save_tasks(project_dir, tasks)

        # Print wave header
        safe_print(f"\n{'═'*60}")
        safe_print(f"  WAVE {wave}: Launching {len(batch)} agent(s) in parallel")
        safe_print(f"{'═'*60}")
        for t in batch:
            deps = t.get("depends_on", [])
            dep_str = f" (deps: {deps})" if deps else ""
            tag = colored_tag(t['id'])
            safe_print(f"  {tag} → {t['description'][:55]}{dep_str}")

        # Write progress state at wave start (dashboard mode)
        if dashboard:
            agents_info = [
                {"task_id": t["id"], "status": "running", "duration_s": 0, "cost_usd": 0}
                for t in batch
            ]
            write_progress_state(project_dir, wave=wave, agents=agents_info, total_cost=total_cost)

        # Record HEAD before wave for conflict detection (non-worktree mode)
        wave_before_hash = None
        if not worktree_isolation and len(batch) > 1:
            wave_before_hash = get_head_hash(project_dir)

        # Execute wave
        results = run_wave(
            batch, project_dir, model, effective_workers, wave,
            test_timeout=test_timeout, session_timeout=session_timeout,
            sandbox=sandbox, worktree_isolation=worktree_isolation,
        )

        # Git-based file conflict detection (only when worktree isolation is off)
        if not worktree_isolation and wave_before_hash and len(results) > 1:
            conflicts = detect_file_conflicts_git(project_dir, results, wave_before_hash)
            if conflicts:
                safe_print(f"\n  ⚠ File conflicts detected in wave {wave}:")
                for c in conflicts:
                    tid_a, tid_b = c["task_ids"]
                    files = ", ".join(c["files"])
                    safe_print(f"    Tasks #{tid_a} & #{tid_b} both modified: {files}")
                    for fpath, diff_text in c["diffs"].items():
                        safe_print(f"    --- {fpath} ---")
                        # Truncate long diffs
                        lines = diff_text.splitlines()
                        if len(lines) > 30:
                            for line in lines[:30]:
                                safe_print(f"      {line}")
                            safe_print(f"      ... ({len(lines) - 30} more lines)")
                        else:
                            for line in lines:
                                safe_print(f"      {line}")

        # Update results one-by-one, checking budget after EACH task
        budget_exceeded = False
        for r in results:
            total_cost = update_tasks_from_results(
                project_dir, [r],
                cumulative_cost=total_cost,
                max_task_cost=max_task_cost,
            )
            # Budget circuit breaker — check after EACH task
            if max_cost is not None and total_cost >= max_cost:
                safe_print(f"\n  BUDGET EXHAUSTED: ${total_cost:.4f} >= ${max_cost:.4f}. Stopping execution.")
                budget_exceeded = True
                break

        if budget_exceeded:
            break

        # Dynamic task generation
        tasks = load_tasks(project_dir)
        new_tasks = extract_new_tasks(results, tasks)
        if new_tasks:
            tasks_before = list(tasks)  # snapshot before modification
            tasks.extend(new_tasks)
            save_tasks(project_dir, tasks)
            safe_print(f"  [dynamic] Added {len(new_tasks)} new task(s) to DAG")

            # Validate DAG after dynamic task insertion
            dag_errors = validate_dag(tasks)
            if dag_errors:
                safe_print(f"  [dynamic] DAG validation failed after adding new tasks:")
                for err in dag_errors:
                    safe_print(f"    - {err}")
                # Revert: remove the offending new tasks and restore
                new_ids = {nt["id"] for nt in new_tasks}
                tasks = [t for t in tasks if t["id"] not in new_ids]
                save_tasks(project_dir, tasks)
                safe_print(f"  [dynamic] Reverted {len(new_tasks)} new task(s) due to DAG errors")

        # Scan for task proposals written by agents
        tasks = load_tasks(project_dir)
        prev_count = len(tasks)
        tasks = apply_task_proposals(project_dir, tasks)
        if len(tasks) != prev_count:
            save_tasks(project_dir, tasks)
            safe_print(f"  [proposals] Applied proposals: {len(tasks) - prev_count} net task change(s)")

        # Cross-model review for successful tasks
        if review:
            done_in_wave = [r for r in results if r.get("success")]
            if done_in_wave:
                safe_print(f"\n  [review] Reviewing {len(done_in_wave)} completed task(s) (mode={review_mode})...")
                tasks_now = load_tasks(project_dir)
                task_map_now = {t["id"]: t for t in tasks_now}
                for r in done_in_wave:
                    t = task_map_now.get(r["task_id"])
                    if t:
                        review_and_fix_task(t, project_dir, model,
                                            review_model=review_model,
                                            review_mode=review_mode,
                                            review_backend=review_backend)
                        # Commit review-fix changes per-task
                        auto_commit(project_dir, f"Task #{r['task_id']}: review fixes")

                # Integration review after wave completion (cross mode with 2+ tasks)
                if review_mode == "cross" and len(done_in_wave) >= 2:
                    wave_done_tasks = [
                        task_map_now[r["task_id"]]
                        for r in done_in_wave
                        if r["task_id"] in task_map_now
                    ]
                    run_integration_review(wave_done_tasks, project_dir,
                                           review_model=review_model,
                                           review_backend=review_backend)

        print_progress(project_dir)

        # Write progress state after wave completion (dashboard mode)
        if dashboard:
            agents_done = [
                {
                    "task_id": r["task_id"],
                    "status": "done" if r.get("success") else "failed",
                    "duration_s": r.get("duration_s", 0),
                    "cost_usd": r.get("cost_usd", 0),
                }
                for r in results
            ]
            write_progress_state(project_dir, wave=wave, agents=agents_done, total_cost=total_cost)

        # Brief delay between waves
        if not is_all_done(load_tasks(project_dir)):
            safe_print(f"\n  Next wave in {WAVE_DELAY}s... (Ctrl+C to pause)")
            try:
                time.sleep(WAVE_DELAY)
            except KeyboardInterrupt:
                safe_print("\n\n  Paused. Re-run to continue.")
                break

    # Final summary
    tasks = load_tasks(project_dir)
    safe_print("\n" + "=" * 60)
    safe_print("  SESSION COMPLETE")
    safe_print("=" * 60)
    print_dag_summary(tasks)
    print_progress(project_dir)

    stats = count_stats(tasks)
    safe_print(f"\n  Project: {project_dir.resolve()}")
    if stats["failed"] > 0:
        safe_print(f"  {stats['failed']} task(s) failed. Check error_log in task_list.json.")
    if stats["done"] == stats["total"]:
        safe_print("  All tasks completed successfully!")
    if total_cost > 0:
        safe_print(f"  Total cost: ${total_cost:.4f}")
    safe_print("  Re-run the same command to continue.\n")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-Coder Multi-Agent: Parallel coding with Claude Code"
    )
    parser.add_argument(
        "--project-dir", type=Path, default=Path("./project"),
        help="Project directory (must contain app_spec.txt for first run)"
    )
    parser.add_argument(
        "--max-workers", type=int, default=None,
        help="Max parallel agents (default: auto from DAG analysis)"
    )
    parser.add_argument(
        "--max-waves", type=int, default=None,
        help="Max execution waves (default: unlimited)"
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"Model to use (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--review", action="store_true", default=False,
        help="Enable review: a Claude agent reviews each task with read-only tools, Claude fixes issues"
    )
    parser.add_argument(
        "--review-model", type=str, default=DEFAULT_REVIEW_MODEL,
        help=f"Model for review agent sessions (default: {DEFAULT_REVIEW_MODEL})"
    )
    parser.add_argument(
        "--review-mode", type=str, default="single", choices=["single", "cross"],
        help="Review mode: 'single' uses one general reviewer (default), "
             "'cross' launches functional + security reviewers in parallel "
             "and runs integration review after each wave"
    )
    parser.add_argument(
        "--review-backend", type=str, default=DEFAULT_REVIEW_BACKEND,
        choices=["claude", "droid"],
        help=f"Backend for review sessions: 'claude' uses claude CLI, "
             f"'droid' uses Factory Droid CLI (default: {DEFAULT_REVIEW_BACKEND})"
    )
    parser.add_argument(
        "--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT,
        help=f"Timeout in seconds for test commands (default: {DEFAULT_TEST_TIMEOUT})"
    )
    parser.add_argument(
        "--session-timeout", type=int, default=DEFAULT_SESSION_TIMEOUT,
        help=f"Timeout in seconds for agent sessions (default: {DEFAULT_SESSION_TIMEOUT})"
    )
    parser.add_argument(
        "--max-cost", type=float, default=None,
        help="Maximum total cost in USD. Stops execution when exceeded (default: unlimited)"
    )
    parser.add_argument(
        "--max-task-cost", type=float, default=None,
        help="Maximum cost per task in USD. Marks task as failed if exceeded, no retry (default: unlimited)"
    )

    parser.add_argument(
        "--sandbox", action=argparse.BooleanOptionalAction, default=True,
        help="Enable agent permission sandboxing (default: True). "
             "Use --no-sandbox to fall back to --dangerously-skip-permissions"
    )

    parser.add_argument(
        "--dashboard", action="store_true", default=False,
        help="Enable progress dashboard: writes .progress/state.json with real-time "
             "execution state (wave, agent status, completed/total counts, costs)"
    )
    parser.add_argument(
        "--worktree-isolation", action="store_true", default=False,
        help="Run each task in a separate git worktree to prevent file conflicts "
             "between parallel agents (default: False)"
    )
    parser.add_argument(
        "--review-plan", action="store_true", default=False,
        help="Review the generated task DAG for spec coverage, task specificity, "
             "dependency correctness, and parallelism before execution begins"
    )
    parser.add_argument(
        "--replan", action="store_true", default=False,
        help="Delete existing task_list.json and regenerate the task DAG from scratch "
             "(useful when the spec has changed)"
    )

    args = parser.parse_args()

    try:
        run_multi_agent(
            project_dir=args.project_dir,
            model=args.model,
            max_workers=args.max_workers,
            max_waves=args.max_waves,
            test_timeout=args.test_timeout,
            session_timeout=args.session_timeout,
            review=args.review,
            review_model=args.review_model,
            review_mode=args.review_mode,
            review_backend=args.review_backend,
            sandbox=args.sandbox,
            max_cost=args.max_cost,
            max_task_cost=args.max_task_cost,
            dashboard=args.dashboard,
            worktree_isolation=args.worktree_isolation,
            review_plan_flag=args.review_plan,
            replan=args.replan,
        )
    except KeyboardInterrupt:
        safe_print("\n\n  Interrupted. Re-run to continue.")


if __name__ == "__main__":
    main()
