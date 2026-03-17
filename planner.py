"""
Planner: uses Claude to read app_spec.txt and generate a task DAG with dependencies.

Includes plan review step (P2-5) to validate spec coverage, task specificity,
dependency correctness, and parallelism after DAG generation.
"""

import json
import re
from pathlib import Path
from worker import run_claude_session
from scheduler import validate_dag, compute_max_parallelism, save_tasks


PLANNER_SYSTEM = """You are a project planner. Your job is to read a project specification and decompose it into a list of implementation tasks with dependencies.

CRITICAL RULES:
1. Output ONLY valid JSON — no markdown fences, no explanation, JUST the JSON array.
2. Each task must have these fields:
   - "id": integer starting from 1
   - "description": clear description of what to implement
   - "depends_on": array of task IDs that must complete before this task can start (empty [] if no dependencies)
   - "test_command": a shell command that exits 0 on success to verify the task is done
   - "status": always "pending"
   - "priority": integer (lower = higher priority)
   - "attempts": 0
   - "error_log": ""

3. Dependencies must form a valid DAG (no cycles).
4. Independent tasks should NOT depend on each other — this enables parallel execution.
5. Order: foundational/setup tasks first, then core features, then integration, then tests, then polish.
6. Group tasks so independent features can be built in parallel.
7. Each task should be self-contained enough for one agent to complete.
8. Keep tasks granular but not too small — aim for 8-25 tasks total depending on project complexity.

Example structure:
- Task 1: Project setup (depends_on: [])
- Task 2: Database schema (depends_on: [1])
- Task 3: API endpoint A (depends_on: [2])
- Task 4: API endpoint B (depends_on: [2])   ← can run parallel with Task 3
- Task 5: Frontend component A (depends_on: [3])
- Task 6: Frontend component B (depends_on: [4])  ← can run parallel with Task 5
- Task 7: Integration tests (depends_on: [3, 4])
- Task 8: End-to-end tests (depends_on: [5, 6, 7])
"""


def generate_task_dag(project_dir: Path, model: str) -> list[dict]:
    """
    Read app_spec.txt and use Claude to generate a task DAG.

    Returns the task list and saves it to task_list.json.
    """
    spec_file = project_dir / "app_spec.txt"
    if not spec_file.exists():
        raise FileNotFoundError(f"{spec_file} not found")

    spec_content = spec_file.read_text()

    prompt = f"""{PLANNER_SYSTEM}

Here is the project specification:

---
{spec_content}
---

Generate the task list as a JSON array. The working directory for all tasks will be: {project_dir.resolve()}

CRITICAL INSTRUCTIONS:
1. Do NOT use any tools. Do NOT call Bash, Write, Read, or any other tool.
2. Your ENTIRE response must be ONLY the JSON array. Start with [ and end with ].
3. No markdown fences, no explanation, no summary, no commentary before or after the JSON.
4. If you feel the urge to explain anything, DON'T. Just output the raw JSON array."""

    print("\n  Planner: Generating task DAG from app_spec.txt...\n")

    result = run_claude_session(
        prompt=prompt,
        project_dir=project_dir,
        model=model,
        task_id=None,
        allowed_tools="Read",  # Minimal tools — planner only needs to read spec, not write
    )

    if result["exit_code"] != 0:
        raise RuntimeError(
            f"Planner session failed (exit={result['exit_code']}):\n"
            f"  stderr: {result.get('stderr', '')[:1000]}\n"
            f"  stdout: {result.get('output', '')[:1000]}"
        )

    # Parse JSON from output
    output = result["output"].strip()
    all_text = result.get("all_text", [])

    if not output and not all_text:
        raise ValueError(
            "Planner returned empty output.\n"
            f"  stderr: {result.get('stderr', '')[:1000]}"
        )

    print(f"  Planner: raw output length = {len(output)} chars")
    print(f"  Planner: collected {len(all_text)} assistant text blocks")

    # Try extracting JSON from the final result first
    tasks = _extract_json(output) if output else None

    # Fallback 1: search through all assistant text blocks (newest first)
    if not tasks and all_text:
        for text_block in reversed(all_text):
            tasks = _extract_json(text_block)
            if tasks:
                print("  Planner: Extracted tasks from assistant text block (fallback 1)")
                break

    # Fallback 2: concatenate all text and try again
    if not tasks and all_text:
        combined = "\n".join(all_text)
        tasks = _extract_json(combined)
        if tasks:
            print("  Planner: Extracted tasks from combined text (fallback 2)")

    # Fallback 3: if Claude wrote task_list.json via tool
    if not tasks:
        task_file = project_dir / "task_list.json"
        if task_file.exists():
            try:
                tasks = json.loads(task_file.read_text())
                if isinstance(tasks, list):
                    print("  Planner: Extracted tasks from task_list.json (fallback 3)")
                else:
                    tasks = None
            except json.JSONDecodeError:
                tasks = None

    if not tasks:
        raise ValueError(
            f"Planner did not produce valid JSON task list.\n"
            f"  Output starts with: {output[:300]}\n"
            f"  Last assistant block: {all_text[-1][:300] if all_text else 'N/A'}"
        )

    # Validate task structure (required fields, unique IDs, depends_on refs)
    tasks = _validate_tasks(tasks)

    # Validate DAG — fail on errors instead of silently filtering
    errors = validate_dag(tasks)
    if errors:
        raise ValueError(
            f"Planner produced invalid DAG:\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    # Save
    save_tasks(project_dir, tasks)

    max_parallel = compute_max_parallelism(tasks)
    print(f"\n  Planner: Generated {len(tasks)} tasks")
    print(f"  Planner: Max parallelism = {max_parallel} agents")

    return tasks


# ──────────────────────────────────────────────────────────
# Plan Review (P2-5)
# ──────────────────────────────────────────────────────────

PLAN_REVIEW_SYSTEM = """You are a PLAN REVIEWER for a multi-agent coding system.

You have been given:
1. The project specification (app_spec.txt)
2. The generated task DAG (list of tasks with dependencies)

Your job is to review the plan for quality and correctness. Check ALL of the following:

## 1. Spec Coverage
- Does every requirement in the spec have at least one corresponding task?
- Are there any spec requirements that are NOT covered by any task?
- List any gaps.

## 2. Task Specificity
- Is each task description specific enough for an autonomous coding agent to implement it?
- Are there vague tasks like "implement the system" or "finish everything"?
- Each task should clearly state WHAT to create/modify, WHERE, and HOW to verify.

## 3. Dependency Correctness
- Are dependencies correct? (Does task B actually need task A's output?)
- Are there missing dependencies? (Does task C use something from task A but not depend on it?)
- Are there unnecessary dependencies that would block parallelism?

## 4. Parallelism
- Is the DAG unnecessarily sequential? (e.g., independent features chained linearly)
- Could more tasks run in parallel if dependencies were restructured?
- Is the parallelism reasonable for the project size?

## Output Format

You MUST output your review as a single JSON object on a line starting with PLAN_REVIEW:
PLAN_REVIEW: {"passed": true/false, "issues": ["issue 1", "issue 2"], "suggestions": ["suggestion 1"], "revised_tasks": null}

- Set "passed" to true if the plan is acceptable (minor issues are OK).
- Set "passed" to false if there are significant gaps or problems.
- List concrete issues in "issues" (empty list if none).
- List improvement suggestions in "suggestions" (empty list if none).
- If passed is false and you can fix it, set "revised_tasks" to the corrected JSON task array.
  Otherwise set "revised_tasks" to null.

IMPORTANT:
- Do NOT use any tools. Do NOT call Bash, Write, Read, or any other tool.
- Your ENTIRE response should be analysis followed by the PLAN_REVIEW: JSON line.
- Be strict but fair — only flag real problems, not stylistic preferences."""


def review_plan(project_dir: Path, tasks: list[dict], model: str) -> dict:
    """Review a generated task DAG for quality: spec coverage, specificity, deps, parallelism.

    Returns a dict with keys: passed (bool), issues (list), suggestions (list),
    revised_tasks (list|None).
    """
    spec_file = project_dir / "app_spec.txt"
    spec_content = spec_file.read_text() if spec_file.exists() else "(spec not found)"

    tasks_json = json.dumps(tasks, indent=2)

    prompt = f"""{PLAN_REVIEW_SYSTEM}

## Project Specification

---
{spec_content}
---

## Generated Task DAG

```json
{tasks_json}
```

Review the plan now. Output your verdict as PLAN_REVIEW: {{...}} JSON."""

    print("\n  Plan Review: Reviewing generated task DAG...\n")

    result = run_claude_session(
        prompt=prompt,
        project_dir=project_dir,
        model=model,
        task_id=None,
        allowed_tools="Read",  # Read-only — reviewer should not modify anything
    )

    # Parse the PLAN_REVIEW: JSON line from output
    review_result = _parse_plan_review(result)

    if review_result["passed"]:
        print("  Plan Review: PASSED")
        if review_result.get("suggestions"):
            for s in review_result["suggestions"]:
                print(f"    suggestion: {s}")
    else:
        print("  Plan Review: FAILED — issues found:")
        for issue in review_result.get("issues", []):
            print(f"    - {issue}")

    return review_result


def _parse_plan_review(result: dict) -> dict:
    """Parse PLAN_REVIEW: JSON from agent output."""
    default = {"passed": True, "issues": [], "suggestions": [], "revised_tasks": None}

    # Search through all text sources
    sources = []
    if result.get("output"):
        sources.append(result["output"])
    for block in result.get("all_text", []):
        sources.append(block)

    for text in sources:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("PLAN_REVIEW:"):
                json_str = line[len("PLAN_REVIEW:"):].strip()
                try:
                    parsed = json.loads(json_str)
                    return {
                        "passed": parsed.get("passed", True),
                        "issues": parsed.get("issues", []),
                        "suggestions": parsed.get("suggestions", []),
                        "revised_tasks": parsed.get("revised_tasks"),
                    }
                except json.JSONDecodeError:
                    print(f"  Plan Review: WARNING — failed to parse review JSON: {json_str[:200]}")

    print("  Plan Review: WARNING — no PLAN_REVIEW line found, defaulting to passed")
    return default


def _extract_json_by_bracket_matching(text: str) -> list[dict] | None:
    """Extract JSON array using bracket-depth matching instead of greedy regex."""
    start = text.find('[')
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, list):
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find('[', start + 1)
    return None


def _extract_json(text: str) -> list[dict] | None:
    """Extract JSON array from text that might contain markdown fences or extra text."""
    # Try direct parse first
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in ```json ... ``` code blocks
    match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Use bracket-matching extraction instead of greedy regex
    return _extract_json_by_bracket_matching(text)


REQUIRED_FIELDS = {"id", "description", "depends_on", "test_command", "status"}


def _validate_tasks(tasks: list[dict]) -> list[dict]:
    """Validate task structure: required fields, unique positive IDs, valid depends_on refs."""
    # Check required fields BEFORE setting defaults, so missing fields are caught
    for task in tasks:
        missing = REQUIRED_FIELDS - task.keys()
        if missing:
            raise ValueError(f"Task #{task.get('id', '?')} missing required fields: {missing}")

    # Validate unique positive task IDs
    seen_ids = set()
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, int) or task_id < 1:
            raise ValueError(f"Task has invalid id={task_id!r}; must be a positive integer")
        if task_id in seen_ids:
            raise ValueError(f"Duplicate task id={task_id}")
        seen_ids.add(task_id)

    # Validate depends_on: must be a list of positive integers referencing existing task IDs
    for task in tasks:
        deps = task["depends_on"]
        if not isinstance(deps, list):
            raise ValueError(f"Task #{task['id']} depends_on must be a list, got {type(deps).__name__}")
        for d in deps:
            if not isinstance(d, int) or d < 1:
                raise ValueError(f"Task #{task['id']} has invalid depends_on entry {d!r}; must be a positive integer")
            if d not in seen_ids:
                raise ValueError(f"Task #{task['id']} depends_on references non-existent task id={d}")

    # Fill defaults for optional fields AFTER validation
    for task in tasks:
        task.setdefault("attempts", 0)
        task.setdefault("error_log", "")
        task.setdefault("priority", task.get("id", 0))

    return tasks
