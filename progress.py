"""
Progress tracking and display for the multi-agent system.

Includes state file writing (.progress/state.json) for real-time monitoring,
and colored [Task#N] tag support for readable interleaved output.
"""

import json
import time
from pathlib import Path
from scheduler import load_tasks, count_stats


# ──────────────────────────────────────────────────────────
# ANSI color codes for [Task#N] tags
# ──────────────────────────────────────────────────────────

_COLORS = [
    "\033[36m",   # cyan
    "\033[33m",   # yellow
    "\033[35m",   # magenta
    "\033[32m",   # green
    "\033[34m",   # blue
    "\033[91m",   # bright red
    "\033[92m",   # bright green
    "\033[93m",   # bright yellow
    "\033[94m",   # bright blue
    "\033[95m",   # bright magenta
    "\033[96m",   # bright cyan
]
_RESET = "\033[0m"


def colored_tag(task_id: int | None) -> str:
    """Return a colored [Task#N] or [Planner] tag string."""
    if task_id is None:
        return f"\033[1m[Planner]{_RESET}"
    color = _COLORS[task_id % len(_COLORS)]
    return f"{color}[Task#{task_id}]{_RESET}"


# ──────────────────────────────────────────────────────────
# Progress state file (.progress/state.json)
# ──────────────────────────────────────────────────────────

PROGRESS_DIR = ".progress"


def write_progress_state(
    project_dir: Path,
    wave: int = 0,
    agents: list[dict] | None = None,
    total_cost: float = 0.0,
) -> None:
    """Write .progress/state.json with current execution state.

    Called after significant events: task start, task complete, wave start/end.

    Args:
        project_dir: Project root directory.
        wave: Current wave number.
        agents: List of agent status dicts, each with:
            task_id, status (running/reviewing/done/failed), duration_s, cost_usd.
        total_cost: Cumulative cost across all tasks.
    """
    progress_dir = project_dir / PROGRESS_DIR
    progress_dir.mkdir(exist_ok=True)

    tasks = load_tasks(project_dir)
    stats = count_stats(tasks)

    state = {
        "wave": wave,
        "agents": agents or [],
        "completed": stats["done"],
        "total": stats["total"],
        "failed": stats["failed"],
        "pending": stats["pending"],
        "in_progress": stats["in_progress"],
        "skipped": stats["skipped"],
        "total_cost": round(total_cost, 4),
        "timestamp": time.time(),
    }

    state_file = progress_dir / "state.json"
    state_file.write_text(json.dumps(state, indent=2) + "\n")


def print_progress(project_dir: Path) -> None:
    """Print a progress summary with DAG awareness."""
    tasks = load_tasks(project_dir)
    if not tasks:
        print("  Progress: task_list.json not yet created.")
        return

    stats = count_stats(tasks)
    total = stats["total"]
    done = stats["done"]
    failed = stats["failed"]
    pending = stats["pending"]
    in_prog = stats["in_progress"]
    skipped = stats["skipped"]
    pct = (done / total * 100) if total > 0 else 0

    bar_len = 30
    filled = int(bar_len * done / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    print(f"\n  Progress: [{bar}] {done}/{total} done ({pct:.0f}%)")
    print(f"  Pending: {pending}  In-progress: {in_prog}  Failed: {failed}  Skipped: {skipped}")


def print_dag_summary(tasks: list[dict]) -> None:
    """Print a visual summary of the task DAG."""
    if not tasks:
        return

    print("\n  Task DAG:")
    print("  " + "─" * 50)

    for t in sorted(tasks, key=lambda x: x["id"]):
        tid = t["id"]
        desc = t["description"][:60]
        status = t["status"]
        deps = t.get("depends_on", [])

        # Status icon
        icons = {
            "done": "✓",
            "failed": "✗",
            "skipped": "⊘",
            "in_progress": "▶",
            "pending": "○",
        }
        icon = icons.get(status, "?")

        dep_str = f" ← [{', '.join(str(d) for d in deps)}]" if deps else ""
        print(f"  {icon} #{tid:2d}: {desc}{dep_str}")

    print("  " + "─" * 50)


def print_session_header(wave: int, running: int, total_waves: str = "?") -> None:
    """Print wave execution header."""
    print(f"\n{'─'*60}")
    print(f"  Wave {wave}: Running {running} agent(s) in parallel")
    print(f"{'─'*60}\n")
