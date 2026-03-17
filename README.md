# Auto-Coder Multi-Agent

Auto-Coder Multi-Agent is a DAG-driven orchestration tool that reads a project spec (`app_spec.txt`), generates executable tasks, and runs multiple coding agents in parallel waves with testing, review, retries, and progress tracking.

## Key Features

- **Automatic planning** from spec to `task_list.json`
- **Parallel execution** based on dependency DAG
- **Task-level context sharing** via `.context/task_<id>_summary.json`
- **Review loop** (single reviewer or functional+security cross-review)
- **Permission sandboxing** (enabled by default)
- **Reliability controls** (timeouts, retries, DAG validation, stale-task recovery)
- **Cost guardrails** (`--max-cost`, `--max-task-cost`)
- **Optional git worktree isolation** for conflict reduction

## Repository Layout

```text
dancodingagent/
├── run.py                # Main orchestrator entrypoint
├── planner.py            # DAG generation + plan review
├── scheduler.py          # Task state, DAG ops, dynamic proposals
├── worker.py             # Claude/Droid session runners
├── git_ops.py            # Git init/commit/worktree/conflict utilities
├── progress.py           # CLI progress + .progress/state.json writer
├── prompts/              # Coder/reviewer prompt templates
├── app_spec.txt          # Project specification input
├── task_list.json        # Generated DAG + runtime task states
├── test_changes.py       # Regression tests
└── logs/                 # Per-task session logs
```

## Requirements

- Python 3.10+
- `git`
- `claude` CLI available in PATH
- Optional: `droid` CLI (if using `--review-backend droid`)

## Droid Backend via CLIProxyAPI

If you need to route Droid through a server built from a subscription source, see:

- https://github.com/router-for-me/CLIProxyAPI

`CLIProxyAPI` can convert subscription configs into a server endpoint that can then be used by Droid.

## Quick Start

Run on a target project directory (must contain `app_spec.txt` on first run):

```bash
python3 run.py --project-dir ./your_project
```

Example with review and dashboard:

```bash
python3 run.py \
  --project-dir ./your_project \
  --max-workers 4 \
  --review \
  --review-mode cross \
  --review-backend droid \
  --sandbox \
  --dashboard
```

## Important CLI Options

- `--project-dir`: target project root
- `--max-workers`: max parallel agents (default: DAG-derived)
- `--max-waves`: cap execution waves
- `--review`: enable task review/fix loop
- `--review-mode {single,cross}`: one reviewer or dual reviewer mode
- `--review-backend {claude,droid}`: review execution backend
- `--test-timeout`: timeout for task test command
- `--session-timeout`: timeout for agent session
- `--max-cost`: total cost circuit breaker
- `--max-task-cost`: per-task cost circuit breaker
- `--sandbox/--no-sandbox`: permission sandbox toggle
- `--worktree-isolation`: run tasks in separate git worktrees
- `--review-plan`: review generated task DAG before execution
- `--replan`: regenerate task DAG from updated spec

## Execution Flow

1. Generate task DAG from `app_spec.txt` (or load existing `task_list.json`)
2. Select ready tasks (dependencies satisfied)
3. Run a wave of tasks in parallel
4. Persist summaries, update statuses, optionally run reviews/fixes
5. Repeat until all tasks are terminal (`done/failed/skipped`)

## Runtime Artifacts

- `task_list.json`: source of truth for task statuses
- `.context/`: dependency summaries, structured task results, proposals
- `.review/`: reviewer verdicts and fix logs
- `logs/`: per-task session logs
- `.progress/state.json`: live dashboard state when `--dashboard` is enabled

## Testing

```bash
python3 test_changes.py
```

## Security Notes

- The default sandbox blocks high-risk operations (for example `sudo`, destructive commands, and `git push`).
- A repository scan for common secret patterns (private keys, API key/token signatures, and raw IPv4 literals) found no hardcoded private keys or API tokens in tracked project files.
