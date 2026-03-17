"""
Worker: runs a single Claude Code or Droid session for one task.

Uses claude CLI in print mode (-p) or droid exec with stream-json output
for real-time progress. Prompts are passed via stdin to avoid shell argument
length limits.
"""

import json
import subprocess
import signal
import threading
import time
from pathlib import Path

# Maximum total characters to buffer from assistant text blocks to prevent OOM.
MAX_ASSISTANT_TEXT_CHARS = 500_000

# Destructive commands/patterns that are always denied in sandbox mode.
_DENIED_BASH_PATTERNS = [
    "rm -rf /",
    "sudo *",
    "su *",
    "chmod 777*",
    "chown *",
    "git push*",
    "git remote*",
    "curl*--upload*",
    "curl*-T *",
    "wget*--post*",
    "kill *",
    "pkill *",
    "killall *",
]


def _plain_tag(task_id: int | None) -> str:
    """Return plain-text tag for compatibility with non-ANSI log parsers."""
    return f"Task#{task_id}" if task_id is not None else "Planner"


def _generate_sandbox_settings(project_dir: Path) -> None:
    """Generate .claude/settings.local.json with two-zone permission model.

    Zone 1 (project_dir): full read + write + execute
    Zone 2 (everywhere else): read-only
    Destructive commands: always denied
    """
    abs_project = str(project_dir.resolve())
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    allow_rules = [
        # Read tools: allowed everywhere
        "Read",
        "Glob",
        "Grep",
        # Write tools: allowed only within project_dir
        f"Write({abs_project}/**)",
        f"Edit({abs_project}/**)",
        # Bash: allowed (deny rules handle restrictions)
        "Bash",
    ]

    deny_rules = []
    for pattern in _DENIED_BASH_PATTERNS:
        deny_rules.append(f"Bash({pattern})")

    settings = {
        "permissions": {
            "allow": allow_rules,
            "deny": deny_rules,
        }
    }

    settings_file = claude_dir / "settings.local.json"
    settings_file.write_text(json.dumps(settings, indent=2) + "\n")

# ──────────────────────────────────────────────────────────
# Process registry for graceful shutdown
# ──────────────────────────────────────────────────────────
_process_registry_lock = threading.Lock()
_active_processes: dict[int, subprocess.Popen] = {}  # task_id -> Popen


def register_process(task_id: int, process: subprocess.Popen) -> None:
    """Register a running subprocess for graceful shutdown tracking."""
    with _process_registry_lock:
        _active_processes[task_id] = process


def unregister_process(task_id: int) -> None:
    """Remove a subprocess from the registry after it finishes."""
    with _process_registry_lock:
        _active_processes.pop(task_id, None)


def terminate_all_processes() -> list[int]:
    """Terminate all registered subprocesses. Returns list of interrupted task IDs."""
    with _process_registry_lock:
        interrupted_task_ids = list(_active_processes.keys())
        for task_id, proc in _active_processes.items():
            try:
                proc.terminate()
            except OSError:
                pass
    # Wait briefly for processes to exit, then force-kill stragglers
    for task_id in interrupted_task_ids:
        with _process_registry_lock:
            proc = _active_processes.get(task_id)
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
    with _process_registry_lock:
        _active_processes.clear()
    return interrupted_task_ids


def _read_structured_output(project_dir: Path, task_id: int) -> dict:
    """Read structured output files written by the agent after a session.

    Checks for:
      - .context/task_{id}_result.json — task completion metadata
      - .review/task_{id}_verdict.json — review verdict
    Returns a dict with 'task_result' and/or 'review_verdict' keys if files exist.
    """
    structured: dict = {}

    result_file = project_dir / ".context" / f"task_{task_id}_result.json"
    if result_file.exists():
        try:
            structured["task_result"] = json.loads(result_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    verdict_file = project_dir / ".review" / f"task_{task_id}_verdict.json"
    if verdict_file.exists():
        try:
            structured["review_verdict"] = json.loads(verdict_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return structured


def run_claude_session(
    prompt: str,
    project_dir: Path,
    model: str,
    task_id: int | None = None,
    allowed_tools: str = "Bash Edit Read Write Glob Grep",
    verbose: bool = True,
    session_timeout: int | None = None,
    sandbox: bool = True,
) -> dict:
    """
    Run a single claude -p session with stream-json output.

    Streams events in real-time so the user can see Claude's reasoning,
    tool calls, and results as they happen.

    Args:
        session_timeout: Maximum seconds for the entire session. If exceeded,
            the process is killed and exit_code is set to -1 (TimeoutExpired).
        sandbox: When True, generates .claude/settings.local.json with a
            two-zone permission model (full access in project_dir, read-only
            elsewhere, destructive commands denied). When False, uses
            --dangerously-skip-permissions for backward compatibility.

    Returns dict with:
      - exit_code: int (-1 if session timed out)
      - output: str (final result text)
      - duration_s: float
    """
    # Set up permission sandboxing
    if sandbox:
        _generate_sandbox_settings(project_dir)

    cmd = [
        "claude",
        "-p",
        "--verbose",
        "--model", model,
        "--output-format", "stream-json",
    ]

    # Only use --dangerously-skip-permissions when --no-sandbox / no_sandbox is set
    if not sandbox:
        cmd.append("--dangerously-skip-permissions")

    # Only add --allowedTools if tools are specified; omit for no-tool sessions
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    from safe_print import safe_print
    from progress import colored_tag
    tag = colored_tag(task_id)
    safe_print(f"\n  {tag} Starting claude session...", flush=True)
    safe_print(f"  {tag} cwd: {project_dir.resolve()}", flush=True)
    safe_print(f"  {tag} prompt: {len(prompt)} chars | model: {model}", flush=True)
    safe_print(f"  {tag} {'─'*50}", flush=True)

    start = time.time()

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(project_dir.resolve()),
    )

    # Track process for graceful shutdown
    if task_id is not None:
        register_process(task_id, process)

    # Send prompt via stdin then close it
    process.stdin.write(prompt)
    process.stdin.close()

    # Read stderr in a separate thread to prevent deadlock.
    # If the subprocess fills the stderr pipe buffer (~64KB) while we're
    # blocked reading stdout, the subprocess will block and never finish
    # writing stdout — classic deadlock.
    stderr_lines: list[str] = []

    def _read_stderr() -> None:
        for err_line in process.stderr:
            stderr_lines.append(err_line)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    final_text = ""
    all_assistant_text = []  # Collect all assistant text blocks for JSON extraction
    total_assistant_chars = 0  # Track total chars to enforce MAX_ASSISTANT_TEXT_CHARS
    num_turns = 0
    cost_usd = 0
    timed_out = False

    # If session_timeout is set, schedule a timer that kills the process.
    # This ensures the timeout fires even if the stdout readline loop blocks
    # (e.g. the subprocess hangs without producing output).
    timeout_timer = None
    if session_timeout is not None:
        def _kill_on_timeout():
            nonlocal timed_out
            timed_out = True
            safe_print(f"  {tag} SESSION TIMEOUT ({session_timeout}s) — killing process", flush=True)
            try:
                process.kill()
            except OSError:
                pass

        timeout_timer = threading.Timer(session_timeout, _kill_on_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

    try:
        for line in process.stdout:
            # If the timeout timer killed the process, stop reading immediately
            if timed_out:
                break
            line = line.rstrip()
            if not line:
                continue

            try:
                event = json.loads(line)
                etype = event.get("type", "")

                if etype == "assistant":
                    # Claude's text response
                    text = ""
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            text += block.get("text", "")
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "?")
                            tool_input = block.get("input", {})
                            if verbose:
                                safe_print(f"  {tag} ┌─ Tool: {tool_name}", flush=True)
                                # Show key params briefly
                                for k, v in tool_input.items():
                                    val = str(v)
                                    if len(val) > 200:
                                        val = val[:200] + "..."
                                    safe_print(f"  {tag} │  {k}: {val}", flush=True)
                    if text:
                        if total_assistant_chars < MAX_ASSISTANT_TEXT_CHARS:
                            all_assistant_text.append(text)
                            total_assistant_chars += len(text)
                        if verbose:
                            for tline in text.split("\n"):
                                if tline.strip():
                                    safe_print(f"  {tag} {tline}", flush=True)

                elif etype == "tool":
                    # Tool result
                    if verbose:
                        content = event.get("content", "")
                        if isinstance(content, list):
                            for block in content:
                                if block.get("type") == "tool_result":
                                    val = str(block.get("content", ""))[:300]
                                    safe_print(f"  {tag} └─ Result: {val}", flush=True)
                        elif isinstance(content, str) and content:
                            safe_print(f"  {tag} └─ Result: {content[:300]}", flush=True)

                elif etype == "result":
                    # Final result
                    final_text = event.get("result", "")
                    cost_usd = event.get("cost_usd", 0)
                    num_turns = event.get("num_turns", 0)
                    dur_ms = event.get("duration_ms", 0)
                    is_error = event.get("is_error", False)
                    status = "ERROR" if is_error else "OK"
                    safe_print(f"  {tag} {'─'*50}", flush=True)
                    safe_print(f"  {tag} {status} | {num_turns} turns | ${cost_usd:.4f} | {dur_ms/1000:.1f}s", flush=True)

            except json.JSONDecodeError:
                # Non-JSON output, print raw
                if verbose:
                    safe_print(f"  {tag} {line}", flush=True)
    except (OSError, ValueError):
        # Process was killed by timeout timer — stdout pipe may be broken
        pass
    finally:
        # Cancel the timer if it hasn't fired yet
        if timeout_timer is not None:
            timeout_timer.cancel()

    # Reap the process
    process.wait()

    stderr_thread.join(timeout=5)
    duration = time.time() - start

    # Unregister process from shutdown registry
    if task_id is not None:
        unregister_process(task_id)

    stderr = "".join(stderr_lines)
    exit_code = -1 if timed_out else process.returncode
    if exit_code != 0:
        safe_print(f"  {tag} EXIT CODE: {exit_code}", flush=True)
        if timed_out:
            safe_print(f"  {tag} Session exceeded {session_timeout}s timeout", flush=True)
        if stderr:
            safe_print(f"  {tag} stderr: {stderr[:1000]}", flush=True)

    result = {
        "exit_code": exit_code,
        "output": final_text if not timed_out else f"Session timed out after {session_timeout}s",
        "all_text": all_assistant_text,  # All assistant text blocks from the session
        "duration_s": duration,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
    }

    # Check for structured output files written by the agent
    if task_id is not None:
        structured = _read_structured_output(project_dir, task_id)
        result.update(structured)

    return result


# Droid autonomy level mapping for allowed_tools patterns
_DROID_REVIEW_AUTO_LEVEL = "low"  # read + basic file ops, no system changes


def run_droid_session(
    prompt: str,
    project_dir: Path,
    model: str,
    task_id: int | None = None,
    allowed_tools: str = "",
    verbose: bool = True,
    session_timeout: int | None = None,
) -> dict:
    """
    Run a single droid exec session with stream-json output.

    Mirrors run_claude_session return format so callers can swap transparently.
    Droid event types: system, message, tool_call, tool_result, completion, error.

    Returns dict with:
      - exit_code: int (-1 if session timed out)
      - output: str (final result text)
      - all_text: list[str] (all assistant text blocks)
      - duration_s: float
      - cost_usd: float (always 0 — droid doesn't report cost)
      - num_turns: int
    """
    cmd = [
        "droid", "exec",
        "--output-format", "stream-json",
        "--model", model,
        "--auto", _DROID_REVIEW_AUTO_LEVEL,
        "--cwd", str(project_dir.resolve()),
    ]

    # Map claude allowed_tools to droid --enabled-tools (comma-separated)
    if allowed_tools:
        droid_tools = allowed_tools.replace(" ", ",")
        cmd.extend(["--enabled-tools", droid_tools])

    from safe_print import safe_print
    from progress import colored_tag
    tag = colored_tag(task_id)
    safe_print(f"\n  {tag} Starting droid session...", flush=True)
    safe_print(f"  {tag} cwd: {project_dir.resolve()}", flush=True)
    safe_print(f"  {tag} prompt: {len(prompt)} chars | model: {model}", flush=True)
    safe_print(f"  {tag} {'─'*50}", flush=True)

    start = time.time()

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(project_dir.resolve()),
    )

    # Track process for graceful shutdown
    if task_id is not None:
        register_process(task_id, process)

    # Send prompt via stdin then close it
    process.stdin.write(prompt)
    process.stdin.close()

    # Read stderr in a separate thread to prevent deadlock
    stderr_lines: list[str] = []

    def _read_stderr() -> None:
        for err_line in process.stderr:
            stderr_lines.append(err_line)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    final_text = ""
    all_assistant_text = []
    total_assistant_chars = 0
    num_turns = 0
    timed_out = False

    timeout_timer = None
    if session_timeout is not None:
        def _kill_on_timeout():
            nonlocal timed_out
            timed_out = True
            safe_print(f"  {tag} SESSION TIMEOUT ({session_timeout}s) — killing process", flush=True)
            try:
                process.kill()
            except OSError:
                pass

        timeout_timer = threading.Timer(session_timeout, _kill_on_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

    try:
        for line in process.stdout:
            if timed_out:
                break
            line = line.rstrip()
            if not line:
                continue

            try:
                event = json.loads(line)
                etype = event.get("type", "")

                if etype == "message" and event.get("role") == "assistant":
                    text = event.get("text", "")
                    if text:
                        if total_assistant_chars < MAX_ASSISTANT_TEXT_CHARS:
                            all_assistant_text.append(text)
                            total_assistant_chars += len(text)
                        if verbose:
                            for tline in text.split("\n"):
                                if tline.strip():
                                    safe_print(f"  {tag} {tline}", flush=True)

                elif etype == "tool_call":
                    tool_name = event.get("toolName", "?")
                    params = event.get("parameters", {})
                    if verbose:
                        safe_print(f"  {tag} ┌─ Tool: {tool_name}", flush=True)
                        for k, v in params.items():
                            val = str(v)
                            if len(val) > 200:
                                val = val[:200] + "..."
                            safe_print(f"  {tag} │  {k}: {val}", flush=True)

                elif etype == "tool_result":
                    if verbose:
                        val = str(event.get("value", ""))[:300]
                        safe_print(f"  {tag} └─ Result: {val}", flush=True)

                elif etype == "completion":
                    final_text = event.get("finalText", "")
                    num_turns = event.get("numTurns", 0)
                    dur_ms = event.get("durationMs", 0)
                    safe_print(f"  {tag} {'─'*50}", flush=True)
                    safe_print(f"  {tag} OK | {num_turns} turns | {dur_ms/1000:.1f}s", flush=True)

                elif etype == "error":
                    err_msg = event.get("message", "unknown error")
                    safe_print(f"  {tag} ERROR: {err_msg}", flush=True)

            except json.JSONDecodeError:
                if verbose:
                    safe_print(f"  {tag} {line}", flush=True)
    except (OSError, ValueError):
        pass
    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()

    process.wait()
    stderr_thread.join(timeout=5)
    duration = time.time() - start

    if task_id is not None:
        unregister_process(task_id)

    stderr = "".join(stderr_lines)
    exit_code = -1 if timed_out else process.returncode
    if exit_code != 0:
        safe_print(f"  {tag} EXIT CODE: {exit_code}", flush=True)
        if timed_out:
            safe_print(f"  {tag} Session exceeded {session_timeout}s timeout", flush=True)
        if stderr:
            safe_print(f"  {tag} stderr: {stderr[:1000]}", flush=True)

    result = {
        "exit_code": exit_code,
        "output": final_text if not timed_out else f"Session timed out after {session_timeout}s",
        "all_text": all_assistant_text,
        "duration_s": duration,
        "cost_usd": 0,  # droid doesn't report cost in stream-json
        "num_turns": num_turns,
    }

    if task_id is not None:
        structured = _read_structured_output(project_dir, task_id)
        result.update(structured)

    return result
