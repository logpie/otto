"""Otto product planner — decomposes user intent into product spec + tasks.

The product planner is the first step of the i2p outer loop. It takes a
user's product intent ("build a bookmark manager with tags and search")
and produces:
- product-spec.md at project root (features, scope, non-goals, user journeys)
- architecture.md at project root (only when meaningful)
- A list of tasks with dependencies for tasks.yaml

The planner runs as a full agent session — it can explore existing codebases,
research technologies, and write files. It classifies the intent as either
single-task (no decomposition) or decomposed (multiple vertical tasks).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
from otto.observability import append_text_log

logger = logging.getLogger("otto.product_planner")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PlannedTask:
    """A task produced by the product planner."""
    prompt: str
    depends_on: list[int] = field(default_factory=list)  # indices into the task list


@dataclass
class ProductPlan:
    """Output of the product planner."""
    mode: str                          # "single_task" or "decomposed"
    tasks: list[PlannedTask]           # 1 task (single) or N tasks (decomposed)
    product_spec_path: Path | None     # path to product-spec.md (decomposed only)
    architecture_path: Path | None     # path to architecture.md (optional)
    assumptions: list[str] = field(default_factory=list)  # planner's assumptions
    cost_usd: float = 0.0
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _planner_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "product-planner.log", lines)


# ---------------------------------------------------------------------------
# Planner prompt
# ---------------------------------------------------------------------------

MONOLITHIC_PLANNER_PROMPT = """\
You are a product planner. Given a user's intent, write a product spec
that a single coding agent will use to build the entire product.

Write product-spec.md at the project root with:
- Features and scope (be specific — data model fields, API endpoints, page list)
- NON-GOALS (critical — what NOT to build)
- 3-5 user journeys from the USER'S perspective

  BAD journey: "search endpoint returns results"
  GOOD journey: "A user saves 20 bookmarks over a week, tags some with
  'work' and 'recipes', then searches for 'pasta' and finds the 3 recipe
  bookmarks."

  Journeys should span multiple features and reflect how a real person uses
  the product over time. These will be used for automated product verification.

Then output JSON:
  {"mode": "single_task", "task_prompt": "<comprehensive build prompt>",
   "assumptions": ["...", ...]}

The task_prompt should be a complete, detailed instruction for a coding agent
to build the entire product. Include data model details, API shapes, page list,
and tech choices. The agent will also read product-spec.md.

RULES:
- Explore the existing codebase FIRST (if code exists). Respect existing choices.
- Do NOT invent features the user didn't ask for.
- If the intent is ambiguous, list your assumptions.
- Write product-spec.md FIRST, then the plan JSON.

AFTER writing product-spec.md, write your plan JSON using the Write tool.
The EXACT file path will be specified in the user prompt.
Do NOT output JSON in text — write it to the file.

IMPORTANT: Use the ABSOLUTE file path from the user prompt for the Write tool.
"""

PARALLEL_PLANNER_PROMPT = """\
You are a product planner. Given a user's intent, decompose the work into
parallel-independent tasks for multiple coding agents.

Step 1: Write product-spec.md at the project root with:
- Features and scope (data model fields, API endpoints, page list)
- NON-GOALS
- 3-5 user journeys (for automated product verification)

Step 2: Write architecture.md with:
- Tech stack, directory structure, naming conventions
- API contracts (route patterns, response shapes)
- Shared types and interfaces
This is the CONTRACT between parallel agents — they must agree on interfaces.

Step 3: Produce task decomposition. The ONLY reason to decompose is
PARALLELISM — multiple agents working simultaneously to save wall time.

Split by SYSTEM BOUNDARY — areas that touch different files:
  Task 0: Scaffold + DB schema + seed data + shared types (no deps)
  Task 1: Backend API routes + server logic (depends_on: [0])
  Task 2: Frontend pages + components (depends_on: [0])
  → Tasks 1+2 run in parallel after scaffold completes.

ONLY valid dependency pattern: task 0 (scaffold, no deps), all others
depend on [0] only. NO chains. If you can't parallelize, output
{"mode": "single_task", ...} instead.

Output JSON:
  {"mode": "decomposed", "tasks": [
    {"prompt": "Scaffold: ...", "depends_on": []},
    {"prompt": "Backend: ...", "depends_on": [0]},
    {"prompt": "Frontend: ...", "depends_on": [0]}
  ], "assumptions": [...]}

Each task prompt must include FULL context the agent needs — data model,
API shapes, conventions from architecture.md.

RULES:
- Explore the existing codebase FIRST (if code exists). Respect existing choices.
- Do NOT invent features the user didn't ask for.
- Each task must be independently testable.
- Do NOT separate "write tests" as a task — tests are part of each task.

AFTER writing spec files, write your plan JSON using the Write tool.
The EXACT file path will be specified in the user prompt.
Do NOT output JSON in text — write it to the file.

IMPORTANT: Use the ABSOLUTE file path from the user prompt for the Write tool.
"""


# ---------------------------------------------------------------------------
# Planner invocation
# ---------------------------------------------------------------------------

async def run_product_planner(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
) -> ProductPlan:
    """Run the product planner agent. Returns a ProductPlan."""

    _planner_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] product planner invoked",
        f"intent: {intent}",
    )

    # Use a temp file with a known absolute path — same pattern as spec agent.
    # This eliminates LLM path guessing (the root cause of planner failures).
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile(suffix=".json", prefix="otto_plan_", delete=False) as tf:
        plan_file = Path(tf.name)

    prompt = (
        f"User intent: {intent}\n\n"
        f"Project directory: {project_dir}\n"
        f"Write your plan JSON to this EXACT path: {plan_file}\n"
    )

    # Select planner prompt based on execution mode
    execution_mode = str(config.get("execution_mode", "monolithic") or "monolithic").strip().lower()
    planner_prompt = PARALLEL_PLANNER_PROMPT if execution_mode == "planned" else MONOLITHIC_PLANNER_PROMPT

    # Full agent session — can explore codebase, research, write files
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_planner_settings(config),
        env=_subprocess_env(),
        system_prompt=planner_prompt,
    )
    model = _planner_model(config)
    if model:
        options.model = model

    started_at = time.monotonic()
    raw_output, cost, _result = await run_agent_query(prompt, options)
    cost_usd = float(cost or 0.0)
    duration_s = round(time.monotonic() - started_at, 1)

    _planner_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] planner completed (${cost_usd:.3f}, {duration_s:.1f}s)",
        f"raw output length: {len(raw_output)} chars",
        f"raw output:\n{raw_output}",
        "",
    )

    try:
        plan = _parse_planner_output(raw_output, project_dir, plan_file=plan_file)
    except ValueError:
        # Planner didn't write otto_plan.json and no JSON in output.
        # Retry with a focused prompt asking just for the JSON file.
        _planner_log(
            project_dir,
            "otto_plan.json not created — retrying with focused prompt",
        )
        retry_prompt = (
            "You described a plan but did not write the otto_plan.json file. "
            "Write it NOW using the Write tool. The file must be at the project root "
            f"({project_dir / 'otto_plan.json'}). "
            "It must be valid JSON matching this schema:\n"
            '{"mode": "single_task"|"decomposed", "task_prompt": "..." (for single_task), '
            '"tasks": [{"prompt": "...", "depends_on": []}] (for decomposed), '
            '"assumptions": ["..."]}\n\n'
            f"Your earlier plan description:\n{raw_output[:3000]}"
        )
        retry_options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            setting_sources=_planner_settings(config),
            env=_subprocess_env(),
            system_prompt={"type": "preset", "preset": "claude_code"},
            max_turns=3,
        )
        if model:
            retry_options.model = model
        retry_output, retry_cost, _ = await run_agent_query(retry_prompt, retry_options)
        cost_usd += float(retry_cost or 0.0)

        _planner_log(project_dir, f"retry output: {retry_output[:500]}")
        plan = _parse_planner_output(retry_output, project_dir)

    plan.cost_usd = cost_usd
    plan.duration_s = duration_s

    _planner_log(
        project_dir,
        f"mode: {plan.mode}",
        f"tasks: {len(plan.tasks)}",
        f"product_spec: {plan.product_spec_path}",
        f"architecture: {plan.architecture_path}",
        f"assumptions: {plan.assumptions}",
        "",
    )

    return plan


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_planner_output(raw: str, project_dir: Path, plan_file: Path | None = None) -> ProductPlan:
    """Parse the product planner's output.

    The planner writes otto_plan.json via the Write tool. We read it from disk.
    Falls back to parsing JSON from the text output if the file doesn't exist.
    """
    data = None

    # Primary: read from file (avoids markdown fence escaping issues)
    if plan_file is None:
        plan_file = project_dir / "otto_plan.json"
    if plan_file.exists():
        try:
            data = json.loads(plan_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("otto_plan.json exists but failed to parse: %s", exc)
        finally:
            # Clean up — this is a transient file, not a project artifact
            plan_file.unlink(missing_ok=True)

    # Fallback: extract JSON from text output
    if data is None:
        text = raw.strip()
        json_str = None

        if "```json" in text:
            parts = text.split("```json")
            if len(parts) > 1:
                json_part = parts[-1].split("```")[0].strip()
                json_str = json_part
        elif "```" in text:
            parts = text.split("```")
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("{"):
                    json_str = stripped
                    break

        if json_str is None:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                json_str = text[start:end + 1]

        if json_str is None:
            raise ValueError(f"No JSON found in planner output and otto_plan.json not created. Output: {text[:500]}")

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            data = json.loads(_fix_json_newlines(json_str))
    mode = data.get("mode", "single_task")

    if mode == "single_task":
        task_prompt = data.get("task_prompt", "")
        if not task_prompt:
            raise ValueError("single_task mode but no task_prompt in output")
        # Planner may still write product-spec.md even for single-task builds
        product_spec_path = project_dir / "product-spec.md"
        architecture_path = project_dir / "architecture.md"
        return ProductPlan(
            mode="single_task",
            tasks=[PlannedTask(prompt=task_prompt)],
            product_spec_path=product_spec_path if product_spec_path.exists() else None,
            architecture_path=architecture_path if architecture_path.exists() else None,
            assumptions=data.get("assumptions", []),
        )

    # Decomposed mode
    raw_tasks = data.get("tasks", [])
    if not raw_tasks:
        raise ValueError("decomposed mode but no tasks in output")

    tasks = []
    for item in raw_tasks:
        if isinstance(item, str):
            tasks.append(PlannedTask(prompt=item))
        elif isinstance(item, dict):
            tasks.append(PlannedTask(
                prompt=item.get("prompt", ""),
                depends_on=item.get("depends_on", []),
            ))
        else:
            raise ValueError(f"Invalid task format: {item}")

    # Check if planner wrote the files
    product_spec_path = project_dir / "product-spec.md"
    architecture_path = project_dir / "architecture.md"
    if not product_spec_path.exists():
        raise ValueError("decomposed mode but planner did not write product-spec.md")

    return ProductPlan(
        mode="decomposed",
        tasks=tasks,
        product_spec_path=product_spec_path,
        architecture_path=architecture_path if architecture_path.exists() else None,
        assumptions=data.get("assumptions", []),
    )


# ---------------------------------------------------------------------------
# Helpers (reuse from planner.py)
# ---------------------------------------------------------------------------

def _fix_json_newlines(s: str) -> str:
    """Fix literal newlines inside JSON string values.

    LLMs often produce JSON with real line breaks inside quoted strings
    instead of \\n escape sequences. This scans the string and replaces
    literal newlines (0x0a) that appear inside quoted values.
    """
    result = []
    in_string = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and in_string and i + 1 < len(s):
            # Escaped character — pass through both chars
            result.append(c)
            result.append(s[i + 1])
            i += 2
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
        elif c == "\n" and in_string:
            result.append("\\n")
        elif c == "\r" and in_string:
            result.append("\\r")
        elif c == "\t" and in_string:
            result.append("\\t")
        else:
            result.append(c)
        i += 1
    return "".join(result)


def _planner_settings(config: dict[str, Any]) -> list[str]:
    return str(config.get("planner_agent_settings", "project") or "project").split(",")


def _planner_model(config: dict[str, Any]) -> str | None:
    model = config.get("planner_model")
    return str(model) if model else None
