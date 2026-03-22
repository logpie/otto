"""Otto v4 planner — execution plan dataclasses and LLM plan/replan.

This module contains:
1. Pure dataclasses (TaskPlan, Batch, ExecutionPlan) — no I/O
2. parse_plan_json() / default_plan() — deterministic plan construction
3. plan() / replan() — LLM-driven planning (Step 3, added later)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("otto.planner")


@dataclass
class TaskPlan:
    """Plan for a single task within a batch."""
    task_key: str
    strategy: str = "direct"      # "direct" | "research_first"
    research_query: str = ""      # query for research agent (if strategy=research_first)
    hint: str = ""                # hint passed to coding agent
    skip_qa: bool = False         # skip QA for trivial tasks
    effort: str = "high"          # agent effort level


@dataclass
class Batch:
    """A group of tasks that can run in parallel."""
    tasks: list[TaskPlan] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    """Ordered list of batches with cross-task learnings."""
    batches: list[Batch] = field(default_factory=list)
    learnings: list[str] = field(default_factory=list)

    @property
    def total_tasks(self) -> int:
        return sum(len(b.tasks) for b in self.batches)

    @property
    def is_empty(self) -> bool:
        return self.total_tasks == 0

    def remaining_after(self, completed_keys: set[str]) -> "ExecutionPlan":
        """Return a new plan with completed tasks removed."""
        remaining_batches = []
        for batch in self.batches:
            remaining = [tp for tp in batch.tasks if tp.task_key not in completed_keys]
            if remaining:
                remaining_batches.append(Batch(tasks=remaining))
        return ExecutionPlan(batches=remaining_batches, learnings=list(self.learnings))


def parse_plan_json(raw: str) -> ExecutionPlan | None:
    """Parse a JSON execution plan from LLM output.

    Handles JSON wrapped in markdown fences. Returns None on malformed input.

    Expected JSON format:
    {
        "batches": [
            {
                "tasks": [
                    {"task_key": "abc123", "strategy": "direct", "hint": "..."},
                    ...
                ]
            },
            ...
        ],
        "learnings": ["..."]
    }
    """
    # Strip markdown fences
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    # Try to find JSON object if there's surrounding text
    if not text.startswith("{"):
        start = text.find("{")
        if start >= 0:
            # Find matching closing brace
            depth = 0
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        text = text[start:i + 1]
                        break

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    batches_raw = data.get("batches")
    if not isinstance(batches_raw, list):
        return None

    batches = []
    for batch_data in batches_raw:
        if not isinstance(batch_data, dict):
            continue
        tasks_raw = batch_data.get("tasks", [])
        if not isinstance(tasks_raw, list):
            continue
        task_plans = []
        for tp_data in tasks_raw:
            if not isinstance(tp_data, dict):
                continue
            task_key = tp_data.get("task_key", "")
            if not task_key:
                continue
            task_plans.append(TaskPlan(
                task_key=task_key,
                strategy=tp_data.get("strategy", "direct"),
                research_query=tp_data.get("research_query", ""),
                hint=tp_data.get("hint", ""),
                skip_qa=bool(tp_data.get("skip_qa", False)),
                effort=tp_data.get("effort", "high"),
            ))
        if task_plans:
            batches.append(Batch(tasks=task_plans))

    if not batches:
        return None

    learnings = data.get("learnings", [])
    if not isinstance(learnings, list):
        learnings = []

    return ExecutionPlan(batches=batches, learnings=[str(l) for l in learnings])


def default_plan(tasks: list[dict[str, Any]]) -> ExecutionPlan:
    """Create a dependency-respecting fallback plan.

    Uses topological sort to respect depends_on constraints. Independent
    tasks are grouped into parallel batches. Used when the planner LLM
    fails or returns malformed JSON.
    """
    import graphlib

    # Build lookup structures
    task_by_key: dict[str, dict[str, Any]] = {}
    id_to_key: dict[int, str] = {}
    for task in tasks:
        key = task.get("key", "")
        if not key:
            continue
        task_by_key[key] = task
        id_to_key[task.get("id", 0)] = key

    if not task_by_key:
        return ExecutionPlan()

    # Build dependency graph: key -> set of dependency keys
    ts = graphlib.TopologicalSorter()
    for key, task in task_by_key.items():
        dep_ids = task.get("depends_on") or []
        dep_keys = [id_to_key[d] for d in dep_ids if d in id_to_key]
        ts.add(key, *dep_keys)

    # Topological sort into batches (each "ready" group is a parallel batch)
    try:
        ts.prepare()
    except graphlib.CycleError:
        # Cycle detected — fall back to sequential order
        batches = [Batch(tasks=[TaskPlan(task_key=k)]) for k in task_by_key]
        return ExecutionPlan(batches=batches)

    batches = []
    while ts.is_active():
        ready = list(ts.get_ready())
        if not ready:
            break
        batch_tasks = [TaskPlan(task_key=k) for k in ready]
        batches.append(Batch(tasks=batch_tasks))
        for k in ready:
            ts.done(k)

    return ExecutionPlan(batches=batches)


# ---------------------------------------------------------------------------
# LLM-driven planning (plan + replan)
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM_PROMPT = """\
You are a planning agent for an autonomous coding pipeline. You analyze tasks
and produce an execution plan as JSON.

Given a list of tasks with their specs, dependencies, and project context,
produce an optimal execution plan that:
1. Groups independent tasks into parallel batches
2. Respects depends_on constraints (dependent tasks go in later batches)
3. Assigns strategies: "direct" for straightforward tasks, "research_first"
   for tasks that need web research or doc reading before coding

Output ONLY a JSON object in this exact format:
```json
{
    "batches": [
        {
            "tasks": [
                {
                    "task_key": "<12-char hex key>",
                    "strategy": "direct",
                    "effort": "high"
                }
            ]
        }
    ],
    "learnings": []
}
```

Rules:
- Tasks with depends_on MUST be in a LATER batch than their dependencies
- Independent tasks SHOULD be in the same batch (parallel execution)
- Keep it simple — most tasks are "direct" strategy
- Only use "research_first" when the task involves unfamiliar APIs/libraries
- Do NOT include hints — the coding agent has full context and decides its own approach
"""


from pathlib import Path


async def plan(
    tasks: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
) -> ExecutionPlan:
    """Create an execution plan using a single Sonnet query.

    Falls back to default_plan() on any failure.
    """
    if not tasks:
        return ExecutionPlan()

    if len(tasks) == 1:
        # Single task — no need for LLM planning
        return default_plan(tasks)

    try:
        from claude_agent_sdk import ClaudeAgentOptions, query as sdk_query
    except ImportError:
        return default_plan(tasks)

    # Build task summary for the planner
    task_lines = []
    for t in tasks:
        deps = t.get("depends_on", [])
        dep_str = f" (depends_on: #{', #'.join(str(d) for d in deps)})" if deps else ""
        spec_count = len(t.get("spec", []))
        task_lines.append(
            f"- key={t['key']} id=#{t.get('id', '?')} "
            f"spec={spec_count} items{dep_str}: {t.get('prompt', '')}"
        )

    prompt = f"""Plan the execution of these {len(tasks)} tasks:

{chr(10).join(task_lines)}

Produce the JSON execution plan. Group independent tasks into parallel batches.
Respect depends_on constraints."""

    try:
        from otto.verify import _subprocess_env
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            setting_sources=["project"],
            env=_subprocess_env(),
            effort="low",
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            model="sonnet",
        )

        # Collect all text output
        text_parts: list[str] = []
        async for message in sdk_query(prompt=prompt, options=agent_opts):
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        text_parts.append(block.text)

        raw_output = "\n".join(text_parts)
        result = parse_plan_json(raw_output)
        if result and not result.is_empty:
            return result

        logger.warning("Planner output unparseable, falling back to default_plan. Raw: %s", raw_output[:300])
    except Exception as exc:
        logger.warning("Planner query failed: %s, falling back to default_plan", exc)

    return default_plan(tasks)


async def replan(
    context: Any,  # PipelineContext
    remaining_plan: ExecutionPlan,
    config: dict[str, Any],
    project_dir: Path,
) -> ExecutionPlan:
    """Replan at batch boundary with accumulated results/learnings.

    Called when a batch completes with failures. Uses context to inform
    retry strategies. Falls back to remaining_plan on failure.
    """
    if remaining_plan.is_empty:
        return remaining_plan

    try:
        from claude_agent_sdk import ClaudeAgentOptions, query as sdk_query
    except ImportError:
        return remaining_plan

    # Build rich context — pass raw environmental feedback, not summaries
    results_summary = []
    for key, result in context.results.items():
        if result.success:
            results_summary.append(
                f"- {key}: PASSED — {result.diff_summary or 'no diff'}"
            )
        else:
            parts = [f"- {key}: FAILED"]
            if result.error:
                parts.append(f"  Error: {result.error}")
            if result.qa_report:
                parts.append(f"  QA report: {result.qa_report}")
            results_summary.append("\n".join(parts))

    learnings_str = "\n".join(f"- {l}" for l in context.learnings) if context.learnings else "None"

    # Include research findings
    research_parts = []
    for key in context.results:
        findings = context.get_research(key)
        if findings:
            research_parts.append(f"- Research for {key}: {findings}")
    research_str = "\n".join(research_parts) if research_parts else "None"

    remaining_tasks = []
    for batch in remaining_plan.batches:
        for tp in batch.tasks:
            remaining_tasks.append(f"- key={tp.task_key} strategy={tp.strategy}")

    prompt = f"""Replan the remaining tasks based on what happened so far.

COMPLETED RESULTS:
{chr(10).join(results_summary) or "None yet"}

LEARNINGS:
{learnings_str}

RESEARCH FINDINGS:
{research_str}

REMAINING TASKS:
{chr(10).join(remaining_tasks)}

Update strategies based on what we learned. Do NOT include hints.
Output the JSON execution plan for REMAINING tasks only."""

    try:
        from otto.verify import _subprocess_env
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            setting_sources=["project"],
            env=_subprocess_env(),
            effort="low",
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            model="sonnet",
        )

        text_parts: list[str] = []
        async for message in sdk_query(prompt=prompt, options=agent_opts):
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        text_parts.append(block.text)

        raw_output = "\n".join(text_parts)
        result = parse_plan_json(raw_output)
        if result and not result.is_empty:
            return result

        logger.warning("Replanner output unparseable, keeping remaining plan. Raw: %s", raw_output[:300])
    except Exception as exc:
        logger.warning("Replan query failed: %s, keeping remaining plan", exc)

    return remaining_plan
