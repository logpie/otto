"""Repair loops and contract-amendment lifecycle.

Extracted from ``otto/v5_runner.py``. The public surface stays on
``otto.v5_runner`` — every symbol defined here is re-exported. Patched
runner symbols are dereferenced lazily via ``_v5r.X`` to honour
``monkeypatch.setattr("otto.v5_runner.X", ...)`` from tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from otto import paths as _paths
from otto.lead import LeadResult
from otto.safe_slug import safe_slug
from otto.schemas import (
    VERDICT_CATASTROPHIC,
    VERDICT_MERGE_BLOCKED,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_UNVERIFIED,
)
from otto.v5_branching import MergeWorktreeDirtyError
from otto.v5_clean_verify import CleanOracleResult
from otto.v5_preflight_repair import RepairPacket
from otto.defaults import (
    DEFAULT_REPAIR_AGENT_TURNS,
    DEFAULT_REPAIR_ORACLE_INVOCATIONS,
)
from otto.queue.subtask import (
    append_pending_entry,
    enqueue_subtask,
    v5_pending_path,
)
from otto.observability import iso_timestamp
from otto.queue.task_graph import (
    clear_contract_amendment_blocked_state,
    clear_contract_amendment_blocked_tasks,
    get_task,
    task_graph_path,
    persist_contract_amendment_retry_success,
    read_graph,
    record_task,
    refresh_contract_amendment_retry_heartbeat,
    set_contract_amendment_blocked,
    terminalize_stale_contract_amendment_retry_if_exhausted,
    update_task_metadata,
)

logger = logging.getLogger("otto.v5_runner")

# Lazy parent-module reference for patchability of ``subprocess``,
# ``verify_from_clean_oracle``, ``run_oracle_repair_agent`` and other
# runner-private symbols at ``otto.v5_runner.X``.
from otto import v5_runner as _v5r  # noqa: E402


def _foundation_contract_for_feedback_path(
    *,
    project_dir: Path,
    parent_task_id: str,
    child_task_id: str,
    feedback: dict[str, Any],
) -> dict[str, Any] | None:
    graph = read_graph(project_dir)
    tasks = graph.get("tasks") or {}
    child = tasks.get(child_task_id) if isinstance(tasks, dict) else None
    child_owned_paths = _v5r._task_owned_paths(child) if isinstance(child, dict) else []
    candidate_paths: list[str] = []
    for path in feedback.get("paths") or []:
        normalized = _v5r._normalize_contract_path(str(path))
        if normalized:
            candidate_paths.append(normalized)
    for item in feedback.get("missing") or []:
        if isinstance(item, dict):
            normalized = _v5r._normalize_contract_path(str(item.get("path") or ""))
            if normalized:
                candidate_paths.append(normalized)
    integration_context = feedback.get("integration_context")
    if isinstance(integration_context, dict):
        guard = integration_context.get("integration_union_guard")
        if isinstance(guard, dict):
            for path in guard.get("paths") or []:
                normalized = _v5r._normalize_contract_path(str(path))
                if normalized:
                    candidate_paths.append(normalized)
            for item in guard.get("missing") or []:
                if isinstance(item, dict):
                    normalized = _v5r._normalize_contract_path(str(item.get("path") or ""))
                    if normalized:
                        candidate_paths.append(normalized)

    contracts = _v5r._foundation_contracts_for_parent(project_dir, parent_task_id, tasks)
    for candidate_path in dict.fromkeys(candidate_paths):
        for contract in contracts:
            contract_path = _v5r._normalize_contract_path(str(contract.get("path") or ""))
            owner_id = str(contract.get("owner_task_id") or "").strip()
            if not contract_path or child_task_id == owner_id:
                continue
            if not _v5r._path_overlaps(candidate_path, contract_path):
                continue
            if any(_v5r._path_overlaps(owned, contract_path) for owned in child_owned_paths):
                continue
            return {
                "path": contract_path,
                "owner_task_id": owner_id,
                "check": contract.get("check"),
            }
    return None

def _enqueue_existing_task_for_merge_retry(
    *,
    project_dir: Path,
    task_id: str,
    parent_task_id: str,
    parent_session_dir: Path,
    intent: str,
    owned_paths: list[str],
    task_role: str,
    parent_integration_branch: str,
) -> None:
    entry = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(parent_session_dir),
        "intent": intent,
        "depends_on": [],
        "owned_paths": list(owned_paths),
        "action_ids": [],
        "task_role": task_role,
        "integration_branch": parent_integration_branch,
        "review_state": "approved",
        "enqueued_at": iso_timestamp(),
    }
    append_pending_entry(v5_pending_path(project_dir), entry)

def _contract_amendment_attempt_key(contract_path: str) -> str:
    return _v5r._normalize_contract_path(contract_path)

def _contract_amendment_attempt_count(task: dict[str, Any], contract_path: str) -> int:
    attempts = task.get("contract_amendment_attempts")
    if not isinstance(attempts, dict):
        if attempts is not None:
            logger.warning(
                "contract_amendment_attempts metadata malformed (got %s); resetting counter to 0",
                type(attempts).__name__,
            )
        return 0
    key = _contract_amendment_attempt_key(contract_path)
    try:
        return int(attempts.get(key, 0))
    except (TypeError, ValueError) as exc:
        logger.warning(
            "contract_amendment_attempts[%s] malformed (%s); resetting counter to 0",
            key,
            exc,
        )
        return 0

def _increment_contract_amendment_attempt(
    project_dir: Path,
    child_task_id: str,
    contract_path: str,
) -> int:
    leaf = get_task(project_dir, child_task_id) or {}
    attempts = leaf.get("contract_amendment_attempts")
    if not isinstance(attempts, dict):
        attempts = {}
    attempts = dict(attempts)
    key = _contract_amendment_attempt_key(contract_path)
    try:
        current = int(attempts.get(key, 0) or 0)
    except (TypeError, ValueError):
        current = 0
    next_count = current + 1
    attempts[key] = next_count
    update_task_metadata(
        project_dir,
        child_task_id,
        contract_amendment_attempts=attempts,
        contract_amendment_last_attempt_contract=key,
        contract_amendment_last_attempt_count=next_count,
    )
    return next_count

def _contract_amendment_exhausted_feedback(
    *,
    child_task_id: str,
    parent_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    contract_path: str,
    owner_id: str,
    union_feedback: dict[str, Any],
    attempt_count: int,
) -> dict[str, Any]:
    return {
        "kind": "contract_amendment_attempts_exhausted",
        "step_id": "foundation_contract_amendment",
        "message": (
            "foundation contract amendment remained ineffective after "
            f"{attempt_count} attempt(s); refusing another amendment retry"
        ),
        "task_id": child_task_id,
        "parent_task_id": parent_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "contract_path": contract_path,
        "owner_task_id": owner_id,
        "attempt_count": attempt_count,
        "max_attempts": _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        "previous_feedback": union_feedback,
        "_written_at": iso_timestamp(),
    }

def _schedule_foundation_contract_amendment(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    union_feedback: dict[str, Any],
    contract: dict[str, Any],
    on_event: Any = None,
) -> str:
    contract_path = _v5r._normalize_contract_path(str(contract.get("path") or ""))
    owner_id = str(contract.get("owner_task_id") or "").strip()
    attempt_count = _increment_contract_amendment_attempt(
        project_dir,
        child_task_id,
        contract_path,
    )
    intent = (
        "Repair foundation contract amendment for "
        f"`{contract_path}` after child `{child_task_id}` exposed an integration "
        "union conflict. Preserve the contract owner's behavior and make the "
        "contract compatible with the blocked leaf's contribution."
    )
    amendment_id = enqueue_subtask(
        project_dir=project_dir,
        parent_task_id=parent_task_id,
        parent_session_dir=child_session_dir,
        intent=intent,
        owned_paths=[contract_path],
        task_role="contract_amendment",
        parent_integration_branch=parent_integration_branch,
    )
    record_task(
        project_dir,
        task_id=amendment_id,
        parent_task_id=parent_task_id,
        intent=intent,
        integration_branch=parent_integration_branch,
        owned_paths=[contract_path],
        task_role="contract_amendment",
    )
    update_task_metadata(
        project_dir,
        amendment_id,
        contract_amendment={
            "contract_path": contract_path,
            "owner_task_id": owner_id,
            "blocked_task_id": child_task_id,
            "source_branch": source_branch,
            "pre_merge_ref": pre_merge_ref,
            "attempt_count": attempt_count,
            "max_attempts": _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        },
        contract_amendment_path=contract_path,
        contract_amendment_owner_task_id=owner_id,
        repair_route="foundation_contract_amendment",
    )
    set_contract_amendment_blocked(
        project_dir,
        child_task_id,
        amendment_id,
        reason=str(union_feedback.get("message") or _v5r._integration_union_reason_text(union_feedback)),
        merge_context={
            "child_session_dir": str(child_session_dir),
            "child_worktree": str(child_worktree),
            "parent_integration_branch": parent_integration_branch,
            "source_branch": source_branch,
            "pre_merge_ref": pre_merge_ref,
            "union_feedback": union_feedback,
        },
    )
    _v5r._emit(on_event, {
        "event": "foundation_contract_amendment_repair",
        "task_id": child_task_id,
        "amendment_task_id": amendment_id,
        "contract_path": contract_path,
        "owner_task_id": owner_id,
        "parent_task_id": parent_task_id,
        "attempt_count": attempt_count,
        "max_attempts": _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        "structured_reason": union_feedback,
    })
    return amendment_id

def _smoke_payload_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for issue in payload.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        for key in ("path", "file"):
            normalized = _v5r._normalize_contract_path(str(issue.get(key) or ""))
            if normalized:
                paths.append(normalized)
        for raw in issue.get("paths") or []:
            normalized = _v5r._normalize_contract_path(str(raw))
            if normalized:
                paths.append(normalized)
    clean_oracle_result = payload.get("clean_oracle_result")
    if isinstance(clean_oracle_result, dict):
        for issue in clean_oracle_result.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            for raw in issue.get("paths") or []:
                normalized = _v5r._normalize_contract_path(str(raw))
                if normalized:
                    paths.append(normalized)
    repair = payload.get("repair")
    if isinstance(repair, dict):
        for raw in repair.get("attempted_paths") or []:
            normalized = _v5r._normalize_contract_path(str(raw))
            if normalized:
                paths.append(normalized)
    return sorted(dict.fromkeys(paths))

def _smoke_payload_within_task_scope(
    payload: dict[str, Any],
    task: dict[str, Any],
) -> bool:
    issue_paths = _smoke_payload_paths(payload)
    owned_paths = _v5r._task_owned_paths(task)
    if not issue_paths or not owned_paths:
        return False
    return all(
        any(_v5r._path_overlaps(issue_path, owned) for owned in owned_paths)
        for issue_path in issue_paths
    )

def _smoke_repair_paths_for_contract(
    *,
    issue_paths: list[str],
    contract: dict[str, Any] | None,
) -> list[str]:
    if contract is None:
        return list(issue_paths)
    contract_path = _v5r._normalize_contract_path(str(contract.get("path") or ""))
    if not contract_path:
        return []
    if not all(
        _v5r._path_overlaps(path, contract_path) or _v5r._path_overlaps(contract_path, path)
        for path in issue_paths
    ):
        return []
    return list(issue_paths)

def _smoke_repair_unrouteable_feedback(
    *,
    child_task_id: str,
    parent_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    smoke_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "integration_smoke_unrouteable",
        "step_id": "integration_smoke_repair",
        "message": _v5r._preflight_blocking_summary(
            "Integration smoke failure is unrouteable because the clean-oracle payload has no issue paths",
            smoke_payload,
        ),
        "task_id": child_task_id,
        "parent_task_id": parent_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "paths": [],
        "repair_path": "",
        "owner_task_id": parent_task_id,
        "smoke_payload": smoke_payload,
        "_written_at": iso_timestamp(),
    }

def _smoke_repair_feedback(
    *,
    child_task_id: str,
    parent_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    smoke_payload: dict[str, Any],
    repair_path: str,
    repair_paths: list[str],
    owner_id: str,
    repair_route: str,
    event_name: str,
) -> dict[str, Any]:
    messages = [
        str(issue.get("message") or issue.get("kind"))
        for issue in smoke_payload.get("issues") or []
        if isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
    ]
    message = (
        "integration smoke detected an out-of-scope clean-deploy failure"
        + (": " + "; ".join(messages) if messages else "")
    )
    return {
        "kind": event_name,
        "step_id": repair_route,
        "message": message,
        "task_id": child_task_id,
        "parent_task_id": parent_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "paths": _smoke_payload_paths(smoke_payload),
        "repair_path": repair_path,
        "repair_paths": repair_paths,
        "owner_task_id": owner_id,
        "smoke_payload": smoke_payload,
        "_written_at": iso_timestamp(),
    }

def _schedule_smoke_repair_needed(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    smoke_payload: dict[str, Any],
    contract: dict[str, Any] | None,
    repair_paths: list[str],
    attempt_key: str,
    on_event: Any = None,
) -> str:
    owner_id = str((contract or {}).get("owner_task_id") or parent_task_id).strip()
    is_foundation = contract is not None
    repair_route = "foundation_contract_amendment" if is_foundation else "integration_smoke_repair"
    event_name = "foundation_repair_needed" if is_foundation else "integration_repair_needed"
    repair_paths = sorted(dict.fromkeys(
        path for path in (_v5r._normalize_contract_path(str(path)) for path in repair_paths) if path
    ))
    repair_path = repair_paths[0] if len(repair_paths) == 1 else ", ".join(repair_paths)
    attempt_count = _increment_contract_amendment_attempt(
        project_dir,
        child_task_id,
        attempt_key,
    )
    intent = (
        "Repair integration smoke clean-deploy failure for "
        f"`{repair_path or 'the integration surface'}` after leaf `{child_task_id}` "
        "detected an out-of-scope failure. Preserve the blocked leaf's scoped "
        "conflict repair and satisfy the integration smoke check."
    )
    amendment_id = enqueue_subtask(
        project_dir=project_dir,
        parent_task_id=parent_task_id,
        parent_session_dir=child_session_dir,
        intent=intent,
        owned_paths=repair_paths,
        task_role="contract_amendment",
        parent_integration_branch=parent_integration_branch,
    )
    record_task(
        project_dir,
        task_id=amendment_id,
        parent_task_id=parent_task_id,
        intent=intent,
        integration_branch=parent_integration_branch,
        owned_paths=repair_paths,
        task_role="contract_amendment",
    )
    feedback = _smoke_repair_feedback(
        child_task_id=child_task_id,
        parent_task_id=parent_task_id,
        parent_integration_branch=parent_integration_branch,
        source_branch=source_branch,
        pre_merge_ref=pre_merge_ref,
        smoke_payload=smoke_payload,
        repair_path=repair_path,
        repair_paths=repair_paths,
        owner_id=owner_id,
        repair_route=repair_route,
        event_name=event_name,
    )
    update_task_metadata(
        project_dir,
        amendment_id,
        contract_amendment={
            "contract_path": repair_path,
            "contract_paths": repair_paths,
            "owner_task_id": owner_id,
            "blocked_task_id": child_task_id,
            "source_branch": source_branch,
            "pre_merge_ref": pre_merge_ref,
            "attempt_count": attempt_count,
            "max_attempts": _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS,
            "repair_route": repair_route,
        },
        contract_amendment_path=repair_path,
        contract_amendment_paths=repair_paths,
        contract_amendment_owner_task_id=owner_id,
        repair_route=repair_route,
        smoke_repair_payload=smoke_payload,
    )
    set_contract_amendment_blocked(
        project_dir,
        child_task_id,
        amendment_id,
        reason=feedback["message"],
        merge_context={
            "child_session_dir": str(child_session_dir),
            "child_worktree": str(child_worktree),
            "parent_integration_branch": parent_integration_branch,
            "source_branch": source_branch,
            "pre_merge_ref": pre_merge_ref,
            "union_feedback": feedback,
        },
    )
    _v5r._emit(on_event, {
        "event": event_name,
        "task_id": child_task_id,
        "amendment_task_id": amendment_id,
        "repair_task_id": amendment_id,
        "repair_route": repair_route,
        "repair_path": repair_path,
        "owner_task_id": owner_id,
        "parent_task_id": parent_task_id,
        "attempt_count": attempt_count,
        "max_attempts": _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        "structured_reason": feedback,
    })
    return amendment_id

def _route_out_of_scope_smoke_failure(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    smoke_payload: dict[str, Any],
    result: LeadResult,
    on_event: Any = None,
) -> bool:
    if not _v5r._integration_smoke_blocks(smoke_payload):
        return False
    child = get_task(project_dir, child_task_id) or {}
    if _smoke_payload_within_task_scope(smoke_payload, child):
        return False
    parent_task_id = _v5r._parent_task_id_for_child(
        project_dir,
        child_task_id,
        parent_integration_branch,
    )
    feedback = {
        "paths": _smoke_payload_paths(smoke_payload),
        "integration_context": {"smoke_payload": smoke_payload},
    }
    contract = _foundation_contract_for_feedback_path(
        project_dir=project_dir,
        parent_task_id=parent_task_id,
        child_task_id=child_task_id,
        feedback=feedback,
    )
    issue_paths = _smoke_payload_paths(smoke_payload)
    repair_paths = _smoke_repair_paths_for_contract(issue_paths=issue_paths, contract=contract)
    if not repair_paths:
        unrouteable = _smoke_repair_unrouteable_feedback(
            child_task_id=child_task_id,
            parent_task_id=parent_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            pre_merge_ref=pre_merge_ref,
            smoke_payload=smoke_payload,
        )
        _v5r._record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=str(unrouteable["message"]),
            origin="integration_smoke_unrouteable",
            phase="integration_smoke_repair",
            structured_reason=unrouteable,
            on_event=on_event,
        )
        return True
    attempt_key = "|".join(repair_paths)
    current_attempts = _contract_amendment_attempt_count(child, attempt_key)
    if current_attempts >= _v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS:
        repair_path = repair_paths[0] if len(repair_paths) == 1 else ", ".join(repair_paths)
        exhausted = _contract_amendment_exhausted_feedback(
            child_task_id=child_task_id,
            parent_task_id=parent_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            pre_merge_ref=pre_merge_ref,
            contract_path=repair_path,
            owner_id=str((contract or {}).get("owner_task_id") or parent_task_id),
            union_feedback={
                "message": _v5r._preflight_blocking_summary(
                    "Integration smoke repair attempts exhausted",
                    smoke_payload,
                ),
                "smoke_payload": smoke_payload,
            },
            attempt_count=current_attempts,
        )
        _v5r._record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=str(exhausted["message"]),
            origin="contract_amendment",
            phase="integration_smoke_repair",
            structured_reason=exhausted,
            on_event=on_event,
        )
        return True
    _schedule_smoke_repair_needed(
        project_dir=project_dir,
        child_task_id=child_task_id,
        child_worktree=child_worktree,
        child_session_dir=child_session_dir,
        parent_task_id=parent_task_id,
        parent_integration_branch=parent_integration_branch,
        source_branch=source_branch,
        pre_merge_ref=pre_merge_ref,
        smoke_payload=smoke_payload,
        contract=contract,
        repair_paths=repair_paths,
        attempt_key=attempt_key,
        on_event=on_event,
    )
    return True

def _tasks_blocked_on_amendment(project_dir: Path, amendment_id: str) -> list[str]:
    graph = read_graph(project_dir)
    blocked: list[str] = []
    for task_id, task in (graph.get("tasks") or {}).items():
        if isinstance(task, dict) and task.get("blocked_on_task_id") == amendment_id:
            blocked.append(str(task_id))
    return blocked

def _settle_contract_amendment_dependents(
    *,
    project_dir: Path,
    amendment_id: str,
    amendment_result: LeadResult,
    completed: set[str],
    child_results: dict[str, LeadResult],
    on_event: Any = None,
) -> None:
    amendment = get_task(project_dir, amendment_id) or {}
    if str(amendment.get("task_role") or "") != "contract_amendment":
        return
    graph_verdict = str(amendment.get("verdict") or amendment_result.verdict or "")
    if graph_verdict == VERDICT_PASS:
        unblocked = clear_contract_amendment_blocked_tasks(project_dir, amendment_id)
        for leaf_id in unblocked:
            leaf = get_task(project_dir, leaf_id) or {}
            merge_context = leaf.get("contract_amendment_merge_context")
            if not isinstance(merge_context, dict):
                merge_context = {}
            completed.discard(leaf_id)
            child_results.pop(leaf_id, None)
            _enqueue_existing_task_for_merge_retry(
                project_dir=project_dir,
                task_id=leaf_id,
                parent_task_id=str(leaf.get("parent_task_id") or _v5r.ROOT_TASK_ID),
                parent_session_dir=Path(
                    str(merge_context.get("child_session_dir") or _paths.cross_sessions_dir(project_dir))
                ),
                intent=str(leaf.get("intent") or leaf_id),
                owned_paths=_v5r._task_owned_paths(leaf),
                task_role=str(leaf.get("task_role") or "feature"),
                parent_integration_branch=str(
                    merge_context.get("parent_integration_branch")
                    or leaf.get("integration_branch")
                    or "main"
                ),
            )
        _v5r._emit(on_event, {
            "event": "foundation_contract_amendment_unblocked",
            "amendment_task_id": amendment_id,
            "unblocked_task_ids": unblocked,
        })
        return

    if graph_verdict not in {
        VERDICT_MERGE_BLOCKED,
        VERDICT_CATASTROPHIC,
        VERDICT_PARTIAL,
        VERDICT_UNVERIFIED,
    }:
        return
    blocked = _tasks_blocked_on_amendment(project_dir, amendment_id)
    if not blocked:
        return
    reason = (
        "contract amendment failed before blocked leaf could retry merge: "
        f"{amendment_result.failure_reason or graph_verdict}"
    )
    structured_reason = {
        "kind": "contract_amendment_failed",
        "step_id": "foundation_contract_amendment",
        "message": reason,
        "amendment_task_id": amendment_id,
        "amendment_verdict": graph_verdict,
        "blocked_task_ids": blocked,
        "_written_at": iso_timestamp(),
    }
    for leaf_id in blocked:
        leaf_result = child_results.get(leaf_id) or LeadResult(
            task_id=leaf_id,
            verdict="merge_blocked",
            decomposition="inline",
        )
        _v5r._record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=leaf_id,
            result=leaf_result,
            reason=reason,
            origin="contract_amendment",
            structured_reason=structured_reason,
        )
        child_results[leaf_id] = leaf_result
        completed.add(leaf_id)
    clear_contract_amendment_blocked_state(project_dir, blocked)
    _v5r._emit(on_event, {
        "event": "foundation_contract_amendment_failed",
        "amendment_task_id": amendment_id,
        "blocked_task_ids": blocked,
        "structured_reason": structured_reason,
    })

def _persist_successful_contract_amendment_retry(
    *,
    project_dir: Path,
    task_id: str,
    verdict: str,
    cost_usd: float,
    on_event: Any = None,
) -> bool:
    latest = get_task(project_dir, task_id) or {}
    if latest.get("blocked_pending_contract_amendment") or latest.get("blocked_on_task_id"):
        return False
    if not latest.get("contract_amendment_retry_in_progress"):
        return False
    if str(latest.get("verdict") or "") in {VERDICT_MERGE_BLOCKED, VERDICT_CATASTROPHIC}:
        return False
    if latest.get("merge_blocked_structured_reason") or latest.get("merge_blocked_reason"):
        return False
    terminal_verdict = verdict if verdict in {VERDICT_PASS, VERDICT_PARTIAL, VERDICT_UNVERIFIED} else "pass"
    if not persist_contract_amendment_retry_success(
        project_dir,
        task_id,
        cast(Any, terminal_verdict),
        cost_usd=cost_usd,
    ):
        return False
    _v5r._emit(on_event, {
        "event": "contract_amendment_leaf_retry_verdict_restored",
        "task_id": task_id,
        "verdict": terminal_verdict,
    })
    return True

def _terminalize_stale_contract_amendment_retry_if_exhausted(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    on_event: Any = None,
) -> bool:
    latest = get_task(project_dir, task_id) or {}
    try:
        claim_count = int(latest.get("contract_amendment_retry_claim_count") or 0)
    except (TypeError, ValueError):
        claim_count = 0
    try:
        max_claims = int(latest.get("contract_amendment_retry_max_claims") or 2)
    except (TypeError, ValueError):
        max_claims = 2
    reason = (
        "contract amendment merge-only retry owner became stale and retry "
        f"claim budget was exhausted ({claim_count}/{max_claims}); refusing "
        "ordinary redispatch"
    )
    structured_reason = {
        "kind": "contract_amendment_retry_claims_exhausted",
        "step_id": "foundation_contract_amendment_retry",
        "message": reason,
        "task_id": task_id,
        "claim_count": claim_count,
        "max_claims": max_claims,
        "retry_owner": latest.get("contract_amendment_retry_owner"),
        "retry_owner_pid": latest.get("contract_amendment_retry_owner_pid"),
        "retry_owner_host": latest.get("contract_amendment_retry_owner_host"),
        "retry_heartbeat_at": latest.get("contract_amendment_retry_heartbeat_at"),
        "_written_at": iso_timestamp(),
    }
    if not terminalize_stale_contract_amendment_retry_if_exhausted(
        project_dir,
        task_id,
        reason=reason,
        structured_reason=structured_reason,
    ):
        return False
    result.verdict = "merge_blocked"
    result.failure_reason = reason
    result.verify_called = True
    result.verify_result = {
        "verdict": "merge_blocked",
        "summary": reason,
        "structured_reason": structured_reason,
    }
    _v5r._emit(on_event, {
        "event": "contract_amendment_leaf_retry_claims_exhausted",
        "task_id": task_id,
        "structured_reason": structured_reason,
    })
    return True

async def _refresh_contract_amendment_retry_heartbeat_until_stopped(
    *,
    project_dir: Path,
    task_id: str,
    owner_id: str,
) -> None:
    while True:
        await asyncio.sleep(_v5r.CONTRACT_AMENDMENT_RETRY_HEARTBEAT_INTERVAL_SECONDS)
        if not refresh_contract_amendment_retry_heartbeat(
            project_dir,
            task_id,
            owner_id=owner_id,
        ):
            return

def _conflict_packet_for_refusal(
    *,
    project_dir: Path,
    source: str,
    target: str,
) -> dict[str, Any]:
    packet = _read_latest_conflict_packet(project_dir)
    if not packet:
        return {}
    if packet.get("source_branch") != source or packet.get("target_branch") != target:
        return {}
    return packet

async def _repair_subtree_propagation_once(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    source: str,
    target: str,
    detail: str,
    config: dict[str, Any],
    on_event: Any = None,
) -> tuple[bool, str]:
    target_worktree = _v5r._worktree_for_branch(project_dir, target)
    conflict_packet = _conflict_packet_for_refusal(
        project_dir=project_dir,
        source=source,
        target=target,
    )
    paths = [
        str(path)
        for path in (conflict_packet.get("unmerged_paths") or _v5r._git_diff_name_only(target_worktree))
        if str(path)
    ]
    session_dir = _paths.cross_sessions_dir(project_dir)
    repair_slug = safe_slug(f"{task_id}-subtree-propagation", max_len=64)
    packet = _v5r._build_repair_packet(
        session_dir=session_dir,
        repair_slug=repair_slug,
        worktree_path=target_worktree,
        task_id=task_id,
        phase="subtree_propagation",
        repair_phase="subtree_propagation",
        verify_scope="subtree",
        config=config,
        budget_prefix="subtree_propagation_repair",
        default_agent_turns=DEFAULT_REPAIR_AGENT_TURNS,
        default_oracle_invocations=DEFAULT_REPAIR_ORACLE_INVOCATIONS,
        latest_oracle_result=lambda oracle_command: _v5r._merge_refusal_oracle_payload(
            worktree=target_worktree,
            scope="subtree",
            oracle_command=oracle_command,
            issue_kind="subtree_propagation_blocked",
            issue_message=detail,
            step_id="subtree_propagation_gate",
            paths=paths,
        ),
        product_contract={
            **_v5r._worktree_product_contract(worktree=target_worktree),
            "propagation": {
                "task_id": task_id,
                "source": source,
                "target": target,
                "detail": detail,
                "conflict_packet": conflict_packet,
            },
        },
        integration_context={
            "project_dir": str(project_dir),
            "task_id": task_id,
            "source": source,
            "target": target,
            "detail": detail,
            "conflict_packet": conflict_packet,
            "target_worktree": str(target_worktree),
            "integration_result": {
                "verdict": result.verdict,
                "verify_called": result.verify_called,
                "verify_result": result.verify_result,
                "failure_reason": result.failure_reason,
            },
            "propagation_safety": {
                "analyze_base_ours_theirs_per_path": True,
                "forbid_whole_side_checkout": True,
                "forbidden_commands": [
                    "git checkout --ours -- <whole-file>",
                    "git checkout --theirs -- <whole-file>",
                ],
            },
        },
        success_criteria={
            "subtree_propagation_retry": True,
            "source_reaches_target": True,
            "no_uncommitted_state": True,
            "no_conflict_markers": True,
        },
        attempt_history_entry={
            "type": "subtree_propagation_refusal",
            "detail": detail,
            "source": source,
            "target": target,
            "paths": paths,
            "conflict_packet": conflict_packet,
        },
        expected_artifact_paths=[],
        allowed_paths=paths,
        scope_policy="allowed_paths" if paths else "unrestricted",
        branch=target,
        repair_unit_extra={
            "source_branch": source,
            "target_branch": target,
            "conflicted_paths": paths,
        },
    )
    if packet.packet_path.exists():
        try:
            packet.agent_session_id = RepairPacket.load(packet.packet_path).agent_session_id
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    packet.persist()
    _v5r._emit(on_event, {
        "event": "subtree_propagation_repair_start",
        "task_id": task_id,
        "source": source,
        "target": target,
        "detail": detail,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_worktree

        feedback = _v5r._foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=task_id,
            parent_integration_branch=target,
            changed_paths=_v5r._git_diff_name_only(target_worktree),
            operation="subtree_propagation_repair_commit",
        )
        ok, commit_detail = commit_worktree(
            worktree_path=target_worktree,
            message=f"v5 subtree propagation repair: {task_id}",
        )
        if ok and feedback is not None:
            _v5r._record_foundation_contract_write_annotation(
                project_dir=project_dir,
                task_id=task_id,
                result=result,
                feedback=feedback,
                on_event=on_event,
            )
        return ok, commit_detail

    repair = await _v5r.run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _v5r._emit(on_event, {
        "event": "subtree_propagation_repair_done",
        "task_id": task_id,
        "ok": repair.verdict == VERDICT_PASS,
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
        "escalation": repair.escalation,
    })
    if repair.verdict != VERDICT_PASS:
        reason = (
            "subtree propagation repair did not pass: "
            f"{repair.summary}; original refusal: {detail}"
        )
        _v5r._record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=task_id,
            result=result,
            reason=reason,
            origin="subtree_propagation",
        )
        return False, reason
    return True, repair.summary

async def _run_child_verify_repair_packet(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    original_intent: str,
    result: LeadResult,
    config: dict[str, Any],
    max_parallel: int,
    run_started_at: float | None,
    spec_path: Path,
    on_event: Any = None,
    merge_gate_feedback: dict[str, Any] | None = None,
) -> Any:
    del max_parallel, run_started_at
    repair_slug = safe_slug(f"{child_task_id}-child-verify", max_len=64)
    verify_result = result.verify_result if isinstance(result.verify_result, dict) else {}
    diff_name_only = _v5r._git_diff_name_only(child_worktree)
    child_task = get_task(project_dir, child_task_id) or {}
    owned_paths = [str(path) for path in (child_task.get("owned_paths") or [])]
    gate_feedback = dict(merge_gate_feedback or {})
    feedback_integration_context = gate_feedback.get("integration_context")
    if not isinstance(feedback_integration_context, dict):
        feedback_integration_context = {}
    issue_kind = str(gate_feedback.get("kind") or "child_verdict_not_mergeable")
    issue_message = str(
        gate_feedback.get("message")
        or (
            "Child verdict is not mergeable; expected pass or an explicit "
            "reviewed_partial before upward merge"
        )
    )
    step_id = str(gate_feedback.get("step_id") or "child_merge_gate")
    feedback_paths = [
        str(path)
        for path in (gate_feedback.get("paths") or diff_name_only)
        if str(path)
    ]
    attempt_history_entry: dict[str, Any] = {
        "type": "pre_repair_verdict",
        "verdict": result.verdict,
        "verify_result": verify_result,
        "diff_stat": _v5r._git_diff_stat(child_worktree),
        "diff_name_only": diff_name_only,
    }
    if gate_feedback:
        attempt_history_entry = {
            "type": "upward_merge_gate_refusal",
            "detail": issue_message,
            "gate_feedback": gate_feedback,
            "verdict": result.verdict,
            "verify_result": verify_result,
            "diff_stat": _v5r._git_diff_stat(child_worktree),
            "diff_name_only": diff_name_only,
        }
    packet = _v5r._build_repair_packet(
        session_dir=child_session_dir,
        repair_slug=repair_slug,
        worktree_path=child_worktree,
        task_id=child_task_id,
        phase="child_verify",
        repair_phase="child_verify",
        verify_scope="subtree",
        config=config,
        budget_prefix="child_verify_repair",
        default_agent_turns=DEFAULT_REPAIR_AGENT_TURNS,
        default_oracle_invocations=DEFAULT_REPAIR_ORACLE_INVOCATIONS,
        latest_oracle_result=lambda oracle_command: _v5r._make_initial_oracle_payload(
            worktree=child_worktree,
            scope="subtree",
            oracle_command=oracle_command,
            issue_kind=issue_kind,
            issue_message=issue_message,
            step_id=step_id,
            paths=feedback_paths,
        ),
        product_contract={
            **_v5r._worktree_product_contract(worktree=child_worktree, spec_path=spec_path),
            "original_intent": original_intent,
        },
        integration_context={
            "parent_integration_branch": parent_integration_branch,
            "child_task": child_task,
            "child_verdict": {
                "verdict": result.verdict,
                "verify_called": result.verify_called,
                "verify_result": verify_result,
                "failure_reason": result.failure_reason,
            },
            "child_diff": {
                "stat": _v5r._git_diff_stat(child_worktree),
                "name_only": diff_name_only,
                "patch": _v5r._git_diff_full(child_worktree),
            },
            "upward_merge_gate_feedback": gate_feedback,
            "decomposition_runtime_context": {
                "spec_path": str(spec_path),
                "session_dir": str(child_session_dir),
            },
            **dict(feedback_integration_context),
        },
        success_criteria={
            "child_merge_gate": "pass_or_reviewed_partial",
            "no_uncommitted_state": True,
            "no_conflict_markers": True,
            **({"upward_merge_gate": "merge_into_parent_integration_passes"} if gate_feedback else {}),
        },
        attempt_history_entry=attempt_history_entry,
        expected_artifact_paths=[str(child_session_dir / "verdict.json")],
        allowed_paths=owned_paths,
        scope_policy="allowed_paths" if owned_paths else "unrestricted",
        repair_unit_extra={"canonical_verdict_path": str(child_session_dir / "verdict.json")},
    )
    if packet.packet_path.exists():
        try:
            packet.agent_session_id = RepairPacket.load(packet.packet_path).agent_session_id
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    packet.persist()
    _v5r._emit(on_event, {
        "event": "child_verify_repair_start",
        "task_id": child_task_id,
        "previous_verdict": result.verdict,
        "repair_packet": str(packet.packet_path),
        "reason": issue_kind,
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_worktree

        feedback = _v5r._foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            changed_paths=_v5r._git_diff_name_only(child_worktree),
            operation="child_verify_repair_commit",
        )
        ok, commit_detail = commit_worktree(
            worktree_path=child_worktree,
            message=f"v5 child verify repair: {child_task_id}",
        )
        if ok and feedback is not None:
            _v5r._record_foundation_contract_write_annotation(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                feedback=feedback,
                on_event=on_event,
            )
        return ok, commit_detail

    return await _v5r.run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )

def _carry_and_reset_prior_repair_packets(
    project_dir: Path,
    new_root_session_dir: Path,
) -> int:
    """Phase 1.2-B (2026-05-19): copy the most recent prior session's
    ``integration/repair/<unit>/repair_packet.json`` (+ archived events.jsonl)
    into the new session's mirror path, rewriting the serialized
    ``packet_dir`` field to the new location. Most-recent-per-unit
    wins. This lets ``_v5r._run_preflight_payload_repair_session``'s
    load-if-exists fire across runs → the repair agent's Claude SDK
    session is resumed (``options.resume=agent_session_id``) instead
    of starting fresh, eliminating the per-resume re-orientation
    overhead (the agent continues its prior conversation).

    Schema bug it fixes: ``packet_dir`` is serialized as a full path
    including the OLD ``session_dir``; without rewriting, a subsequent
    ``persist()`` would write back to the old location (research-phase-
    1.2-b.md). Codex was waived this session — extra-careful TDD via
    test_phase_1_2_b_carry_repair_packet.py.

    Also resets the per-attempt bookkeeping."""
    import shutil

    prior_packets = sorted(
        _paths.sessions_root(project_dir).glob(
            "*/integration/repair/*/repair_packet.json"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # most recent first
    )
    if not prior_packets:
        return 0

    seen_units: set[str] = set()
    carried = 0
    for prior_packet in prior_packets:
        unit_name = prior_packet.parent.name
        if unit_name in seen_units:
            continue
        seen_units.add(unit_name)
        # Self-copy guard (Path.is_relative_to is 3.9+; codebase is 3.12).
        try:
            if prior_packet.is_relative_to(new_root_session_dir):
                continue
        except (AttributeError, ValueError):  # noqa: BLE001 - belt-and-suspenders
            pass

        new_packet_dir = (
            new_root_session_dir / "integration" / "repair" / unit_name
        )
        new_packet_path = new_packet_dir / "repair_packet.json"
        try:
            payload = json.loads(prior_packet.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Phase 1.2-B: skipping unreadable prior packet %s: %s",
                prior_packet,
                exc,
            )
            continue
        # The schema-bug fix: rewrite packet_dir to the NEW location so
        # subsequent persist() goes there, not the prior session's path.
        payload["packet_dir"] = str(new_packet_dir)
        # Phase 1.2-B v2 (2026-05-20, live evidence): clear carried
        # packet bookkeeping so the new resume starts from current run
        # state. The active events file is intentionally not copied
        # below; prior events are archived for context without feeding
        # _replay_budget_usage(packet).
        payload["attempt_history"] = []
        payload["current_state"] = {}

        try:
            new_packet_dir.mkdir(parents=True, exist_ok=True)
            new_packet_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str)
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Phase 1.2-B: failed to write carried packet %s: %s",
                new_packet_path,
                exc,
            )
            continue

        prior_events = prior_packet.parent / "repair_packet.events.jsonl"
        if prior_events.is_file():
            try:
                shutil.copy2(
                    prior_events,
                    new_packet_dir / "prior_repair_packet.events.jsonl",
                )
            except OSError as exc:
                logger.warning(
                    "Phase 1.2-B: failed to archive events.jsonl from %s: %s",
                    prior_events,
                    exc,
                )
        carried += 1

    return carried

async def _run_plan_amendment_repair_packet(
    *,
    project_dir: Path,
    architect_tid: str,
    feedback: dict[str, Any],
    config: dict[str, Any],
    on_event: Any = None,
) -> Any:
    repair_slug = safe_slug(f"{architect_tid}-plan-amendment", max_len=64)
    packet = _v5r._build_repair_packet(
        session_dir=_paths.cross_sessions_dir(project_dir),
        repair_slug=repair_slug,
        worktree_path=project_dir,
        task_id=architect_tid,
        phase="plan_amendment",
        repair_phase="plan_amendment",
        verify_scope="subtree",
        config=config,
        budget_prefix="plan_amendment_repair",
        default_agent_turns=DEFAULT_REPAIR_AGENT_TURNS,
        # Plan amendment has no clean_verify oracle re-run loop — the
        # "oracle" here is a single feedback payload with one issue
        # (the plan amendment itself), so 1 invocation is correct.
        # All other repair phases use 3 (their oracle re-runs after fixes).
        default_oracle_invocations=1,
        latest_oracle_result={
            "passed": False,
            "issues": [{
                "kind": str(feedback.get("kind") or "plan_amendment_needed"),
                "severity": "block",
                "message": str(feedback.get("message") or "plan amendment needed"),
            }],
            "feedback": feedback,
        },
        product_contract=_v5r._worktree_product_contract(worktree=project_dir),
        integration_context={
            "architect_task_id": architect_tid,
            "feedback": feedback,
            "task_graph_path": str(task_graph_path(project_dir)),
            "pending_path": str(v5_pending_path(project_dir)),
        },
        success_criteria={
            "plan_amendment_only": True,
            "no_full_architect_redispatch": True,
            "foundation_isolation_feedback_clears": True,
        },
        attempt_history_entry={
            "type": "plan_amendment_feedback",
            "feedback": feedback,
        },
        allowed_paths=[
            "CHARTER.md",
            str(task_graph_path(project_dir).relative_to(project_dir)),
            str(v5_pending_path(project_dir).relative_to(project_dir)),
        ],
        scope_policy="allowed_paths",
        repair_unit_extra={"prompt_template": "plan-amendment.md"},
    )
    _v5r._emit(on_event, {
        "event": "plan_amendment_repair_start",
        "task_id": architect_tid,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        changed_paths = _v5r._git_diff_name_only(project_dir)
        scope_feedback = _v5r._allowed_paths_write_feedback(
            acting_task_id=architect_tid,
            changed_paths=changed_paths,
            allowed_paths=list(packet.repair_unit.get("allowed_paths") or []),
            scope_policy="allowed_paths",
            operation="plan_amendment_commit",
        )
        if scope_feedback is not None:
            return False, _v5r._foundation_contract_write_block_detail(scope_feedback)
        return _v5r._commit_runner_output_paths(
            worktree_path=project_dir,
            paths=[path for path in changed_paths if path == "CHARTER.md"],
            message="chore(otto): amend v5 ownership partition",
        )

    repair = await _v5r.run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _v5r._emit(on_event, {
        "event": "plan_amendment_repair_done",
        "task_id": architect_tid,
        "verdict": repair.verdict,
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
    })
    return repair

async def _reenter_or_block_architect_contract(
    *,
    project_dir: Path,
    architect_tid: str,
    child_results: dict[str, LeadResult],
    completed: set[str],
    feedback: dict[str, Any],
    origin: str,
    config: dict[str, Any] | None = None,
    on_event: Any = None,
) -> bool:
    parent_id = str(feedback.get("parent_task_id") or "").strip()
    if (
        feedback.get("kind") == "shared_foundation_not_isolated"
        and parent_id
        and _v5r._unambiguous_foundation_contract_overlap_findings(feedback)
    ):
        rescope = _v5r._remove_feature_owned_foundation_contract_paths(
            project_dir=project_dir,
            feedback=feedback,
        )
        if rescope is not None:
            contracts = _v5r._foundation_contracts_for_parent(
                project_dir,
                parent_id,
                read_graph(project_dir).get("tasks") or {},
            )
            followup = _v5r._foundation_isolation_feedback(
                parent_task_id=parent_id,
                architect_task_id=architect_tid,
                tasks=read_graph(project_dir).get("tasks") or {},
                contracts=contracts,
            )
            _v5r._emit(on_event, {
                "event": "architect_contract_rescoped",
                "task_id": architect_tid,
                "structured_reason": rescope,
                "followup_feedback": followup,
            })
            if followup is None:
                return False
            feedback = followup

    if (
        feedback.get("kind") == "shared_foundation_not_isolated"
        and _v5r._feature_overlap_findings(feedback)
        and config is not None
    ):
        repair = await _run_plan_amendment_repair_packet(
            project_dir=project_dir,
            architect_tid=architect_tid,
            feedback=feedback,
            config=config,
            on_event=on_event,
        )
        if repair.verdict == VERDICT_PASS and parent_id:
            contracts = _v5r._foundation_contracts_for_parent(
                project_dir,
                parent_id,
                read_graph(project_dir).get("tasks") or {},
            )
            followup = _v5r._foundation_isolation_feedback(
                parent_task_id=parent_id,
                architect_task_id=architect_tid,
                tasks=read_graph(project_dir).get("tasks") or {},
                contracts=contracts,
            )
            if followup is None:
                return False
            feedback = followup
        else:
            result = child_results.get(architect_tid) or LeadResult(
                task_id=architect_tid,
                verdict="merge_blocked",
            )
            completed.discard(architect_tid)
            reason = (
                "Plan-amendment repair did not clear feature ownership overlap: "
                f"{getattr(repair, 'summary', '')}"
            )
            _v5r._record_task_merge_blocked_reason(
                project_dir=project_dir,
                task_id=architect_tid,
                result=result,
                reason=reason,
                origin=origin,
                structured_reason=feedback,
            )
            child_results[architect_tid] = result
            return True

    # Phase 5 (2026-05-19): NO fresh-Lead re-decomposition. Architect
    # contract failure lands `partial`+annotation via the chokepoint
    # (Part A) immediately — uniform for first-time and N-th occurrence.
    # The retry branch (clear_verdict_for_retry + child_results.pop)
    # was the cascade trigger and is gone. Scheduler restarts via the
    # outer-while loop (return True) on the architect's new verdict.
    reason = _v5r._architect_contract_feedback_reason(feedback)
    result = child_results.get(architect_tid) or LeadResult(
        task_id=architect_tid,
        verdict="partial",
    )
    completed.discard(architect_tid)
    _v5r._record_task_merge_blocked_reason(
        project_dir=project_dir,
        task_id=architect_tid,
        result=result,
        reason=reason,
        origin=origin,
        structured_reason=feedback,
    )
    child_results[architect_tid] = result
    _v5r._emit(on_event, {
        "event": "architect_contract_landed_partial",
        "task_id": architect_tid,
        "structured_reason": feedback,
    })
    return True

async def _run_scaffold_repair_packet(
    *,
    project_dir: Path,
    architect_tid: str,
    architect_task: dict[str, Any],
    latest_result: CleanOracleResult,
    result: LeadResult,
    config: dict[str, Any],
    on_event: Any = None,
) -> Any:
    repair_slug = safe_slug(f"{architect_tid}-scaffold", max_len=64)
    owned_paths = [str(path) for path in (architect_task.get("owned_paths") or [])]
    packet = _v5r._build_repair_packet(
        session_dir=_paths.cross_sessions_dir(project_dir),
        repair_slug=repair_slug,
        worktree_path=project_dir,
        task_id=architect_tid,
        phase="scaffold",
        repair_phase="scaffold",
        verify_scope="scaffold",
        config=config,
        budget_prefix="scaffold_repair",
        default_agent_turns=DEFAULT_REPAIR_AGENT_TURNS,
        default_oracle_invocations=DEFAULT_REPAIR_ORACLE_INVOCATIONS,
        latest_oracle_result=latest_result.to_jsonable(),
        product_contract={
            **_v5r._worktree_product_contract(worktree=project_dir),
            "architect_task": architect_task,
        },
        integration_context={
            "architect_task_id": architect_tid,
            "architect_task": architect_task,
            "scaffold_oracle": latest_result.to_jsonable(),
            "current_diff": {
                "stat": _v5r._git_diff_stat(project_dir),
                "name_only": _v5r._git_diff_name_only(project_dir),
                "patch": _v5r._git_diff_full(project_dir),
            },
        },
        success_criteria={
            "scaffold_scope": True,
            "no_uncommitted_state": True,
            "no_conflict_markers": True,
        },
        attempt_history_entry={
            "type": "scaffold_oracle_failure",
            "oracle_result": latest_result.to_jsonable(),
            "diff_stat": _v5r._git_diff_stat(project_dir),
            "diff_name_only": _v5r._git_diff_name_only(project_dir),
        },
        allowed_paths=owned_paths,
        scope_policy="allowed_paths" if owned_paths else "unrestricted",
    )
    _v5r._emit(on_event, {
        "event": "scaffold_repair_start",
        "task_id": architect_tid,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_integration_worktree

        feedback = _v5r._foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=architect_tid,
            parent_integration_branch=str(architect_task.get("integration_branch") or "main"),
            changed_paths=_v5r._git_diff_name_only(project_dir),
            operation="scaffold_repair_commit",
        )
        ok, commit_detail = commit_integration_worktree(
            worktree_path=project_dir,
            task_id=f"{architect_tid}-scaffold-repair",
        )
        if ok and feedback is not None:
            _v5r._record_foundation_contract_write_annotation(
                project_dir=project_dir,
                task_id=architect_tid,
                result=result,
                feedback=feedback,
                on_event=on_event,
            )
        return ok, commit_detail

    repair = await _v5r.run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _v5r._emit(on_event, {
        "event": "scaffold_repair_done",
        "task_id": architect_tid,
        "verdict": repair.verdict,
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
    })
    return repair

def _preserve_timed_out_repair_work(project_dir: Path) -> tuple[bool, str]:
    """Phase 1.2 / Task #8: a timed-out/escalated integration-repair agent
    may have left near-complete work uncommitted (Linkboard e2e: a
    tsc-clean BookmarksPage.tsx fix killed mid `git add -A && git commit`
    at the 1199s wall, then discarded). Preserve it — commit the worktree
    the agent was killed mid-committing so the work LANDS (Part A makes the
    terminal `partial`+annotated) instead of being thrown away. Locked
    invariant: bugs are acceptable output; discarded work is not."""
    from otto.v5_branching import commit_worktree, git_status_porcelain

    dirty = git_status_porcelain(project_dir)
    if not dirty:
        return False, "worktree clean — nothing to preserve"
    ok, detail = commit_worktree(
        worktree_path=project_dir,
        message=(
            "otto: preserve timed-out integration-repair work "
            "(Phase 1.2 Task #8)"
        ),
    )
    return bool(ok), str(detail)

def _child_repair_helper_crashed_feedback(
    *,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    origin: str,
    phase: str,
    exc: Exception,
    previous_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": "child_repair_helper_crashed",
        "step_id": phase,
        "message": f"{origin} crashed: {type(exc).__name__}: {exc}",
        "task_id": child_task_id,
        "origin": origin,
        "phase": phase,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "exception": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
        "_written_at": iso_timestamp(),
    }
    if previous_feedback is not None:
        feedback["previous_gate_feedback"] = previous_feedback
    return feedback

async def _repair_child_upward_merge_gate_once(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    result: LeadResult,
    config: dict[str, Any],
    original_detail: str,
    on_event: Any = None,
    gate_feedback: dict[str, Any] | None = None,
    origin: str = "upward_merge_gate",
) -> tuple[bool, str]:
    spec_path = child_session_dir / "spec" / "spec.json"
    paths = _v5r._git_diff_name_only(child_worktree)
    feedback = dict(gate_feedback or {})
    feedback.setdefault("kind", "upward_merge_gate_blocked")
    feedback.setdefault("step_id", "upward_merge_gate")
    feedback.setdefault("message", original_detail)
    feedback.setdefault("paths", paths)
    feedback.setdefault("parent_integration_branch", parent_integration_branch)
    feedback.setdefault("_written_at", iso_timestamp())
    _v5r._emit(on_event, {
        "event": "upward_merge_gate_repair_start",
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "detail": original_detail,
        "reason": feedback.get("kind"),
    })
    repair = await _run_child_verify_repair_packet(
        project_dir=project_dir,
        child_task_id=child_task_id,
        child_worktree=child_worktree,
        child_session_dir=child_session_dir,
        parent_integration_branch=parent_integration_branch,
        original_intent=(get_task(project_dir, child_task_id) or {}).get("intent", ""),
        result=result,
        config=config,
        max_parallel=1,
        run_started_at=None,
        spec_path=spec_path,
        on_event=on_event,
        merge_gate_feedback=feedback,
    )
    _v5r._emit(on_event, {
        "event": "upward_merge_gate_repair_done",
        "task_id": child_task_id,
        "ok": repair.verdict == VERDICT_PASS,
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
        "escalation": repair.escalation,
    })
    if repair.verdict != VERDICT_PASS:
        reason = (
            f"{origin} repair did not pass: "
            f"{repair.summary}; original refusal: {original_detail}"
        )
        return False, reason
    return True, repair.summary

def _stale_target_gate_feedback(
    *,
    project_dir: Path,
    child_task_id: str,
    parent_integration_branch: str,
    detail: str,
    prior_repair_detail: str,
    origin: str,
    previous_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from otto.v5_branching import child_branch_name

    source_branch = child_branch_name(child_task_id)
    base_ref = _v5r._git_capture(project_dir, ["merge-base", parent_integration_branch, source_branch])
    target_head = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
    source_head = _v5r._git_capture(project_dir, ["rev-parse", source_branch])
    feedback: dict[str, Any] = {
        "kind": "stale_integration_target_after_repair",
        "step_id": "child_merge_retry",
        "message": detail,
        "paths": _v5r._git_changed_paths_between_refs(project_dir, base_ref, source_branch)
        if base_ref
        else [],
        "parent_integration_branch": parent_integration_branch,
        "prior_repair_detail": prior_repair_detail,
        "origin": origin,
        "_written_at": iso_timestamp(),
        "integration_context": {
            "merge_refs": {
                "base_ref": base_ref,
                "ours_ref": parent_integration_branch,
                "ours_head": target_head,
                "theirs_ref": source_branch,
                "theirs_head": source_head,
            },
            "stale_target_gate": {
                "kind": "stale_integration_target_after_repair",
                "detail": detail,
                "prior_repair_detail": prior_repair_detail,
                "previous_feedback": previous_feedback or {},
            },
        },
    }
    if previous_feedback is not None:
        feedback["previous_gate_feedback"] = previous_feedback
    return feedback

async def _repair_child_stale_target_gate_once(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    result: LeadResult,
    config: dict[str, Any],
    detail: str,
    prior_repair_detail: str,
    origin: str,
    previous_feedback: dict[str, Any] | None = None,
    on_event: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    feedback = _stale_target_gate_feedback(
        project_dir=project_dir,
        child_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        detail=detail,
        prior_repair_detail=prior_repair_detail,
        origin=origin,
        previous_feedback=previous_feedback,
    )
    try:
        repaired, repair_detail = await _v5r._repair_child_upward_merge_gate_once(
            project_dir=project_dir,
            child_task_id=child_task_id,
            child_worktree=child_worktree,
            child_session_dir=child_session_dir,
            parent_integration_branch=parent_integration_branch,
            result=result,
            config=config,
            original_detail=detail,
            on_event=on_event,
            gate_feedback=feedback,
            origin=origin,
        )
    except Exception as exc:  # noqa: BLE001 - terminal block must stay structured
        repaired = False
        repair_detail = (
            "stale target repair crashed: "
            f"{type(exc).__name__}: {exc}"
        )
    return repaired, repair_detail, feedback

@dataclass(frozen=True)
class _UpwardMergeRetryResult:
    """Result of `_repair_child_upward_merge_after_failure`.

    This was renamed from the historical stale-target helper name; the old
    name is gone. This is NOT a cheap re-fetch + retry. The function runs a
    full Lead repair agent (~200-300s) against the child task to resolve
    upward-merge-gate blockers, then attempts the merge once. The old
    "stale_target" framing came from an earlier model where the only
    failure mode was a stale parent ref; today it covers any
    upward-merge-gate failure including real semantic conflicts. Audit ref:
    audit3-repair-loops.md.
    """

    ok: bool
    detail: str
    pre_merge_ref: str
    terminal_recorded: bool = False

async def _repair_child_upward_merge_after_failure(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    result: LeadResult,
    config: dict[str, Any],
    detail: str,
    prior_repair_detail: str,
    origin: str,
    terminal_phase: str,
    source_branch: str,
    previous_feedback: dict[str, Any] | None = None,
    run_smoke_preflight: bool = False,
    check_union_after_merge: bool = False,  # noqa: ARG001 — surface kept for compat; see docstring
    emit_union_feedback: bool = False,
    on_event: Any = None,
) -> _UpwardMergeRetryResult:
    """Re-enter the existing child repair loop, retry merge, and own terminal blocks."""
    from otto.v5_branching import merge_child_into_integration

    if emit_union_feedback and previous_feedback is not None:
        union_detail = str(
            previous_feedback.get("message")
            or _v5r._integration_union_reason_text(previous_feedback)
        )
        _v5r._emit(on_event, {
            "event": "integration_union_incomplete",
            "task_id": child_task_id,
            "into": parent_integration_branch,
            "detail": union_detail,
            "structured_reason": previous_feedback,
            "after_repair": True,
        })

    stale_repaired, stale_detail, stale_feedback = await _v5r._repair_child_stale_target_gate_once(
        project_dir=project_dir,
        child_task_id=child_task_id,
        child_worktree=child_worktree,
        child_session_dir=child_session_dir,
        parent_integration_branch=parent_integration_branch,
        result=result,
        config=config,
        detail=detail,
        prior_repair_detail=prior_repair_detail,
        origin=origin,
        previous_feedback=previous_feedback,
        on_event=on_event,
    )

    def record_terminal(
        *,
        reason: str,
        structured_reason: dict[str, Any],
    ) -> _UpwardMergeRetryResult:
        _v5r._record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=reason,
            origin=origin,
            phase=terminal_phase,
            structured_reason=structured_reason,
            on_event=on_event,
        )
        return _UpwardMergeRetryResult(
            ok=False,
            detail=reason,
            pre_merge_ref="",
            terminal_recorded=True,
        )

    if not stale_repaired:
        reason = (
            f"{detail}; stale target repair attempt: {stale_detail}; "
            f"prior repair attempt: {prior_repair_detail}"
        )
        return record_terminal(reason=reason, structured_reason=stale_feedback)

    pre_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])
    if not pre_merge_ref:
        reason = (
            "stale target retry could not resolve integration pre-merge ref; "
            f"stale target repair attempt: {stale_detail}; "
            f"prior repair attempt: {prior_repair_detail}"
        )
        feedback = _v5r._pre_merge_ref_unresolved_feedback(
            kind="stale_target_pre_merge_ref_unresolved",
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            detail=reason,
            prior_repair_detail=prior_repair_detail,
            previous_feedback=previous_feedback,
            stale_feedback=stale_feedback,
        )
        return record_terminal(reason=reason, structured_reason=feedback)
    stale_target_contract_violation = _v5r._foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_v5r._git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
        operation="stale_target_retry_merge_delta",
    )
    try:
        ok, merge_detail = merge_child_into_integration(
            project_dir=project_dir,
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
        )
    except MergeWorktreeDirtyError as exc:
        ok = False
        merge_detail = str(exc)
    except Exception as exc:  # noqa: BLE001 - retry merge failure must stay terminal-structured
        ok = False
        merge_detail = f"stale target retry merge crashed: {type(exc).__name__}: {exc}"

    if ok and stale_target_contract_violation is not None:
        _v5r._record_foundation_contract_write_annotation(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            feedback=stale_target_contract_violation,
            phase="stale_target_retry_annotation",
            on_event=on_event,
        )

    if ok and run_smoke_preflight:
        try:
            oracle = _v5r._run_integration_smoke_preflight(
                worktree_path=project_dir,
                task_id=child_task_id,
                phase="child_merge_conflict_repair",
                spec_path=child_session_dir / "spec" / "spec.json",
                journey_artifact_dir=(
                    child_session_dir
                    / "journeys"
                    / safe_slug("child_merge_conflict_repair", max_len=48)
                ),
                on_event=on_event,
            )
            if _v5r._preflight_repair_escalated(oracle) or _v5r._integration_smoke_blocks(oracle):
                if _route_out_of_scope_smoke_failure(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    child_worktree=child_worktree,
                    child_session_dir=child_session_dir,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    smoke_payload=oracle,
                    result=result,
                    on_event=on_event,
                ):
                    return _UpwardMergeRetryResult(
                        ok=False,
                        detail=_v5r._preflight_blocking_summary(
                            "Child merge conflict repair smoke routed to owner",
                            oracle,
                        ),
                        pre_merge_ref=pre_merge_ref,
                        terminal_recorded=True,
                    )
                oracle = await _v5r._run_integration_smoke_preflight_with_repair(
                    project_dir=project_dir,
                    worktree_path=project_dir,
                    task_id=child_task_id,
                    phase="child_merge_conflict_repair",
                    session_dir=child_session_dir,
                    config=config,
                    integration_branch=parent_integration_branch,
                    allowed_paths=_v5r._task_owned_paths(get_task(project_dir, child_task_id) or {}),
                    scope_policy="allowed_paths",
                    on_event=on_event,
                )
                if _v5r._preflight_repair_escalated(oracle) or _v5r._integration_smoke_blocks(oracle):
                    ok = False
                    merge_detail = _v5r._preflight_blocking_summary(
                        "Child merge conflict repair smoke oracle failed",
                        oracle,
                    )
        except Exception as exc:  # noqa: BLE001 - keep stale-target terminal structured
            ok = False
            merge_detail = (
                "Child merge conflict repair smoke oracle crashed: "
                f"{type(exc).__name__}: {exc}"
            )

    if not ok:
        reason = (
            f"{merge_detail}; stale target repair attempt: {stale_detail}; "
            f"prior repair attempt: {prior_repair_detail}"
        )
        return record_terminal(reason=reason, structured_reason=stale_feedback)

    if check_union_after_merge:
        try:
            followup_feedback = _v5r._record_and_check_integration_union(
                project_dir=project_dir,
                parent_integration_branch=parent_integration_branch,
                child_task_id=child_task_id,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
            )
        except Exception as exc:  # noqa: BLE001 - keep terminal block structured
            feedback = _v5r._integration_union_guard_error_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                exc=exc,
                previous_feedback=previous_feedback,
                stale_feedback=stale_feedback,
            )
            reason = str(feedback["message"])
            return record_terminal(reason=reason, structured_reason=feedback)
        if followup_feedback is not None:
            followup_detail = str(
                followup_feedback.get("message")
                or _v5r._integration_union_reason_text(followup_feedback)
            )
            terminal_feedback = dict(followup_feedback)
            terminal_feedback["stale_target_repair"] = {
                "kind": "stale_integration_target_after_repair",
                "repair_detail": stale_detail,
                "prior_repair_detail": prior_repair_detail,
                "stale_feedback": stale_feedback,
                "_written_at": iso_timestamp(),
            }
            reason = (
                f"{followup_detail}; stale target repair attempt: {stale_detail}; "
                f"prior repair attempt: {prior_repair_detail}"
            )
            return record_terminal(reason=reason, structured_reason=terminal_feedback)

    return _UpwardMergeRetryResult(
        ok=True,
        detail=merge_detail,
        pre_merge_ref=pre_merge_ref,
    )

def _looks_like_merge_conflict(detail: str) -> bool:
    lowered = detail.lower()
    return "conflict on:" in lowered or "merge conflict" in lowered

def _read_latest_conflict_packet(project_dir: Path) -> dict[str, Any]:
    from otto.v5_branching import latest_conflict_packet_path

    path = latest_conflict_packet_path(project_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable merge conflict packet %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}

async def _repair_child_merge_conflict_once(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    result: LeadResult,
    config: dict[str, Any],
    original_detail: str,
    on_event: Any = None,
) -> tuple[bool, str]:
    conflict_packet = _read_latest_conflict_packet(project_dir)
    paths = [str(path) for path in (conflict_packet.get("unmerged_paths") or ()) if str(path)]
    conflict_packet_path = ""
    try:
        from otto.v5_branching import latest_conflict_packet_path

        conflict_packet_path = str(latest_conflict_packet_path(project_dir))
    except Exception:  # noqa: BLE001
        conflict_packet_path = ""
    repair_slug = safe_slug(f"{child_task_id}-merge-conflict", max_len=64)
    source_branch = str(conflict_packet.get("source_branch") or "")
    target_branch = str(conflict_packet.get("target_branch") or parent_integration_branch)
    if not source_branch:
        from otto.v5_branching import child_branch_name

        source_branch = child_branch_name(child_task_id)
    base_ref = _v5r._git_capture(project_dir, ["merge-base", target_branch, source_branch])
    packet = _v5r._build_repair_packet(
        session_dir=child_session_dir,
        repair_slug=repair_slug,
        worktree_path=child_worktree,
        task_id=child_task_id,
        phase="merge",
        repair_phase="merge",
        verify_scope="subtree",
        config=config,
        budget_prefix="merge_repair",
        default_agent_turns=DEFAULT_REPAIR_AGENT_TURNS,
        default_oracle_invocations=DEFAULT_REPAIR_ORACLE_INVOCATIONS,
        latest_oracle_result=lambda oracle_command: _v5r._make_initial_oracle_payload(
            worktree=child_worktree,
            scope="subtree",
            oracle_command=oracle_command,
            issue_kind="merge_conflict",
            issue_message=original_detail,
            step_id="merge_retry",
            paths=paths,
        ),
        product_contract={
            **_v5r._worktree_product_contract(worktree=child_worktree),
            "contract_deltas": {
                "target_to_source_diff_stat": _v5r._git_diff_stat_for_ref_range(
                    project_dir,
                    target_branch,
                    source_branch,
                ),
                "source_to_target_diff_stat": _v5r._git_diff_stat_for_ref_range(
                    project_dir,
                    source_branch,
                    target_branch,
                ),
            },
        },
        integration_context={
            "parent_integration_branch": parent_integration_branch,
            "original_detail": original_detail,
            "conflict_packet_path": conflict_packet_path,
            "conflict_packet": conflict_packet,
            "merge_refs": {
                "base_ref": base_ref,
                "ours_ref": target_branch,
                "theirs_ref": source_branch,
            },
            "merge_safety": {
                "resolve_on_source_child_branch": True,
                "analyze_base_ours_theirs_per_path": True,
                "forbid_whole_side_checkout": True,
                "forbidden_commands": [
                    "git checkout --ours -- <whole-file>",
                    "git checkout --theirs -- <whole-file>",
                ],
            },
            "child_diff": {
                "stat": _v5r._git_diff_stat_for_ref_range(project_dir, base_ref, source_branch),
                "name_only": paths,
            },
        },
        success_criteria={
            "merge_retry": True,
            "three_way_conflicts_resolved": True,
            "no_whole_side_checkout": True,
        },
        attempt_history_entry={
            "type": "merge_conflict_detected",
            "detail": original_detail,
            "conflict_packet": conflict_packet_path,
            "unmerged_paths": paths,
        },
        expected_artifact_paths=[conflict_packet_path] if conflict_packet_path else [],
        allowed_paths=paths,
        scope_policy="allowed_paths" if paths else "unrestricted",
        repair_unit_extra={"conflicted_paths": paths},
    )
    _v5r._emit(on_event, {
        "event": "merge_conflict_repair_agent_start",
        "task_id": child_task_id,
        "paths": list(paths),
        "conflict_packet": conflict_packet_path,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_worktree

        feedback = _v5r._foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            changed_paths=_v5r._git_diff_name_only(child_worktree),
            operation="merge_conflict_repair_commit",
        )
        ok, commit_detail = commit_worktree(
            worktree_path=child_worktree,
            message=f"v5 merge conflict repair: {child_task_id}",
        )
        if ok and feedback is not None:
            _v5r._record_foundation_contract_write_annotation(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                feedback=feedback,
                on_event=on_event,
            )
        return ok, commit_detail

    repair = await _v5r.run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _v5r._emit(on_event, {
        "event": "merge_conflict_repair_agent_done",
        "task_id": child_task_id,
        "ok": repair.verdict == VERDICT_PASS,
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
    })
    return repair.verdict == VERDICT_PASS, repair.summary
