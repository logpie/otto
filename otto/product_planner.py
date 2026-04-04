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

PRODUCT_PLANNER_SYSTEM_PROMPT = """\
You are a product planner for an autonomous coding system. Given a user's
intent, you produce everything needed for a team of coding agents to build
the product without further human input.

DECISION 1: Single task or decomposed?

Single task when the intent is cohesive:
- One surface, one data model, one core workflow
- "Build a weather app" — one page, one API, one data source
- "Build a CLI tool that converts CSV to JSON"
- If in doubt, prefer single task — bad decomposition is worse than none

Decompose when the intent has multiple failure domains:
- Multiple user-facing surfaces (web + extension + API)
- Auth/payments/security as separate concerns
- Multiple external integrations or user roles
- Background jobs / async processing

DECISION 2: What to produce

For SINGLE TASK:
- Write a comprehensive task prompt that covers the full product
- ALSO write product-spec.md with features, scope, non-goals, and user journeys
  (needed for product verification after the build)
- Output JSON: {"mode": "single_task", "task_prompt": "..."}

For DECOMPOSED:
- Write product-spec.md at the project root with:
  - Features and scope
  - NON-GOALS (critical — what NOT to build)
  - 3-5 user journeys from the USER'S perspective

  BAD journey: "search endpoint returns results"
  GOOD journey: "A user saves 20 bookmarks over a week, tags some with
  'work' and 'recipes', then searches for 'pasta' and finds the 3 recipe
  bookmarks. They export all bookmarks, delete app data, re-import, and
  everything is still there."

  Journeys should span multiple features and reflect how a real person
  uses the product over time.

- Write architecture.md at the project root ONLY when there are genuine
  tradeoffs (multiple viable tech stacks, complex data model, external
  integrations). For simple projects, let the coding agents decide.
  When you do write it, include conventions that agents should follow:
  route patterns, directory structure, naming conventions, error formats.

- Produce task decomposition as JSON. The ONLY reason to decompose is
  PARALLELISM — multiple agents working simultaneously to save wall time.

  RULE: Every task must be INDEPENDENT — no depends_on between tasks.
  If tasks can't run in parallel, DON'T decompose — use single_task instead.

  Split by SYSTEM BOUNDARY — areas that touch different files:

  GOOD (parallel-safe — different files):
    Task 1: Scaffold + DB schema + seed data + shared types
    Task 2: Backend API routes + server logic (reads schema from task 1)
    Task 3: Frontend pages + components (reads types from task 1)
    → Task 1 runs first (depends_on: []), tasks 2+3 run in parallel (depends_on: [0])

  EXCEPTION: A single shared scaffold task (task 0) that sets up the project
  skeleton, DB schema, and shared types is allowed as a dependency for all
  other tasks. This is the ONLY valid dependency pattern:
    Task 0: scaffold (no deps)
    Task 1..N: parallel groups (each depends_on: [0] only)

  BAD (serial chain — no parallelism, worse than monolithic):
    Task 1: Auth → Task 2: CRUD → Task 3: Dashboard → Task 4: Polish
    (each depends on previous = serial execution with merge overhead)

  BAD (overlapping files — merge conflicts):
    Task 1: User CRUD + user API + user pages
    Task 2: Project CRUD + project API + project pages
    (both touch layout.tsx, Prisma schema, middleware — conflict guaranteed)

  Each task prompt must include the FULL context from product-spec.md and
  architecture.md that the agent needs. The agent reads existing code but
  gets its scope from the task prompt.

- Output JSON:
  {"mode": "decomposed", "tasks": [
    {"prompt": "Scaffold: set up project, DB schema, shared types...", "depends_on": []},
    {"prompt": "Backend: API routes, server logic...", "depends_on": [0]},
    {"prompt": "Frontend: pages, components...", "depends_on": [0]}
  ], "assumptions": ["SQLite chosen for simplicity", ...]}

  depends_on uses 0-based indices. Only valid pattern: task 0 (scaffold)
  with no deps, all others depend on [0] only. NO chains.

RULES:
- Explore the existing codebase FIRST (if code exists). Respect existing
  choices (framework, language, patterns).
- Research unfamiliar technologies before committing to them.
- Do NOT invent features the user didn't ask for. Scope creep is the enemy.
- If the intent is ambiguous, list your assumptions in the output.
  Do NOT silently fill gaps.
- Each task must be independently testable.
- Do NOT separate "write tests" as a task — tests are part of each task.
- Task prompts should be detailed enough for a coding agent to implement
  without reading the product spec. Include relevant API shapes, data model
  details, and conventions in each task prompt.

AFTER writing spec files, write your task plan using the Write tool.
The EXACT file path will be specified in the user prompt — use that path.
The file must contain valid JSON matching the schema above.
Do NOT output the JSON in your text response — write it to the file.

IMPORTANT: Use the ABSOLUTE file path from the user prompt for the Write tool.
Do NOT guess the path. Task prompts may contain markdown with code blocks.
Writing to a file avoids JSON-in-markdown escaping issues.
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

    # Full agent session — can explore codebase, research, write files
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_planner_settings(config),
        env=_subprocess_env(),
        system_prompt=PRODUCT_PLANNER_SYSTEM_PROMPT,
        # No max_turns — let it think as long as needed
        # 1hr circuit breaker via max_task_time in config
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
