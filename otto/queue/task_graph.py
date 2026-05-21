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

from otto.schemas import (
    VERDICT_CATASTROPHIC,
    VERDICT_MERGE_BLOCKED,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_PENDING_CHILDREN,
    VERDICT_UNVERIFIED,
    TaskGraphEntry,
)
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
RETRY_IN_PROGRESS_STALE_SECONDS = 60 * 60
BLOCKER_METADATA_KEYS = (
    "merge_blocked_reason",
    "merge_blocked_structured_reason",
    "merge_blocked_origin",
)


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


def _retry_in_progress_stale_seconds() -> int:
    raw = os.environ.get("OTTO_RETRY_IN_PROGRESS_STALE_SECONDS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return RETRY_IN_PROGRESS_STALE_SECONDS


def _owner_field_matches(
    left: dict[str, Any],
    right: dict[str, Any],
    key: str,
) -> bool:
    return str(left.get(key) or "") == str(right.get(key) or "")


def _retry_in_progress_owner_matches(entry: Any, owner: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    current_owner = entry.get("owner")
    if not isinstance(current_owner, dict):
        return False
    return (
        _owner_field_matches(current_owner, owner, "pid")
        and _owner_field_matches(current_owner, owner, "host")
        and _owner_field_matches(current_owner, owner, "started_at")
    )


def _retry_in_progress_is_live(
    entry: dict[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """Return whether a retry-children scheduler guard should still block.

    New markers are owner dictionaries. Legacy bare ``True`` markers are treated
    as stale so a crashed retry cannot strand a task forever.
    """
    marker = entry.get("retry_in_progress") if isinstance(entry, dict) else None
    if not marker or not isinstance(marker, dict):
        return False
    owner = marker.get("owner")
    if not isinstance(owner, dict):
        return False

    owner_host = str(owner.get("host") or "")
    if owner_host == socket.gethostname() and _pid_is_running(owner.get("pid")):
        return True

    started_at = (
        _parse_iso_seconds(owner.get("started_at"))
        or _parse_iso_seconds(entry.get("retry_in_progress_at"))
    )
    if started_at is None:
        return False
    now = time.time() if now is None else now
    return now - started_at <= _retry_in_progress_stale_seconds()


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
        if verdict != VERDICT_MERGE_BLOCKED:
            _clear_blocker_metadata_from_task(graph["tasks"][task_id])


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
        if verdict != VERDICT_MERGE_BLOCKED:
            _clear_blocker_metadata_from_task(graph["tasks"][task_id])
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


def _clear_blocker_metadata_from_task(task: dict[str, Any]) -> None:
    for key in BLOCKER_METADATA_KEYS:
        if key in task:
            task[key] = None


def clear_blocker_metadata(project_dir: Path, task_id: str) -> None:
    """Clear stale merge-blocked metadata without changing verdict state."""
    with _locked_graph(project_dir) as (_path, graph):
        task = graph["tasks"].get(task_id)
        if isinstance(task, dict):
            _clear_blocker_metadata_from_task(task)


def mark_retry_in_progress(
    project_dir: Path,
    task_ids: list[str],
    in_progress: bool,
    *,
    owner: dict[str, Any] | None = None,
) -> None:
    """Durably mark tasks being rewritten by retry-children."""
    now = _now_iso()
    owner_token = dict(owner or {})
    with _locked_graph(project_dir) as (_path, graph):
        tasks = graph.get("tasks") or {}
        if not isinstance(tasks, dict):
            return
        for task_id in task_ids:
            task = tasks.get(task_id)
            if not isinstance(task, dict):
                continue
            if in_progress:
                if not owner_token:
                    owner_token = {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "started_at": now,
                    }
                task["retry_in_progress"] = {"owner": dict(owner_token)}
                task["retry_in_progress_at"] = str(
                    owner_token.get("started_at") or now
                )
                task["retry_in_progress_cleared_at"] = None
            else:
                if owner is not None and not _retry_in_progress_owner_matches(
                    task.get("retry_in_progress"), owner
                ):
                    continue
                task["retry_in_progress"] = False
                task["retry_in_progress_cleared_at"] = now


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
        if task.get("verdict") in {
            VERDICT_PASS,
            VERDICT_PARTIAL,
            VERDICT_UNVERIFIED,
            VERDICT_MERGE_BLOCKED,
            VERDICT_CATASTROPHIC,
        }:
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
        if task.get("verdict") in {
            VERDICT_PASS,
            VERDICT_PARTIAL,
            VERDICT_UNVERIFIED,
            VERDICT_MERGE_BLOCKED,
            VERDICT_CATASTROPHIC,
        }:
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
        if task.get("verdict") in {
            VERDICT_PASS,
            VERDICT_PARTIAL,
            VERDICT_UNVERIFIED,
            VERDICT_MERGE_BLOCKED,
            VERDICT_CATASTROPHIC,
        }:
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
        if str(task.get("verdict") or "") in {VERDICT_MERGE_BLOCKED, VERDICT_CATASTROPHIC}:
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


def entry_is_satisfactory_terminal(entry: dict[str, Any]) -> bool:
    """Single source of truth: is this task entry in a satisfactory
    terminal state for downstream consumers (upward-merge AND
    dependency-satisfaction)?

    Locked invariant from [[project_v5_one_hard_gate_redesign]]:
    annotated partials (set via chokepoint LAND path, where
    `landed_with_annotation=True`) count as satisfactory — they have
    been deemed safe enough to land by the chokepoint's cause analysis
    (anything not INFRA_CORRUPT). Without this widening, the chokepoint's
    "always LAND" outcome doesn't actually result in merged work,
    because the upward-merge + dependency-satisfaction predicates
    historically only accepted `pass` or `partial + reviewed_partial`.

    History: pre-2026-05-20, three predicate sites had separate strict
    implementations of "is this satisfactory":
      - _child_result_allows_upward_merge (v5_runner)
      - _task_entry_allows_upward_merge   (v5_runner)
      - _verdict_satisfies_dependency     (subtask.py)
    The iTracker Opus run wasted ~$120 partly because of the
    cumulative effect of these strict + duplicated predicates.
    Centralizing them here, with the wider semantics, is part of
    plan-checkpoint-resume-v2.md Phase 0 (Codex Plan Gate APPROVED).
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("blocked_pending_contract_amendment"):
        return False
    if entry.get("blocked_on_task_id"):
        return False
    verdict = str(entry.get("verdict") or "")
    if verdict == VERDICT_MERGE_BLOCKED:
        return False
    # Stale merge_blocked metadata is also disqualifying (a previous
    # terminal that hasn't been cleared). retry helpers clear these.
    if entry.get("merge_blocked_structured_reason"):
        return False
    if entry.get("merge_blocked_reason"):
        return False
    if verdict == VERDICT_PASS:
        return True
    if verdict != VERDICT_PARTIAL:
        return False
    # `partial` is satisfactory if EITHER human-reviewed-partial OR
    # otto-annotated-partial via the chokepoint's LAND path.
    return bool(
        entry.get("review_state") == "reviewed_partial"
        or entry.get("landed_with_annotation")
    )


def clear_task_for_retry(
    project_dir: Path,
    task_id: str,
    retry_reason: str,
) -> int:
    """Strictly-more-thorough sibling of `clear_verdict_for_retry`:
    clears the terminal verdict AND all stale-blocker metadata so the
    task is genuinely re-runnable.

    Beyond `clear_verdict_for_retry`'s `verdict + completed_at + retry_*`
    reset, this also clears:
      - merge_blocked_reason / merge_blocked_structured_reason /
        merge_blocked_origin
      - failure_reason
      - annotation_origin / annotation_detail / annotation_cause /
        annotation_structured_reason / landed_with_annotation
      - review_state (set to None)

    Without this thoroughness, `entry_is_satisfactory_terminal` would
    refuse to advance the retried task post-retry because stale
    merge_blocked_reason / merge_blocked_structured_reason metadata
    would still indicate a blocked terminal. (Codex Plan Gate R2#2.)

    Returns the new retry_count.
    """
    blocker_keys = (
        "merge_blocked_reason",
        "merge_blocked_structured_reason",
        "merge_blocked_origin",
        "failure_reason",
        "annotation_origin",
        "annotation_detail",
        "annotation_cause",
        "annotation_structured_reason",
        "landed_with_annotation",
    )
    with _locked_graph(project_dir) as (_path, graph):
        t = graph["tasks"].get(task_id)
        if t is None:
            return 0
        t["verdict"] = None
        t["completed_at"] = None
        t["retry_reason"] = retry_reason
        t["retry_count"] = int(t.get("retry_count", 0)) + 1
        t["review_state"] = None
        for key in blocker_keys:
            if key in t:
                # Use None for nullable fields; bools become None too —
                # the satisfactory-terminal helper treats falsy as
                # "blocker not present."
                t[key] = None
        return int(t["retry_count"])


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
        if v is None or v == VERDICT_PENDING_CHILDREN:
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
