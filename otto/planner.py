"""Otto planner — execution plan dataclasses and smart relationship analysis."""

from __future__ import annotations

import graphlib
import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

logger = logging.getLogger("otto.planner")

RELATIONSHIP_VALUES = {
    "INDEPENDENT",
    "ADDITIVE",
    "DEPENDENT",
    "CONTRADICTORY",
    "UNCERTAIN",
}


@dataclass
class TaskPlan:
    """Plan for a single task within a batch."""

    task_key: str
    strategy: str = "direct"
    research_query: str = ""
    skip_qa: bool = False
    effort: str = "high"


@dataclass
class Batch:
    """A group of tasks that can run in parallel."""

    tasks: list[TaskPlan] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    """Ordered list of batches with planner metadata."""

    batches: list[Batch] = field(default_factory=list)
    learnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    analysis: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tasks(self) -> int:
        return sum(len(batch.tasks) for batch in self.batches)

    @property
    def is_empty(self) -> bool:
        return self.total_tasks == 0

    def remaining_after(self, completed_keys: set[str]) -> "ExecutionPlan":
        """Return a new plan with completed tasks removed and metadata preserved."""
        remaining_batches: list[Batch] = []
        unresolved_keys: set[str] = set()
        for batch in self.batches:
            remaining = [tp for tp in batch.tasks if tp.task_key not in completed_keys]
            if remaining:
                remaining_batches.append(Batch(tasks=remaining))
                unresolved_keys.update(tp.task_key for tp in remaining)

        remaining_conflicts = [
            conflict
            for conflict in self.conflicts
            if set(_conflict_task_keys(conflict)).issubset(unresolved_keys)
        ]
        remaining_analysis = [
            item
            for item in self.analysis
            if item.get("task_a") in unresolved_keys
            and item.get("task_b") in unresolved_keys
        ]
        return ExecutionPlan(
            batches=remaining_batches,
            learnings=list(self.learnings),
            conflicts=remaining_conflicts,
            analysis=remaining_analysis,
        )


def _strip_json_wrapper(raw: str) -> str:
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    if text.startswith("{"):
        return text

    start = text.find("{")
    if start < 0:
        return text

    depth = 0
    for idx, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return text


def _normalize_relationship(value: Any) -> str | None:
    relation = str(value or "").strip().upper()
    if relation in RELATIONSHIP_VALUES:
        return relation
    return None


def _conflict_task_keys(conflict: dict[str, Any]) -> list[str]:
    tasks = conflict.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [str(task_key) for task_key in tasks if str(task_key)]


def parse_plan_json(raw: str) -> ExecutionPlan | None:
    """Parse planner JSON from model output."""
    try:
        data = json.loads(_strip_json_wrapper(raw))
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    batches_raw = data.get("batches", [])
    conflicts_raw = data.get("conflicts", [])
    analysis_raw = data.get("analysis", [])

    if not isinstance(batches_raw, list):
        return None
    if conflicts_raw is not None and not isinstance(conflicts_raw, list):
        return None
    if analysis_raw is not None and not isinstance(analysis_raw, list):
        return None

    batches: list[Batch] = []
    for batch_data in batches_raw:
        if not isinstance(batch_data, dict):
            continue
        task_plans: list[TaskPlan] = []
        tasks_raw = batch_data.get("tasks", [])
        if not isinstance(tasks_raw, list):
            continue
        for tp_data in tasks_raw:
            if not isinstance(tp_data, dict):
                continue
            task_key = str(tp_data.get("task_key", "") or "").strip()
            if not task_key:
                continue
            task_plans.append(
                TaskPlan(
                    task_key=task_key,
                    strategy=str(tp_data.get("strategy", "direct") or "direct"),
                    research_query=str(tp_data.get("research_query", "") or ""),
                    skip_qa=bool(tp_data.get("skip_qa", False)),
                    effort=str(tp_data.get("effort", "high") or "high"),
                )
            )
        if task_plans:
            batches.append(Batch(tasks=task_plans))

    conflicts: list[dict[str, Any]] = []
    for conflict in conflicts_raw or []:
        if not isinstance(conflict, dict):
            continue
        keys = _conflict_task_keys(conflict)
        if len(keys) < 2:
            continue
        conflicts.append(
            {
                "tasks": keys,
                "description": str(conflict.get("description", "") or ""),
                "suggestion": str(conflict.get("suggestion", "") or ""),
            }
        )

    analysis: list[dict[str, Any]] = []
    for item in analysis_raw or []:
        if not isinstance(item, dict):
            continue
        task_a = str(item.get("task_a", "") or "").strip()
        task_b = str(item.get("task_b", "") or "").strip()
        relationship = _normalize_relationship(item.get("relationship"))
        if not task_a or not task_b or not relationship:
            continue
        analysis.append(
            {
                "task_a": task_a,
                "task_b": task_b,
                "relationship": relationship,
                "reason": str(item.get("reason", "") or ""),
            }
        )

    if not batches and not conflicts:
        return None

    learnings = data.get("learnings", [])
    if not isinstance(learnings, list):
        learnings = []

    return ExecutionPlan(
        batches=batches,
        learnings=[str(item) for item in learnings],
        conflicts=conflicts,
        analysis=analysis,
    )


def _task_graph(tasks: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[int, str], dict[str, list[str]]]:
    task_by_key: dict[str, dict[str, Any]] = {}
    id_to_key: dict[int, str] = {}
    dep_keys_by_key: dict[str, list[str]] = {}

    for task in tasks:
        key = str(task.get("key", "") or "").strip()
        if not key:
            continue
        task_by_key[key] = task
        if isinstance(task.get("id"), int):
            id_to_key[int(task["id"])] = key

    for key, task in task_by_key.items():
        deps = []
        for dep_id in task.get("depends_on") or []:
            dep_key = id_to_key.get(dep_id)
            if dep_key:
                deps.append(dep_key)
        dep_keys_by_key[key] = deps

    return task_by_key, id_to_key, dep_keys_by_key


def _topological_layers(tasks: list[dict[str, Any]]) -> list[list[str]]:
    task_by_key, _id_to_key, dep_keys_by_key = _task_graph(tasks)
    if not task_by_key:
        return []

    sorter = graphlib.TopologicalSorter()
    for key, dep_keys in dep_keys_by_key.items():
        sorter.add(key, *dep_keys)

    try:
        sorter.prepare()
    except graphlib.CycleError:
        return [[key] for key in task_by_key]

    layers: list[list[str]] = []
    while sorter.is_active():
        ready = list(sorter.get_ready())
        if not ready:
            break
        layers.append(ready)
        for key in ready:
            sorter.done(key)
    return layers


def default_plan(tasks: list[dict[str, Any]]) -> ExecutionPlan:
    """Create a dependency-respecting parallel plan."""
    return ExecutionPlan(
        batches=[Batch(tasks=[TaskPlan(task_key=key) for key in layer]) for layer in _topological_layers(tasks)],
    )


def serial_plan(tasks: list[dict[str, Any]]) -> ExecutionPlan:
    """Create a dependency-respecting serial plan."""
    batches: list[Batch] = []
    for layer in _topological_layers(tasks):
        for key in layer:
            batches.append(Batch(tasks=[TaskPlan(task_key=key)]))
    return ExecutionPlan(batches=batches)


def _serial_plan_from_remaining(remaining_plan: ExecutionPlan) -> ExecutionPlan:
    batches = [
        Batch(tasks=[TaskPlan(
            task_key=task_plan.task_key,
            strategy=task_plan.strategy,
            research_query=task_plan.research_query,
            skip_qa=task_plan.skip_qa,
            effort=task_plan.effort,
        )])
        for batch in remaining_plan.batches
        for task_plan in batch.tasks
    ]
    return ExecutionPlan(
        batches=batches,
        learnings=list(remaining_plan.learnings),
        conflicts=list(remaining_plan.conflicts),
        analysis=list(remaining_plan.analysis),
    )


def _task_summary(tasks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for task in tasks:
        key = str(task.get("key", "") or "")
        deps = task.get("depends_on") or []
        dep_str = f" depends_on=#{', #'.join(str(dep) for dep in deps)}" if deps else ""
        feedback = str(task.get("feedback", "") or "").strip()
        feedback_str = f"\n  feedback: {feedback[:400]}" if feedback else ""
        lines.append(
            f"- task_key={key} id=#{task.get('id', '?')}{dep_str}\n"
            f"  prompt: {str(task.get('prompt', '') or '')[:1200]}{feedback_str}"
        )
    return "\n".join(lines)


def _explicit_dependency_analysis(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _task_by_key, id_to_key, _dep_keys_by_key = _task_graph(tasks)
    analysis: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        task_key = str(task.get("key", "") or "")
        if not task_key:
            continue
        for dep_id in task.get("depends_on") or []:
            dep_key = id_to_key.get(dep_id)
            if not dep_key:
                continue
            pair = (dep_key, task_key)
            if pair in seen:
                continue
            seen.add(pair)
            analysis.append(
                {
                    "task_a": dep_key,
                    "task_b": task_key,
                    "relationship": "DEPENDENT",
                    "reason": "Explicit depends_on constraint.",
                }
            )
    return analysis


def _serialize_analysis_batches(
    batches: list[Batch],
    analysis: list[dict[str, Any]],
) -> list[Batch]:
    serialized_pairs = {
        frozenset((item["task_a"], item["task_b"]))
        for item in analysis
        if item.get("relationship") in {"DEPENDENT", "ADDITIVE", "UNCERTAIN"}
        and item.get("task_a") != item.get("task_b")
    }
    if not serialized_pairs:
        return batches

    serialized_batches: list[Batch] = []
    for batch_index, batch in enumerate(batches):
        while len(serialized_batches) <= batch_index:
            serialized_batches.append(Batch(tasks=[]))
        for task_plan in batch.tasks:
            target_batch = batch_index
            while True:
                while len(serialized_batches) <= target_batch:
                    serialized_batches.append(Batch(tasks=[]))
                existing_keys = {
                    existing.task_key
                    for existing in serialized_batches[target_batch].tasks
                }
                if any(
                    frozenset((task_plan.task_key, other_key)) in serialized_pairs
                    for other_key in existing_keys
                ):
                    target_batch += 1
                    continue
                serialized_batches[target_batch].tasks.append(task_plan)
                break

    return [batch for batch in serialized_batches if batch.tasks]


def _normalize_plan(plan: ExecutionPlan, tasks: list[dict[str, Any]]) -> ExecutionPlan:
    valid_keys = {str(task.get("key", "") or "") for task in tasks if task.get("key")}
    explicit_analysis = _explicit_dependency_analysis(tasks)

    analysis: list[dict[str, Any]] = []
    seen_analysis: set[tuple[str, str, str]] = set()
    for item in list(plan.analysis) + explicit_analysis:
        task_a = str(item.get("task_a", "") or "")
        task_b = str(item.get("task_b", "") or "")
        relationship = _normalize_relationship(item.get("relationship"))
        if task_a not in valid_keys or task_b not in valid_keys or not relationship:
            continue
        signature = (task_a, task_b, relationship)
        if signature in seen_analysis:
            continue
        seen_analysis.add(signature)
        analysis.append(
            {
                "task_a": task_a,
                "task_b": task_b,
                "relationship": relationship,
                "reason": str(item.get("reason", "") or ""),
            }
        )

    conflicts: list[dict[str, Any]] = []
    seen_conflicts: set[str] = set()
    conflict_sets: list[set[str]] = []
    for conflict in plan.conflicts:
        keys = [key for key in _conflict_task_keys(conflict) if key in valid_keys]
        if len(keys) < 2:
            continue
        signature = "|".join(sorted(keys))
        if signature in seen_conflicts:
            continue
        seen_conflicts.add(signature)
        conflict_sets.append(set(keys))
        conflicts.append(
            {
                "tasks": keys,
                "description": str(conflict.get("description", "") or ""),
                "suggestion": str(conflict.get("suggestion", "") or ""),
            }
        )

    for item in analysis:
        if item.get("relationship") != "CONTRADICTORY":
            continue
        pair = {item["task_a"], item["task_b"]}
        if len(pair) < 2 or any(pair.issubset(existing) for existing in conflict_sets):
            continue
        ordered_pair = sorted(pair)
        signature = "|".join(ordered_pair)
        if signature in seen_conflicts:
            continue
        seen_conflicts.add(signature)
        conflict_sets.append(pair)
        conflicts.append(
            {
                "tasks": ordered_pair,
                "description": str(item.get("reason", "") or "Planner marked these tasks as contradictory."),
                "suggestion": "Do not run these tasks in the same batch.",
            }
        )

    conflict_keys = {
        key
        for conflict in conflicts
        for key in conflict.get("tasks", [])
    }

    seed_batches: list[Batch] = []
    seen_planned: set[str] = set()
    for batch in plan.batches:
        normalized_tasks: list[TaskPlan] = []
        for task_plan in batch.tasks:
            if task_plan.task_key not in valid_keys:
                continue
            if task_plan.task_key in conflict_keys or task_plan.task_key in seen_planned:
                continue
            seen_planned.add(task_plan.task_key)
            normalized_tasks.append(task_plan)
        if normalized_tasks:
            seed_batches.append(Batch(tasks=normalized_tasks))

    batches = _serialize_analysis_batches(seed_batches, analysis)

    return ExecutionPlan(
        batches=batches,
        learnings=list(plan.learnings),
        conflicts=conflicts,
        analysis=analysis,
    )


def _parse_shortlist_json(raw: str) -> list[dict[str, str]] | None:
    try:
        data = json.loads(_strip_json_wrapper(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return None
    parsed: list[dict[str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        task_a = str(item.get("task_a", "") or "").strip()
        task_b = str(item.get("task_b", "") or "").strip()
        if not task_a or not task_b or task_a == task_b:
            continue
        parsed.append(
            {
                "task_a": task_a,
                "task_b": task_b,
                "reason": str(item.get("reason", "") or ""),
            }
        )
    return parsed


def _shortlist_signature(item: dict[str, str]) -> tuple[str, str]:
    task_a = item["task_a"]
    task_b = item["task_b"]
    return tuple(sorted((task_a, task_b)))


def _project_context(project_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return "(git ls-files unavailable)"

    from otto.git_ops import _is_otto_owned
    files = [line.strip() for line in result.stdout.splitlines()
             if line.strip() and not _is_otto_owned(line.strip())]
    if not files:
        return "(no tracked files)"
    limited = files[:200]
    suffix = "\n... (truncated)" if len(files) > len(limited) else ""
    return "\n".join(f"- {path}" for path in limited) + suffix


def _planner_settings(config: dict[str, Any]) -> list[str]:
    return str(config.get("planner_agent_settings", "project") or "project").split(",")


def _planner_effort(config: dict[str, Any], default: str = "medium") -> str:
    return str(config.get("planner_effort", default) or default)


def _planner_model(config: dict[str, Any]) -> str | None:
    model = config.get("planner_model")
    return str(model) if model else None


async def _run_planner_prompt(
    prompt: str,
    config: dict[str, Any],
    project_dir: Path,
    *,
    model: str | None,
    effort: str,
) -> str:
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_planner_settings(config),
        env=_subprocess_env(),
        effort=effort,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    if model:
        options.model = model
    raw_output, _cost, _result = await run_agent_query(prompt, options)
    return raw_output


async def _shortlist_pairs(
    tasks: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
) -> list[dict[str, str]]:
    prompt = f"""You are triaging coding task relationships.

Review ALL tasks below and return ONLY suspicious task pairs that need deeper review.
Suspicious means any likely overlap, dependency, contradiction, or uncertainty.
Do not include obviously independent pairs.

Return JSON only:
{{
  "candidates": [
    {{"task_a": "task_key_a", "task_b": "task_key_b", "reason": "brief reason"}}
  ]
}}

Tasks:
{_task_summary(tasks)}
"""
    raw_output = await _run_planner_prompt(
        prompt,
        config,
        project_dir,
        model="haiku",
        effort="low",
    )
    candidates = _parse_shortlist_json(raw_output)
    if candidates is None:
        raise ValueError("shortlist parse failed")

    valid_keys = {str(task.get("key", "") or "") for task in tasks if task.get("key")}
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        if item["task_a"] not in valid_keys or item["task_b"] not in valid_keys:
            continue
        signature = _shortlist_signature(item)
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append(item)
    return normalized


async def plan(
    tasks: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
) -> ExecutionPlan:
    """Create an execution plan using smart relationship analysis."""
    if not tasks:
        return ExecutionPlan()

    if len(tasks) == 1:
        return ExecutionPlan(batches=[Batch(tasks=[TaskPlan(task_key=str(tasks[0]["key"]))])])

    try:
        shortlist = await _shortlist_pairs(tasks, config, project_dir)
        has_explicit_deps = any(task.get("depends_on") for task in tasks)
        if not shortlist:
            if has_explicit_deps:
                return _normalize_plan(default_plan(tasks), tasks)
            return _normalize_plan(default_plan(tasks), tasks)

        candidate_lines = "\n".join(
            f"- {item['task_a']} <-> {item['task_b']}: {item['reason']}"
            for item in shortlist
        )
        prompt = f"""You are planning execution for an autonomous coding pipeline.

Classify the shortlisted task pairs and build an execution plan.

Relationship labels:
- INDEPENDENT: different files/components, parallel OK
- ADDITIVE: same file, different functions/sections, serialize (same-file parallel causes merge conflicts)
- DEPENDENT: task B needs task A output, serialize
- CONTRADICTORY: incompatible edits to the same thing, flag and exclude from execution
- UNCERTAIN: not enough information, serialize conservatively

Rules:
- Respect explicit depends_on constraints.
- Exclude contradictory tasks from batches and report them in conflicts.
- ADDITIVE and UNCERTAIN pairs should not run in the same batch.
- Only truly INDEPENDENT tasks (different files) should run in parallel.

Return JSON only:
{{
  "analysis": [
    {{"task_a": "a", "task_b": "b", "relationship": "INDEPENDENT|ADDITIVE|DEPENDENT|CONTRADICTORY|UNCERTAIN", "reason": "brief reason"}}
  ],
  "conflicts": [
    {{"tasks": ["a", "b"], "description": "what conflicts", "suggestion": "how to resolve"}}
  ],
  "batches": [
    {{"tasks": [{{"task_key": "a", "strategy": "direct", "effort": "high"}}]}}
  ],
  "learnings": []
}}

Tasks:
{_task_summary(tasks)}

Shortlisted pairs for detailed review:
{candidate_lines}

Project context (git ls-files, capped at 200):
{_project_context(project_dir)}
"""
        raw_output = await _run_planner_prompt(
            prompt,
            config,
            project_dir,
            model=_planner_model(config),
            effort=_planner_effort(config),
        )
        result = parse_plan_json(raw_output)
        if result is None:
            raise ValueError("planner parse failed")
        return _normalize_plan(result, tasks)
    except Exception as exc:
        logger.warning("Planner failed; falling back to serial plan: %s", exc)
        return serial_plan(tasks)


async def replan(
    context: Any,
    remaining_plan: ExecutionPlan,
    config: dict[str, Any],
    project_dir: Path,
) -> ExecutionPlan:
    """Replan remaining batches while preserving conflict metadata."""
    if remaining_plan.is_empty:
        return remaining_plan

    results_summary: list[str] = []
    for key, result in context.results.items():
        if result.success:
            results_summary.append(f"- {key}: PASSED — {result.diff_summary or 'no diff summary'}")
        else:
            parts = [f"- {key}: FAILED"]
            if result.error:
                parts.append(f"  error: {result.error}")
            if result.qa_report:
                parts.append(f"  qa: {result.qa_report[:400]}")
            results_summary.append("\n".join(parts))

    learnings_str = "\n".join(f"- [{item.source}] {item.text}" for item in context.learnings) if context.learnings else "None"
    remaining_tasks = [
        f"- key={task_plan.task_key} strategy={task_plan.strategy}"
        for batch in remaining_plan.batches
        for task_plan in batch.tasks
    ]

    prompt = f"""You are replanning remaining autonomous coding tasks.

Keep the task set exactly the same. Adjust batch order or strategies only if the completed results require it.
Return JSON only with batches/learnings. Preserve any existing conflict handling outside this response.

Completed results:
{chr(10).join(results_summary) or "None"}

Learnings:
{learnings_str}

Remaining tasks:
{chr(10).join(remaining_tasks)}
"""
    try:
        raw_output = await _run_planner_prompt(
            prompt,
            config,
            project_dir,
            model=_planner_model(config),
            effort=_planner_effort(config),
        )
        replanned = parse_plan_json(raw_output)
        if replanned is None or replanned.is_empty:
            raise ValueError("replan parse failed")
        return ExecutionPlan(
            batches=replanned.batches,
            learnings=replanned.learnings,
            conflicts=list(remaining_plan.conflicts),
            analysis=list(remaining_plan.analysis),
        )
    except Exception as exc:
        logger.warning("Replan failed; falling back to serial remaining plan: %s", exc)
        return _serial_plan_from_remaining(remaining_plan)
