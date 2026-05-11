"""Deterministic pre-flight checks on the v5 task graph.

Catches structural bugs in decomposition before children dispatch:
- Architect sub-decomposed (should be inline)
- CHARTER.md missing after architect-pass
- DAG cycles in depends_on
- Duplicate task IDs

Phase 1 (this file): cheap deterministic checks. Returns issues; the
caller decides whether to log, fix, or block dispatch. Semantic checks
(path overlap, contract gaps) belong to a Phase 2 LLM reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


Severity = Literal["warn", "error", "block"]


@dataclass
class PreflightIssue:
    """A single issue found by pre-flight.

    Attributes:
        kind: machine-readable issue identifier.
        severity: warn (log), error (log + emit event), block (refuse dispatch).
        message: human-readable description.
        task_id: the task this issue is about, if applicable.
    """

    kind: str
    severity: Severity
    message: str
    task_id: str | None = None


def _is_architect(task: dict[str, Any]) -> bool:
    """Heuristic: an architect task has empty depends_on and an intent
    that starts with 'Architect' or contains 'CHARTER' as a deliverable.

    The architect-first prompt convention names this role explicitly.
    """
    if task.get("depends_on"):
        return False
    intent = (task.get("intent") or "").lstrip()
    return intent.startswith("Architect") or intent.startswith("architect")


def _detect_architect_sub_decomp(graph: dict[str, Any]) -> list[PreflightIssue]:
    """Architect must run inline; if it called submit_subtask, flag it.

    The whiteboard regression: architect emitted 3 grandchildren that
    wrote scaffolds to the same dirs the root's parallel feature
    siblings later wrote to. Unrecoverable merge_blocked.
    """
    issues: list[PreflightIssue] = []
    tasks = graph.get("tasks") or {}
    for tid, task in tasks.items():
        if not _is_architect(task):
            continue
        if task.get("decomposition") == "emit":
            issues.append(
                PreflightIssue(
                    kind="architect_sub_decomposed",
                    severity="block",
                    message=(
                        f"Architect task {tid} called submit_subtask; "
                        f"must call begin_inline. Grandchildren will "
                        f"conflict with parallel feature siblings."
                    ),
                    task_id=tid,
                )
            )
    return issues


def _detect_missing_charter(
    graph: dict[str, Any], project_dir: Path
) -> list[PreflightIssue]:
    """If architect completed with verdict=pass but CHARTER.md is
    missing at the repo root, the architect didn't deliver.

    Decomp3 regression: architect was skipped or didn't write CHARTER;
    integration session had to patch 3 contract bugs to recover.
    """
    issues: list[PreflightIssue] = []
    charter = project_dir / "CHARTER.md"
    if charter.exists():
        return issues
    tasks = graph.get("tasks") or {}
    for tid, task in tasks.items():
        if not _is_architect(task):
            continue
        if task.get("verdict") == "pass":
            issues.append(
                PreflightIssue(
                    kind="charter_missing",
                    severity="error",
                    message=(
                        f"Architect task {tid} reported verdict=pass "
                        f"but CHARTER.md is missing at {charter}. The "
                        f"primary architect deliverable is absent."
                    ),
                    task_id=tid,
                )
            )
    return issues


def _detect_dag_cycle(graph: dict[str, Any]) -> list[PreflightIssue]:
    """depends_on cycle would cause the runner to deadlock.

    Walks each task; if revisiting a task during DFS, there's a cycle.
    """
    issues: list[PreflightIssue] = []
    tasks = graph.get("tasks") or {}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in tasks}

    def visit(tid: str, path: list[str]) -> str | None:
        if color.get(tid) == GRAY:
            cycle_start = path.index(tid)
            return " -> ".join(path[cycle_start:] + [tid])
        if color.get(tid) == BLACK:
            return None
        color[tid] = GRAY
        for dep in tasks.get(tid, {}).get("depends_on") or []:
            if dep in tasks:
                cycle = visit(dep, path + [tid])
                if cycle:
                    return cycle
        color[tid] = BLACK
        return None

    for tid in tasks:
        if color.get(tid) == WHITE:
            cycle = visit(tid, [])
            if cycle:
                issues.append(
                    PreflightIssue(
                        kind="dag_cycle",
                        severity="block",
                        message=f"Dependency cycle in task graph: {cycle}",
                    )
                )
                break
    return issues


def _detect_duplicate_task_ids(pending: list[dict[str, Any]]) -> list[PreflightIssue]:
    """Two pending tasks with the same task_id would corrupt state.

    Very rare but cheap to check.
    """
    issues: list[PreflightIssue] = []
    seen: set[str] = set()
    for entry in pending:
        tid = entry.get("task_id")
        if not tid:
            continue
        if tid in seen:
            issues.append(
                PreflightIssue(
                    kind="duplicate_task_id",
                    severity="block",
                    message=f"Duplicate task_id in v5_pending: {tid}",
                    task_id=tid,
                )
            )
        seen.add(tid)
    return issues


def run_preflight(
    project_dir: Path,
    graph: dict[str, Any],
    pending: list[dict[str, Any]],
) -> list[PreflightIssue]:
    """Run all Phase-1 deterministic checks. Returns list of issues."""
    issues: list[PreflightIssue] = []
    issues.extend(_detect_architect_sub_decomp(graph))
    issues.extend(_detect_missing_charter(graph, project_dir))
    issues.extend(_detect_dag_cycle(graph))
    issues.extend(_detect_duplicate_task_ids(pending))
    return issues


def filter_blocked_descendants(
    graph: dict[str, Any],
    pending: list[dict[str, Any]],
    blocking_issues: list[PreflightIssue],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Filter out pending tasks that descend from a blocked task.

    For severity=block issues with a task_id, remove that task's
    descendants from the pending list so they don't dispatch.

    Returns (filtered_pending, set_of_blocked_task_ids).
    """
    blocked: set[str] = set()
    tasks = graph.get("tasks") or {}
    for issue in blocking_issues:
        if issue.severity == "block" and issue.task_id:
            blocked.add(issue.task_id)

    if not blocked:
        return pending, blocked

    # Walk: a pending task is blocked if any of its ancestors (via
    # depends_on) is in `blocked`.
    def is_descendant_of_blocked(tid: str) -> bool:
        task = tasks.get(tid) or {}
        deps = task.get("depends_on") or []
        return any(d in blocked or is_descendant_of_blocked(d) for d in deps)

    filtered: list[dict[str, Any]] = []
    for entry in pending:
        tid = entry.get("task_id", "")
        if tid in blocked:
            blocked.add(tid)
            continue
        # Also check if this pending task's depends_on includes a blocked task
        deps = entry.get("depends_on") or []
        if any(d in blocked for d in deps):
            blocked.add(tid)
            continue
        filtered.append(entry)

    return filtered, blocked
