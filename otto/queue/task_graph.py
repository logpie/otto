"""Task graph storage — durable record of parent-child relationships and verdicts.

The task graph is the source of truth for v5's hierarchical decomposition.
Each Lead session that emits subtasks adds edges; the watcher reads the graph
to know when a parent's children have all resolved.

File layout per project: ``otto_logs/cross-sessions/task_graph.json``

Schema:
    {
        "schema_version": 1,
        "tasks": {
            "<task_id>": {
                "parent_task_id": "<id or null>",
                "intent": "...",
                "decomposition": "inline | emit | pending | unknown",
                "verdict": "pass | partial | unverified | merge_blocked |
                            pending_children | catastrophic | None",
                "integration_branch": "i2p/.../integration",
                "started_at": "iso8601",
                "completed_at": "iso8601 or null",
                "cost_usd": 0.0,
                "failure_reason": "...",
                "merge_blocked_origin": "merge | verification | ...",
                "child_task_ids": ["..."],
                "depends_on": ["..."]
            }
        }
    }

Concurrency: writes go through ``_write_atomic`` which uses fcntl.flock + write
to a tempfile + rename. Multiple Leads writing concurrently must be safe — see
``test_concurrency.py`` for the empirical confirmation that submit_subtask
calls under load do not race.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from otto.paths import cross_sessions_dir

SCHEMA_VERSION = 1
TASK_GRAPH_FILENAME = "task_graph.json"


Verdict = Literal[
    "pass",
    "partial",
    "unverified",
    "merge_blocked",
    "pending_children",
    "catastrophic",
]

Decomposition = Literal["inline", "emit", "pending", "unknown"]
TaskRole = Literal["foundation", "feature", "contract_amendment", "integration"]
TASK_ROLES: set[str] = {"foundation", "feature", "contract_amendment", "integration"}


def task_graph_path(project_dir: Path) -> Path:
    """Return the canonical task_graph.json path for a project."""
    return cross_sessions_dir(project_dir) / TASK_GRAPH_FILENAME


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty_graph() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tasks": {}}


@contextlib.contextmanager
def _locked_graph(project_dir: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Acquire fcntl lock, read graph, yield it for mutation, write atomically.

    Caller mutates the dict in-place; we write back on context exit.
    """
    path = task_graph_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.exists():
            try:
                graph = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                graph = _empty_graph()
        else:
            graph = _empty_graph()
        if not isinstance(graph, dict) or graph.get("schema_version") != SCHEMA_VERSION:
            graph = _empty_graph()
        if "tasks" not in graph or not isinstance(graph["tasks"], dict):
            graph["tasks"] = {}
        yield path, graph
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def read_graph(project_dir: Path) -> dict[str, Any]:
    """Read the task graph (no lock). Caller must not mutate the result."""
    path = task_graph_path(project_dir)
    if not path.exists():
        return _empty_graph()
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_graph()
    if not isinstance(graph, dict) or graph.get("schema_version") != SCHEMA_VERSION:
        return _empty_graph()
    if "tasks" not in graph or not isinstance(graph["tasks"], dict):
        graph["tasks"] = {}
    return graph


def get_task(project_dir: Path, task_id: str) -> dict[str, Any] | None:
    """Return one task entry or None."""
    graph = read_graph(project_dir)
    return graph["tasks"].get(task_id)


def record_task(
    project_dir: Path,
    *,
    task_id: str,
    intent: str,
    parent_task_id: str | None = None,
    integration_branch: str | None = None,
    depends_on: list[str] | None = None,
    owned_paths: list[str] | None = None,
    action_ids: list[str] | None = None,
    task_role: TaskRole = "feature",
    foundation_contracts: list[dict[str, Any]] | None = None,
    decomposition: Decomposition = "unknown",
) -> None:
    """Atomically register a task in the graph (or update if it exists).

    Updates are non-destructive: missing fields preserve the existing value
    rather than overwriting with the arg's default. This matters when the
    same task is registered twice — once by ``submit_subtask`` (which knows
    the parent) and again by the child's own ``run_lead`` (which does not).
    """
    with _locked_graph(project_dir) as (_path, graph):
        existing_raw = graph["tasks"].get(task_id, {})
        existing: dict[str, Any] = existing_raw if isinstance(existing_raw, dict) else {}
        entry: dict[str, Any] = dict(existing)
        role = task_role if task_role in TASK_ROLES else "feature"
        existing_role = entry.get("task_role")
        if isinstance(existing_role, str) and existing_role in TASK_ROLES and task_role == "feature":
            role = existing_role
        entry.update({
            "parent_task_id": parent_task_id if parent_task_id is not None
                              else existing.get("parent_task_id"),
            "intent": intent or existing.get("intent", ""),
            "decomposition": existing.get("decomposition", decomposition),
            "verdict": existing.get("verdict"),
            "integration_branch": integration_branch
                                   if integration_branch is not None
                                   else existing.get("integration_branch"),
            "started_at": existing.get("started_at") or _now_iso(),
            "completed_at": existing.get("completed_at"),
            "cost_usd": existing.get("cost_usd", 0.0),
            "child_task_ids": existing.get("child_task_ids", []),
            "depends_on": list(depends_on or existing.get("depends_on", []) or []),
            "owned_paths": list(
                owned_paths
                if owned_paths is not None
                else existing.get("owned_paths", []) or []
            ),
            "action_ids": list(
                action_ids
                if action_ids is not None
                else existing.get("action_ids", []) or []
            ),
            "task_role": role,
            "foundation_contracts": list(
                foundation_contracts
                if foundation_contracts is not None
                else existing.get("foundation_contracts", []) or []
            ),
        })
        graph["tasks"][task_id] = entry
        if parent_task_id and parent_task_id in graph["tasks"]:
            parent = graph["tasks"][parent_task_id]
            kids = parent.setdefault("child_task_ids", [])
            if task_id not in kids:
                kids.append(task_id)


def set_decomposition(project_dir: Path, task_id: str, decomposition: Decomposition) -> None:
    """Record the Lead's decomposition decision for ``task_id``."""
    with _locked_graph(project_dir) as (_path, graph):
        if task_id in graph["tasks"]:
            graph["tasks"][task_id]["decomposition"] = decomposition


def set_verdict(
    project_dir: Path,
    task_id: str,
    verdict: Verdict,
    *,
    cost_usd: float | None = None,
) -> None:
    """Set a task's terminal verdict (and optionally update its cost)."""
    with _locked_graph(project_dir) as (_path, graph):
        if task_id not in graph["tasks"]:
            graph["tasks"][task_id] = {
                "parent_task_id": None,
                "intent": "",
                "decomposition": "unknown",
                "verdict": verdict,
                "integration_branch": None,
                "started_at": _now_iso(),
                "completed_at": _now_iso(),
                "cost_usd": cost_usd or 0.0,
                "child_task_ids": [],
                "depends_on": [],
                "owned_paths": [],
                "action_ids": [],
                "task_role": "feature",
                "foundation_contracts": [],
            }
            return
        graph["tasks"][task_id]["verdict"] = verdict
        graph["tasks"][task_id]["completed_at"] = _now_iso()
        if cost_usd is not None:
            graph["tasks"][task_id]["cost_usd"] = float(cost_usd)


def update_task_metadata(
    project_dir: Path,
    task_id: str,
    **metadata: Any,
) -> None:
    """Update extra durable task metadata without changing verdict semantics."""
    clean = {key: value for key, value in metadata.items() if value is not None}
    if not clean:
        return
    with _locked_graph(project_dir) as (_path, graph):
        if task_id not in graph["tasks"]:
            graph["tasks"][task_id] = {
                "parent_task_id": None,
                "intent": "",
                "decomposition": "unknown",
                "verdict": None,
                "integration_branch": None,
                "started_at": _now_iso(),
                "completed_at": None,
                "cost_usd": 0.0,
                "child_task_ids": [],
                "depends_on": [],
                "owned_paths": [],
                "action_ids": [],
                "task_role": "feature",
                "foundation_contracts": [],
            }
        graph["tasks"][task_id].update(clean)


def mark_reviewed_partial(
    project_dir: Path,
    task_id: str,
    *,
    reason: str = "",
    reviewer: str = "oracle",
) -> None:
    """Record explicit approval for a partial verdict to merge upward.

    The terminal verdict remains ``partial`` so aggregate product semantics stay
    honest. The separate review_state is the merge gate's durable proof that
    this was not a raw agent self-claim.
    """
    with _locked_graph(project_dir) as (_path, graph):
        if task_id not in graph["tasks"]:
            graph["tasks"][task_id] = {
                "parent_task_id": None,
                "intent": "",
                "decomposition": "unknown",
                "verdict": "partial",
                "integration_branch": None,
                "started_at": _now_iso(),
                "completed_at": _now_iso(),
                "cost_usd": 0.0,
                "child_task_ids": [],
                "depends_on": [],
            }
        entry = graph["tasks"][task_id]
        entry["review_state"] = "reviewed_partial"
        entry["reviewed_partial_at"] = _now_iso()
        entry["reviewed_partial_by"] = reviewer
        if reason:
            entry["reviewed_partial_reason"] = reason


def add_cost(project_dir: Path, task_id: str, cost_usd: float) -> None:
    """Increment a task's accumulated cost by ``cost_usd``."""
    with _locked_graph(project_dir) as (_path, graph):
        if task_id in graph["tasks"]:
            cur = float(graph["tasks"][task_id].get("cost_usd", 0.0))
            graph["tasks"][task_id]["cost_usd"] = cur + float(cost_usd)


def clear_verdict_for_retry(
    project_dir: Path,
    task_id: str,
    retry_reason: str,
) -> int:
    """Reset a task's terminal verdict so it can be re-dispatched.

    Used when a deterministic runner check (e.g., scaffold preflight)
    invalidates the agent's self-declared verdict. Clearing the verdict
    + completed_at lets ``take_ready`` pick the task back up. The
    ``retry_reason`` is stored on the task so the next dispatch can
    surface it to the agent.

    Returns the new ``retry_count`` (1 after the first retry, 2 after
    the second, ...). Callers use this to enforce a retry cap.
    """
    with _locked_graph(project_dir) as (_path, graph):
        t = graph["tasks"].get(task_id)
        if t is None:
            return 0
        t["verdict"] = None
        t["completed_at"] = None
        t["retry_reason"] = retry_reason
        t["retry_count"] = int(t.get("retry_count", 0)) + 1
        return int(t["retry_count"])


def get_retry_reason(project_dir: Path, task_id: str) -> str | None:
    """Return the stored retry_reason for ``task_id``, or None."""
    graph = read_graph(project_dir)
    t = graph.get("tasks", {}).get(task_id) or {}
    reason = t.get("retry_reason")
    if isinstance(reason, str) and reason.strip():
        return reason
    return None


def get_retry_count(project_dir: Path, task_id: str) -> int:
    """Return how many times ``task_id`` has been retried so far."""
    graph = read_graph(project_dir)
    t = graph.get("tasks", {}).get(task_id) or {}
    return int(t.get("retry_count", 0))


def children_of(project_dir: Path, task_id: str) -> list[str]:
    """Return the ordered list of child task_ids for ``task_id``."""
    graph = read_graph(project_dir)
    entry = graph["tasks"].get(task_id) or {}
    return list(entry.get("child_task_ids", []))


def all_children_resolved(project_dir: Path, task_id: str) -> bool:
    """True iff every child of ``task_id`` has a non-None, non-pending verdict."""
    graph = read_graph(project_dir)
    entry = graph["tasks"].get(task_id) or {}
    kids = entry.get("child_task_ids", [])
    if not kids:
        return False
    for kid in kids:
        kid_entry = graph["tasks"].get(kid) or {}
        v = kid_entry.get("verdict")
        if v is None or v == "pending_children":
            return False
    return True


def aggregate_verdict(project_dir: Path, task_id: str) -> Verdict:
    """Compute a parent's verdict from itself + its children.

    Severity order (worst-wins for parent):
        catastrophic > merge_blocked > unverified > partial > pending_children > pass
    """
    severity: dict[Verdict, int] = {
        "pass": 0,
        "pending_children": 1,
        "partial": 2,
        "unverified": 3,
        "merge_blocked": 4,
        "catastrophic": 5,
    }
    graph = read_graph(project_dir)
    entry = graph["tasks"].get(task_id) or {}
    own: Verdict = entry.get("verdict") or "pass"
    worst = own
    for kid in entry.get("child_task_ids", []):
        kid_entry = graph["tasks"].get(kid) or {}
        kid_v: Verdict = kid_entry.get("verdict") or "pending_children"
        if severity.get(kid_v, 0) > severity.get(worst, 0):
            worst = kid_v
    return worst


def tree_total_cost(project_dir: Path, task_id: str) -> float:
    """Sum cost across the task's full subtree."""
    graph = read_graph(project_dir)
    visited: set[str] = set()

    def _walk(tid: str) -> float:
        if tid in visited:
            return 0.0
        visited.add(tid)
        entry = graph["tasks"].get(tid) or {}
        own = float(entry.get("cost_usd", 0.0))
        for kid in entry.get("child_task_ids", []):
            own += _walk(kid)
        return own

    return _walk(task_id)
