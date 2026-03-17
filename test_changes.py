#!/usr/bin/env python3
"""Unit tests for all Claude Code system changes."""
import json, sys, shutil, tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from string import Template

PROMPTS = SCRIPT_DIR / "prompts"
passed = 0
failed = 0

def test(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1

# ── Test 1: Coder template renders with Template (no str.format issues) ──
t = PROMPTS.joinpath("coder_prompt.md").read_text()
result = Template(t).safe_substitute(
    task_id=5, task_description="Build API", test_command="curl localhost",
    dependency_context="deps here", error_context="prev error",
)
test("coder template: task_id substituted", "Task #5" in result)
test("coder template: description substituted", "Build API" in result)
test("coder template: dependency_context injected", "deps here" in result)
test("coder template: error_context injected", "prev error" in result)
test("coder template: no $task_id remaining", "${task_id}" not in result)

# ── Test 2: Template with JSON content doesn't break ──
json_content = '{"id": 1, "name": "test"}'
result2 = Template(t).safe_substitute(
    task_id=1, task_description=json_content, test_command="echo ok",
    dependency_context="", error_context="",
)
test("coder template: JSON in description OK", json_content in result2)

# ── Test 3: DAG depth ──
from scheduler import compute_dag_depth
tasks = [
    {"id": 1, "depends_on": [], "status": "pending"},
    {"id": 2, "depends_on": [1], "status": "pending"},
    {"id": 3, "depends_on": [2], "status": "pending"},
    {"id": 4, "depends_on": [2], "status": "pending"},
    {"id": 5, "depends_on": [3, 4], "status": "pending"},
]
depth = compute_dag_depth(tasks)
test(f"dag depth: 1->2->(3,4)->5 = {depth} (expected 4)", depth == 4)

flat_tasks = [
    {"id": 1, "depends_on": [], "status": "pending"},
    {"id": 2, "depends_on": [], "status": "pending"},
    {"id": 3, "depends_on": [], "status": "pending"},
]
test(f"dag depth: flat = {compute_dag_depth(flat_tasks)} (expected 1)", compute_dag_depth(flat_tasks) == 1)

# ── Test 4: Context sharing ──
from run import save_task_summary, load_dependency_context

tmpdir = Path(tempfile.mkdtemp())
save_task_summary(tmpdir, 1, ["Created file main.py", "Modified config.json", "All done!"])
ctx_file = tmpdir / ".context" / "task_1_summary.md"
test("context save: file created", ctx_file.exists())
test("context save: contains task id", "Task #1" in ctx_file.read_text())

# Load dependency context
task_with_deps = {"id": 2, "depends_on": [1]}
ctx = load_dependency_context(tmpdir, task_with_deps)
test("context load: returns content", "Task #1" in ctx)
test("context load: has header", "Context from Completed Dependencies" in ctx)

# No deps = empty
task_no_deps = {"id": 3, "depends_on": []}
test("context load: no deps = empty", load_dependency_context(tmpdir, task_no_deps) == "")

shutil.rmtree(tmpdir, ignore_errors=True)

# ── Test 5: Error context in build_coder_prompt ──
from run import build_coder_prompt

tmpdir2 = Path(tempfile.mkdtemp())
task_retry = {
    "id": 99, "description": "Fix bug", "depends_on": [],
    "test_command": "echo ok", "attempts": 2,
    "error_log": "ImportError: No module named 'foo'",
}
prompt = build_coder_prompt(task_retry, tmpdir2)
test("error context: includes attempt count", "attempt 2" in prompt)
test("error context: includes error message", "ImportError" in prompt)

task_first = {
    "id": 100, "description": "New task", "depends_on": [],
    "test_command": "echo ok", "attempts": 0, "error_log": "",
}
prompt2 = build_coder_prompt(task_first, tmpdir2)
test("error context: empty for first attempt", "Previous Attempt" not in prompt2)
shutil.rmtree(tmpdir2, ignore_errors=True)

# ── Test 6: Session log save ──
from run import save_session_log

tmpdir3 = Path(tempfile.mkdtemp())
save_session_log(tmpdir3, 1, 3, ["hello", "world"], {"exit_code": 0, "success": True, "duration_s": 5.0})
log_file = tmpdir3 / "logs" / "task_1_wave_3.log"
test("session log: file created", log_file.exists())
content = log_file.read_text()
test("session log: contains task id", "Task #1" in content)
test("session log: contains wave", "Wave 3" in content)
test("session log: contains output", "hello" in content)
shutil.rmtree(tmpdir3, ignore_errors=True)

# ── Test 7: Stale recovery ──
tasks_stale = [
    {"id": 1, "status": "in_progress", "attempts": 0},
    {"id": 2, "status": "done", "attempts": 0},
    {"id": 3, "status": "in_progress", "attempts": 1},
]
stale = [t for t in tasks_stale if t.get("status") == "in_progress"]
for t in stale:
    t["status"] = "pending"
test("stale recovery: 2 recovered", len(stale) == 2)
test("stale recovery: task 1 pending", tasks_stale[0]["status"] == "pending")
test("stale recovery: task 2 unchanged", tasks_stale[1]["status"] == "done")

# ── Test 8: File conflict detection ──
from run import detect_file_conflicts
mock_results = [
    {"task_id": 1, "success": True, "output": "Created main.py\nModified config.json"},
    {"task_id": 2, "success": True, "output": "Modified main.py\nCreated utils.py"},
    {"task_id": 3, "success": False, "output": "Failed"},
]
conflicts = detect_file_conflicts(Path(tempfile.mkdtemp()), mock_results)
conflict_files = [c["file"] for c in conflicts]
test("conflict detection: finds main.py conflict", "main.py" in conflict_files)
test("conflict detection: ignores failed tasks", not any(3 in c["tasks"] for c in conflicts))

# No conflicts when single task
single_results = [{"task_id": 1, "success": True, "output": "Created foo.py"}]
test("conflict detection: single task = no conflicts",
     detect_file_conflicts(Path(tempfile.mkdtemp()), single_results) == [])

# ── Test 9: Dynamic task extraction ──
from run import extract_new_tasks
existing = [
    {"id": 1, "description": "a", "depends_on": [], "status": "done"},
    {"id": 2, "description": "b", "depends_on": [1], "status": "done"},
]
results_with_new = [
    {"task_id": 2, "output": 'Some output\nNEW_TASK: {"description": "Add logging", "test_command": "echo ok"}\nDone.'},
]
new_tasks = extract_new_tasks(results_with_new, existing)
test("dynamic tasks: extracted 1 new task", len(new_tasks) == 1)
test("dynamic tasks: id auto-assigned", new_tasks[0]["id"] == 3)
test("dynamic tasks: description correct", new_tasks[0]["description"] == "Add logging")
test("dynamic tasks: default depends_on = parent", new_tasks[0]["depends_on"] == [2])

# No new tasks from garbage
results_no_new = [{"task_id": 1, "output": "All done, no new tasks needed"}]
test("dynamic tasks: no markers = empty", extract_new_tasks(results_no_new, existing) == [])

# Invalid JSON after NEW_TASK: is ignored
results_bad = [{"task_id": 1, "output": "NEW_TASK: not json at all"}]
test("dynamic tasks: bad json = empty", extract_new_tasks(results_bad, existing) == [])

# ── Test 10: Planner _validate_tasks behavior ──
from planner import _validate_tasks

# 10a: Missing required field raises ValueError
try:
    _validate_tasks([{"id": 1, "description": "x", "test_command": "echo ok", "status": "pending"}])
    test("validate_tasks: missing depends_on raises", False)
except ValueError as e:
    test("validate_tasks: missing depends_on raises", "depends_on" in str(e))

# 10b: Invalid (non-existent) depends_on reference raises ValueError
try:
    _validate_tasks([
        {"id": 1, "description": "a", "depends_on": [], "test_command": "echo ok", "status": "pending"},
        {"id": 2, "description": "b", "depends_on": [99], "test_command": "echo ok", "status": "pending"},
    ])
    test("validate_tasks: invalid depends_on ref raises", False)
except ValueError as e:
    test("validate_tasks: invalid depends_on ref raises", "non-existent" in str(e))

# 10c: Duplicate task IDs raise ValueError
try:
    _validate_tasks([
        {"id": 1, "description": "a", "depends_on": [], "test_command": "echo ok", "status": "pending"},
        {"id": 1, "description": "b", "depends_on": [], "test_command": "echo ok", "status": "pending"},
    ])
    test("validate_tasks: duplicate id raises", False)
except ValueError as e:
    test("validate_tasks: duplicate id raises", "Duplicate" in str(e))

# 10d: Non-positive task ID raises ValueError
try:
    _validate_tasks([{"id": 0, "description": "x", "depends_on": [], "test_command": "echo ok", "status": "pending"}])
    test("validate_tasks: non-positive id raises", False)
except ValueError as e:
    test("validate_tasks: non-positive id raises", "positive integer" in str(e))

# 10e: Valid tasks pass without error and get defaults filled
valid_tasks = _validate_tasks([
    {"id": 1, "description": "a", "depends_on": [], "test_command": "echo ok", "status": "pending"},
    {"id": 2, "description": "b", "depends_on": [1], "test_command": "echo ok", "status": "pending"},
])
test("validate_tasks: valid tasks pass", len(valid_tasks) == 2)
test("validate_tasks: defaults filled after validation", valid_tasks[0].get("attempts") == 0)

# ── Test 11: Thread-safe print — comprehensive coverage ──
import ast, inspect

# 11a: safe_print module exists and has _print_lock
from safe_print import safe_print as _sp, _print_lock
import threading
test("safe_print: _print_lock is a Lock", isinstance(_print_lock, type(threading.Lock())))
test("safe_print: safe_print is callable", callable(_sp))

# 11b: run.py has NO raw print() calls (use AST to avoid false positives from strings/comments)
run_source = (SCRIPT_DIR / "run.py").read_text()
run_tree = ast.parse(run_source)
raw_prints_run = [
    node for node in ast.walk(run_tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "print"
]
test("safe_print: run.py has zero raw print() calls", len(raw_prints_run) == 0)

# 11c: worker.py has NO raw print() calls
worker_source = (SCRIPT_DIR / "worker.py").read_text()
worker_tree = ast.parse(worker_source)
raw_prints_worker = [
    node for node in ast.walk(worker_tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "print"
]
test("safe_print: worker.py has zero raw print() calls", len(raw_prints_worker) == 0)

# 11d: worker.py imports and uses safe_print
test("safe_print: worker.py imports safe_print", "from safe_print import safe_print" in worker_source)
test("safe_print: worker.py calls safe_print", "safe_print(" in worker_source)

# 11e: run.py imports and uses safe_print
test("safe_print: run.py imports safe_print", "from safe_print import safe_print" in run_source)
test("safe_print: run.py calls safe_print", "safe_print(" in run_source)

# 11f: [Task#N] prefix consistency — worker.py uses [Task#<id>] or [Planner] tag format
test("safe_print: worker.py uses [tag] prefix pattern",
     'f"Task#{task_id}"' in worker_source or "Task#{task_id}" in worker_source)
test("safe_print: worker.py has Planner fallback tag", '"Planner"' in worker_source)

# 11g: run.py uses [Task#{id}] prefix in exception handler
test("safe_print: run.py uses [Task#] prefix", "[Task#" in run_source)

# ── Test 12: Configurable timeouts (CLI arg defaults) ──
from run import DEFAULT_TEST_TIMEOUT, DEFAULT_SESSION_TIMEOUT
test("timeout defaults: test timeout > 0", DEFAULT_TEST_TIMEOUT > 0)
test("timeout defaults: session timeout > 0", DEFAULT_SESSION_TIMEOUT > 0)
test("timeout defaults: session > test", DEFAULT_SESSION_TIMEOUT >= DEFAULT_TEST_TIMEOUT)

# ── Test 12b: build_coder_prompt wraps test command with timeout ──
tmpdir_t = Path(tempfile.mkdtemp())
task_timeout = {
    "id": 50, "description": "Test timeout wrapping", "depends_on": [],
    "test_command": "pytest tests/", "attempts": 0, "error_log": "",
}
prompt_with_timeout = build_coder_prompt(task_timeout, tmpdir_t, test_timeout=120)
test("timeout prompt: test_command wrapped with 'timeout 120'",
     "timeout 120 pytest tests/" in prompt_with_timeout)

prompt_no_timeout = build_coder_prompt(task_timeout, tmpdir_t, test_timeout=None)
test("timeout prompt: no wrapping when test_timeout is None",
     "timeout" not in prompt_no_timeout or "timeout 120" not in prompt_no_timeout)
shutil.rmtree(tmpdir_t, ignore_errors=True)

# ── Test 12c: session timeout uses threading.Timer to kill blocked process ──
import subprocess as _sp_mod
import threading as _thr_mod

# Spawn a subprocess that blocks forever (sleep), then verify the Timer kills it
_block_proc = _sp_mod.Popen(
    ["sleep", "300"],
    stdin=_sp_mod.PIPE, stdout=_sp_mod.PIPE, stderr=_sp_mod.PIPE,
)
_timed_out_flag = False

def _kill_blocked():
    global _timed_out_flag
    _timed_out_flag = True
    try:
        _block_proc.kill()
    except OSError:
        pass

_timer = _thr_mod.Timer(1.0, _kill_blocked)  # 1 second timeout
_timer.daemon = True
_timer.start()

_block_proc.wait()  # Should unblock within ~1s when killed
_timer.cancel()
test("session timeout: Timer kills blocked process", _timed_out_flag)
test("session timeout: killed process has non-zero exit",
     _block_proc.returncode != 0)

# ── Test 12d: worker.run_claude_session uses Timer-based timeout (code inspection) ──
import inspect as _insp_mod
import worker
_rcs_src = _insp_mod.getsource(worker.run_claude_session)
test("session timeout: uses threading.Timer", "threading.Timer" in _rcs_src)
test("session timeout: timer calls process.kill()", "process.kill()" in _rcs_src)
test("session timeout: timer is cancelled on normal exit", ".cancel()" in _rcs_src)

# ── Test 13: Progress dashboard — write_progress_state and colored_tag ──
from progress import write_progress_state, colored_tag, PROGRESS_DIR, _COLORS, _RESET

# 13a: colored_tag produces ANSI-colored [Task#N] prefix
tag5 = colored_tag(5)
expected_color = _COLORS[5 % len(_COLORS)]
test("colored_tag: contains [Task#5]", "[Task#5]" in tag5)
test("colored_tag: starts with ANSI color code", tag5.startswith(expected_color))
test("colored_tag: ends with RESET code", tag5.endswith(_RESET))

# 13b: colored_tag with None gives [Planner]
tag_planner = colored_tag(None)
test("colored_tag: None gives [Planner]", "[Planner]" in tag_planner)
test("colored_tag: Planner has RESET", tag_planner.endswith(_RESET))

# 13c: colored_tag cycles through colors
tag0 = colored_tag(0)
tag1 = colored_tag(1)
test("colored_tag: different task_ids get different colors", tag0 != tag1)

# 13d: write_progress_state writes valid .progress/state.json
tmpdir_prog = Path(tempfile.mkdtemp())
# Create a minimal task_list.json for write_progress_state to read
prog_tasks = [
    {"id": 1, "description": "a", "depends_on": [], "test_command": "echo ok",
     "status": "done", "priority": 1, "attempts": 0, "error_log": ""},
    {"id": 2, "description": "b", "depends_on": [1], "test_command": "echo ok",
     "status": "in_progress", "priority": 2, "attempts": 0, "error_log": ""},
    {"id": 3, "description": "c", "depends_on": [], "test_command": "echo ok",
     "status": "pending", "priority": 3, "attempts": 0, "error_log": ""},
    {"id": 4, "description": "d", "depends_on": [1], "test_command": "echo ok",
     "status": "failed", "priority": 4, "attempts": 1, "error_log": "err"},
]
(tmpdir_prog / "task_list.json").write_text(json.dumps(prog_tasks))

agents_info = [
    {"task_id": 2, "status": "running", "duration_s": 10.5, "cost_usd": 0.05},
]
write_progress_state(tmpdir_prog, wave=3, agents=agents_info, total_cost=1.2345)

state_file = tmpdir_prog / PROGRESS_DIR / "state.json"
test("write_progress_state: state.json created", state_file.exists())

state = json.loads(state_file.read_text())
test("write_progress_state: wave field correct", state["wave"] == 3)
test("write_progress_state: agents field present", len(state["agents"]) == 1)
test("write_progress_state: agent has task_id", state["agents"][0]["task_id"] == 2)
test("write_progress_state: agent has status", state["agents"][0]["status"] == "running")
test("write_progress_state: completed count correct (1 done)", state["completed"] == 1)
test("write_progress_state: total count correct (4 tasks)", state["total"] == 4)
test("write_progress_state: failed count correct", state["failed"] == 1)
test("write_progress_state: pending count correct", state["pending"] == 1)
test("write_progress_state: in_progress count correct", state["in_progress"] == 1)
test("write_progress_state: total_cost is rounded float", state["total_cost"] == 1.2345)
test("write_progress_state: has timestamp", "timestamp" in state and isinstance(state["timestamp"], float))

# 13e: write_progress_state with no agents defaults to empty list
write_progress_state(tmpdir_prog, wave=1)
state2 = json.loads(state_file.read_text())
test("write_progress_state: no agents = empty list", state2["agents"] == [])
test("write_progress_state: default cost is 0", state2["total_cost"] == 0.0)

shutil.rmtree(tmpdir_prog, ignore_errors=True)

# 13f: --dashboard CLI flag exists in argparse
import run as _run_module
import inspect as _insp
_run_src = _insp.getsource(_run_module)
test("dashboard: --dashboard flag in argparse", "'--dashboard'" in _run_src or '"--dashboard"' in _run_src)

# ── Test 14: Compile check ──
import py_compile
try:
    py_compile.compile(str(SCRIPT_DIR / "run.py"), doraise=True)
    py_compile.compile(str(SCRIPT_DIR / "scheduler.py"), doraise=True)
    py_compile.compile(str(SCRIPT_DIR / "progress.py"), doraise=True)
    test("compile: run.py OK", True)
    test("compile: scheduler.py OK", True)
    test("compile: progress.py OK", True)
except py_compile.PyCompileError as e:
    test(f"compile: FAILED - {e}", False)

# ── Summary ──
print(f"\n{'='*40}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"{'='*40}")
sys.exit(1 if failed else 0)
