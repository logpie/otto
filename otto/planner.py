"""Otto planner — execution plan dataclasses and smart relationship analysis."""

from __future__ import annotations

import graphlib
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
from otto.config import planner_provider
from otto.observability import append_text_log

logger = logging.getLogger("otto.planner")

RELATIONSHIP_VALUES = {
    "INDEPENDENT",
    "ADDITIVE",
    "DEPENDENT",
    "CONTRADICTORY",
    "UNCERTAIN",
}


def _planner_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "planner.log", lines)


def _format_batches(plan: ExecutionPlan) -> list[str]:
    if not plan.batches:
        return ["- (none)"]
    lines: list[str] = []
    for idx, batch in enumerate(plan.batches, start=1):
        unit_entries = []
        for unit in batch.units:
            entries = [
                f"{task.task_key}[strategy={task.strategy}, effort={task.effort}]"
                for task in unit.tasks
            ]
            wrapped = ", ".join(entries)
            if unit.is_integrated:
                unit_entries.append(f"{{{wrapped}}}")
            else:
                unit_entries.append(wrapped)
        lines.append(f"- batch {idx}: {' | '.join(unit_entries)}")
    return lines


def _format_shortlist(shortlist: list[dict[str, str]]) -> list[str]:
    if not shortlist:
        return ["- (none)"]
    return [
        f"- {item['task_a']} <-> {item['task_b']}: {item['reason']}"
        for item in shortlist
    ]


def _format_analysis(analysis: list[dict[str, Any]]) -> list[str]:
    if not analysis:
        return ["- (none)"]
    return [
        f"- {item.get('task_a')} <-> {item.get('task_b')}: "
        f"{item.get('relationship')} ({str(item.get('reason', '') or '')[:160]})"
        for item in analysis
    ]


def _format_conflicts(conflicts: list[dict[str, Any]]) -> list[str]:
    if not conflicts:
        return ["- (none)"]
    return [
        f"- {', '.join(str(key) for key in conflict.get('tasks') or [])}: "
        f"{str(conflict.get('description', '') or '')[:160]}"
        for conflict in conflicts
    ]


@dataclass
class TaskPlan:
    """Plan for a single task within a batch."""

    task_key: str
    strategy: str = "direct"
    research_query: str = ""
    skip_qa: bool = False
    effort: str = "high"


@dataclass
class BatchUnit:
    """Execution unit inside a batch.

    A unit with one task preserves current behavior.
    A unit with multiple tasks is intended for integrated execution.
    """

    tasks: list[TaskPlan] = field(default_factory=list)

    @property
    def task_keys(self) -> list[str]:
        return [task.task_key for task in self.tasks]

    @property
    def is_integrated(self) -> bool:
        return len(self.tasks) > 1


@dataclass
class Batch:
    """A group of execution units that can advance in the same stage."""

    tasks: list[TaskPlan] = field(default_factory=list)
    units: list[BatchUnit] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.units and not self.tasks:
            self.tasks = [task for unit in self.units for task in unit.tasks]
        elif self.tasks and not self.units:
            self.units = [BatchUnit(tasks=[task]) for task in self.tasks]
        elif self.tasks and self.units:
            unit_tasks = [task for unit in self.units for task in unit.tasks]
            if [task.task_key for task in unit_tasks] != [task.task_key for task in self.tasks]:
                self.tasks = unit_tasks

    @classmethod
    def from_tasks(cls, tasks: list[TaskPlan]) -> "Batch":
        return cls(tasks=list(tasks), units=[BatchUnit(tasks=[task]) for task in tasks])


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
            remaining_units: list[BatchUnit] = []
            for unit in batch.units:
                remaining = [tp for tp in unit.tasks if tp.task_key not in completed_keys]
                if remaining:
                    remaining_units.append(BatchUnit(tasks=remaining))
                    unresolved_keys.update(tp.task_key for tp in remaining)
            if remaining_units:
                remaining_batches.append(Batch(units=remaining_units))

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
        parsed_units: list[BatchUnit] = []
        units_raw = batch_data.get("units")
        if isinstance(units_raw, list):
            for unit_data in units_raw:
                if not isinstance(unit_data, dict):
                    continue
                task_keys_raw = unit_data.get("task_keys", [])
                if not isinstance(task_keys_raw, list):
                    continue
                unit_tasks: list[TaskPlan] = []
                for task_key_raw in task_keys_raw:
                    task_key = str(task_key_raw or "").strip()
                    if not task_key:
                        continue
                    unit_tasks.append(TaskPlan(task_key=task_key))
                if unit_tasks:
                    parsed_units.append(BatchUnit(tasks=unit_tasks))
        else:
            # Backward compatibility: old planner schema with batch.tasks.
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
                parsed_units = [BatchUnit(tasks=[task_plan]) for task_plan in task_plans]
        if parsed_units:
            batches.append(Batch(units=parsed_units))

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
        batches=[Batch.from_tasks([TaskPlan(task_key=key) for key in layer]) for layer in _topological_layers(tasks)],
    )


def serial_plan(tasks: list[dict[str, Any]]) -> ExecutionPlan:
    """Create a dependency-respecting serial plan."""
    batches: list[Batch] = []
    for layer in _topological_layers(tasks):
        for key in layer:
            batches.append(Batch.from_tasks([TaskPlan(task_key=key)]))
    return ExecutionPlan(batches=batches)


def _serial_plan_from_remaining(remaining_plan: ExecutionPlan) -> ExecutionPlan:
    batches = [
        Batch.from_tasks([TaskPlan(
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
    task_plans_by_key: dict[str, TaskPlan] = {}
    original_batch_by_key: dict[str, int] = {}
    original_order_by_key: dict[str, tuple[int, int]] = {}
    for batch_index, batch in enumerate(batches):
        for task_index, task_plan in enumerate(batch.tasks):
            task_plans_by_key[task_plan.task_key] = task_plan
            original_batch_by_key[task_plan.task_key] = batch_index
            original_order_by_key[task_plan.task_key] = (batch_index, task_index)

    # Only UNCERTAIN forces serialization. ADDITIVE (same file, different
    # functions) can parallelize — git merge handles non-overlapping changes,
    # and the merge conflict resolver handles the rest.
    serialized_pairs = {
        frozenset((item["task_a"], item["task_b"]))
        for item in analysis
        if item.get("relationship") in {"UNCERTAIN"}
        and item.get("task_a") in task_plans_by_key
        and item.get("task_b") in task_plans_by_key
        and item.get("task_a") != item.get("task_b")
    }
    dependency_predecessors: dict[str, set[str]] = {
        task_key: set() for task_key in task_plans_by_key
    }
    for item in analysis:
        if item.get("relationship") != "DEPENDENT":
            continue
        task_a = str(item.get("task_a", "") or "")
        task_b = str(item.get("task_b", "") or "")
        if (
            task_a not in task_plans_by_key
            or task_b not in task_plans_by_key
            or task_a == task_b
        ):
            continue
        dependency_predecessors[task_b].add(task_a)

    if not serialized_pairs and not any(dependency_predecessors.values()):
        return batches

    serialized_task_lists: list[list[TaskPlan]] = []
    task_batch_by_key: dict[str, int] = {}
    pending_keys = sorted(task_plans_by_key, key=original_order_by_key.__getitem__)
    while pending_keys:
        next_pending: list[str] = []
        progressed = False
        for task_key in pending_keys:
            predecessors = dependency_predecessors.get(task_key, set())
            if any(pred not in task_batch_by_key for pred in predecessors):
                next_pending.append(task_key)
                continue

            task_plan = task_plans_by_key[task_key]
            target_batch = original_batch_by_key[task_key]
            if predecessors:
                target_batch = max(
                    target_batch,
                    max(task_batch_by_key[pred] + 1 for pred in predecessors),
                )
            while True:
                while len(serialized_task_lists) <= target_batch:
                    serialized_task_lists.append([])
                existing_keys = {
                    existing.task_key
                    for existing in serialized_task_lists[target_batch]
                }
                if any(
                    frozenset((task_key, other_key)) in serialized_pairs
                    for other_key in existing_keys
                ):
                    target_batch += 1
                    continue
                serialized_task_lists[target_batch].append(task_plan)
                task_batch_by_key[task_key] = target_batch
                progressed = True
                break

        if progressed:
            pending_keys = next_pending
            continue

        # Dependency cycles are invalid, but keep execution deterministic and conservative.
        for task_key in next_pending:
            task_plan = task_plans_by_key[task_key]
            serialized_task_lists.append([task_plan])
            task_batch_by_key[task_key] = len(serialized_task_lists) - 1
        break

    return [Batch.from_tasks(tasks) for tasks in serialized_task_lists if tasks]


def _serialize_analysis_units(
    batches: list[Batch],
    analysis: list[dict[str, Any]],
) -> list[Batch]:
    unit_entries: list[tuple[int, int, BatchUnit]] = []
    task_to_unit: dict[str, int] = {}

    for batch_index, batch in enumerate(batches):
        for unit_index, unit in enumerate(batch.units):
            global_index = len(unit_entries)
            unit_entries.append((batch_index, unit_index, unit))
            for task in unit.tasks:
                task_to_unit[task.task_key] = global_index

    if not unit_entries:
        return []

    invalid_units: set[int] = set()
    dependency_predecessors: dict[int, set[int]] = {i: set() for i in range(len(unit_entries))}
    uncertain_pairs: set[frozenset[int]] = set()

    for item in analysis:
        task_a = str(item.get("task_a", "") or "")
        task_b = str(item.get("task_b", "") or "")
        relationship = item.get("relationship")
        if task_a not in task_to_unit or task_b not in task_to_unit:
            continue
        unit_a = task_to_unit[task_a]
        unit_b = task_to_unit[task_b]
        if relationship == "DEPENDENT":
            if unit_a != unit_b:
                dependency_predecessors[unit_b].add(unit_a)
        elif relationship == "UNCERTAIN":
            if unit_a == unit_b:
                invalid_units.add(unit_a)
            else:
                uncertain_pairs.add(frozenset((unit_a, unit_b)))
        elif relationship == "CONTRADICTORY" and unit_a == unit_b:
            invalid_units.add(unit_a)

    normalized_units: list[tuple[int, int, BatchUnit]] = []
    for idx, (batch_index, unit_index, unit) in enumerate(unit_entries):
        if idx in invalid_units:
            for offset, task in enumerate(unit.tasks):
                normalized_units.append((batch_index, unit_index + offset, BatchUnit(tasks=[task])))
        else:
            normalized_units.append((batch_index, unit_index, unit))

    serialized_batches: list[list[BatchUnit]] = []
    unit_batch_by_index: dict[int, int] = {}
    pending_indices = list(range(len(normalized_units)))

    while pending_indices:
        next_pending: list[int] = []
        progressed = False
        for idx in pending_indices:
            original_batch, _unit_order, unit = normalized_units[idx]
            predecessors = dependency_predecessors.get(idx, set())
            if any(pred not in unit_batch_by_index for pred in predecessors):
                next_pending.append(idx)
                continue

            target_batch = original_batch
            if predecessors:
                target_batch = max(target_batch, max(unit_batch_by_index[pred] + 1 for pred in predecessors))

            while True:
                while len(serialized_batches) <= target_batch:
                    serialized_batches.append([])
                existing_indices = [
                    other_idx
                    for other_idx, batch_idx in unit_batch_by_index.items()
                    if batch_idx == target_batch
                ]
                if any(frozenset((idx, other_idx)) in uncertain_pairs for other_idx in existing_indices):
                    target_batch += 1
                    continue
                serialized_batches[target_batch].append(unit)
                unit_batch_by_index[idx] = target_batch
                progressed = True
                break

        if progressed:
            pending_indices = next_pending
            continue

        for idx in next_pending:
            _batch_index, _unit_order, unit = normalized_units[idx]
            serialized_batches.append([unit])
            unit_batch_by_index[idx] = len(serialized_batches) - 1
        break

    return [Batch(units=units) for units in serialized_batches if units]


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

    # Build batches — do NOT exclude conflict tasks. They should still
    # be scheduled (in separate batches). Conflicts are for reporting,
    # not for dropping tasks.
    seed_batches: list[Batch] = []
    seen_planned: set[str] = set()
    for batch in plan.batches:
        normalized_units: list[BatchUnit] = []
        for unit in batch.units:
            normalized_tasks: list[TaskPlan] = []
            for task_plan in unit.tasks:
                if task_plan.task_key not in valid_keys:
                    continue
                if task_plan.task_key in seen_planned:
                    continue
                seen_planned.add(task_plan.task_key)
                normalized_tasks.append(task_plan)
            if normalized_tasks:
                normalized_units.append(BatchUnit(tasks=normalized_tasks))
        if normalized_units:
            seed_batches.append(Batch(units=normalized_units))

    # Add any tasks missing from the plan (planner dropped them)
    missing_keys = valid_keys - seen_planned
    if missing_keys:
        for key in sorted(missing_keys):
            seed_batches.append(Batch.from_tasks([TaskPlan(task_key=key)]))
        logger.warning("Planner dropped %d task(s): %s — added as serial batches", len(missing_keys), ", ".join(sorted(missing_keys)))

    # Planner owns unit grouping decisions. Post-processing may serialize units
    # into later batches to satisfy dependency/uncertainty constraints, but it
    # must not rewrite the unit boundaries themselves.
    batches = _serialize_analysis_units(seed_batches, analysis)

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
) -> tuple[str, float, float]:
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_planner_settings(config),
        env=_subprocess_env(project_dir),
        effort=effort,
        system_prompt={"type": "preset", "preset": "claude_code"},
        provider=planner_provider(config),
    )
    if model:
        options.model = model
    started_at = time.monotonic()
    raw_output, cost, _result = await run_agent_query(prompt, options)
    return raw_output, float(cost or 0.0), round(time.monotonic() - started_at, 1)


async def _shortlist_pairs(
    tasks: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
) -> list[dict[str, str]]:
    task_summary = _task_summary(tasks)
    _planner_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] shortlist request",
        "task summary sent to LLM:",
        task_summary or "- (none)",
    )
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
    raw_output, cost_usd, duration_s = await _run_planner_prompt(
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
    _planner_log(
        project_dir,
        f"shortlist LLM call: model=haiku effort=low time_s={duration_s:.1f} cost_usd={cost_usd:.4f}",
        "shortlist results:",
        *_format_shortlist(normalized),
        "",
    )
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
        result = ExecutionPlan(batches=[Batch(tasks=[TaskPlan(task_key=str(tasks[0]["key"]))])])
        _planner_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] planner shortcut",
            "task summary sent to LLM:",
            _task_summary(tasks),
            "fallback trigger: single task; skipped planner LLM",
            "final batch structure:",
            *_format_batches(result),
            "",
        )
        return result

    try:
        _planner_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] planner request",
            "task summary sent to LLM:",
            _task_summary(tasks),
        )
        # Single LLM call for both analysis + batching.
        # The old two-phase approach (Haiku shortlist → Sonnet classify) was fragile:
        # Haiku missed pairs → tasks silently dropped from batches.
        # One strong call is more reliable. Cost difference: ~$0.01-0.02.
        prompt = f"""You are planning execution for an autonomous coding pipeline.

Analyze ALL task pairs for relationships and build an execution plan.
CRITICAL: Every input task MUST appear in exactly one batch. Do NOT drop any tasks.

Relationship labels:
- INDEPENDENT: different files/components, parallel OK
- ADDITIVE: same file but different functions/sections — parallel OK (git merge handles non-overlapping changes; merge conflict resolver handles the rest). If tasks modify the SAME function/section, use UNCERTAIN instead.
- DEPENDENT: task B needs task A output, serialize (later batch)
- CONTRADICTORY: incompatible goals for the same code — flag in conflicts BUT STILL schedule both in separate batches
- UNCERTAIN: not enough information, serialize conservatively

Rules:
- Respect explicit depends_on constraints.
- INDEPENDENT and ADDITIVE tasks can run in parallel within a batch.
- DEPENDENT tasks can be handled in either of two ways:
  1. separate units with later checkpoint(s)
  2. the same integrated unit in one batch, but only when they are tightly layered parts of one coherent feature slice
- CONTRADICTORY tasks go in conflicts AND in separate units/batches.
- UNCERTAIN pairs should not run in the same unit or the same batch.
- EVERY input task key MUST appear in exactly one batch and exactly one unit.
- A batch contains one or more execution units.
- A singleton unit like {{"task_keys": ["a"]}} means task `a` runs normally on its own.
- A multi-task unit like {{"task_keys": ["a", "b"]}} means tasks `a` and `b` should be executed together as one integrated coding pass because they form one coherent feature slice.
- Single-batch execution is valid when the tasks together are one coherent feature slice and there is little value in an earlier integration checkpoint.
- Multiple batches are valid when later work benefits from an earlier merged checkpoint, or when retry/rollback should stay narrower than the whole feature.
- Use integrated units for tightly layered tasks when preserving whole-system context is likely more valuable than staged checkpoints.
- Before grouping 3 or more tasks into one integrated unit, ask whether a partial failure would make retry too coarse. If retrying the whole slice would likely be wasteful, prefer smaller units or an earlier checkpoint.
- A long dependency chain is NOT automatically one unit. Only keep 3+ tasks together when they are truly one inseparable feature slice that should usually be implemented and retried as a whole.
- Do not treat all DEPENDENT tasks as separate by default, but do not collapse them all into one unit by default either.
- Do NOT over-group. Keep unrelated, contradictory, or loosely related tasks in separate singleton units.

Return JSON only:
{{
  "analysis": [
    {{"task_a": "a", "task_b": "b", "relationship": "INDEPENDENT|ADDITIVE|DEPENDENT|CONTRADICTORY|UNCERTAIN", "reason": "brief reason"}}
  ],
  "conflicts": [
    {{"tasks": ["a", "b"], "description": "what conflicts", "suggestion": "how to resolve"}}
  ],
  "batches": [
    {{"units": [{{"task_keys": ["a"]}}, {{"task_keys": ["b", "c"]}}]}}
  ],
  "learnings": []
}}

Tasks:
{_task_summary(tasks)}

Project context (git ls-files, capped at 200):
{_project_context(project_dir)}

Examples:
- data layer + service layer + CLI for one small feature → usually one integrated unit
- unrelated bug fixes in different files → separate singleton units
- conflicting same-file rewrites with different goals → separate singleton units
"""
        raw_output, cost_usd, duration_s = await _run_planner_prompt(
            prompt,
            config,
            project_dir,
            model=_planner_model(config),
            effort=_planner_effort(config, default="high"),
        )
        result = parse_plan_json(raw_output)
        if result is None:
            raise ValueError(
                f"planner parse failed: raw output ({len(raw_output)} chars): "
                f"{raw_output[:300]!r}"
            )
        normalized = _normalize_plan(result, tasks)
        _planner_log(
            project_dir,
            (
                "planner LLM call: "
                f"model={_planner_model(config) or 'default'} "
                f"effort={_planner_effort(config, default='high')} "
                f"time_s={duration_s:.1f} cost_usd={cost_usd:.4f}"
            ),
            "relationship analysis:",
            *_format_analysis(normalized.analysis),
            "conflicts found:",
            *_format_conflicts(normalized.conflicts),
            "final batch structure:",
            *_format_batches(normalized),
            "",
        )
        return normalized
    except Exception as exc:
        logger.warning("Planner failed; falling back to serial plan: %s", exc)
        fallback = serial_plan(tasks)
        _planner_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] planner fallback",
            f"fallback trigger: planner failed ({exc})",
            f"error detail: {exc!r}",
            "final batch structure:",
            *_format_batches(fallback),
            "",
        )
        return fallback


async def replan(
    context: Any,
    remaining_plan: ExecutionPlan,
    config: dict[str, Any],
    project_dir: Path,
    *,
    failed_keys: set[str] | None = None,
    rolled_back_keys: set[str] | None = None,
    pending_by_key: dict[str, Any] | None = None,
) -> ExecutionPlan:
    """Replan remaining batches using dependency analysis and failure context."""
    if remaining_plan.is_empty:
        return remaining_plan

    failed_keys = failed_keys or set()
    rolled_back_keys = rolled_back_keys or set()
    pending_by_key = pending_by_key or {}

    # Completed task summaries
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

    # Remaining tasks with prompts for context
    remaining_tasks: list[str] = []
    for batch in remaining_plan.batches:
        for task_plan in batch.tasks:
            key = task_plan.task_key
            task = pending_by_key.get(key, {})
            prompt_text = task.get("prompt", "(unknown)")
            status = "ROLLED_BACK" if key in rolled_back_keys else "PENDING"
            remaining_tasks.append(f"- key={key} status={status} prompt={prompt_text!r}")

    # Original dependency analysis (from smart planner)
    analysis_lines: list[str] = []
    for item in remaining_plan.analysis:
        analysis_lines.append(
            f"- {item.get('task_a')} ↔ {item.get('task_b')}: "
            f"{item.get('relationship', 'UNKNOWN')} — {item.get('reason', '')}"
        )

    # Dependencies on failed tasks
    failed_deps: list[str] = []
    if failed_keys:
        for item in context.results.get("_all_analysis", []) if hasattr(context, "_all_analysis") else []:
            pass  # handled below
        # Check original plan analysis for deps on failed tasks
        for item in (remaining_plan.analysis or []):
            a, b = item.get("task_a", ""), item.get("task_b", "")
            rel = item.get("relationship", "")
            if rel == "DEPENDENT":
                if a in failed_keys:
                    failed_deps.append(f"- {b} depends on {a} (FAILED)")
                elif b in failed_keys:
                    failed_deps.append(f"- {a} depends on {b} (FAILED)")

    learnings_str = "\n".join(f"- [{item.source}] {item.text}" for item in context.learnings) if context.learnings else "None"

    prompt = f"""You are replanning remaining autonomous coding tasks after batch failures.

CONTEXT:
- Some tasks FAILED permanently (QA rejected after retries). Their changes are NOT on main.
- Some tasks were ROLLED_BACK (innocent bystanders — they passed coding+tests but were in the same batch as a failed task). They need to be re-run.
- Remaining tasks from later batches are PENDING (not yet started).

FAILED TASKS (changes NOT on main):
{chr(10).join(f"- {k}" for k in sorted(failed_keys)) or "None"}

DEPENDENCY ANALYSIS (from original plan):
{chr(10).join(analysis_lines) or "No pairwise analysis available"}

DEPENDENCIES ON FAILED TASKS:
{chr(10).join(failed_deps) or "None — no remaining tasks depend on failed tasks"}

RULES:
1. EVERY remaining task key MUST appear in exactly one batch (never drop tasks).
2. Tasks that DEPEND on a failed task should be placed in a late batch with a note — they may fail but should still attempt.
3. ROLLED_BACK tasks are safe to run — they passed before and just need re-execution.
4. INDEPENDENT and ADDITIVE tasks can run in parallel within a batch.
5. UNCERTAIN pairs should not run in the same batch.
6. Keep batch count minimal — don't over-fragment.

COMPLETED RESULTS:
{chr(10).join(results_summary) or "None"}

LEARNINGS:
{learnings_str}

REMAINING TASKS:
{chr(10).join(remaining_tasks)}

Return JSON only:
{{
  "batches": [
    {{"units": [{{"task_keys": ["key"]}}]}}
  ],
  "learnings": ["any new learnings from the failure pattern"]
}}
"""
    try:
        _planner_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] replan request",
            "completed results summary:",
            *(results_summary or ["- None"]),
            "remaining tasks:",
            *(remaining_tasks or ["- None"]),
            f"existing batch structure: {'; '.join(_format_batches(remaining_plan))}",
        )
        raw_output, cost_usd, duration_s = await _run_planner_prompt(
            prompt,
            config,
            project_dir,
            model=_planner_model(config),
            effort=_planner_effort(config),
        )
        replanned = parse_plan_json(raw_output)
        if replanned is None or replanned.is_empty:
            reason = "empty plan" if (replanned is not None and replanned.is_empty) else "parse returned None"
            raise ValueError(
                f"replan parse failed ({reason}): raw output ({len(raw_output)} chars): "
                f"{raw_output[:300]!r}"
            )
        # Use full task dicts (with id, depends_on) so _normalize_plan can
        # enforce explicit dependency constraints deterministically.
        normalize_input = [
            pending_by_key.get(task_plan.task_key, {"key": task_plan.task_key})
            for batch in remaining_plan.batches
            for task_plan in batch.tasks
        ]
        normalized = _normalize_plan(
            ExecutionPlan(
                batches=replanned.batches,
                learnings=replanned.learnings,
                conflicts=list(remaining_plan.conflicts),
                analysis=list(remaining_plan.analysis),
            ),
            normalize_input,
        )
        _planner_log(
            project_dir,
            (
                "replan LLM call: "
                f"model={_planner_model(config) or 'default'} "
                f"effort={_planner_effort(config)} "
                f"time_s={duration_s:.1f} cost_usd={cost_usd:.4f}"
            ),
            "final batch structure:",
            *_format_batches(normalized),
            "",
        )
        return normalized
    except Exception as exc:
        logger.warning("Replan failed; falling back to serial remaining plan: %s", exc)
        fallback = _serial_plan_from_remaining(remaining_plan)
        _planner_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] replan fallback",
            f"replan LLM call: model={_planner_model(config) or 'default'} effort={_planner_effort(config)}"
            if 'cost_usd' in locals() else "replan LLM call: not completed",
            (
                f"replan timing: {duration_s:.1f}s cost_usd={cost_usd:.4f}"
                if 'duration_s' in locals() and 'cost_usd' in locals()
                else ""
            ),
            f"fallback trigger: replan failed ({exc})",
            f"error detail: {exc!r}",
            "final batch structure:",
            *_format_batches(fallback),
            "",
        )
        return fallback
