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

import calendar
import contextlib
import fcntl
import json
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

from otto.schemas import TaskGraphEntry
from typing import Any, Literal

from otto.paths import cross_sessions_dir, sidecar_lock_path
from otto.observability import iso_timestamp

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
CONTRACT_AMENDMENT_RETRY_MAX_CLAIMS = 2
CONTRACT_AMENDMENT_RETRY_STALE_SECONDS = 15 * 60


def task_graph_path(project_dir: Path) -> Path:
    """Return the canonical task_graph.json path for a project."""
    return cross_sessions_dir(project_dir) / TASK_GRAPH_FILENAME


def _now_iso() -> str:
    return iso_timestamp()


def _parse_iso_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return calendar.timegm(time.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _pid_is_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def contract_amendment_retry_is_stale(
    task: dict[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """Return whether an in-progress merge-only retry can be reclaimed."""
    if not task.get("contract_amendment_retry_in_progress"):
        return False
    if not task.get("contract_amendment_retry_merge"):
        return False
    now = time.time() if now is None else now
    owner_host = str(task.get("contract_amendment_retry_owner_host") or "")
    if owner_host == socket.gethostname():
        owner_pid = task.get("contract_amendment_retry_owner_pid")
        if owner_pid is not None and not _pid_is_running(owner_pid):
            return True
    heartbeat = (
        _parse_iso_seconds(task.get("contract_amendment_retry_heartbeat_at"))
        or _parse_iso_seconds(task.get("contract_amendment_retry_merge_started_at"))
    )
    if heartbeat is None:
        return True
    return now - heartbeat > CONTRACT_AMENDMENT_RETRY_STALE_SECONDS


def _empty_graph() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tasks": {}}


@contextlib.contextmanager
def _locked_graph(project_dir: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Acquire fcntl lock, read graph, yield it for mutation, write atomically.

    Caller mutates the dict in-place; we write back on context exit.
    """
    path = task_graph_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sidecar_lock_path(path)
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


def get_task(project_dir: Path, task_id: str) -> TaskGraphEntry | None:
    """Return one task entry or None.

    Return type is the documentation TypedDict from ``otto.schemas`` so
    callers' editors surface the known keys. Behaviour is unchanged: the
    underlying value is a plain dict and TypedDict is ``total=False``.
    """
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
    task_role: TaskRole | None = None,
    foundation_contracts: list[dict[str, Any]] | None = None,
    decomposition: Decomposition = "unknown",
) -> None:
    """Atomically register a task in the graph (or update if it exists).

    Updates are non-destructive: missing fields preserve the existing value
    rather than overwriting with the arg's default. This matters when the
    same task is registered twice — once by ``submit_subtask`` (which knows
    the parent) and again by the child's own ``run_lead`` (which does not).
    Passing ``task_role=None`` means "role omitted"; preserve an existing role
    or use the construction default. Passing an explicit role, including
    ``feature``, sets the role authoritatively.
    """
    with _locked_graph(project_dir) as (_path, graph):
        existing_raw = graph["tasks"].get(task_id, {})
        existing: dict[str, Any] = existing_raw if isinstance(existing_raw, dict) else {}
        entry: dict[str, Any] = dict(existing)
        existing_role = entry.get("task_role")
        if task_role is None:
            role = (
                existing_role
                if isinstance(existing_role, str) and existing_role in TASK_ROLES
                else "feature"
            )
        else:
            role = task_role if task_role in TASK_ROLES else "feature"
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


def set_verdict_and_metadata(
    project_dir: Path,
    task_id: str,
    verdict: Verdict,
    *,
    cost_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Atomically set a verdict and associated metadata in one graph write."""
    clean = {key: value for key, value in (metadata or {}).items() if value is not None}
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
        else:
            graph["tasks"][task_id]["verdict"] = verdict
            graph["tasks"][task_id]["completed_at"] = _now_iso()
            if cost_usd is not None:
                graph["tasks"][task_id]["cost_usd"] = float(cost_usd)
        graph["tasks"][task_id].update(clean)


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


def set_contract_amendment_blocked(
    project_dir: Path,
    task_id: str,
    amendment_id: str,
    *,
    reason: str = "",
    merge_context: dict[str, Any] | None = None,
) -> None:
    """Put a passing leaf into non-terminal amendment-blocked state.

    The leaf already has a persisted terminal verdict before upward merge runs.
    Preserve that verdict as history, then clear terminal fields so the pending
    queue can later retry only the merge after the amendment lands.
    """
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
        task = graph["tasks"][task_id]
        if task.get("verdict") is not None:
            task["last_agent_verdict"] = task.get("verdict")
        task["verdict"] = None
        task["completed_at"] = None
        task["blocked_pending_contract_amendment"] = True
        task["blocked_on_task_id"] = amendment_id
        task["contract_amendment_retry_merge"] = False
        task["contract_amendment_retry_in_progress"] = False
        task["contract_amendment_blocked_at"] = _now_iso()
        if reason:
            task["contract_amendment_blocked_reason"] = reason
        if merge_context is not None:
            task["contract_amendment_merge_context"] = dict(merge_context)


def clear_contract_amendment_blocked_tasks(
    project_dir: Path,
    amendment_id: str,
) -> list[str]:
    """Clear every task blocked on ``amendment_id`` and mark merge retry intent."""
    cleared: list[str] = []
    with _locked_graph(project_dir) as (_path, graph):
        for task_id, task in graph["tasks"].items():
            if not isinstance(task, dict):
                continue
            if task.get("blocked_on_task_id") != amendment_id:
                continue
            task["blocked_pending_contract_amendment"] = False
            task["blocked_on_task_id"] = None
            task["contract_amendment_retry_merge"] = True
            task["contract_amendment_retry_in_progress"] = False
            task["contract_amendment_unblocked_at"] = _now_iso()
            cleared.append(str(task_id))
    return cleared


def mark_contract_amendment_retry_in_progress(
    project_dir: Path,
    task_id: str,
    *,
    owner_id: str | None = None,
) -> bool:
    """Atomically claim a merge-only amendment retry under the task-graph lock."""
    with _locked_graph(project_dir) as (_path, graph):
        task = graph["tasks"].get(task_id)
        if not isinstance(task, dict):
            return False
        if task.get("blocked_pending_contract_amendment") or task.get("blocked_on_task_id"):
            return False
        if task.get("verdict") in {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}:
            return False
        if not task.get("contract_amendment_retry_merge"):
            return False
        if task.get("contract_amendment_retry_in_progress"):
            if not contract_amendment_retry_is_stale(task):
                return False
            try:
                claim_count = int(task.get("contract_amendment_retry_claim_count") or 0)
            except (TypeError, ValueError):
                claim_count = 0
            try:
                max_claims = int(
                    task.get("contract_amendment_retry_max_claims")
                    or CONTRACT_AMENDMENT_RETRY_MAX_CLAIMS
                )
            except (TypeError, ValueError):
                max_claims = CONTRACT_AMENDMENT_RETRY_MAX_CLAIMS
            if claim_count >= max_claims:
                return False
        else:
            try:
                claim_count = int(task.get("contract_amendment_retry_claim_count") or 0)
            except (TypeError, ValueError):
                claim_count = 0
            max_claims = CONTRACT_AMENDMENT_RETRY_MAX_CLAIMS
        task["contract_amendment_retry_in_progress"] = True
        now = _now_iso()
        claim_count += 1
        owner_token = owner_id or f"{socket.gethostname()}:{os.getpid()}:{now}:{claim_count}"
        task["contract_amendment_retry_merge_started_at"] = now
        task["contract_amendment_retry_heartbeat_at"] = now
        task["contract_amendment_retry_owner"] = owner_token
        task["contract_amendment_retry_owner_pid"] = os.getpid()
        task["contract_amendment_retry_owner_host"] = socket.gethostname()
        task["contract_amendment_retry_claim_count"] = claim_count
        task["contract_amendment_retry_max_claims"] = max_claims
        merge_context = task.get("contract_amendment_merge_context")
        if not isinstance(merge_context, dict):
            merge_context = {}
        merge_context = dict(merge_context)
        merge_context["retry_owner"] = owner_token
        merge_context["retry_claim_count"] = claim_count
        merge_context["retry_claimed_at"] = now
        task["contract_amendment_merge_context"] = merge_context
        return True


def refresh_contract_amendment_retry_heartbeat(
    project_dir: Path,
    task_id: str,
    *,
    owner_id: str,
) -> bool:
    """Refresh a live merge-only retry claim if this owner still holds it."""
    with _locked_graph(project_dir) as (_path, graph):
        task = graph["tasks"].get(task_id)
        if not isinstance(task, dict):
            return False
        if task.get("contract_amendment_retry_owner") != owner_id:
            return False
        if not task.get("contract_amendment_retry_in_progress"):
            return False
        if not task.get("contract_amendment_retry_merge"):
            return False
        if task.get("blocked_pending_contract_amendment") or task.get("blocked_on_task_id"):
            return False
        if task.get("verdict") in {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}:
            return False
        task["contract_amendment_retry_heartbeat_at"] = _now_iso()
        return True


def terminalize_stale_contract_amendment_retry_if_exhausted(
    project_dir: Path,
    task_id: str,
    *,
    reason: str,
    structured_reason: dict[str, Any],
) -> bool:
    """Persist merge_blocked when stale retry claims are exhausted."""
    with _locked_graph(project_dir) as (_path, graph):
        task = graph["tasks"].get(task_id)
        if not isinstance(task, dict):
            return False
        if task.get("blocked_pending_contract_amendment") or task.get("blocked_on_task_id"):
            return False
        if task.get("verdict") in {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}:
            return False
        if not task.get("contract_amendment_retry_merge"):
            return False
        if not task.get("contract_amendment_retry_in_progress"):
            return False
        if not contract_amendment_retry_is_stale(task):
            return False
        try:
            claim_count = int(task.get("contract_amendment_retry_claim_count") or 0)
        except (TypeError, ValueError):
            claim_count = 0
        try:
            max_claims = int(
                task.get("contract_amendment_retry_max_claims")
                or CONTRACT_AMENDMENT_RETRY_MAX_CLAIMS
            )
        except (TypeError, ValueError):
            max_claims = CONTRACT_AMENDMENT_RETRY_MAX_CLAIMS
        if claim_count < max_claims:
            return False
        task["verdict"] = "merge_blocked"
        task["completed_at"] = _now_iso()
        task["failure_reason"] = reason
        task["merge_blocked_origin"] = "contract_amendment"
        task["merge_blocked_reason"] = reason
        task["merge_blocked_structured_reason"] = dict(structured_reason)
        task["contract_amendment_retry_merge"] = False
        task["contract_amendment_retry_in_progress"] = False
        task["contract_amendment_retry_exhausted_at"] = _now_iso()
        return True


def persist_contract_amendment_retry_success(
    project_dir: Path,
    task_id: str,
    verdict: Verdict,
    *,
    cost_usd: float | None = None,
) -> bool:
    """Atomically restore a terminal verdict and clear retry-only state."""
    with _locked_graph(project_dir) as (_path, graph):
        task = graph["tasks"].get(task_id)
        if not isinstance(task, dict):
            return False
        if task.get("blocked_pending_contract_amendment") or task.get("blocked_on_task_id"):
            return False
        if str(task.get("verdict") or "") in {"merge_blocked", "catastrophic"}:
            return False
        if task.get("merge_blocked_structured_reason") or task.get("merge_blocked_reason"):
            return False
        task["verdict"] = verdict
        task["completed_at"] = _now_iso()
        if cost_usd is not None:
            task["cost_usd"] = float(cost_usd)
        task["contract_amendment_retry_merge"] = False
        task["contract_amendment_retry_in_progress"] = False
        task["contract_amendment_retry_restored_at"] = _now_iso()
        return True


def clear_contract_amendment_blocked_state(
    project_dir: Path,
    task_ids: list[str],
) -> None:
    """Clear amendment blocker fields for the supplied tasks."""
    wanted = set(task_ids)
    if not wanted:
        return
    with _locked_graph(project_dir) as (_path, graph):
        for task_id in wanted:
            task = graph["tasks"].get(task_id)
            if not isinstance(task, dict):
                continue
            task["blocked_pending_contract_amendment"] = False
            task["blocked_on_task_id"] = None
            task["contract_amendment_retry_merge"] = False
            task["contract_amendment_retry_in_progress"] = False


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
