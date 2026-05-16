"""v5 hierarchical run coordinator.

Drives a full v5 run end-to-end:
    1. Compile flat spec at root.
    2. Run root Lead.
    3. If root emitted children: process the v5_pending queue, dispatching
       children up to a concurrency cap, respecting depends_on.
    4. When all children of a parent resolve, spawn an integration Lead for
       that parent.
    5. Continue until root has its own verdict.
    6. Render proof packet + summary.

Phase 2 design notes:

- Children run in-process (asyncio tasks), not as subprocess. This is simpler
  than spawning fresh `otto v5 run-child` subprocesses and works at the scale
  v5 targets. If we hit context-budget issues with deep trees, Phase 4 can
  revisit subprocess isolation.

- Per-parent integration branches: ``i2p/<parent_task_id>/integration``.
  Children's worktrees are NOT physically separate yet — Phase 2 keeps
  children operating on the same project_dir for simplicity. Real worktrees
  are wired in Phase 2.5 if needed.

- Best-effort everywhere: any child crash → its verdict becomes catastrophic;
  parent's integration runs anyway with whatever children produced.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import re
import shutil
import subprocess
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any, cast

from otto import paths as _paths
from otto.journey_scope_policy import ExecutionScope
from otto.lead import LeadKind, LeadResult, run_lead
from otto.safe_slug import safe_slug
from otto.v5_preflight import (
    filter_blocked_descendants,
    run_preflight,
    preflight_issues_from_clean_oracle,
)
from otto.v5_preflight_repair import (
    RepairBudget,
    RepairPacket,
    run_oracle_repair_agent,
)
from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
    Scope,
    build_clean_verify_oracle_command,
    verify_from_clean_oracle,
)
from otto.queue.subtask import (
    append_pending_entry,
    enqueue_subtask,
    read_pending,
    take_ready,
    v5_pending_path,
    _verdict_satisfies_dependency,
)
from otto.queue.task_graph import (
    aggregate_verdict,
    children_of,
    clear_contract_amendment_blocked_state,
    clear_contract_amendment_blocked_tasks,
    clear_verdict_for_retry,
    get_retry_count,
    get_retry_reason,
    get_task,
    mark_contract_amendment_retry_in_progress,
    mark_reviewed_partial,
    persist_contract_amendment_retry_success,
    read_graph,
    record_task,
    set_contract_amendment_blocked,
    set_verdict,
    set_verdict_and_metadata,
    tree_total_cost,
    update_task_metadata,
)
from otto.spec_compile_flat import (
    FlatSpec,
    SpecContractRepairExhaustedError,
    compile_flat_spec,
)
from otto.v5_branching import MergeWorktreeDirtyError

logger = logging.getLogger("otto.v5_runner")

ROOT_TASK_ID = "root"

# When scaffold preflight invalidates an architect's self-declared pass,
# the runner re-dispatches the architect with the failure summary
# prepended to its intent. This is the cap on those retries (architect
# is allowed 1 original attempt + ``MAX_ARCHITECT_RETRIES`` re-runs).
MAX_ARCHITECT_RETRIES = 2
MAX_CONTRACT_AMENDMENT_ATTEMPTS = 2


class _DispatchLease:
    """Shared per-run child dispatch capacity.

    ``_process_children`` can be entered recursively, and tests can exercise
    two scheduler loops concurrently. Local ``in_flight`` dicts are not enough
    in that shape: every loop sees its own capacity. This lease is the single
    in-process source of truth for active child runs.
    """

    def __init__(self, max_parallel: int) -> None:
        self.max_parallel = max(1, int(max_parallel or 1))
        self._active: set[str] = set()
        self._condition = asyncio.Condition()

    async def active_task_ids(self) -> set[str]:
        async with self._condition:
            return set(self._active)

    async def try_acquire(self, task_id: str) -> bool:
        async with self._condition:
            if task_id in self._active:
                return False
            if len(self._active) >= self.max_parallel:
                return False
            self._active.add(task_id)
            return True

    async def release(self, task_id: str) -> None:
        async with self._condition:
            if task_id in self._active:
                self._active.remove(task_id)
                self._condition.notify_all()

    async def wait_for_change(self, timeout_s: float = 0.25) -> None:
        async with self._condition:
            try:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return


@dataclass
class V5RunResult:
    """Top-level result of a v5 run."""

    root_task_id: str = ROOT_TASK_ID
    spec: FlatSpec | None = None
    root_lead_result: LeadResult | None = None
    integration_results: dict[str, LeadResult] = field(default_factory=dict)
    child_results: dict[str, LeadResult] = field(default_factory=dict)
    verdict: str = "unverified"
    total_cost_usd: float = 0.0
    duration_s: float = 0.0
    failure_reason: str = ""


def _new_session_id() -> str:
    return time.strftime("%Y-%m-%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


def _v5_root_branch(project_dir: Path, config: dict[str, Any]) -> str:
    """Branch that should receive root-level v5 integration work."""
    configured = str(config.get("default_branch") or "").strip()
    if configured:
        return configured
    try:
        from otto.config import detect_default_branch

        detected = detect_default_branch(project_dir)
        if detected:
            return detected
    except Exception:  # noqa: BLE001 - fall back to the historical default.
        pass
    return "main"


def _checkout_v5_branch_clean(
    *,
    project_dir: Path,
    branch: str,
    context: str,
    on_event: Any = None,
) -> dict[str, Any] | None:
    """Checkout ``branch`` for v5 orchestration.

    Dirty/failed checkout states are returned as preflight payloads so the
    repair loop can hand them to an agent. A clean checkout returns None.
    """
    from otto.v5_branching import (
        assert_clean_before_checkout,
        git_current_branch,
        git_status_porcelain,
    )

    is_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if is_repo.returncode != 0:
        return None
    current = git_current_branch(project_dir)
    if current == branch:
        try:
            remaining = git_status_porcelain(project_dir)
        except RuntimeError as exc:
            return _checkout_issue_payload(
                project_dir=project_dir,
                branch=branch,
                current_branch=current,
                context=context,
                kind="checkout_status_failed_at_phase",
                message=str(exc),
                status_lines=[],
                on_event=on_event,
            )
        if remaining:
            return _checkout_issue_payload(
                project_dir=project_dir,
                branch=branch,
                current_branch=current,
                context=context,
                kind="worktree_dirty_at_phase",
                message=(
                    f"already on {branch!r} during {context}, but worktree is dirty"
                ),
                status_lines=remaining,
                on_event=on_event,
            )
        return None
    try:
        assert_clean_before_checkout(
            project_dir=project_dir,
            source_branch=current,
            target_branch=branch,
        )
    except Exception as exc:  # noqa: BLE001 - repair agent decides the action.
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=current,
            context=context,
            kind="worktree_dirty_at_phase",
            message=str(exc),
            status_lines=getattr(exc, "dirty_status", ()),
            on_event=on_event,
        )
    cp = subprocess.run(
        ["git", "checkout", branch],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=current,
            context=context,
            kind="checkout_failed_at_phase",
            message=(
                f"checkout {branch!r} failed during {context} from "
                f"{current!r}: {detail}"
            ),
            status_lines=[],
            on_event=on_event,
        )
    actual = git_current_branch(project_dir)
    if actual != branch:
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=actual,
            context=context,
            kind="checkout_failed_at_phase",
            message=(
                f"checkout {branch!r} during {context} reported success but "
                f"current branch is {actual!r}"
            ),
            status_lines=[],
            on_event=on_event,
        )
    remaining = git_status_porcelain(project_dir)
    if remaining:
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=actual,
            context=context,
            kind="worktree_dirty_at_phase",
            message=f"checkout {branch!r} during {context} left dirty worktree",
            status_lines=remaining,
            on_event=on_event,
        )
    _emit(on_event, {
        "event": "project_branch_checked_out",
        "context": context,
        "from": current,
        "to": branch,
    })
    return None


def _checkout_issue_payload(
    *,
    project_dir: Path,
    branch: str,
    current_branch: str,
    context: str,
    kind: str,
    message: str,
    status_lines: Any,
    on_event: Any = None,
) -> dict[str, Any]:
    lines = [str(line) for line in (status_lines or []) if str(line).strip()]
    issue = {
        "kind": kind,
        "severity": "block",
        "message": (
            f"{message}\n"
            f"phase={context}; current_branch={current_branch}; target_branch={branch}\n"
            f"dirty_status:\n" + "\n".join(lines[:80])
        ).strip(),
        "phase": context,
        "current_branch": current_branch,
        "target_branch": branch,
        "paths": [_status_path(line) for line in lines if _status_path(line)],
        "diff_stat": _git_diff_stat(project_dir),
    }
    payload = {
        "check": "git_checkout_clean",
        "phase": context,
        "cwd": str(project_dir),
        "passed": False,
        "issues": [issue],
        "error": None,
    }
    _emit(on_event, {
        "event": "checkout_preflight_issue",
        "phase": context,
        "kind": kind,
        "message": issue["message"],
    })
    return payload


def _status_path(line: str) -> str:
    text = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in text:
        text = text.split(" -> ", 1)[1].strip()
    return text


def _port_cleanup_payload(cleanup: Any, *, project_dir: Path) -> dict[str, Any]:
    still_bound = list(getattr(cleanup, "still_bound_ports", []) or [])
    payload: dict[str, Any] = {
        "check": "startup_port_cleanup",
        "phase": "pipeline_start",
        "cwd": str(project_dir),
        "passed": not still_bound,
        "issues": [],
        "cleanup": {
            "killed_ports": list(getattr(cleanup, "killed_ports", []) or []),
            "freed_ports": list(getattr(cleanup, "freed_ports", []) or []),
            "still_bound_ports": still_bound,
            "killed_pids": getattr(cleanup, "killed_pids", {}) or {},
            "pids_before": getattr(cleanup, "pids_before", {}) or {},
            "pids_after": getattr(cleanup, "pids_after", {}) or {},
            "ports_without_owned_process": list(
                getattr(cleanup, "ports_without_owned_process", []) or []
            ),
        },
        "error": None,
    }
    if still_bound:
        payload["issues"] = [
            {
                "kind": "clean_deploy_port_busy",
                "severity": "block",
                "message": (
                    "Declared ports still bound after startup cleanup: "
                    + ", ".join(str(port) for port in still_bound)
                ),
            }
        ]
    return payload


async def _run_startup_port_cleanup_with_repair(
    *,
    project_dir: Path,
    session_dir: Path,
    config: dict[str, Any],
    on_event: Any = None,
) -> dict[str, Any]:
    from otto.v5_clean_verify import cleanup_stale_declared_ports

    def run_once() -> dict[str, Any]:
        cleanup = cleanup_stale_declared_ports(
            project_dir,
            logger_fn=lambda m: logger.info("preflight: %s", m),
        )
        payload = _port_cleanup_payload(cleanup, project_dir=project_dir)
        if payload["cleanup"]["killed_ports"] or payload["cleanup"]["still_bound_ports"]:
            _emit(on_event, {
                "event": "stale_ports_checked",
                "ports": payload["cleanup"],
                "passed": payload["passed"],
            })
        return payload

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first

    return await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=project_dir,
        session_dir=session_dir,
        config=config,
        task_id=ROOT_TASK_ID,
        repair_phase="startup_port_cleanup",
        event_prefix="startup_port_cleanup",
        integration_branch=None,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={"cleanup": first.get("cleanup") or {}},
    )


def _git_diff_stat(project_dir: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_capture(
    worktree: Path,
    args: list[str],
    *,
    timeout: int = 10,
) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _git_diff_name_only(worktree: Path) -> list[str]:
    paths = [
        line.strip()
        for line in _git_capture(worktree, ["diff", "--name-only"]).splitlines()
        if line.strip()
    ]
    for line in _git_status_short(worktree).splitlines():
        raw = line[3:] if len(line) > 3 else ""
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        rel = raw.strip().strip('"')
        if rel:
            paths.append(rel)
    return sorted(dict.fromkeys(paths))


def _foundation_contract_write_feedback(
    *,
    project_dir: Path,
    acting_task_id: str,
    parent_integration_branch: str,
    changed_paths: list[str],
    operation: str,
) -> dict[str, Any] | None:
    normalized_changes = [
        _normalize_contract_path(path)
        for path in changed_paths
        if _normalize_contract_path(path)
    ]
    if not normalized_changes:
        return None
    parent_id = (
        ROOT_TASK_ID
        if acting_task_id == ROOT_TASK_ID
        else _parent_task_id_for_child(project_dir, acting_task_id, parent_integration_branch)
    )
    graph = read_graph(project_dir)
    tasks = graph.get("tasks") or {}
    contracts = _foundation_contracts_for_parent(project_dir, parent_id, tasks)
    task = get_task(project_dir, acting_task_id) or {}
    role = str(task.get("task_role") or "feature")
    bound_amendment = task.get("contract_amendment")
    if not isinstance(bound_amendment, dict):
        bound_amendment = {}
    bound_contract_path = _normalize_contract_path(
        str(bound_amendment.get("contract_path") or task.get("contract_amendment_path") or "")
    )
    bound_owner_id = str(
        bound_amendment.get("owner_task_id") or task.get("contract_amendment_owner_task_id") or ""
    ).strip()
    violations: list[dict[str, Any]] = []
    if role == "contract_amendment":
        outside_bound = [
            path
            for path in normalized_changes
            if not bound_contract_path or not _path_overlaps(path, bound_contract_path)
        ]
        if outside_bound:
            violations.append({
                "contract_path": bound_contract_path,
                "owner_task_id": bound_owner_id,
                "changed_paths": outside_bound,
            })
    if not contracts:
        if not violations:
            return None
        return {
            "kind": "foundation_contract_write_blocked",
            "step_id": "foundation_contract_write_gate",
            "message": "contract amendment attempted to write outside its bound contract path",
            "task_id": acting_task_id,
            "task_role": role,
            "parent_task_id": parent_id,
            "parent_integration_branch": parent_integration_branch,
            "operation": operation,
            "violations": violations,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    for contract in contracts:
        contract_path = _normalize_contract_path(str(contract.get("path") or ""))
        owner_id = str(contract.get("owner_task_id") or "").strip()
        if not contract_path:
            continue
        if acting_task_id == owner_id:
            continue
        overlapping = [path for path in normalized_changes if _path_overlaps(path, contract_path)]
        if (
            overlapping
            and role == "contract_amendment"
            and bound_contract_path == contract_path
            and bound_owner_id == owner_id
        ):
            continue
        if overlapping:
            violations.append({
                "contract_path": contract_path,
                "owner_task_id": owner_id,
                "changed_paths": overlapping,
            })
    if not violations:
        return None
    return {
        "kind": "foundation_contract_write_blocked",
        "step_id": "foundation_contract_write_gate",
        "message": "non-owner task attempted to write a foundation contract path",
        "task_id": acting_task_id,
        "task_role": role,
        "parent_task_id": parent_id,
        "parent_integration_branch": parent_integration_branch,
        "operation": operation,
        "violations": violations,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _foundation_contract_write_block_detail(feedback: dict[str, Any]) -> str:
    try:
        return json.dumps(feedback, sort_keys=True)
    except TypeError:
        return str(feedback)


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
    child_owned_paths = _task_owned_paths(child) if isinstance(child, dict) else []
    candidate_paths: list[str] = []
    for path in feedback.get("paths") or []:
        normalized = _normalize_contract_path(str(path))
        if normalized:
            candidate_paths.append(normalized)
    for item in feedback.get("missing") or []:
        if isinstance(item, dict):
            normalized = _normalize_contract_path(str(item.get("path") or ""))
            if normalized:
                candidate_paths.append(normalized)
    integration_context = feedback.get("integration_context")
    if isinstance(integration_context, dict):
        guard = integration_context.get("integration_union_guard")
        if isinstance(guard, dict):
            for path in guard.get("paths") or []:
                normalized = _normalize_contract_path(str(path))
                if normalized:
                    candidate_paths.append(normalized)
            for item in guard.get("missing") or []:
                if isinstance(item, dict):
                    normalized = _normalize_contract_path(str(item.get("path") or ""))
                    if normalized:
                        candidate_paths.append(normalized)

    contracts = _foundation_contracts_for_parent(project_dir, parent_task_id, tasks)
    for candidate_path in dict.fromkeys(candidate_paths):
        for contract in contracts:
            contract_path = _normalize_contract_path(str(contract.get("path") or ""))
            owner_id = str(contract.get("owner_task_id") or "").strip()
            if not contract_path or child_task_id == owner_id:
                continue
            if not _path_overlaps(candidate_path, contract_path):
                continue
            if any(_path_overlaps(owned, contract_path) for owned in child_owned_paths):
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
        "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    append_pending_entry(v5_pending_path(project_dir), entry)


def _contract_amendment_attempt_key(contract_path: str) -> str:
    return _normalize_contract_path(contract_path)


def _contract_amendment_attempt_count(task: dict[str, Any], contract_path: str) -> int:
    attempts = task.get("contract_amendment_attempts")
    if not isinstance(attempts, dict):
        return 0
    key = _contract_amendment_attempt_key(contract_path)
    try:
        return int(attempts.get(key, 0))
    except (TypeError, ValueError):
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
        "max_attempts": MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        "previous_feedback": union_feedback,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
    contract_path = _normalize_contract_path(str(contract.get("path") or ""))
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
            "max_attempts": MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        },
        contract_amendment_path=contract_path,
        contract_amendment_owner_task_id=owner_id,
        repair_route="foundation_contract_amendment",
    )
    set_contract_amendment_blocked(
        project_dir,
        child_task_id,
        amendment_id,
        reason=str(union_feedback.get("message") or _integration_union_reason_text(union_feedback)),
        merge_context={
            "child_session_dir": str(child_session_dir),
            "child_worktree": str(child_worktree),
            "parent_integration_branch": parent_integration_branch,
            "source_branch": source_branch,
            "pre_merge_ref": pre_merge_ref,
            "union_feedback": union_feedback,
        },
    )
    _emit(on_event, {
        "event": "foundation_contract_amendment_repair",
        "task_id": child_task_id,
        "amendment_task_id": amendment_id,
        "contract_path": contract_path,
        "owner_task_id": owner_id,
        "parent_task_id": parent_task_id,
        "attempt_count": attempt_count,
        "max_attempts": MAX_CONTRACT_AMENDMENT_ATTEMPTS,
        "structured_reason": union_feedback,
    })
    return amendment_id


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
    if graph_verdict == "pass":
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
                parent_task_id=str(leaf.get("parent_task_id") or ROOT_TASK_ID),
                parent_session_dir=Path(
                    str(merge_context.get("child_session_dir") or _paths.cross_sessions_dir(project_dir))
                ),
                intent=str(leaf.get("intent") or leaf_id),
                owned_paths=_task_owned_paths(leaf),
                task_role=str(leaf.get("task_role") or "feature"),
                parent_integration_branch=str(
                    merge_context.get("parent_integration_branch")
                    or leaf.get("integration_branch")
                    or "main"
                ),
            )
        _emit(on_event, {
            "event": "foundation_contract_amendment_unblocked",
            "amendment_task_id": amendment_id,
            "unblocked_task_ids": unblocked,
        })
        return

    if graph_verdict not in {"merge_blocked", "catastrophic", "partial", "unverified"}:
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
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for leaf_id in blocked:
        leaf_result = child_results.get(leaf_id) or LeadResult(
            task_id=leaf_id,
            verdict="merge_blocked",
            decomposition="inline",
        )
        _record_task_merge_blocked_reason(
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
    _emit(on_event, {
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
    if str(latest.get("verdict") or "") in {"merge_blocked", "catastrophic"}:
        return False
    if latest.get("merge_blocked_structured_reason") or latest.get("merge_blocked_reason"):
        return False
    terminal_verdict = verdict if verdict in {"pass", "partial", "unverified"} else "pass"
    if not persist_contract_amendment_retry_success(
        project_dir,
        task_id,
        cast(Any, terminal_verdict),
        cost_usd=cost_usd,
    ):
        return False
    _emit(on_event, {
        "event": "contract_amendment_leaf_retry_verdict_restored",
        "task_id": task_id,
        "verdict": terminal_verdict,
    })
    return True


def _git_diff_full(worktree: Path, *, max_chars: int = 60000) -> str:
    text = _git_capture(worktree, ["diff", "--", "."], timeout=20)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... <truncated>"


def _git_diff_stat_for_ref_range(
    worktree: Path,
    base_ref: str,
    head_ref: str,
) -> str:
    if not base_ref or not head_ref:
        return ""
    return _git_capture(worktree, ["diff", "--stat", f"{base_ref}..{head_ref}"], timeout=20)


_INTEGRATION_UNION_GUARD_SCHEMA_VERSION = 1


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def _parse_added_lines_by_path(diff_text: str) -> dict[str, list[str]]:
    additions: dict[str, list[str]] = {}
    current_path: str | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current_path = None
            continue
        if raw_line.startswith("+++ "):
            marker = raw_line[4:].strip()
            if marker == "/dev/null":
                current_path = None
            elif marker.startswith("b/"):
                current_path = marker[2:]
            else:
                current_path = marker
            continue
        if not current_path or not raw_line.startswith("+") or raw_line.startswith("+++ "):
            continue
        line = raw_line[1:].rstrip()
        if line.strip():
            additions.setdefault(current_path, []).append(line)
    return additions


def _git_added_lines_by_path_between(
    worktree: Path,
    base_ref: str,
    head_ref: str,
) -> dict[str, list[str]]:
    if not base_ref or not head_ref:
        return {}
    diff_text = _git_capture(
        worktree,
        [
            "diff",
            "--unified=0",
            "--diff-filter=AM",
            f"{base_ref}..{head_ref}",
            "--",
        ],
        timeout=30,
    )
    if not diff_text:
        return {}
    return _parse_added_lines_by_path(diff_text)


def _git_changed_paths_between_refs(
    worktree: Path,
    base_ref: str,
    head_ref: str,
) -> list[str]:
    if not base_ref or not head_ref:
        return []
    out = _git_capture(
        worktree,
        ["diff", "--name-only", f"{base_ref}..{head_ref}", "--"],
        timeout=30,
    )
    return sorted(dict.fromkeys(line.strip() for line in out.splitlines() if line.strip()))


def _git_show_text_at_ref(worktree: Path, ref: str, path: str) -> str:
    if not ref or not path:
        return ""
    return _git_capture(worktree, ["show", f"{ref}:{path}"], timeout=20)


def _task_id_for_integration_branch(project_dir: Path, integration_branch: str) -> str:
    graph = read_graph(project_dir)
    for task_id, entry in (graph.get("tasks") or {}).items():
        if isinstance(entry, dict) and entry.get("integration_branch") == integration_branch:
            return str(task_id)
    return ROOT_TASK_ID if integration_branch == "main" else integration_branch


def _parent_task_id_for_child(
    project_dir: Path,
    child_task_id: str,
    parent_integration_branch: str,
) -> str:
    child_task = get_task(project_dir, child_task_id) or {}
    parent_task_id = str(child_task.get("parent_task_id") or "").strip()
    if parent_task_id:
        return parent_task_id
    return _task_id_for_integration_branch(project_dir, parent_integration_branch)


def _normalize_contract_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _path_overlaps(lhs: str, rhs: str) -> bool:
    left = _normalize_contract_path(lhs)
    right = _normalize_contract_path(rhs)
    if not left or not right:
        return False
    if left == right:
        return True
    return left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _task_owned_paths(task: dict[str, Any]) -> list[str]:
    return [
        _normalize_contract_path(path)
        for path in (task.get("owned_paths") or [])
        if _normalize_contract_path(str(path))
    ]


def _is_foundation_task(task: dict[str, Any]) -> bool:
    if task.get("task_role") == "foundation":
        return True
    intent = str(task.get("intent") or "").lstrip().lower()
    return intent.startswith("architect") or intent.startswith("scaffold")


def _foundation_contracts_for_parent(
    project_dir: Path,
    parent_task_id: str,
    tasks: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    tasks = tasks if tasks is not None else (read_graph(project_dir).get("tasks") or {})
    parent = tasks.get(parent_task_id) if isinstance(tasks, dict) else None
    raw = parent.get("foundation_contracts") if isinstance(parent, dict) else None
    contracts = [dict(item) for item in (raw or []) if isinstance(item, dict)]
    if contracts:
        return contracts
    # Compatibility with early S0 repros: parent metadata is authoritative, but
    # a passed foundation child may still carry the contracts before persistence.
    for task_id, task in (tasks or {}).items():
        if not isinstance(task, dict):
            continue
        if task.get("parent_task_id") != parent_task_id:
            continue
        if not _is_foundation_task(task):
            continue
        raw_child = task.get("foundation_contracts")
        for item in raw_child or []:
            if isinstance(item, dict):
                contract = dict(item)
                contract.setdefault("owner_task_id", str(task_id))
                contracts.append(contract)
    return contracts


def _foundation_contract_findings(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, contract in enumerate(contracts):
        path = _normalize_contract_path(str(contract.get("path") or ""))
        owner = str(contract.get("owner_task_id") or "").strip()
        check = str(contract.get("check") or "").strip()
        if not path:
            findings.append({"index": index, "field": "path", "detail": "missing path"})
        if not owner:
            findings.append({"index": index, "field": "owner_task_id", "detail": "missing owner_task_id"})
        if check not in {"literal", "semantic"}:
            findings.append({"index": index, "field": "check", "detail": "check must be literal or semantic"})
        if path:
            if path in seen:
                findings.append({"index": index, "field": "path", "detail": f"duplicate contract path {path}"})
            seen.add(path)
    return findings


def _integration_union_empty_state(parent_integration_branch: str) -> dict[str, Any]:
    return {
        "schema_version": _INTEGRATION_UNION_GUARD_SCHEMA_VERSION,
        "parent_integration_branch": parent_integration_branch,
        "contributions": [],
        "touches": [],
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _integration_union_state_from_task(
    task: dict[str, Any],
    parent_integration_branch: str,
) -> dict[str, Any]:
    raw = task.get("integration_union_guard")
    if not isinstance(raw, dict):
        return _integration_union_empty_state(parent_integration_branch)
    if raw.get("schema_version") != _INTEGRATION_UNION_GUARD_SCHEMA_VERSION:
        return _integration_union_empty_state(parent_integration_branch)
    state = dict(raw)
    if not isinstance(state.get("contributions"), list):
        state["contributions"] = []
    if not isinstance(state.get("touches"), list):
        state["touches"] = []
    state["parent_integration_branch"] = parent_integration_branch
    return state


def _merge_integration_union_state(
    *,
    state: dict[str, Any],
    child_task_id: str,
    source_branch: str,
    base_ref: str,
    head_ref: str,
    additions_by_path: dict[str, list[str]],
    touched_paths: list[str],
) -> dict[str, Any]:
    next_state = dict(state)
    contributions = [
        dict(item)
        for item in (next_state.get("contributions") or [])
        if isinstance(item, dict)
    ]
    touches = [
        dict(item)
        for item in (next_state.get("touches") or [])
        if isinstance(item, dict)
    ]
    seen_contributions = {
        (
            str(item.get("child_task_id") or ""),
            str(item.get("path") or ""),
            str(item.get("line") or ""),
        )
        for item in contributions
    }
    seen_touches = {
        (
            str(item.get("child_task_id") or ""),
            str(item.get("path") or ""),
        )
        for item in touches
    }
    recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for path in touched_paths:
        touch_key = (child_task_id, path)
        if touch_key not in seen_touches:
            touches.append({
                "child_task_id": child_task_id,
                "path": path,
                "source_branch": source_branch,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "recorded_at": recorded_at,
            })
            seen_touches.add(touch_key)
    for path, lines in additions_by_path.items():
        for line in lines:
            key = (child_task_id, path, line)
            if key in seen_contributions:
                continue
            contributions.append({
                "child_task_id": child_task_id,
                "path": path,
                "line": line,
                "line_hash": _line_hash(line),
                "source_branch": source_branch,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "recorded_at": recorded_at,
            })
            seen_contributions.add(key)
    next_state["contributions"] = contributions
    next_state["touches"] = touches
    next_state["_written_at"] = recorded_at
    return next_state


def _integration_union_shared_paths(state: dict[str, Any]) -> set[str]:
    contributors_by_path: dict[str, set[str]] = {}
    for item in state.get("touches") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        child_task_id = str(item.get("child_task_id") or "")
        if path and child_task_id:
            contributors_by_path.setdefault(path, set()).add(child_task_id)
    return {
        path
        for path, child_ids in contributors_by_path.items()
        if len(child_ids) > 1
    }


def _integration_union_missing_contributions(
    state: dict[str, Any],
    final_text_by_path: dict[str, str],
) -> list[dict[str, Any]]:
    shared_paths = _integration_union_shared_paths(state)
    missing: list[dict[str, Any]] = []
    final_lines_by_path = {
        path: {line.rstrip() for line in text.splitlines()}
        for path, text in final_text_by_path.items()
    }
    for item in state.get("contributions") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        line = str(item.get("line") or "").rstrip()
        if not path or not line or path not in shared_paths:
            continue
        if line in final_lines_by_path.get(path, set()):
            continue
        missing.append({
            "path": path,
            "line": line,
            "line_hash": str(item.get("line_hash") or _line_hash(line)),
            "contributed_by": str(item.get("child_task_id") or ""),
            "source_branch": str(item.get("source_branch") or ""),
            "base_ref": str(item.get("base_ref") or ""),
            "head_ref": str(item.get("head_ref") or ""),
        })
    return missing


@contextlib.contextmanager
def _integration_union_guard_lock(
    project_dir: Path,
    parent_integration_branch: str,
) -> Iterator[None]:
    """Serialize union-guard state updates for one integration target."""
    from otto.v5_branching import _git_common_dir

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", parent_integration_branch).strip("-") or "target"
    lock_dir = _git_common_dir(project_dir) / "otto-union-guard-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _integration_union_reason_text(feedback: dict[str, Any]) -> str:
    raw_missing = feedback.get("missing")
    missing: list[Any] = raw_missing if isinstance(raw_missing, list) else []
    rendered: list[str] = []
    for item in missing[:5]:
        if not isinstance(item, dict):
            continue
        line = str(item.get("line") or "")
        if len(line) > 160:
            line = line[:157] + "..."
        rendered.append(
            f"{item.get('path')} missing line contributed by "
            f"{item.get('contributed_by')}: {line}"
        )
    suffix = "; ".join(rendered) if rendered else "missing child-contributed lines"
    return f"integration union incomplete: {suffix}"


def _integration_union_feedback(
    *,
    parent_integration_branch: str,
    child_task_id: str,
    source_branch: str,
    base_ref: str,
    post_merge_ref: str,
    missing: list[dict[str, Any]],
    final_text_by_path: dict[str, str],
) -> dict[str, Any]:
    paths = sorted(dict.fromkeys(str(item.get("path") or "") for item in missing if item.get("path")))
    conflicts = [
        {
            "path": path,
            "base": final_text_by_path.get(path, ""),
            "ours": final_text_by_path.get(path, ""),
            "theirs": "\n".join(
                str(item.get("line") or "")
                for item in missing
                if item.get("path") == path and item.get("line")
            ),
        }
        for path in paths
    ]
    feedback: dict[str, Any] = {
        "kind": "integration_union_incomplete",
        "step_id": "integration_union_guard",
        "message": "",
        "paths": paths,
        "missing": missing,
        "child_task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "base_ref": base_ref,
        "post_merge_ref": post_merge_ref,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "integration_context": {
            "merge_refs": {
                "base_ref": base_ref,
                "ours_ref": parent_integration_branch,
                "theirs_ref": source_branch,
            },
            "conflict_packet": {
                "schema_version": 1,
                "source_branch": source_branch,
                "target_branch": parent_integration_branch,
                "unmerged_paths": paths,
                "conflicts": conflicts,
                "instruction": (
                    "Restore every missing child-contributed line listed by "
                    "the integration union guard. Preserve already integrated "
                    "target behavior and the source child behavior."
                ),
            },
            "integration_union_guard": {
                "kind": "integration_union_incomplete",
                "missing": missing,
                "paths": paths,
                "post_merge_ref": post_merge_ref,
            },
        },
    }
    feedback["message"] = _integration_union_reason_text(feedback)
    return feedback


def _record_and_check_integration_union(
    *,
    project_dir: Path,
    parent_integration_branch: str,
    child_task_id: str,
    source_branch: str,
    pre_merge_ref: str,
) -> dict[str, Any] | None:
    with _integration_union_guard_lock(
        project_dir,
        parent_integration_branch,
    ):
        parent_task_id = _parent_task_id_for_child(
            project_dir,
            child_task_id,
            parent_integration_branch,
        )
        parent_task = get_task(project_dir, parent_task_id) or {}
        state = _integration_union_state_from_task(parent_task, parent_integration_branch)
        additions_by_path = _git_added_lines_by_path_between(
            project_dir,
            pre_merge_ref,
            source_branch,
        )
        touched_paths = _git_changed_paths_between_refs(
            project_dir,
            pre_merge_ref,
            source_branch,
        )
        state = _merge_integration_union_state(
            state=state,
            child_task_id=child_task_id,
            source_branch=source_branch,
            base_ref=pre_merge_ref,
            head_ref=source_branch,
            additions_by_path=additions_by_path,
            touched_paths=touched_paths,
        )
        update_task_metadata(project_dir, parent_task_id, integration_union_guard=state)
        shared_paths = _integration_union_shared_paths(state)
        final_text_by_path = {
            path: _git_show_text_at_ref(project_dir, parent_integration_branch, path)
            for path in shared_paths
        }
        missing = _integration_union_missing_contributions(state, final_text_by_path)
        if not missing:
            return None
        post_merge_ref = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
        return _integration_union_feedback(
            parent_integration_branch=parent_integration_branch,
            child_task_id=child_task_id,
            source_branch=source_branch,
            base_ref=pre_merge_ref,
            post_merge_ref=post_merge_ref,
            missing=missing,
            final_text_by_path=final_text_by_path,
        )


def _read_text_artifact(path: Path, *, max_chars: int = 60000) -> dict[str, Any]:
    payload: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists() or not path.is_file():
        return payload
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    payload["text"] = text[:max_chars]
    payload["truncated"] = len(text) > max_chars
    return payload


def _read_json_artifact(path: Path, *, max_chars: int = 120000) -> dict[str, Any]:
    payload = _read_text_artifact(path, max_chars=max_chars)
    text = payload.get("text")
    if not isinstance(text, str):
        return payload
    try:
        payload["json"] = json.loads(text)
    except json.JSONDecodeError as exc:
        payload["json_error"] = f"{type(exc).__name__}: {exc}"
    return payload


def _repair_budget_from_config(
    config: dict[str, Any],
    *,
    prefix: str,
    default_agent_turns: int,
    default_oracle_invocations: int,
    default_wall_clock_s: float = 1800.0,
) -> RepairBudget:
    def number(key: str, default: float | None) -> float | None:
        raw = config.get(f"{prefix}_{key}", config.get(f"repair_{key}", default))
        if isinstance(raw, (int, float)):
            return float(raw)
        return default

    def integer(key: str, default: int | None) -> int | None:
        raw = config.get(f"{prefix}_{key}", config.get(f"repair_{key}", default))
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw)
        return default

    return RepairBudget(
        wall_clock_s=float(number("wall_clock_s", default_wall_clock_s) or default_wall_clock_s),
        cost_usd=number("cost_usd", None),
        agent_turns=max(0, int(integer("agent_turns", default_agent_turns) or 0)),
        oracle_invocations=max(
            0,
            int(integer("oracle_invocations", default_oracle_invocations) or 0),
        ),
        idle_s=number("idle_s", None),
        diff_churn=integer("diff_churn", None),
        closeout_agent_turns=max(0, int(integer("closeout_agent_turns", 0) or 0)),
        provider_max_turns=integer(
            "provider_max_turns",
            int(config.get("max_turns_per_call") or 1),
        ),
    )


def _packet_attempt_history(packet_path: Path) -> list[dict[str, Any]]:
    if not packet_path.exists():
        return []
    try:
        packet = RepairPacket.load(packet_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    history = list(packet.attempt_history)
    events = packet.events()
    if events:
        history.append({
            "type": "prior_packet_events",
            "event_count": len(events),
            "events_tail": events[-20:],
        })
    return history


def _make_initial_oracle_payload(
    *,
    worktree: Path,
    scope: Scope,
    oracle_command: Any,
    issue_kind: str,
    issue_message: str,
    step_id: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    step = CleanOracleStepResult(
        id=step_id,
        status="failed",
        return_code=1,
        command_identity=getattr(oracle_command, "command_identity", "clean-verify"),
        command=list(getattr(oracle_command, "command", []) or []),
        cwd=str(worktree),
        env=dict(getattr(oracle_command, "env", {}) or {}),
        reason=issue_message,
    )
    issue = CleanOracleIssue(
        kind=issue_kind,
        severity="block",
        message=issue_message,
        step_id=step_id,
        paths=list(paths or []),
        command_identity=step.command_identity,
        return_code=1,
    )
    result = CleanOracleResult.from_parts(
        passed=False,
        scope=scope,
        issues=[issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=worktree,
        temp_dir=None,
    )
    return result.to_jsonable()


def _merge_refusal_oracle_payload(
    *,
    worktree: Path,
    scope: Scope,
    oracle_command: Any,
    issue_kind: str,
    issue_message: str,
    step_id: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    return _make_initial_oracle_payload(
        worktree=worktree,
        scope=scope,
        oracle_command=oracle_command,
        issue_kind=issue_kind,
        issue_message=issue_message,
        step_id=step_id,
        paths=paths,
    )


def _blocking_payload_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for issue in payload.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity") not in ("error", "block"):
            continue
        issues.append(issue)
    if issues:
        return issues
    if payload.get("passed") is False:
        return [
            {
                "kind": str(payload.get("error") or payload.get("check") or "preflight_failed"),
                "severity": "block",
                "message": str(payload.get("error") or "preflight failed without issue detail"),
            }
        ]
    return []


def _clean_oracle_payload_from_preflight_payload(
    *,
    payload: dict[str, Any],
    worktree: Path,
    scope: Scope,
    oracle_command: Any,
    step_id: str,
) -> dict[str, Any]:
    clean_oracle = payload.get("clean_oracle_result")
    if isinstance(clean_oracle, dict) and isinstance(clean_oracle.get("issues"), list):
        return clean_oracle

    issues: list[CleanOracleIssue] = []
    issue_payloads = _blocking_payload_issues(payload)
    for raw_issue in issue_payloads:
        raw_severity = str(raw_issue.get("severity") or "block")
        severity = raw_severity if raw_severity in {"warn", "error", "block"} else "block"
        ports: list[int] = []
        for raw_port in raw_issue.get("ports") or []:
            try:
                ports.append(int(raw_port))
            except (TypeError, ValueError):
                continue
        issues.append(
            CleanOracleIssue(
                kind=str(raw_issue.get("kind") or payload.get("check") or "preflight_failed"),
                severity=cast(Any, severity),
                message=str(raw_issue.get("message") or payload.get("error") or ""),
                step_id=str(raw_issue.get("step_id") or step_id),
                paths=[str(path) for path in (raw_issue.get("paths") or [])],
                ports=ports,
                command_identity=getattr(oracle_command, "command_identity", "clean-verify"),
                return_code=(
                    int(raw_issue["return_code"])
                    if isinstance(raw_issue.get("return_code"), int)
                    else 1
                ),
            )
        )

    message = "; ".join(issue.message for issue in issues) or str(
        payload.get("error") or "preflight failed"
    )
    step = CleanOracleStepResult(
        id=step_id,
        status="failed",
        return_code=1,
        command_identity=getattr(oracle_command, "command_identity", "clean-verify"),
        command=list(getattr(oracle_command, "command", []) or []),
        cwd=str(worktree),
        env=dict(getattr(oracle_command, "env", {}) or {}),
        reason=message,
    )
    result = CleanOracleResult.from_parts(
        passed=False,
        scope=scope,
        issues=issues,
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=worktree,
        temp_dir=None,
    )
    return result.to_jsonable()


def _worktree_product_contract(
    *,
    worktree: Path,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "worktree": str(worktree),
        "config": _read_json_artifact(worktree / "otto.yaml"),
        "charter": _read_text_artifact(worktree / "CHARTER.md"),
    }
    if spec_path is not None:
        contract["spec"] = _read_json_artifact(spec_path)
    else:
        contract["spec"] = _read_json_artifact(worktree / "spec.json")
    return contract


def _build_repair_packet(
    *,
    session_dir: Path,
    repair_slug: str,
    worktree_path: Path,
    task_id: str,
    phase: str,
    repair_phase: str,
    verify_scope: Scope,
    config: dict[str, Any],
    budget_prefix: str,
    default_agent_turns: int,
    default_oracle_invocations: int,
    latest_oracle_result: dict[str, Any] | Callable[[Any], dict[str, Any]],
    product_contract: dict[str, Any],
    integration_context: dict[str, Any],
    success_criteria: dict[str, Any],
    attempt_history_entry: dict[str, Any],
    expected_artifact_paths: list[str] | None = None,
    allowed_paths: list[str] | None = None,
    scope_policy: str = "unrestricted",
    branch: str | None = None,
    repair_unit_extra: dict[str, Any] | None = None,
    oracle_spec_path: Path | None = None,
    oracle_journey_scope: ExecutionScope = "subtree_integration",
    oracle_journey_artifact_dir: Path | None = None,
) -> RepairPacket:
    packet_dir = session_dir / "repair" / repair_slug
    packet_path = packet_dir / "repair_packet.json"
    packet_dir.mkdir(parents=True, exist_ok=True)
    link_path = packet_dir / "worktree"
    if not link_path.exists():
        try:
            link_path.symlink_to(worktree_path)
        except OSError as exc:
            logger.debug("could not symlink repair worktree %s: %s", link_path, exc)

    oracle_command = build_clean_verify_oracle_command(
        worktree_path=worktree_path,
        verify_scope=verify_scope,
        repair_packet_path=packet_path,
        spec_path=oracle_spec_path,
        journey_scope=oracle_journey_scope,
        journey_artifact_dir=oracle_journey_artifact_dir,
    )
    if callable(latest_oracle_result):
        latest_payload = latest_oracle_result(oracle_command)
    else:
        latest_payload = latest_oracle_result
    attempt_history = _packet_attempt_history(packet_path)
    attempt = dict(attempt_history_entry)
    attempt.setdefault("_written_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    attempt_history.append(attempt)
    current_branch = branch if branch is not None else _git_capture(worktree_path, ["branch", "--show-current"])
    head = _git_capture(worktree_path, ["rev-parse", "HEAD"])
    packet = RepairPacket(
        repair_unit={
            "id": repair_slug,
            "worktree": str(worktree_path),
            "branch": current_branch,
            "task_id": task_id,
            "phase": phase,
            "repair_phase": repair_phase,
            "allowed_paths": list(allowed_paths or []),
            "scope_policy": scope_policy,
            **dict(repair_unit_extra or {}),
        },
        acceptance_oracle={
            "verify_scope": verify_scope,
            "command": oracle_command.command,
            "env": oracle_command.env,
            "timeout_s": int(config.get(f"{budget_prefix}_oracle_timeout_s") or 300),
            "expected_artifact_paths": list(expected_artifact_paths or []),
            "success_criteria": {
                "clean_deploy": True,
                "composite_gate": True,
                **dict(success_criteria),
            },
        },
        latest_oracle_result=latest_payload,
        product_contract=product_contract,
        integration_context=integration_context,
        attempt_history=attempt_history,
        current_state={
            "git_status": _git_status_short(worktree_path),
            "head": head,
            "pre_repair_head": head,
            "branch": current_branch,
        },
        budget=_repair_budget_from_config(
            config,
            prefix=budget_prefix,
            default_agent_turns=default_agent_turns,
            default_oracle_invocations=default_oracle_invocations,
        ),
        packet_dir=packet_dir,
    )
    packet.capture_scope_baseline()
    return packet


def _repair_result_payload(
    *,
    repair_phase: str,
    repair: Any,
    terminal_state: str,
    final_payload: dict[str, Any],
) -> dict[str, Any]:
    repaired = terminal_state == "continued"
    attempt_outcome = "repaired" if repaired else "escalated"
    return {
        "terminal_state": terminal_state,
        "repaired": repaired,
        "repair_phase": repair_phase,
        "oracle_verdict": getattr(repair, "verdict", ""),
        "summary": getattr(repair, "summary", ""),
        "cost_usd": float(getattr(repair, "cost_usd", 0.0) or 0.0),
        "agent_turns_used": int(getattr(repair, "agent_turns_used", 0) or 0),
        "oracle_invocations": int(getattr(repair, "oracle_invocations", 0) or 0),
        "packet_path": str(getattr(repair, "packet_path", "") or ""),
        "composite_gate": getattr(repair, "composite_gate", None),
        "escalation": getattr(repair, "escalation", None),
        "final_preflight_passed": not _integration_smoke_blocks(final_payload),
        "attempts": [
            {
                "action": "agent",
                "outcome": attempt_outcome,
                "repair_phase": repair_phase,
                "repair_packet": str(getattr(repair, "packet_path", "") or ""),
                "summary": getattr(repair, "summary", ""),
                "cost_usd": float(getattr(repair, "cost_usd", 0.0) or 0.0),
            }
        ],
    }


async def _run_preflight_payload_repair_session(
    *,
    initial_payload: dict[str, Any],
    run_once: Any,
    project_dir: Path,
    worktree_path: Path,
    session_dir: Path,
    config: dict[str, Any],
    task_id: str,
    repair_phase: str,
    event_prefix: str,
    integration_branch: str | None,
    verify_scope: Scope = "subtree",
    on_event: Any = None,
    integration_context: dict[str, Any] | None = None,
    allowed_paths: list[str] | None = None,
    scope_policy: str = "unrestricted",
    journey_scope: ExecutionScope = "subtree_integration",
) -> dict[str, Any]:
    repair_slug = safe_slug(
        f"{task_id}-{repair_phase}-{initial_payload.get('phase') or initial_payload.get('check') or 'repair'}",
        max_len=64,
    )
    branch = _git_capture(worktree_path, ["branch", "--show-current"])
    packet = _build_repair_packet(
        session_dir=session_dir,
        repair_slug=repair_slug,
        worktree_path=worktree_path,
        task_id=task_id,
        phase="preflight",
        repair_phase=repair_phase,
        verify_scope=verify_scope,
        config=config,
        budget_prefix=f"{repair_phase}_repair",
        default_agent_turns=1,
        default_oracle_invocations=3,
        latest_oracle_result=lambda oracle_command: _clean_oracle_payload_from_preflight_payload(
            payload=initial_payload,
            worktree=worktree_path,
            scope=verify_scope,
            oracle_command=oracle_command,
            step_id=str(initial_payload.get("check") or repair_phase),
        ),
        product_contract=_worktree_product_contract(worktree=worktree_path),
        integration_context={
            "integration_branch": integration_branch,
            "project_dir": str(project_dir),
            "initial_preflight": initial_payload,
            **dict(integration_context or {}),
        },
        success_criteria={
            "preflight_operation": initial_payload.get("check") or repair_phase,
        },
        attempt_history_entry={
            "type": "preflight_blocking_payload",
            "repair_phase": repair_phase,
            "payload": initial_payload,
            "git_status": _git_status_short(worktree_path),
        },
        allowed_paths=list(allowed_paths or []),
        scope_policy=scope_policy,
        branch=branch or integration_branch or "",
        oracle_spec_path=session_dir / "spec" / "spec.json",
        oracle_journey_scope=journey_scope,
        oracle_journey_artifact_dir=session_dir / "journeys" / safe_slug(repair_phase, max_len=48),
    )
    _emit(on_event, {
        "event": f"{event_prefix}_repair_start",
        "task_id": task_id,
        "repair_phase": repair_phase,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_integration_worktree

        feedback = _foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=task_id,
            parent_integration_branch=integration_branch or _branch_checked_out(worktree_path) or "main",
            changed_paths=_git_diff_name_only(worktree_path),
            operation=f"{repair_phase}_repair_commit",
        )
        if feedback is not None:
            detail = _foundation_contract_write_block_detail(feedback)
            _emit(on_event, {
                "event": f"{event_prefix}_repair_commit_failed",
                "task_id": task_id,
                "repair_phase": repair_phase,
                "worktree": str(worktree_path),
                "detail": detail,
                "structured_reason": feedback,
            })
            return False, detail
        commit_ok, commit_detail = commit_integration_worktree(
            worktree_path=worktree_path,
            task_id=f"{task_id}-{repair_phase}",
        )
        _emit(on_event, {
            "event": (
                f"{event_prefix}_repair_commit"
                if commit_ok
                else f"{event_prefix}_repair_commit_failed"
            ),
            "task_id": task_id,
            "repair_phase": repair_phase,
            "worktree": str(worktree_path),
            "detail": commit_detail,
        })
        return commit_ok, commit_detail

    repair = await run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    if repair.verdict == "pass":
        final_payload = run_once()
    else:
        final_payload = dict(initial_payload)
    terminal_state = (
        "continued"
        if repair.verdict == "pass" and not _integration_smoke_blocks(final_payload)
        else "escalated"
    )
    final_payload["repair"] = _repair_result_payload(
        repair_phase=repair_phase,
        repair=repair,
        terminal_state=terminal_state,
        final_payload=final_payload,
    )
    _emit(on_event, {
        "event": f"{event_prefix}_repair_done",
        "task_id": task_id,
        "terminal_state": terminal_state,
        "repair_phase": repair_phase,
        "repair_packet": getattr(repair, "packet_path", str(packet.packet_path)),
    })
    return final_payload


async def _checkout_v5_branch_clean_with_repair(
    *,
    project_dir: Path,
    branch: str,
    context: str,
    session_dir: Path,
    config: dict[str, Any],
    integration_branch: str | None,
    task_id: str,
    on_event: Any = None,
) -> dict[str, Any]:
    """Checkout a branch, repairing dirty/failed checkout states via agent."""

    def passed_payload() -> dict[str, Any]:
        return {
            "check": "git_checkout_clean",
            "phase": context,
            "cwd": str(project_dir),
            "passed": True,
            "issues": [],
            "error": None,
        }

    def run_once() -> dict[str, Any]:
        payload = _checkout_v5_branch_clean(
            project_dir=project_dir,
            branch=branch,
            context=context,
            on_event=on_event,
        )
        return payload or passed_payload()

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first
    return await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=project_dir,
        session_dir=session_dir,
        config=config,
        task_id=task_id,
        repair_phase="checkout_clean",
        event_prefix="checkout",
        integration_branch=integration_branch,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={
            "checkout": {
                "branch": branch,
                "context": context,
            }
        },
    )


def _integration_restore_branch(
    project_dir: Path,
    task_id: str,
    config: dict[str, Any],
) -> str:
    """Where ``project_dir`` should be restored after a task integration."""
    task = get_task(project_dir, task_id) or {}
    branch = str(task.get("integration_branch") or "").strip()
    return branch or _v5_root_branch(project_dir, config)


def _commit_integration_agent_changes(
    *,
    project_dir: Path,
    task_id: str,
    worktree_path: Path,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Runner-owned commit for integration-agent edits."""
    if result.verdict == "catastrophic":
        return
    from otto.v5_branching import commit_integration_worktree

    feedback = _foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=task_id,
        parent_integration_branch=_integration_restore_branch(project_dir, task_id, {}),
        changed_paths=_git_diff_name_only(worktree_path),
        operation="integration_agent_commit",
    )
    if feedback is not None:
        detail = _foundation_contract_write_block_detail(feedback)
        _emit(on_event, {
            "event": "integration_commit_failed",
            "task_id": task_id,
            "worktree": str(worktree_path),
            "detail": detail,
            "structured_reason": feedback,
        })
        _record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=task_id,
            result=result,
            reason=detail,
            origin="foundation_contract_write_gate",
            structured_reason=feedback,
        )
        return
    ok, detail = commit_integration_worktree(
        worktree_path=worktree_path,
        task_id=task_id,
    )
    _emit(on_event, {
        "event": "integration_commit" if ok else "integration_commit_failed",
        "task_id": task_id,
        "worktree": str(worktree_path),
        "detail": detail,
    })
    if not ok:
        logger.warning("integration commit failed for %s: %s", task_id, detail)
        set_verdict(project_dir, task_id, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"


def _commit_root_inline_changes(
    *,
    project_dir: Path,
    root_branch: str,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Runner-owned commit for root inline builds."""
    if result.decomposition != "inline" or result.verdict == "catastrophic":
        return

    from otto.v5_branching import commit_worktree, git_current_branch

    current_branch = git_current_branch(project_dir)
    if current_branch != root_branch:
        detail = (
            f"root inline finished on {current_branch!r}, expected {root_branch!r}; "
            "refusing runner commit"
        )
        _emit(on_event, {
            "event": "inline_commit_failed",
            "task_id": ROOT_TASK_ID,
            "worktree": str(project_dir),
            "detail": detail,
        })
        logger.warning(detail)
        set_verdict(project_dir, ROOT_TASK_ID, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"
        return

    feedback = _foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=ROOT_TASK_ID,
        parent_integration_branch=root_branch,
        changed_paths=_git_diff_name_only(project_dir),
        operation="root_inline_commit",
    )
    if feedback is not None:
        detail = _foundation_contract_write_block_detail(feedback)
        _emit(on_event, {
            "event": "inline_commit_failed",
            "task_id": ROOT_TASK_ID,
            "worktree": str(project_dir),
            "detail": detail,
            "structured_reason": feedback,
        })
        _record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=ROOT_TASK_ID,
            result=result,
            reason=detail,
            origin="foundation_contract_write_gate",
            structured_reason=feedback,
        )
        return
    ok, detail = commit_worktree(worktree_path=project_dir, message="v5 inline build")
    _emit(on_event, {
        "event": "inline_commit" if ok else "inline_commit_failed",
        "task_id": ROOT_TASK_ID,
        "worktree": str(project_dir),
        "detail": detail,
    })
    if not ok:
        logger.warning("root inline commit failed: %s", detail)
        set_verdict(project_dir, ROOT_TASK_ID, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"


def _propagate_subtree_integration(
    *,
    project_dir: Path,
    task_id: str,
) -> tuple[bool, str, str, str]:
    """Merge a decomposed child's integration branch into its parent target."""
    from otto.v5_branching import integration_branch_name, merge_branch_into

    child_entry = get_task(project_dir, task_id) or {}
    parent_id = child_entry.get("parent_task_id") or ROOT_TASK_ID
    target = "main" if parent_id == ROOT_TASK_ID else integration_branch_name(parent_id)
    source = integration_branch_name(task_id)
    if source == target:
        detail = (
            "refusing subtree integration self-merge: source and target are both "
            f"{source!r} for task {task_id!r} with parent_task_id={parent_id!r}; "
            "propagation would otherwise be a silent no-op"
        )
        set_verdict(project_dir, task_id, "merge_blocked")
        return False, detail, source, target

    try:
        ok, detail = merge_branch_into(
            project_dir=project_dir,
            source_branch=source,
            target_branch=target,
        )
    except MergeWorktreeDirtyError as exc:
        detail = str(exc)
        set_verdict(project_dir, task_id, "merge_blocked")
        return False, detail, source, target
    if not ok:
        set_verdict(project_dir, task_id, "merge_blocked")
    return ok, detail, source, target


def _worktree_for_branch(project_dir: Path, branch: str) -> Path:
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return project_dir
    current_path: Path | None = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree "):].strip())
            continue
        if line.startswith("branch ") and current_path is not None:
            ref = line[len("branch "):].strip()
            if ref == branch or ref.endswith(f"/{branch}"):
                return current_path
    return project_dir


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
    target_worktree = _worktree_for_branch(project_dir, target)
    conflict_packet = _conflict_packet_for_refusal(
        project_dir=project_dir,
        source=source,
        target=target,
    )
    paths = [
        str(path)
        for path in (conflict_packet.get("unmerged_paths") or _git_diff_name_only(target_worktree))
        if str(path)
    ]
    session_dir = _paths.cross_sessions_dir(project_dir)
    repair_slug = safe_slug(f"{task_id}-subtree-propagation", max_len=64)
    packet = _build_repair_packet(
        session_dir=session_dir,
        repair_slug=repair_slug,
        worktree_path=target_worktree,
        task_id=task_id,
        phase="subtree_propagation",
        repair_phase="subtree_propagation",
        verify_scope="subtree",
        config=config,
        budget_prefix="subtree_propagation_repair",
        default_agent_turns=1,
        default_oracle_invocations=3,
        latest_oracle_result=lambda oracle_command: _merge_refusal_oracle_payload(
            worktree=target_worktree,
            scope="subtree",
            oracle_command=oracle_command,
            issue_kind="subtree_propagation_blocked",
            issue_message=detail,
            step_id="subtree_propagation_gate",
            paths=paths,
        ),
        product_contract={
            **_worktree_product_contract(worktree=target_worktree),
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
    _emit(on_event, {
        "event": "subtree_propagation_repair_start",
        "task_id": task_id,
        "source": source,
        "target": target,
        "detail": detail,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_worktree

        feedback = _foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=task_id,
            parent_integration_branch=target,
            changed_paths=_git_diff_name_only(target_worktree),
            operation="subtree_propagation_repair_commit",
        )
        if feedback is not None:
            return False, _foundation_contract_write_block_detail(feedback)
        return commit_worktree(
            worktree_path=target_worktree,
            message=f"v5 subtree propagation repair: {task_id}",
        )

    repair = await run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _emit(on_event, {
        "event": "subtree_propagation_repair_done",
        "task_id": task_id,
        "ok": repair.verdict == "pass",
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
        "escalation": repair.escalation,
    })
    if repair.verdict != "pass":
        reason = (
            "subtree propagation repair did not pass: "
            f"{repair.summary}; original refusal: {detail}"
        )
        _record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=task_id,
            result=result,
            reason=reason,
            origin="subtree_propagation",
        )
        return False, reason
    return True, repair.summary


def _verify_child_branches_reached_parent(
    *,
    project_dir: Path,
    parent_task_id: str,
    on_event: Any = None,
) -> None:
    """Verify terminal child branch tips are reachable from their parent target."""
    from otto.v5_branching import child_branch_name, integration_branch_name

    target = "main" if parent_task_id == ROOT_TASK_ID else integration_branch_name(parent_task_id)
    for child_id in children_of(project_dir, parent_task_id):
        child = get_task(project_dir, child_id) or {}
        if not _task_entry_allows_upward_merge(child):
            continue

        branches = [child_branch_name(child_id)]
        if child.get("child_task_ids") or child.get("decomposition") == "emit":
            branches.append(integration_branch_name(child_id))

        for branch in dict.fromkeys(branches):
            ok, detail = _branch_is_ancestor(project_dir, branch, target)
            _emit(on_event, {
                "event": "child_branch_ancestry_ok" if ok else "child_branch_ancestry_failed",
                "task_id": child_id,
                "branch": branch,
                "target": target,
                "detail": detail,
            })
            if ok:
                continue
            logger.warning(
                "child branch ancestry verification failed for %s: %s",
                child_id,
                detail,
            )
            set_verdict(project_dir, child_id, "merge_blocked")


def _branch_is_ancestor(project_dir: Path, branch: str, target: str) -> tuple[bool, str]:
    exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(project_dir),
        capture_output=True,
    )
    if exists.returncode != 0:
        return False, f"branch {branch!r} is missing"

    target_exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target}"],
        cwd=str(project_dir),
        capture_output=True,
    )
    if target_exists.returncode != 0:
        return False, f"target branch {target!r} is missing"

    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, target],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if ancestor.returncode == 0:
        return True, f"{branch} reaches {target}"
    detail = (ancestor.stderr or ancestor.stdout or "").strip()
    suffix = f": {detail}" if detail else ""
    return False, f"{branch} is not an ancestor of {target}{suffix}"


def _task_entry_allows_upward_merge(entry: dict[str, Any]) -> bool:
    if entry.get("blocked_pending_contract_amendment") or entry.get("blocked_on_task_id"):
        return False
    if str(entry.get("verdict") or "") == "merge_blocked":
        return False
    if entry.get("merge_blocked_structured_reason") or entry.get("merge_blocked_reason"):
        return False
    verdict = str(entry.get("verdict") or "")
    if verdict == "pass":
        return True
    return verdict == "partial" and entry.get("review_state") == "reviewed_partial"


def _child_result_allows_upward_merge(
    project_dir: Path,
    task_id: str,
    result: LeadResult,
) -> bool:
    entry = get_task(project_dir, task_id) or {}
    if entry.get("blocked_pending_contract_amendment") or entry.get("blocked_on_task_id"):
        return False
    if str(entry.get("verdict") or "") == "merge_blocked":
        return False
    if entry.get("merge_blocked_structured_reason") or entry.get("merge_blocked_reason"):
        return False
    if result.verdict == "pass":
        return True
    if result.verdict != "partial":
        return False
    return _result_has_reviewed_partial(result) or entry.get("review_state") == "reviewed_partial"


def _result_has_reviewed_partial(result: LeadResult) -> bool:
    if result.verdict != "partial" or not isinstance(result.verify_result, dict):
        return False
    payload = result.verify_result
    return (
        payload.get("review_state") == "reviewed_partial"
        or payload.get("merge_review_state") == "reviewed_partial"
        or payload.get("reviewed_partial") is True
    )


def _record_reviewed_partial_if_present(
    project_dir: Path,
    task_id: str,
    result: LeadResult,
) -> None:
    if not _result_has_reviewed_partial(result):
        return
    payload = result.verify_result if isinstance(result.verify_result, dict) else {}
    mark_reviewed_partial(
        project_dir,
        task_id,
        reason=str(
            payload.get("reviewed_partial_reason")
            or payload.get("summary")
            or "partial explicitly reviewed before merge"
        ),
        reviewer=str(payload.get("reviewed_partial_by") or "agent-oracle"),
    )


def _block_child_before_upward_merge(
    *,
    project_dir: Path,
    child_task_id: str,
    result: LeadResult,
    reason: str,
    on_event: Any = None,
) -> LeadResult:
    logger.error("child %s blocked before upward merge: %s", child_task_id, reason)
    set_verdict(project_dir, child_task_id, "merge_blocked", cost_usd=result.cost_usd)
    update_task_metadata(
        project_dir,
        child_task_id,
        failure_reason=reason,
        merge_blocked_origin="verification",
        merge_blocked_reason=reason,
    )
    result.verdict = "merge_blocked"
    result.failure_reason = reason
    if result.verify_result is None:
        result.verify_result = {}
    if isinstance(result.verify_result, dict):
        result.verify_result["verdict"] = "merge_blocked"
        result.verify_result["summary"] = reason
    _emit(on_event, {
        "event": "child_merge_blocked",
        "task_id": child_task_id,
        "reason": reason,
    })
    return result


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
    diff_name_only = _git_diff_name_only(child_worktree)
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
        "diff_stat": _git_diff_stat(child_worktree),
        "diff_name_only": diff_name_only,
    }
    if gate_feedback:
        attempt_history_entry = {
            "type": "upward_merge_gate_refusal",
            "detail": issue_message,
            "gate_feedback": gate_feedback,
            "verdict": result.verdict,
            "verify_result": verify_result,
            "diff_stat": _git_diff_stat(child_worktree),
            "diff_name_only": diff_name_only,
        }
    packet = _build_repair_packet(
        session_dir=child_session_dir,
        repair_slug=repair_slug,
        worktree_path=child_worktree,
        task_id=child_task_id,
        phase="child_verify",
        repair_phase="child_verify",
        verify_scope="subtree",
        config=config,
        budget_prefix="child_verify_repair",
        default_agent_turns=1,
        default_oracle_invocations=3,
        latest_oracle_result=lambda oracle_command: _make_initial_oracle_payload(
            worktree=child_worktree,
            scope="subtree",
            oracle_command=oracle_command,
            issue_kind=issue_kind,
            issue_message=issue_message,
            step_id=step_id,
            paths=feedback_paths,
        ),
        product_contract={
            **_worktree_product_contract(worktree=child_worktree, spec_path=spec_path),
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
                "stat": _git_diff_stat(child_worktree),
                "name_only": diff_name_only,
                "patch": _git_diff_full(child_worktree),
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
    _emit(on_event, {
        "event": "child_verify_repair_start",
        "task_id": child_task_id,
        "previous_verdict": result.verdict,
        "repair_packet": str(packet.packet_path),
        "reason": issue_kind,
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_worktree

        feedback = _foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            changed_paths=_git_diff_name_only(child_worktree),
            operation="child_verify_repair_commit",
        )
        if feedback is not None:
            return False, _foundation_contract_write_block_detail(feedback)
        return commit_worktree(
            worktree_path=child_worktree,
            message=f"v5 child verify repair: {child_task_id}",
        )

    return await run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )


def _refresh_child_result_from_verdict_file(
    *,
    project_dir: Path,
    child_task_id: str,
    child_session_dir: Path,
    result: LeadResult,
    repair: Any,
) -> LeadResult:
    from otto.lead import _read_agent_verdict

    verify_called, payload = _read_agent_verdict(child_session_dir)
    if not verify_called or not isinstance(payload, dict):
        return result
    verdict = str(payload.get("verdict") or "unverified")
    if verdict not in {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}:
        verdict = "unverified"
    result.verify_called = True
    result.verify_result = payload
    result.verify_result["repair_packet"] = repair.packet_path
    result.verdict = cast(Any, verdict)
    if verdict in {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}:
        set_verdict(
            project_dir,
            child_task_id,
            cast(Any, verdict),
            cost_usd=result.cost_usd,
        )
    return result


async def _ensure_child_merge_ready(
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
) -> LeadResult:
    _record_reviewed_partial_if_present(project_dir, child_task_id, result)
    if _child_result_allows_upward_merge(project_dir, child_task_id, result):
        return result
    if result.verdict not in ("partial", "unverified"):
        return result

    current = result
    original_cost = result.cost_usd

    repair = await _run_child_verify_repair_packet(
        project_dir=project_dir,
        child_task_id=child_task_id,
        child_worktree=child_worktree,
        child_session_dir=child_session_dir,
        parent_integration_branch=parent_integration_branch,
        original_intent=original_intent,
        result=current,
        config=config,
        max_parallel=max_parallel,
        run_started_at=run_started_at,
        spec_path=spec_path,
        on_event=on_event,
    )
    current.cost_usd = original_cost + repair.cost_usd
    if current.verify_result is None:
        current.verify_result = {}
    if isinstance(current.verify_result, dict):
        current.verify_result["child_verify_repair"] = {
            "verdict": repair.verdict,
            "summary": repair.summary,
            "repair_packet": repair.packet_path,
            "composite_gate": repair.composite_gate,
            "escalation": repair.escalation,
        }
        current.verify_result["repair_packet"] = repair.packet_path
    if repair.verdict != "pass":
        return _block_child_before_upward_merge(
            project_dir=project_dir,
            child_task_id=child_task_id,
            result=current,
            reason=(
                "Child verify/repair oracle did not pass: "
                f"{repair.summary}"
            ),
            on_event=on_event,
        )

    current = _refresh_child_result_from_verdict_file(
        project_dir=project_dir,
        child_task_id=child_task_id,
        child_session_dir=child_session_dir,
        result=current,
        repair=repair,
    )
    _record_reviewed_partial_if_present(project_dir, child_task_id, current)
    _emit(on_event, {
        "event": "child_verify_repair_done",
        "task_id": child_task_id,
        "verdict": current.verdict,
        "repair_packet": repair.packet_path,
    })
    if _child_result_allows_upward_merge(project_dir, child_task_id, current):
        return current

    return _block_child_before_upward_merge(
        project_dir=project_dir,
        child_task_id=child_task_id,
        result=current,
        reason=(
            "Child verify/repair passed its oracle but did not produce "
            "a mergeable child verdict (pass or reviewed_partial); "
            f"current verdict is {current.verdict!r}"
        ),
        on_event=on_event,
    )


async def run_v5_pipeline(
    *,
    project_dir: Path,
    intent: str,
    config: dict[str, Any],
    max_parallel: int = 3,
    tree_budget_usd: float = 25.0,
    on_event: Any = None,  # optional callback(event_dict) for streaming
) -> V5RunResult:
    """Run a full v5 hierarchical pipeline against ``intent``.

    Best-effort: on any error in any phase, write a verdict and continue.
    Returns the final V5RunResult after the root has its terminal verdict.
    """
    started = time.monotonic()
    result = V5RunResult()

    try:
        # ---- Phase A0: Repo hygiene ----
        # Greenfield projects often start with `git init` and no commits.
        # Without an initial commit, every `git branch i2p/...` creation fails
        # with "not a valid object name: 'HEAD'", which cascades into worktree
        # failures and serialised execution.
        try:
            from otto.v5_branching import ensure_initial_commit
            ensure_initial_commit(project_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ensure_initial_commit failed: %s", exc)

        root_branch = _v5_root_branch(project_dir, config)
        root_session_id = _new_session_id()
        root_session_dir = _paths.session_dir(project_dir, root_session_id)
        root_session_dir.mkdir(parents=True, exist_ok=True)
        _emit(on_event, {"event": "session_open", "session_id": root_session_id})

        checkout_result = await _checkout_v5_branch_clean_with_repair(
            project_dir=project_dir,
            branch=root_branch,
            context="v5_pipeline_start",
            session_dir=root_session_dir,
            config=config,
            integration_branch=None,
            task_id=ROOT_TASK_ID,
            on_event=on_event,
        )
        if _preflight_repair_escalated(checkout_result) or _integration_smoke_blocks(checkout_result):
            result.verdict = "merge_blocked"
            result.failure_reason = _preflight_blocking_summary(
                "Pipeline start branch checkout could not be repaired",
                checkout_result,
            )
            return result

        # Clean up stale dev-server processes bound to this project's
        # declared ports. Each "port already in use" error inside an agent
        # session burns ~30-60s of agent time + tokens diagnosing it; one
        # cleanup pass up-front saves that across the whole run.
        try:
            port_cleanup = await _run_startup_port_cleanup_with_repair(
                project_dir=project_dir,
                session_dir=root_session_dir,
                config=config,
                on_event=on_event,
            )
            if _preflight_repair_escalated(port_cleanup) or _integration_smoke_blocks(port_cleanup):
                result.verdict = "merge_blocked"
                result.failure_reason = _preflight_blocking_summary(
                    "Startup declared-port cleanup could not be repaired",
                    port_cleanup,
                )
                return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("port cleanup failed: %s", exc)

        # ---- Phase A: Root session setup ----

        # ---- Phase B: Compile flat spec ----
        _emit(on_event, {"event": "compile_start"})
        try:
            spec = await compile_flat_spec(
                project_dir=project_dir,
                session_dir=root_session_dir,
                intent=intent,
                config=config,
            )
            result.spec = spec
            _emit(on_event, {
                "event": "compile_done",
                "journey_count": len(spec.behavior_journeys),
                "lint_warnings": len(spec.lint_warnings),
            })
        except SpecContractRepairExhaustedError as exc:
            logger.warning("flat spec pass-model repair exhausted: %s", exc)
            result.verdict = "merge_blocked"
            result.failure_reason = f"spec_contract_repair_exhausted: {exc}"
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("flat spec compile failed")
            result.verdict = "catastrophic"
            result.failure_reason = f"spec_compile: {type(exc).__name__}: {exc}"
            return result

        # Record root in task graph.
        record_task(
            project_dir,
            task_id=ROOT_TASK_ID,
            intent=intent,
            integration_branch=None,
        )

        # ---- Phase C: Run root Lead ----
        _emit(on_event, {"event": "lead_start", "task_id": ROOT_TASK_ID})
        root_result = await run_lead(
            task_id=ROOT_TASK_ID,
            intent=intent,
            project_dir=project_dir,
            session_dir=root_session_dir,
            integration_branch=None,
            config=config,
            kind="plan_or_inline",
            decomp_runtime_context=_build_decomp_runtime_context(
                project_dir=project_dir,
                config=config,
                max_parallel=max_parallel,
                run_started_at=started,
                spec=spec,
            ),
        )
        result.root_lead_result = root_result
        _emit(on_event, {
            "event": "lead_done",
            "task_id": ROOT_TASK_ID,
            "verdict": root_result.verdict,
            "decomposition": root_result.decomposition,
            "emitted": len(root_result.emitted_subtask_ids),
        })
        _commit_root_inline_changes(
            project_dir=project_dir,
            root_branch=root_branch,
            result=root_result,
            on_event=on_event,
        )

        # ---- Phase C.5: Optional review pause for root's emitted children ----
        if (
            root_result.decomposition == "emit"
            and root_result.emitted_subtask_ids
            and bool(config.get("v5_review_first_decomp"))
        ):
            from otto.v5_review import list_pending_review, mark_pending_review

            n = mark_pending_review(project_dir, parent_task_id=ROOT_TASK_ID)
            _emit(on_event, {
                "event": "review_pause",
                "task_id": ROOT_TASK_ID,
                "pending_count": n,
            })
            # Wait until all root children are out of pending_review state.
            # Either approved (proceed), or cancelled (treated as not-emitted).
            await _wait_for_review(
                project_dir, parent_task_id=ROOT_TASK_ID, on_event=on_event
            )
            # Drop cancelled children from emitted list so we don't try to run them.
            still_pending = list_pending_review(project_dir, parent_task_id=ROOT_TASK_ID)
            assert still_pending == []  # post-condition
            _emit(on_event, {"event": "review_resume", "task_id": ROOT_TASK_ID})

        # ---- Phase D: Process emitted children, if any ----
        if root_result.decomposition == "emit" and root_result.emitted_subtask_ids:
            await _process_children(
                project_dir=project_dir,
                parent_task_id=ROOT_TASK_ID,
                config=config,
                max_parallel=max_parallel,
                tree_budget_usd=tree_budget_usd,
                child_results=result.child_results,
                integration_results=result.integration_results,
                on_event=on_event,
                run_started_at=started,
            )
            # ---- Phase E: Run root integration ----
            child_summaries = _build_child_summaries(
                project_dir, ROOT_TASK_ID, result.child_results, result.integration_results
            )
            integration_session_dir = root_session_dir / "integration"
            integration_session_dir.mkdir(parents=True, exist_ok=True)
            # Copy the flat spec so the integration Lead's verify call can
            # find it; without this the verifier returns "unverified" even
            # when leaf children all passed (root integration doesn't get a
            # fresh sibling session — it lives under root_session_dir/).
            _root_spec = root_session_dir / "spec" / "spec.json"
            _integ_spec = integration_session_dir / "spec" / "spec.json"
            if _root_spec.exists() and not _integ_spec.exists():
                try:
                    _integ_spec.parent.mkdir(parents=True, exist_ok=True)
                    _integ_spec.write_text(
                        _root_spec.read_text(encoding="utf-8"), encoding="utf-8",
                    )
                except OSError as exc:
                    logger.warning("could not copy spec for root integration: %s", exc)
            _emit(on_event, {"event": "integration_start", "task_id": ROOT_TASK_ID})
            checkout_result = await _checkout_v5_branch_clean_with_repair(
                project_dir=project_dir,
                branch=root_branch,
                context="root_integration_start",
                session_dir=integration_session_dir,
                config=config,
                integration_branch=None,
                task_id=ROOT_TASK_ID,
                on_event=on_event,
            )
            if _preflight_repair_escalated(checkout_result) or _integration_smoke_blocks(checkout_result):
                preflight_result = checkout_result
            else:
                preflight_result = await _run_integration_smoke_preflight_with_repair(
                    project_dir=project_dir,
                    worktree_path=project_dir,
                    task_id=ROOT_TASK_ID,
                    phase="pre_agent",
                    session_dir=integration_session_dir,
                    config=config,
                    integration_branch=None,
                    journey_scope="root_integration",
                    on_event=on_event,
                )
            if _preflight_repair_escalated(preflight_result):
                integration_result = _preflight_blocked_result(
                    task_id=ROOT_TASK_ID,
                    preflight_result=preflight_result,
                )
            else:
                integration_packet_path = _write_integration_packet(
                    project_dir=project_dir,
                    parent_task_id=ROOT_TASK_ID,
                    session_dir=integration_session_dir,
                    child_results=result.child_results,
                    integration_results=result.integration_results,
                    child_summaries=child_summaries,
                    preflight_result=preflight_result,
                    integration_branch=root_branch,
                    integration_worktree=project_dir,
                )
                integration_result = await run_lead(
                    task_id=ROOT_TASK_ID,
                    intent=intent,
                    project_dir=project_dir,
                    session_dir=integration_session_dir,
                    integration_branch=None,  # root integration ultimately merges to main
                    config=config,
                    kind="integration",
                    child_summaries=child_summaries,
                    preflight_result=preflight_result,
                    integration_packet_path=str(integration_packet_path),
                    execution_scope="root_integration",
                )
                _commit_integration_agent_changes(
                    project_dir=project_dir,
                    task_id=ROOT_TASK_ID,
                    worktree_path=project_dir,
                    result=integration_result,
                    on_event=on_event,
                )
            if _preflight_repair_escalated(preflight_result):
                post_preflight_result = preflight_result
            else:
                post_preflight_result = await _run_integration_smoke_preflight_with_repair(
                    project_dir=project_dir,
                    worktree_path=project_dir,
                    task_id=ROOT_TASK_ID,
                    phase="post_agent",
                    session_dir=integration_session_dir,
                    config=config,
                    integration_branch=None,
                    journey_scope="root_integration",
                    on_event=on_event,
                )
            if integration_result.verify_result is None:
                integration_result.verify_result = {}
            if isinstance(integration_result.verify_result, dict):
                integration_result.verify_result["pre_integration_preflight"] = preflight_result
                integration_result.verify_result["post_integration_preflight"] = post_preflight_result
            if (
                integration_result.verdict != "catastrophic"
                and _integration_smoke_blocks(post_preflight_result)
            ):
                integration_result.verdict = "merge_blocked"
                integration_result.failure_reason = (
                    "Post-agent smoke_clean_deploy still has blocking issues: "
                    + "; ".join(
                        str(issue.get("message") or issue.get("kind"))
                        for issue in post_preflight_result.get("issues", [])
                        if isinstance(issue, dict)
                        and issue.get("severity") in ("error", "block")
                    )
                )
                if isinstance(integration_result.verify_result, dict):
                    integration_result.verify_result["verdict"] = "merge_blocked"
                    integration_result.verify_result["summary"] = integration_result.failure_reason
                set_verdict(
                    project_dir,
                    ROOT_TASK_ID,
                    "merge_blocked",
                    cost_usd=integration_result.cost_usd,
                )
                _emit(on_event, {
                    "event": "integration_smoke_failed",
                    "task_id": ROOT_TASK_ID,
                    "verdict": "merge_blocked",
                    "worktree": str(project_dir),
                })
            result.integration_results[ROOT_TASK_ID] = integration_result
            _emit(on_event, {
                "event": "integration_done",
                "task_id": ROOT_TASK_ID,
                "verdict": integration_result.verdict,
            })
            # Override root's verdict with the integration verdict (which audits the FULL product).
            set_verdict(
                project_dir, ROOT_TASK_ID, integration_result.verdict,
                cost_usd=root_result.cost_usd + integration_result.cost_usd,
            )

            # ---- Phase F-pre: Reconcile recovered merge_blocked children ----
            # The integration agent's Step 0b can manually `git merge` the
            # build branches of merge_blocked children. When that succeeds,
            # the children's persisted verdict is still "merge_blocked"
            # (the runner has no way to know what the agent did). Without
            # reconciliation, aggregate_verdict() rolls up that stale
            # merge_blocked even when Step 0b restored everything.
            #
            # Check git: for each merge_blocked direct child of root, is
            # its build_branch an ancestor of the integration branch?
            # If yes, the integration agent merged it — update verdict to
            # "pass" so aggregate reflects the real product state.
            try:
                _reconcile_recovered_children(project_dir, ROOT_TASK_ID, on_event)
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("post-integration reconciliation failed: %s", exc)

        # ---- Phase F: Aggregate final verdict ----
        result.verdict = aggregate_verdict(project_dir, ROOT_TASK_ID)
        result.total_cost_usd = tree_total_cost(project_dir, ROOT_TASK_ID)

    except Exception as exc:  # noqa: BLE001 — top-level safety net
        logger.exception("v5 pipeline crashed")
        result.verdict = "catastrophic"
        result.failure_reason = f"pipeline: {type(exc).__name__}: {exc}"

    finally:
        result.duration_s = time.monotonic() - started

    return result


_CONTRACT_STRUCTURAL_INVALID_KINDS = {
    "contract_structural_invalid",
    "contract_contradiction",
    "product_contract_contradiction",
    "ia_contract_invalid",
    "structured_contract_invalid",
}


def _scaffold_oracle_contract_structurally_invalid(result: CleanOracleResult) -> bool:
    for issue in result.issues:
        kind = issue.kind.lower()
        message = issue.message.lower()
        if kind in _CONTRACT_STRUCTURAL_INVALID_KINDS:
            return True
        if kind.startswith("contract_") and (
            "contradict" in kind
            or "invalid" in kind
            or "contradict" in message
            or "structural" in message
        ):
            return True
    return False


def _emit_scaffold_oracle_issues(
    *,
    result: CleanOracleResult,
    architect_tid: str,
    retry_count: int,
    preflight_seen: set[str],
    on_event: Any = None,
) -> list[str]:
    blocking_messages: list[str] = []
    for issue in result.issues:
        key = f"{issue.kind}:scaffold:{architect_tid}:{retry_count}"
        if key in preflight_seen:
            continue
        preflight_seen.add(key)
        severity = issue.severity
        log_fn = logger.error if severity in ("error", "block") else logger.warning
        log_fn("preflight %s [%s]: %s", issue.kind, severity, issue.message)
        _emit(on_event, {
            "event": "preflight_issue",
            "kind": issue.kind,
            "severity": severity,
            "message": issue.message,
            "task_id": architect_tid,
        })
        if severity == "block":
            blocking_messages.append(f"[{issue.kind}] {issue.message}")
    if not result.issues and not result.passed:
        blocking_messages.append("[scaffold_oracle_failed] scaffold oracle failed without issues")
    return blocking_messages


def _architect_contract_feedback_reason(feedback: dict[str, Any]) -> str:
    message = str(feedback.get("message") or feedback.get("kind") or "architect contract invalid")
    try:
        structured = json.dumps(feedback, indent=2, sort_keys=True)
    except TypeError:
        structured = repr(feedback)
    if len(structured) > 4000:
        structured = structured[:4000] + "\n...<truncated>"
    return (
        "The architect-produced scaffold/contract is structurally unsafe for "
        "the planned leaf decomposition. Re-enter the architect and regenerate "
        "the scaffold/CHARTER before dispatching leaves.\n\n"
        f"{message}\n\nStructured reason:\n{structured}"
    )


def _reenter_or_block_architect_contract(
    *,
    project_dir: Path,
    architect_tid: str,
    child_results: dict[str, LeadResult],
    completed: set[str],
    feedback: dict[str, Any],
    origin: str,
    on_event: Any = None,
) -> bool:
    current_retries = get_retry_count(project_dir, architect_tid)
    reason = _architect_contract_feedback_reason(feedback)
    if current_retries < MAX_ARCHITECT_RETRIES:
        new_count = clear_verdict_for_retry(project_dir, architect_tid, reason)
        completed.discard(architect_tid)
        child_results.pop(architect_tid, None)
        logger.warning(
            "architect %s contract gate failed (attempt %d/%d): re-dispatching",
            architect_tid,
            new_count,
            MAX_ARCHITECT_RETRIES,
        )
        _emit(on_event, {
            "event": "architect_retry",
            "task_id": architect_tid,
            "retry_count": new_count,
            "max_retries": MAX_ARCHITECT_RETRIES,
            "reason_tail": str(feedback.get("message") or reason)[:200],
            "structured_reason": feedback,
        })
        return True

    result = child_results.get(architect_tid) or LeadResult(
        task_id=architect_tid,
        verdict="merge_blocked",
    )
    completed.discard(architect_tid)
    _record_task_merge_blocked_reason(
        project_dir=project_dir,
        task_id=architect_tid,
        result=result,
        reason=reason,
        origin=origin,
        structured_reason=feedback,
    )
    child_results[architect_tid] = result
    logger.error(
        "architect %s contract gate failed after %d retries; marking merge_blocked",
        architect_tid,
        MAX_ARCHITECT_RETRIES,
    )
    _emit(on_event, {
        "event": "architect_retry_exhausted",
        "task_id": architect_tid,
        "retry_count": current_retries,
        "structured_reason": feedback,
    })
    return True


def _foundation_scheduler_feedback(
    *,
    parent_task_id: str,
    tasks: dict[str, Any],
    ready: list[dict[str, Any]],
    in_flight_task_ids: set[str],
    contracts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    sibling_items = [
        (str(task_id), task)
        for task_id, task in tasks.items()
        if isinstance(task, dict) and task.get("parent_task_id") == parent_task_id
    ]
    foundation_ids = [
        task_id
        for task_id, task in sibling_items
        if task.get("task_role") == "foundation"
    ]
    if not foundation_ids:
        return None
    affected_features = [
        str(task_id)
        for task_id, task in sibling_items
        if str((task or {}).get("task_role") or "feature") == "feature"
        and not _task_entry_allows_upward_merge(task or {})
        and str((task or {}).get("verdict") or "") != "merge_blocked"
    ]
    terminal_blocked_foundations = [
        task_id
        for task_id in foundation_ids
        if _foundation_entry_is_terminal_blocked(tasks.get(task_id) or {})
    ]
    if terminal_blocked_foundations and affected_features:
        return {
            "kind": "shared_foundation_not_ready",
            "step_id": "foundation_scheduler_ordering",
            "message": "feature dispatch held until foundation siblings pass and foundation contracts are valid",
            "parent_task_id": parent_task_id,
            "ready_feature_task_ids": [],
            "affected_feature_task_ids": affected_features,
            "foundation_task_ids": foundation_ids,
            "unverified_foundation_task_ids": [],
            "terminal_blocked_foundation_task_ids": terminal_blocked_foundations,
            "contracts_present": bool(contracts),
            "contract_findings": _foundation_contract_findings(contracts),
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    ready_features = [
        str(entry.get("task_id") or "")
        for entry in ready
        if str((tasks.get(str(entry.get("task_id") or "")) or entry).get("task_role") or "feature") == "feature"
    ]
    if not ready_features:
        return None
    unverified_foundations = [
        task_id
        for task_id in foundation_ids
        if task_id in in_flight_task_ids
        or not _task_entry_allows_upward_merge(tasks.get(task_id) or {})
    ]
    mergeable_foundations = [
        task_id
        for task_id in foundation_ids
        if task_id not in in_flight_task_ids
        and _task_entry_allows_upward_merge(tasks.get(task_id) or {})
    ]
    contract_findings = _foundation_contract_findings(contracts)
    if mergeable_foundations and (not contracts or contract_findings):
        return {
            "kind": "foundation_contracts_missing_after_pass",
            "step_id": "foundation_scheduler_contracts_after_pass",
            "message": (
                "foundation sibling passed but did not produce valid foundation contracts; "
                "re-enter the foundation before dispatching dependent features"
            ),
            "parent_task_id": parent_task_id,
            "ready_feature_task_ids": ready_features,
            "affected_feature_task_ids": affected_features,
            "foundation_task_ids": foundation_ids,
            "mergeable_foundation_task_ids": mergeable_foundations,
            "unverified_foundation_task_ids": unverified_foundations,
            "terminal_blocked_foundation_task_ids": terminal_blocked_foundations,
            "contracts_present": bool(contracts),
            "contract_findings": contract_findings,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    if unverified_foundations or not contracts or contract_findings:
        return {
            "kind": "shared_foundation_not_ready",
            "step_id": "foundation_scheduler_ordering",
            "message": "feature dispatch held until foundation siblings pass and foundation contracts are valid",
            "parent_task_id": parent_task_id,
            "ready_feature_task_ids": ready_features,
            "affected_feature_task_ids": affected_features,
            "foundation_task_ids": foundation_ids,
            "unverified_foundation_task_ids": unverified_foundations,
            "terminal_blocked_foundation_task_ids": terminal_blocked_foundations,
            "contracts_present": bool(contracts),
            "contract_findings": contract_findings,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    return None


def _foundation_entry_is_terminal_blocked(entry: dict[str, Any]) -> bool:
    if entry.get("merge_blocked_structured_reason") or entry.get("merge_blocked_reason"):
        return True
    return str(entry.get("verdict") or "") in {"merge_blocked", "catastrophic", "failed"}


def _foundation_isolation_feedback(
    *,
    parent_task_id: str,
    architect_task_id: str,
    tasks: dict[str, Any],
    contracts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    findings: list[dict[str, Any]] = _foundation_contract_findings(contracts)
    contracts_by_path: dict[str, dict[str, Any]] = {}
    for contract in contracts:
        path = _normalize_contract_path(str(contract.get("path") or ""))
        if path:
            contracts_by_path[path] = contract

    for path, contract in contracts_by_path.items():
        owner_id = str(contract.get("owner_task_id") or "").strip()
        owner = tasks.get(owner_id) if owner_id else None
        owner_paths = _task_owned_paths(owner) if isinstance(owner, dict) else []
        if not owner_id or not isinstance(owner, dict):
            findings.append({
                "kind": "foundation_contract_owner_missing",
                "path": path,
                "owner_task_id": owner_id,
            })
        elif not any(_path_overlaps(path, owned) for owned in owner_paths):
            findings.append({
                "kind": "foundation_contract_not_owned_by_owner",
                "path": path,
                "owner_task_id": owner_id,
                "owner_owned_paths": owner_paths,
            })

    feature_owners: list[tuple[str, list[str]]] = []
    for task_id, task in tasks.items():
        if not isinstance(task, dict) or task.get("parent_task_id") != parent_task_id:
            continue
        if task.get("task_role", "feature") != "feature":
            continue
        if _task_entry_allows_upward_merge(task) or task.get("verdict") == "merge_blocked":
            continue
        owned_paths = _task_owned_paths(task)
        feature_owners.append((str(task_id), owned_paths))
        for contract_path, contract in contracts_by_path.items():
            owner_id = str(contract.get("owner_task_id") or "").strip()
            if str(task_id) == owner_id:
                continue
            overlaps = [owned for owned in owned_paths if _path_overlaps(owned, contract_path)]
            if overlaps:
                findings.append({
                    "kind": "feature_overlaps_foundation_contract",
                    "task_id": str(task_id),
                    "owned_paths": overlaps,
                    "contract_path": contract_path,
                    "owner_task_id": owner_id,
                })
            owner = tasks.get(owner_id) if owner_id else None
            owner_paths = _task_owned_paths(owner) if isinstance(owner, dict) else []
            exclusive_trees = [
                owned
                for owned in owner_paths
                if owned and _path_overlaps(contract_path, owned)
            ]
            nested_under_foundation = [
                {
                    "owned_path": owned,
                    "foundation_tree": tree,
                }
                for owned in owned_paths
                for tree in exclusive_trees
                if _path_overlaps(owned, tree)
            ]
            if nested_under_foundation:
                findings.append({
                    "kind": "feature_nested_under_foundation_tree",
                    "task_id": str(task_id),
                    "overlaps": nested_under_foundation,
                    "contract_path": contract_path,
                    "owner_task_id": owner_id,
                })

    for index, (task_id, owned_paths) in enumerate(feature_owners):
        for other_id, other_paths in feature_owners[index + 1:]:
            overlaps = [
                {"path": path, "other_path": other}
                for path in owned_paths
                for other in other_paths
                if _path_overlaps(path, other)
            ]
            if overlaps:
                findings.append({
                    "kind": "feature_owned_paths_overlap",
                    "task_id": task_id,
                    "other_task_id": other_id,
                    "overlaps": overlaps,
                })

    if not findings:
        return None
    return {
        "kind": "shared_foundation_not_isolated",
        "step_id": "foundation_isolation_gate",
        "message": "foundation contract paths must be exclusively owned before feature dispatch",
        "architect_task_id": architect_task_id,
        "parent_task_id": parent_task_id,
        "findings": findings,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def _run_scaffold_repair_packet(
    *,
    project_dir: Path,
    architect_tid: str,
    architect_task: dict[str, Any],
    latest_result: CleanOracleResult,
    config: dict[str, Any],
    on_event: Any = None,
) -> Any:
    repair_slug = safe_slug(f"{architect_tid}-scaffold", max_len=64)
    owned_paths = [str(path) for path in (architect_task.get("owned_paths") or [])]
    packet = _build_repair_packet(
        session_dir=_paths.cross_sessions_dir(project_dir),
        repair_slug=repair_slug,
        worktree_path=project_dir,
        task_id=architect_tid,
        phase="scaffold",
        repair_phase="scaffold",
        verify_scope="scaffold",
        config=config,
        budget_prefix="scaffold_repair",
        default_agent_turns=1,
        default_oracle_invocations=3,
        latest_oracle_result=latest_result.to_jsonable(),
        product_contract={
            **_worktree_product_contract(worktree=project_dir),
            "architect_task": architect_task,
        },
        integration_context={
            "architect_task_id": architect_tid,
            "architect_task": architect_task,
            "scaffold_oracle": latest_result.to_jsonable(),
            "current_diff": {
                "stat": _git_diff_stat(project_dir),
                "name_only": _git_diff_name_only(project_dir),
                "patch": _git_diff_full(project_dir),
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
            "diff_stat": _git_diff_stat(project_dir),
            "diff_name_only": _git_diff_name_only(project_dir),
        },
        allowed_paths=owned_paths,
        scope_policy="allowed_paths" if owned_paths else "unrestricted",
    )
    _emit(on_event, {
        "event": "scaffold_repair_start",
        "task_id": architect_tid,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_integration_worktree

        feedback = _foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=architect_tid,
            parent_integration_branch=str(architect_task.get("integration_branch") or "main"),
            changed_paths=_git_diff_name_only(project_dir),
            operation="scaffold_repair_commit",
        )
        if feedback is not None:
            return False, _foundation_contract_write_block_detail(feedback)
        return commit_integration_worktree(
            worktree_path=project_dir,
            task_id=f"{architect_tid}-scaffold-repair",
        )

    repair = await run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _emit(on_event, {
        "event": "scaffold_repair_done",
        "task_id": architect_tid,
        "verdict": repair.verdict,
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
    })
    return repair


async def _process_children(
    *,
    project_dir: Path,
    parent_task_id: str,
    config: dict[str, Any],
    max_parallel: int,
    tree_budget_usd: float,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    on_event: Any = None,
    dispatch_lease: _DispatchLease | None = None,
    run_started_at: float | None = None,
) -> None:
    """Process the v5_pending queue for ``parent_task_id``'s subtree.

    Runs children concurrently (up to max_parallel), waits for all, then
    recursively handles any grandchildren. Returns when all descendants of
    ``parent_task_id`` have terminal verdicts.

    Best-effort: a child crash doesn't stop siblings.
    """
    completed: set[str] = set()
    in_flight: dict[str, asyncio.Task[Any]] = {}
    preflight_seen: set[str] = set()  # issue kinds already emitted, dedupe
    architect_preflight_done: set[tuple[str, int]] = set()
    if dispatch_lease is None:
        dispatch_lease = _DispatchLease(max_parallel)

    while True:
        # Check tree budget cap.
        if tree_total_cost(project_dir, ROOT_TASK_ID) > tree_budget_usd:
            logger.warning("tree budget cap exceeded; refusing new dispatches")
            _emit(on_event, {
                "event": "budget_cap_hit",
                "spent": tree_total_cost(project_dir, ROOT_TASK_ID),
                "cap": tree_budget_usd,
            })
            # Wait for in-flight to drain, then exit.
            if in_flight:
                await asyncio.gather(*in_flight.values(), return_exceptions=True)
                for leased_tid in list(in_flight):
                    await dispatch_lease.release(leased_tid)
                in_flight.clear()
            break

        # Pre-flight: deterministic checks on the task graph.
        graph = read_graph(project_dir)
        pending = read_pending(project_dir)
        issues = run_preflight(project_dir, graph, pending)
        for issue in issues:
            key = f"{issue.kind}:{issue.task_id or '-'}"
            if key in preflight_seen:
                continue
            preflight_seen.add(key)
            log_fn = logger.error if issue.severity in ("error", "block") else logger.warning
            log_fn("preflight %s [%s]: %s", issue.kind, issue.severity, issue.message)
            _emit(on_event, {
                "event": "preflight_issue",
                "kind": issue.kind,
                "severity": issue.severity,
                "message": issue.message,
                "task_id": issue.task_id,
            })

        # Find ready tasks not yet running.
        active_task_ids = await dispatch_lease.active_task_ids()
        ready = take_ready(
            project_dir,
            completed_task_ids=completed,
            in_flight_task_ids=set(in_flight.keys()) | active_task_ids,
        )
        # Filter to descendants of parent_task_id.
        ready = [r for r in ready if _is_descendant_of(project_dir, r["task_id"], parent_task_id)]

        # Apply blocking pre-flight issues: drop blocked descendants from ready.
        if any(i.severity == "block" for i in issues):
            _filtered, blocked = filter_blocked_descendants(graph, ready, issues)
            if blocked:
                logger.warning(
                    "preflight blocked %d tasks from dispatching: %s",
                    len(blocked), sorted(blocked),
                )
            ready = _filtered

        # Post-architect scaffold compile check: run once when architect
        # transitions to verdict=pass, before feature children dispatch.
        # Catches "architect said pass but scaffold doesn't compile" —
        # otherwise discovered 20+ min later when features try to build on it.
        retry_architect = False
        tasks = (graph.get("tasks") or {})
        for architect_tid, architect_task in tasks.items():
            if not (
                (architect_task.get("intent") or "").lstrip().lower().startswith("architect")
                and architect_task.get("verdict") == "pass"
                and not (architect_task.get("depends_on") or [])
            ):
                continue
            retry_count = get_retry_count(project_dir, architect_tid)
            preflight_key = (architect_tid, retry_count)
            if preflight_key in architect_preflight_done:
                continue
            architect_preflight_done.add(preflight_key)
            logger.info("preflight: running scaffold clean oracle after architect-pass (task=%s)", architect_tid)
            scaffold_result = verify_from_clean_oracle(project_dir, scope="scaffold")
            blocking_messages = _emit_scaffold_oracle_issues(
                result=scaffold_result,
                architect_tid=architect_tid,
                retry_count=retry_count,
                preflight_seen=preflight_seen,
                on_event=on_event,
            )

            # Only a typed structural contract contradiction re-enters the
            # architect. Ordinary scaffold oracle failures are repaired as a
            # scaffold repair unit with the full packet and composite gate.
            if (
                blocking_messages
                and _scaffold_oracle_contract_structurally_invalid(scaffold_result)
            ):
                current_retries = get_retry_count(project_dir, architect_tid)
                if current_retries < MAX_ARCHITECT_RETRIES:
                    reason = (
                        "The scaffold oracle found a structured product-contract "
                        "contradiction. Re-enter the architect because the contract "
                        "itself must be corrected before code repair can be scoped "
                        "safely:\n\n"
                        + "\n".join(f"  - {m}" for m in blocking_messages)
                        + "\n\nFix the contract/scaffold contradiction, then "
                        "re-emit the scaffold."
                    )
                    new_count = clear_verdict_for_retry(
                        project_dir, architect_tid, reason
                    )
                    completed.discard(architect_tid)
                    child_results.pop(architect_tid, None)
                    logger.warning(
                        "architect %s scaffold preflight failed (attempt %d/%d): re-dispatching",
                        architect_tid,
                        new_count,
                        MAX_ARCHITECT_RETRIES,
                    )
                    _emit(on_event, {
                        "event": "architect_retry",
                        "task_id": architect_tid,
                        "retry_count": new_count,
                        "max_retries": MAX_ARCHITECT_RETRIES,
                        "reason_tail": blocking_messages[-1][:200],
                    })
                    # The architect is now eligible for re-dispatch, but
                    # the `ready` list computed at the top of this loop
                    # iteration is stale (the architect wasn't in it).
                    # Re-enter the loop so take_ready picks it up.
                    retry_architect = True
                    break
                logger.error(
                    "architect %s scaffold preflight failed after %d retries; "
                    "descendants will remain blocked",
                    architect_tid,
                    MAX_ARCHITECT_RETRIES,
                )
                _emit(on_event, {
                    "event": "architect_retry_exhausted",
                    "task_id": architect_tid,
                    "retry_count": current_retries,
                })
                continue

            if blocking_messages:
                repair = await _run_scaffold_repair_packet(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    architect_task=architect_task,
                    latest_result=scaffold_result,
                    config=config,
                    on_event=on_event,
                )
                if repair.verdict != "pass":
                    reason = (
                        "Scaffold oracle repair did not pass: "
                        f"{repair.summary}"
                    )
                    logger.error("architect %s scaffold repair blocked: %s", architect_tid, reason)
                    set_verdict(project_dir, architect_tid, "merge_blocked")
                    update_task_metadata(
                        project_dir,
                        architect_tid,
                        failure_reason=reason,
                        merge_blocked_origin="scaffold",
                        merge_blocked_reason=reason,
                        scaffold_repair_packet=repair.packet_path,
                        scaffold_repair_escalation=repair.escalation,
                    )
                    child_results.pop(architect_tid, None)
                    _emit(on_event, {
                        "event": "architect_scaffold_repair_blocked",
                        "task_id": architect_tid,
                        "reason": reason,
                        "repair_packet": repair.packet_path,
                    })
                    continue
                update_task_metadata(
                    project_dir,
                    architect_tid,
                    scaffold_repair_packet=repair.packet_path,
                    scaffold_repair_summary=repair.summary,
                )

            try:
                from otto.v5_capability_inventory import check_route_registration_isolation

                route_isolation_feedback = check_route_registration_isolation(
                    project_dir,
                    graph=read_graph(project_dir),
                    architect_task_id=architect_tid,
                )
            except Exception as exc:  # noqa: BLE001
                route_isolation_feedback = None
                logger.warning("route registration isolation check failed: %s", exc)
            if route_isolation_feedback is not None:
                _emit(on_event, {
                    "event": "architect_contract_invalid",
                    "task_id": architect_tid,
                    "reason": route_isolation_feedback.get("kind"),
                    "structured_reason": route_isolation_feedback,
                })
                retry_architect = _reenter_or_block_architect_contract(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    child_results=child_results,
                    completed=completed,
                    feedback=route_isolation_feedback,
                    origin="architect_contract",
                    on_event=on_event,
                )
                break

            parent_id = _parent_task_id_for_child(
                project_dir,
                architect_tid,
                str(architect_task.get("integration_branch") or "main"),
            )
            try:
                from otto.v5_capability_inventory import persist_foundation_contracts_from_charter

                foundation_contracts, foundation_findings = (
                    persist_foundation_contracts_from_charter(
                        project_dir,
                        parent_task_id=parent_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                foundation_contracts = []
                foundation_findings = []
                logger.warning("foundation contracts parse failed: %s", exc)
            if foundation_findings:
                foundation_feedback = {
                    "kind": "foundation_contracts_contract_invalid",
                    "step_id": "architect_foundation_contracts",
                    "message": "architect Foundation Contracts block is invalid",
                    "architect_task_id": architect_tid,
                    "parent_task_id": parent_id,
                    "contract_findings": [
                        {"kind": f.kind, "reference": f.reference, "detail": f.detail}
                        for f in foundation_findings
                    ],
                    "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                _emit(on_event, {
                    "event": "architect_contract_invalid",
                    "task_id": architect_tid,
                    "reason": foundation_feedback.get("kind"),
                    "structured_reason": foundation_feedback,
                })
                retry_architect = _reenter_or_block_architect_contract(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    child_results=child_results,
                    completed=completed,
                    feedback=foundation_feedback,
                    origin="architect_contract",
                    on_event=on_event,
                )
                break
            if foundation_contracts:
                _emit(on_event, {
                    "event": "foundation_contracts_recorded",
                    "architect_task_id": architect_tid,
                    "count": len(foundation_contracts),
                })
            else:
                foundation_contracts = _foundation_contracts_for_parent(
                    project_dir,
                    parent_id,
                    read_graph(project_dir).get("tasks") or {},
                )

            isolation_feedback = _foundation_isolation_feedback(
                parent_task_id=parent_id,
                architect_task_id=architect_tid,
                tasks=read_graph(project_dir).get("tasks") or {},
                contracts=foundation_contracts,
            )
            if isolation_feedback is not None:
                retry_architect = _reenter_or_block_architect_contract(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    child_results=child_results,
                    completed=completed,
                    feedback=isolation_feedback,
                    origin="architect_contract",
                    on_event=on_event,
                )
                break

            # Architect passed AND scaffold preflight is clean.
            # Run shared toolchain preflight in the architect
            # worktree so ignored install dirs exist there before
            # propagation. This is non-blocking optimization state:
            # clean scaffold verification above remains the
            # correctness gate, while this preflight logs exactly
            # what dependency bootstrap commands ran.
            arch_worktree = project_dir / ".worktrees" / architect_tid
            try:
                from otto.v5_clean_verify import preflight_shared_toolchains

                toolchain_started = time.monotonic()
                toolchain_result = preflight_shared_toolchains(
                    arch_worktree,
                    timeout_s=300,
                    logger_fn=lambda m: logger.info("preflight: %s", m),
                )
                toolchain_duration_s = round(time.monotonic() - toolchain_started, 3)
                toolchain_payload = toolchain_result.to_jsonable()
                toolchain_payload["duration_s"] = toolchain_duration_s
                log_path = _write_toolchain_preflight_log(
                    project_dir=project_dir,
                    architect_task_id=architect_tid,
                    retry_count=retry_count,
                    result=toolchain_payload,
                )
                logger.info(
                    "preflight: toolchain preflight completed for architect %s in %.3fs "
                    "(passed=%s, commands=%d)",
                    architect_tid,
                    toolchain_duration_s,
                    toolchain_result.passed,
                    len(toolchain_result.commands),
                )
                _emit(on_event, {
                    "event": "toolchain_preflight_done",
                    "architect_task_id": architect_tid,
                    "passed": toolchain_result.passed,
                    "command_count": len(toolchain_result.commands),
                    "duration_s": toolchain_duration_s,
                    "log_path": str(log_path),
                })
                if not toolchain_result.passed:
                    logger.warning(
                        "toolchain preflight had failures for architect %s: %s",
                        architect_tid,
                        "; ".join(toolchain_result.failure_messages[:3]),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("toolchain preflight failed: %s", exc)

            # Propagate its node_modules/.venv into project_dir so
            # feature children's worktrees can symlink instead of
            # re-running `npm install` / `uv sync`.
            try:
                n = _propagate_install_dirs_from_architect(
                    project_dir, arch_worktree
                )
                logger.info(
                    "preflight: architect %s install-dir propagation complete (count=%d)",
                    architect_tid,
                    n,
                )
                _emit(on_event, {
                    "event": "install_dirs_propagated",
                    "architect_task_id": architect_tid,
                    "count": n,
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "propagate install dirs from architect failed: %s", exc
                )

            # Source-of-truth fix (Part A): build the capability
            # inventory from the actual scaffold + inject it into
            # CHARTER as a managed "Detected Infrastructure" block.
            # This gives feature children a deterministic source
            # for operational facts (scripts, deps, configs) that
            # the architect can't unintentionally contradict in
            # prose.
            inv = None
            try:
                from otto.v5_capability_inventory import (
                    build_inventory, render_inventory, inject_into_charter,
                )
                inv = build_inventory(project_dir)
                rendered = render_inventory(inv)
                if inject_into_charter(project_dir, rendered):
                    logger.info(
                        "Detected Infrastructure section injected into CHARTER.md "
                        "(%d package.jsons, %d pyprojects, %d configs)",
                        len(inv.package_jsons),
                        len(inv.pyprojects),
                        len(inv.known_configs),
                    )
                    _emit(on_event, {
                        "event": "capability_inventory_injected",
                        "package_json_count": len(inv.package_jsons),
                        "pyproject_count": len(inv.pyprojects),
                        "known_config_count": len(inv.known_configs),
                    })
            except Exception as exc:  # noqa: BLE001
                logger.warning("capability inventory injection failed: %s", exc)

            # Source-of-truth fix (Part B): coherence gate
            # (warning-only). For each backticked reference in the
            # architect's "Agent operating notes" section, verify
            # the path/script/command actually resolves against the
            # scaffold. Emit warnings; don't block dispatch.
            try:
                if inv is not None:
                    from otto.v5_capability_inventory import check_coherence as _check_coherence

                    findings = _check_coherence(project_dir, inv)
                    for f in findings:
                        logger.warning(
                            "coherence: %s — %s (in CHARTER operating notes)",
                            f.kind, f.detail,
                        )
                        _emit(on_event, {
                            "event": "coherence_finding",
                            "kind": f.kind,
                            "reference": f.reference,
                            "detail": f.detail,
                        })
            except Exception as exc:  # noqa: BLE001
                logger.warning("coherence check raised: %s", exc)
        if retry_architect:
            continue

        graph = read_graph(project_dir)
        tasks = graph.get("tasks") or {}
        contracts = _foundation_contracts_for_parent(project_dir, parent_task_id, tasks)
        scheduler_feedback = _foundation_scheduler_feedback(
            parent_task_id=parent_task_id,
            tasks=tasks,
            ready=ready,
            in_flight_task_ids=set(in_flight.keys()) | active_task_ids,
            contracts=contracts,
        )
        if scheduler_feedback is not None:
            ready_feature_ids = set(scheduler_feedback.get("ready_feature_task_ids") or [])
            affected_feature_ids = [
                str(task_id)
                for task_id in (scheduler_feedback.get("affected_feature_task_ids") or ready_feature_ids)
                if str(task_id)
            ]
            if scheduler_feedback.get("kind") == "foundation_contracts_missing_after_pass":
                reenter_foundation_ids = [
                    str(task_id)
                    for task_id in (scheduler_feedback.get("mergeable_foundation_task_ids") or [])
                    if str(task_id)
                ]
                reenter_foundation_id = reenter_foundation_ids[0] if reenter_foundation_ids else ""
                if reenter_foundation_id:
                    _emit(on_event, {
                        "event": "architect_contract_invalid",
                        "task_id": reenter_foundation_id,
                        "reason": scheduler_feedback.get("kind"),
                        "structured_reason": scheduler_feedback,
                    })
                    _reenter_or_block_architect_contract(
                        project_dir=project_dir,
                        architect_tid=reenter_foundation_id,
                        child_results=child_results,
                        completed=completed,
                        feedback=scheduler_feedback,
                        origin="foundation_scheduler",
                        on_event=on_event,
                    )
                    foundation_after_reenter = get_task(project_dir, reenter_foundation_id) or {}
                    if str(foundation_after_reenter.get("verdict") or "") != "merge_blocked":
                        continue
                block_reason = dict(scheduler_feedback)
                for feature_id in affected_feature_ids:
                    result = child_results.get(feature_id) or LeadResult(
                        task_id=feature_id,
                        verdict="merge_blocked",
                        decomposition="inline",
                    )
                    _record_task_merge_blocked_reason(
                        project_dir=project_dir,
                        task_id=feature_id,
                        result=result,
                        reason=block_reason["message"],
                        origin="foundation_scheduler",
                        structured_reason=block_reason,
                    )
                    child_results[feature_id] = result
                _emit(on_event, {
                    "event": "foundation_feature_dispatch_blocked",
                    "parent_task_id": parent_task_id,
                    "structured_reason": block_reason,
                })
            terminal_foundation_ids = [
                str(task_id)
                for task_id in (scheduler_feedback.get("terminal_blocked_foundation_task_ids") or [])
                if str(task_id)
            ]
            if terminal_foundation_ids:
                block_reason = dict(scheduler_feedback)
                block_reason["kind"] = "foundation_unsatisfied"
                block_reason["step_id"] = "foundation_scheduler_terminal_block"
                block_reason["message"] = (
                    "feature blocked because a sibling foundation task is terminal and "
                    "foundation contracts cannot be satisfied"
                )
                for feature_id in affected_feature_ids:
                    result = child_results.get(feature_id) or LeadResult(
                        task_id=feature_id,
                        verdict="merge_blocked",
                        decomposition="inline",
                    )
                    _record_task_merge_blocked_reason(
                        project_dir=project_dir,
                        task_id=feature_id,
                        result=result,
                        reason=block_reason["message"],
                        origin="foundation_scheduler",
                        structured_reason=block_reason,
                    )
                    child_results[feature_id] = result
                _emit(on_event, {
                    "event": "foundation_feature_dispatch_blocked",
                    "parent_task_id": parent_task_id,
                    "structured_reason": block_reason,
                })
            ready = [
                entry
                for entry in ready
                if str(entry.get("task_id") or "") not in (ready_feature_ids | set(affected_feature_ids))
            ]
            _emit(on_event, {
                "event": "foundation_feature_dispatch_held",
                "parent_task_id": parent_task_id,
                "structured_reason": scheduler_feedback,
            })

        # Spawn ready tasks up to max_parallel.
        spawned_any = False
        for entry in ready:
            tid = entry["task_id"]
            if not await dispatch_lease.try_acquire(tid):
                continue
            in_flight[tid] = asyncio.create_task(
                _run_child(
                    project_dir=project_dir,
                    entry=entry,
                    config=config,
                    max_parallel=max_parallel,
                    run_started_at=run_started_at,
                    on_event=on_event,
                )
            )
            spawned_any = True
            _emit(on_event, {"event": "child_dispatch", "task_id": tid})

        # If nothing in flight and nothing ready, we're done.
        if not in_flight and not ready:
            _verify_child_branches_reached_parent(
                project_dir=project_dir,
                parent_task_id=parent_task_id,
                on_event=on_event,
            )
            break
        if not in_flight and ready and not spawned_any:
            await dispatch_lease.wait_for_change()
            continue

        # Wait for at least one to complete.
        if in_flight:
            done, _pending = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for fut in done:
                # Find which task this future belongs to.
                tid = next(t for t, f in in_flight.items() if f is fut)
                in_flight.pop(tid, None)
                released = False
                try:
                    result: LeadResult = fut.result()
                    await dispatch_lease.release(tid)
                    released = True
                    child_results[tid] = result
                    _record_reviewed_partial_if_present(project_dir, tid, result)
                    _settle_contract_amendment_dependents(
                        project_dir=project_dir,
                        amendment_id=tid,
                        amendment_result=result,
                        completed=completed,
                        child_results=child_results,
                        on_event=on_event,
                    )
                    if _child_result_allows_upward_merge(project_dir, tid, result):
                        completed.add(tid)
                    _emit(on_event, {
                        "event": "child_done",
                        "task_id": tid,
                        "verdict": result.verdict,
                    })

                    # If this child itself emitted grandchildren, recursively process.
                    if result.decomposition == "emit" and result.emitted_subtask_ids:
                        await _process_children(
                            project_dir=project_dir,
                            parent_task_id=tid,
                            config=config,
                            max_parallel=max_parallel,
                            tree_budget_usd=tree_budget_usd,
                            child_results=child_results,
                            integration_results=integration_results,
                            on_event=on_event,
                            dispatch_lease=dispatch_lease,
                            run_started_at=run_started_at,
                        )
                        # Run this child's integration Lead.
                        integ_result = await _run_integration(
                            project_dir=project_dir,
                            task_id=tid,
                            intent=(get_task(project_dir, tid) or {}).get("intent", ""),
                            config=config,
                            child_results=child_results,
                            integration_results=integration_results,
                            on_event=on_event,
                        )
                        # Propagate this subtree's integration up to the
                        # parent's integration branch. WITHOUT THIS, a
                        # decomposed child's work stays orphaned on
                        # i2p/integ/<tid> and never lands on main — the
                        # chat-platform decomp shipped a broken product
                        # because the web subtree never propagated.
                        _record_reviewed_partial_if_present(
                            project_dir,
                            tid,
                            integ_result,
                        )
                        if _child_result_allows_upward_merge(project_dir, tid, integ_result):
                            try:
                                ok, detail, source, target = _propagate_subtree_integration(
                                    project_dir=project_dir,
                                    task_id=tid,
                                )
                                _emit(on_event, {
                                    "event": "subtree_propagated" if ok else "subtree_propagation_blocked",
                                    "task_id": tid,
                                    "source": source,
                                    "target": target,
                                    "detail": detail,
                                })
                                if not ok:
                                    logger.warning(
                                        "subtree integration propagation failed for %s: %s",
                                        tid, detail,
                                    )
                                    repaired, repair_detail = await _repair_subtree_propagation_once(
                                        project_dir=project_dir,
                                        task_id=tid,
                                        result=integ_result,
                                        source=source,
                                        target=target,
                                        detail=detail,
                                        config=config,
                                        on_event=on_event,
                                    )
                                    if repaired:
                                        ok, detail, source, target = _propagate_subtree_integration(
                                            project_dir=project_dir,
                                            task_id=tid,
                                        )
                                        _emit(on_event, {
                                            "event": (
                                                "subtree_propagated"
                                                if ok
                                                else "subtree_propagation_blocked"
                                            ),
                                            "task_id": tid,
                                            "source": source,
                                            "target": target,
                                            "detail": detail,
                                            "after_repair": True,
                                        })
                                        if ok:
                                            set_verdict(
                                                project_dir,
                                                tid,
                                                cast(Any, integ_result.verdict),
                                                cost_usd=integ_result.cost_usd,
                                            )
                                    if not ok:
                                        final_detail = (
                                            f"{detail}; subtree propagation repair attempt: "
                                            f"{repair_detail}"
                                        )
                                        _record_task_merge_blocked_reason(
                                            project_dir=project_dir,
                                            task_id=tid,
                                            result=integ_result,
                                            reason=final_detail,
                                            origin="subtree_propagation",
                                        )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "subtree propagation crashed for %s: %s",
                                    tid, exc,
                                )
                        elif integ_result.verdict in ("partial", "unverified"):
                            _block_child_before_upward_merge(
                                project_dir=project_dir,
                                child_task_id=tid,
                                result=integ_result,
                                reason=(
                                    "Subtree integration remained "
                                    f"{integ_result.verdict!r}; refusing propagation"
                                ),
                                on_event=on_event,
                            )

                except Exception as exc:  # noqa: BLE001
                    if not released:
                        await dispatch_lease.release(tid)
                    logger.exception("child task wrapper crashed: %s", tid)
                    set_verdict(project_dir, tid, "catastrophic")
                    crash_result = LeadResult(
                        task_id=tid,
                        verdict="catastrophic",
                        decomposition="inline",
                        failure_reason=str(exc),
                    )
                    child_results[tid] = crash_result
                    _settle_contract_amendment_dependents(
                        project_dir=project_dir,
                        amendment_id=tid,
                        amendment_result=crash_result,
                        completed=completed,
                        child_results=child_results,
                        on_event=on_event,
                    )
                    completed.add(tid)
                    _emit(on_event, {
                        "event": "child_crash",
                        "task_id": tid,
                        "error": str(exc),
                    })


async def _run_child(
    *,
    project_dir: Path,
    entry: dict[str, Any],
    config: dict[str, Any],
    max_parallel: int = 1,
    run_started_at: float | None = None,
    on_event: Any = None,
) -> LeadResult:
    """Run one child Lead in its own session + worktree, with provider fallback."""
    tid = entry["task_id"]
    child_session_id = _new_session_id()
    child_session_dir = _paths.session_dir(project_dir, child_session_id)
    child_session_dir.mkdir(parents=True, exist_ok=True)

    # Copy parent's spec so child can read frozen journeys.
    parent_session_dir = Path(entry.get("parent_session_dir", str(child_session_dir)))
    parent_spec = parent_session_dir / "spec" / "spec.json"
    child_spec_dir = child_session_dir / "spec"
    child_spec_dir.mkdir(parents=True, exist_ok=True)
    child_spec_path = child_spec_dir / "spec.json"
    if parent_spec.exists() and not child_spec_path.exists():
        try:
            child_spec_path.write_text(parent_spec.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not copy parent spec to child session: %s", exc)

    # Set up the child's per-task worktree off the parent's integration branch.
    # Queue entries are expected to carry this branch explicitly; missing
    # identity is corrupt state and must not silently fall back to main.
    parent_integration_branch = str(entry.get("integration_branch") or "").strip()
    if not parent_integration_branch:
        reason = "child queue entry missing integration_branch before dispatch"
        logger.error("%s: %s", tid, reason)
        set_verdict(project_dir, tid, "merge_blocked")
        _emit(on_event, {
            "event": "child_worktree_setup_failed",
            "task_id": tid,
            "detail": reason,
        })
        return LeadResult(
            task_id=tid,
            verdict="merge_blocked",
            failure_reason=reason,
            verify_called=True,
            verify_result={
                "verdict": "merge_blocked",
                "summary": reason,
                "phase": "worktree_setup",
            },
        )
    child_worktree: Path | None = None
    try:
        from otto.v5_branching import setup_child_worktree

        child_worktree = setup_child_worktree(
            project_dir=project_dir,
            child_task_id=tid,
            parent_integration_branch=parent_integration_branch,
        )
        if child_worktree is not None:
            # Symlink the worktree under the child's session_dir for Lead's CWD discovery.
            link_path = child_session_dir / "worktree"
            try:
                if not link_path.exists():
                    link_path.symlink_to(child_worktree)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not symlink worktree: %s", exc)
            # Share node_modules / .venv across worktrees. Without this,
            # every child re-downloads packages from scratch (~30-60s each,
            # ~5-7min total on a 7-task tree). Worktrees share the same
            # package.json / pyproject.toml at this branch so the deps are
            # identical. The earlier implementation only looked at
            # project_dir/{node_modules,.venv} — but real projects put them
            # in subdirs (frontend/node_modules, api/.venv), so the symlink
            # never fired. Glob for them now.
            linked_install_dirs = _link_shared_install_dirs(project_dir, child_worktree, tid)
            if linked_install_dirs:
                logger.info(
                    "linked %d shared install dir(s) into child %s",
                    linked_install_dirs,
                    tid,
                )
                _emit(on_event, {
                    "event": "install_dirs_linked",
                    "task_id": tid,
                    "count": linked_install_dirs,
                })
            _emit(on_event, {
                "event": "worktree_created",
                "task_id": tid,
                "path": str(child_worktree),
            })
    except Exception as exc:  # noqa: BLE001
        reason = f"child worktree setup failed before dispatch: {type(exc).__name__}: {exc}"
        logger.error("%s", reason)
        set_verdict(project_dir, tid, "merge_blocked")
        _emit(on_event, {
            "event": "child_worktree_setup_failed",
            "task_id": tid,
            "detail": reason,
        })
        return LeadResult(
            task_id=tid,
            verdict="merge_blocked",
            failure_reason=reason,
            verify_called=True,
            verify_result={
                "verdict": "merge_blocked",
                "summary": reason,
                "phase": "worktree_setup",
            },
        )
    if child_worktree is None:
        reason = "child worktree setup returned no worktree before dispatch"
        logger.error("%s: %s", tid, reason)
        set_verdict(project_dir, tid, "merge_blocked")
        _emit(on_event, {
            "event": "child_worktree_setup_failed",
            "task_id": tid,
            "detail": reason,
        })
        return LeadResult(
            task_id=tid,
            verdict="merge_blocked",
            failure_reason=reason,
            verify_called=True,
            verify_result={
                "verdict": "merge_blocked",
                "summary": reason,
                "phase": "worktree_setup",
            },
        )

    task_entry = get_task(project_dir, tid) or {}
    if task_entry.get("contract_amendment_retry_merge"):
        merge_context = task_entry.get("contract_amendment_merge_context")
        if not isinstance(merge_context, dict):
            merge_context = {}
        retry_session_dir = Path(
            str(merge_context.get("child_session_dir") or child_session_dir)
        )
        retry_result = LeadResult(
            task_id=tid,
            verdict=str(task_entry.get("last_agent_verdict") or "pass"),
            decomposition=str(task_entry.get("decomposition") or "inline"),
            verify_called=True,
        )
        mark_contract_amendment_retry_in_progress(project_dir, tid)
        _emit(on_event, {
            "event": "contract_amendment_leaf_merge_retry",
            "task_id": tid,
            "blocked_on_task_id": task_entry.get("blocked_on_task_id"),
        })
        if _child_result_allows_upward_merge(project_dir, tid, retry_result):
            await _merge_child_branch(
                project_dir=project_dir,
                child_task_id=tid,
                child_worktree=child_worktree,
                child_session_dir=retry_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=retry_result,
                config=config,
                on_event=on_event,
            )
            _persist_successful_contract_amendment_retry(
                project_dir=project_dir,
                task_id=tid,
                verdict=retry_result.verdict,
                cost_usd=retry_result.cost_usd,
                on_event=on_event,
            )
        return retry_result

    context_slice_note = ""
    if _context_slicing_enabled(config):
        try:
            from otto.v5_context_slicer import write_context_slice_for_child

            full_charter_path = child_worktree / "CHARTER.md"
            if not full_charter_path.exists():
                full_charter_path = project_dir / "CHARTER.md"
            child_scope = _child_scope_from_entry(tid, entry)
            slice_result = write_context_slice_for_child(
                project_dir=project_dir,
                child_session_dir=child_session_dir,
                child_scope=child_scope,
                parent_spec_path=parent_spec,
                full_charter_path=full_charter_path,
                child_spec_path=child_spec_path,
            )
            if _context_slice_needs_agent_resolution(slice_result.audit):
                resolved_scope = await _resolve_child_scope_with_agent(
                    project_dir=project_dir,
                    child_session_dir=child_session_dir,
                    child_task_id=tid,
                    child_scope=child_scope,
                    parent_spec_path=parent_spec,
                    fallback_reason=str(slice_result.audit.get("fallback_reason") or ""),
                    config=config,
                    on_event=on_event,
                )
                if resolved_scope is not None:
                    slice_result = write_context_slice_for_child(
                        project_dir=project_dir,
                        child_session_dir=child_session_dir,
                        child_scope=child_scope,
                        parent_spec_path=parent_spec,
                        full_charter_path=full_charter_path,
                        child_spec_path=child_spec_path,
                        scope_resolver=lambda _spec, _scope, _reason: resolved_scope,
                    )
            context_slice_note = slice_result.context_note
            _emit(on_event, {
                "event": "context_slice",
                "task_id": tid,
                "fallback_to_full": slice_result.audit.get("fallback_to_full"),
                "included_entities": slice_result.audit.get("included_entities", []),
                "excluded_entities": slice_result.audit.get("excluded_entities", []),
                "log_path": str(child_session_dir / "context_slice.json"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("context slicing failed for child %s: %s", tid, exc)
            log_path = _write_context_slice_failure_log(
                child_session_dir=child_session_dir,
                child_id=tid,
                reason=f"{type(exc).__name__}: {exc}",
            )
            context_slice_note = (
                "Context slicing was requested, but Otto fell back to full "
                f"context after a slicing error. Audit log: `{log_path}`."
            )

    # Augment intent with retry context if this is a re-dispatch after a
    # runner-side check (e.g., scaffold preflight) invalidated the agent's
    # prior verdict. The reason explains what failed; the agent is
    # responsible for fixing it before declaring pass again.
    intent = entry["intent"]
    retry_reason = get_retry_reason(project_dir, tid)
    if retry_reason:
        intent = (
            "## RETRY — previous attempt failed runner-side verification\n\n"
            f"{retry_reason}\n\n"
            "Your previous code is on the same branch; iterate on it, "
            "fix the underlying issue, and re-declare pass only after the "
            "build genuinely works.\n\n"
            "---\n\n"
            "## Original intent (your scope hasn't changed)\n\n"
            f"{intent}"
        )

    # Run the Lead. If we created a worktree, lead.py's _resolve_worktree picks it up.
    result = await _run_lead_with_fallback(
        task_id=tid,
        intent=intent,
        project_dir=project_dir,
        session_dir=child_session_dir,
        integration_branch=parent_integration_branch,
        config=config,
        kind="plan_or_inline",
        context_slice_note=context_slice_note,
        decomp_runtime_context=_build_decomp_runtime_context(
            project_dir=project_dir,
            config=config,
            max_parallel=max_parallel,
            run_started_at=run_started_at,
            spec_path=child_spec_path,
        ),
        on_event=on_event,
    )

    # Merge child's branch into parent's integration branch only after an
    # oracle-backed result. Raw partial/unverified results get one focused
    # verify/repair dispatch; if that still does not produce pass or explicit
    # reviewed-partial, the child becomes merge_blocked instead of best-effort
    # merging upward.
    if child_worktree is not None:
        result = await _ensure_child_merge_ready(
            project_dir=project_dir,
            child_task_id=tid,
            child_worktree=child_worktree,
            child_session_dir=child_session_dir,
            parent_integration_branch=parent_integration_branch,
            original_intent=entry.get("intent") or intent,
            result=result,
            config=config,
            max_parallel=max_parallel,
            run_started_at=run_started_at,
            spec_path=child_spec_path,
            on_event=on_event,
        )
        if _child_result_allows_upward_merge(project_dir, tid, result):
            await _merge_child_branch(
                project_dir=project_dir,
                child_task_id=tid,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=result,
                config=config,
                on_event=on_event,
            )

    return result


def _record_task_merge_blocked_reason(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    reason: str,
    origin: str,
    structured_reason: dict[str, Any] | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "failure_reason": reason,
        "merge_blocked_origin": origin,
        "merge_blocked_reason": reason,
        "contract_amendment_retry_merge": False,
        "contract_amendment_retry_in_progress": False,
    }
    if structured_reason is not None:
        metadata["merge_blocked_structured_reason"] = structured_reason
    set_verdict_and_metadata(
        project_dir,
        task_id,
        "merge_blocked",
        cost_usd=result.cost_usd,
        metadata=metadata,
    )
    result.verdict = "merge_blocked"
    result.failure_reason = reason
    if result.verify_result is None:
        result.verify_result = {}
    if isinstance(result.verify_result, dict):
        result.verify_result["verdict"] = "merge_blocked"
        result.verify_result["summary"] = reason
        if structured_reason is not None:
            result.verify_result["structured_reason"] = structured_reason


def _record_structured_merge_failed(
    *,
    project_dir: Path,
    task_id: str,
    result: LeadResult,
    reason: str,
    origin: str,
    phase: str,
    structured_reason: dict[str, Any],
    on_event: Any = None,
) -> None:
    try:
        result.verdict = "merge_blocked"
        result.failure_reason = reason
        if not isinstance(result.verify_result, dict):
            result.verify_result = {}
        result.verify_result["verdict"] = "merge_blocked"
        result.verify_result["summary"] = reason
        result.verify_result["structured_reason"] = structured_reason
    except Exception as exc:  # noqa: BLE001 - terminal fallback must not raise
        logger.warning(
            "failed to stage in-memory merge_blocked reason for %s: %s",
            task_id,
            exc,
        )

    try:
        _record_task_merge_blocked_reason(
            project_dir=project_dir,
            task_id=task_id,
            result=result,
            reason=reason,
            origin=origin,
            structured_reason=structured_reason,
        )
    except Exception as exc:  # noqa: BLE001 - durable terminal recording is best-effort
        recording_error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            structured_reason["recording_error"] = recording_error
            if isinstance(result.verify_result, dict):
                result.verify_result["structured_reason"] = structured_reason
                result.verify_result["recording_error"] = recording_error
        except Exception:  # noqa: BLE001 - keep terminal recorder no-throw
            pass
        logger.warning(
            "failed to durably record merge_blocked for %s: %s",
            task_id,
            exc,
        )

    try:
        _emit(on_event, {
            "event": "merge_failed",
            "task_id": task_id,
            "phase": phase,
            "detail": reason,
            "structured_reason": structured_reason,
        })
    except Exception as exc:  # noqa: BLE001 - event sink must not reopen terminal path
        logger.warning("failed to emit structured merge_failed for %s: %s", task_id, exc)


def _integration_union_guard_error_feedback(
    *,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    exc: Exception,
    previous_feedback: dict[str, Any] | None = None,
    stale_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": "integration_union_guard_error",
        "step_id": "integration_union_guard",
        "message": (
            "integration union guard errored: "
            f"{type(exc).__name__}: {exc}"
        ),
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "exception": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if previous_feedback is not None:
        feedback["previous_gate_feedback"] = previous_feedback
    if stale_feedback is not None:
        feedback["stale_feedback"] = stale_feedback
    return feedback


def _pre_merge_ref_unresolved_feedback(
    *,
    kind: str,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    detail: str,
    prior_repair_detail: str,
    previous_feedback: dict[str, Any] | None = None,
    stale_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": kind,
        "step_id": "child_merge_retry",
        "message": detail,
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "prior_repair_detail": prior_repair_detail,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if previous_feedback is not None:
        feedback["previous_gate_feedback"] = previous_feedback
    if stale_feedback is not None:
        feedback["stale_feedback"] = stale_feedback
    return feedback


def _child_merge_conflict_smoke_failed_feedback(
    *,
    child_task_id: str,
    parent_integration_branch: str,
    source_branch: str,
    pre_merge_ref: str,
    detail: str,
    oracle: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "kind": "child_merge_conflict_smoke_failed",
        "step_id": "child_merge_conflict_repair_smoke",
        "message": detail,
        "task_id": child_task_id,
        "parent_integration_branch": parent_integration_branch,
        "source_branch": source_branch,
        "pre_merge_ref": pre_merge_ref,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if oracle is not None:
        feedback["oracle"] = oracle
    if exc is not None:
        feedback["exception"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    return feedback


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
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
    paths = _git_diff_name_only(child_worktree)
    feedback = dict(gate_feedback or {})
    feedback.setdefault("kind", "upward_merge_gate_blocked")
    feedback.setdefault("step_id", "upward_merge_gate")
    feedback.setdefault("message", original_detail)
    feedback.setdefault("paths", paths)
    feedback.setdefault("parent_integration_branch", parent_integration_branch)
    feedback.setdefault("_written_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _emit(on_event, {
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
    _emit(on_event, {
        "event": "upward_merge_gate_repair_done",
        "task_id": child_task_id,
        "ok": repair.verdict == "pass",
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
        "escalation": repair.escalation,
    })
    if repair.verdict != "pass":
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
    base_ref = _git_capture(project_dir, ["merge-base", parent_integration_branch, source_branch])
    target_head = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
    source_head = _git_capture(project_dir, ["rev-parse", source_branch])
    feedback: dict[str, Any] = {
        "kind": "stale_integration_target_after_repair",
        "step_id": "child_merge_retry",
        "message": detail,
        "paths": _git_changed_paths_between_refs(project_dir, base_ref, source_branch)
        if base_ref
        else [],
        "parent_integration_branch": parent_integration_branch,
        "prior_repair_detail": prior_repair_detail,
        "origin": origin,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        repaired, repair_detail = await _repair_child_upward_merge_gate_once(
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
class _StaleTargetRetryResult:
    ok: bool
    detail: str
    pre_merge_ref: str
    terminal_recorded: bool = False


async def _repair_stale_target_and_retry_merge(
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
    check_union_after_merge: bool = False,
    emit_union_feedback: bool = False,
    on_event: Any = None,
) -> _StaleTargetRetryResult:
    """Re-enter the existing child repair loop, retry merge, and own terminal blocks."""
    from otto.v5_branching import merge_child_into_integration

    if emit_union_feedback and previous_feedback is not None:
        union_detail = str(
            previous_feedback.get("message")
            or _integration_union_reason_text(previous_feedback)
        )
        _emit(on_event, {
            "event": "integration_union_incomplete",
            "task_id": child_task_id,
            "into": parent_integration_branch,
            "detail": union_detail,
            "structured_reason": previous_feedback,
            "after_repair": True,
        })

    stale_repaired, stale_detail, stale_feedback = await _repair_child_stale_target_gate_once(
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
    ) -> _StaleTargetRetryResult:
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=reason,
            origin=origin,
            phase=terminal_phase,
            structured_reason=structured_reason,
            on_event=on_event,
        )
        return _StaleTargetRetryResult(
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

    pre_merge_ref = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
    if not pre_merge_ref:
        reason = (
            "stale target retry could not resolve integration pre-merge ref; "
            f"stale target repair attempt: {stale_detail}; "
            f"prior repair attempt: {prior_repair_detail}"
        )
        feedback = _pre_merge_ref_unresolved_feedback(
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
    feedback = _foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
        operation="stale_target_retry_merge_delta",
    )
    if feedback is not None:
        reason = _foundation_contract_write_block_detail(feedback)
        return record_terminal(reason=reason, structured_reason=feedback)
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

    if ok and run_smoke_preflight:
        try:
            oracle = await _run_integration_smoke_preflight_with_repair(
                project_dir=project_dir,
                worktree_path=project_dir,
                task_id=child_task_id,
                phase="child_merge_conflict_repair",
                session_dir=child_session_dir,
                config=config,
                integration_branch=parent_integration_branch,
                on_event=on_event,
            )
            if _preflight_repair_escalated(oracle) or _integration_smoke_blocks(oracle):
                ok = False
                merge_detail = _preflight_blocking_summary(
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
            followup_feedback = _record_and_check_integration_union(
                project_dir=project_dir,
                parent_integration_branch=parent_integration_branch,
                child_task_id=child_task_id,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
            )
        except Exception as exc:  # noqa: BLE001 - keep terminal block structured
            feedback = _integration_union_guard_error_feedback(
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
                or _integration_union_reason_text(followup_feedback)
            )
            terminal_feedback = dict(followup_feedback)
            terminal_feedback["stale_target_repair"] = {
                "kind": "stale_integration_target_after_repair",
                "repair_detail": stale_detail,
                "prior_repair_detail": prior_repair_detail,
                "stale_feedback": stale_feedback,
                "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            reason = (
                f"{followup_detail}; stale target repair attempt: {stale_detail}; "
                f"prior repair attempt: {prior_repair_detail}"
            )
            return record_terminal(reason=reason, structured_reason=terminal_feedback)

    return _StaleTargetRetryResult(
        ok=True,
        detail=merge_detail,
        pre_merge_ref=pre_merge_ref,
    )


async def _merge_child_branch(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    child_session_dir: Path,
    parent_integration_branch: str,
    result: LeadResult,
    config: dict[str, Any],
    on_event: Any = None,
) -> None:
    """Commit the child's worktree changes and merge into parent's integration branch.

    Best-effort: on any failure, mark the child's verdict as merge_blocked
    (without crashing the parent run).
    """
    from otto.v5_branching import (
        child_branch_name,
        commit_worktree,
        merge_child_into_integration,
    )

    source_branch = child_branch_name(child_task_id)
    commit_msg = f"v5 task {child_task_id}: {result.verdict}"
    feedback = _foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_git_diff_name_only(child_worktree),
        operation="child_worktree_commit",
    )
    if feedback is not None:
        detail = _foundation_contract_write_block_detail(feedback)
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="foundation_contract_write_gate",
            phase="commit",
            structured_reason=feedback,
            on_event=on_event,
        )
        return
    ok, detail = commit_worktree(worktree_path=child_worktree, message=commit_msg)
    if not ok:
        logger.warning("commit_worktree(%s) failed: %s", child_task_id, detail)
        feedback = {
            "kind": "child_commit_failed",
            "step_id": "child_commit",
            "message": detail,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="commit",
            phase="commit",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    pre_merge_ref = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
    feedback = _foundation_contract_write_feedback(
        project_dir=project_dir,
        acting_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
        changed_paths=_git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
        operation="child_branch_merge_delta",
    )
    if feedback is not None:
        detail = _foundation_contract_write_block_detail(feedback)
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="foundation_contract_write_gate",
            phase="merge",
            structured_reason=feedback,
            on_event=on_event,
        )
        return
    try:
        ok, detail = merge_child_into_integration(
            project_dir=project_dir,
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
        )
    except MergeWorktreeDirtyError as exc:
        ok = False
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
        ok = False
        detail = f"merge_child_into_integration crashed: {type(exc).__name__}: {exc}"
    if not ok and _looks_like_merge_conflict(detail):
        try:
            repaired, repair_detail = await _repair_child_merge_conflict_once(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                config=config,
                original_detail=detail,
                on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
            feedback = _child_repair_helper_crashed_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                origin="merge_conflict_repair",
                phase="merge_conflict_repair",
                exc=exc,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="merge_conflict_repair",
                phase="merge_conflict_repair",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if repaired:
            pre_merge_ref = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
            feedback = _foundation_contract_write_feedback(
                project_dir=project_dir,
                acting_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                changed_paths=_git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
                operation="merge_after_conflict_repair_delta",
            )
            if feedback is not None:
                detail = _foundation_contract_write_block_detail(feedback)
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=detail,
                    origin="foundation_contract_write_gate",
                    phase="merge_conflict_repair",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            try:
                ok, detail = merge_child_into_integration(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                )
            except MergeWorktreeDirtyError as exc:
                ok = False
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
                ok = False
                detail = f"merge after conflict repair crashed: {type(exc).__name__}: {exc}"
            if ok:
                try:
                    oracle = await _run_integration_smoke_preflight_with_repair(
                        project_dir=project_dir,
                        worktree_path=project_dir,
                        task_id=child_task_id,
                        phase="child_merge_conflict_repair",
                        session_dir=child_session_dir,
                        config=config,
                        integration_branch=parent_integration_branch,
                        on_event=on_event,
                    )
                except Exception as exc:  # noqa: BLE001 - terminal block must stay structured
                    detail = (
                        "Child merge conflict repair smoke oracle crashed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    feedback = _child_merge_conflict_smoke_failed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        detail=detail,
                        exc=exc,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=detail,
                        origin="child_merge_conflict_smoke",
                        phase="child_merge_conflict_repair",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
                try:
                    smoke_blocks = (
                        _preflight_repair_escalated(oracle)
                        or _integration_smoke_blocks(oracle)
                    )
                    detail = _preflight_blocking_summary(
                        "Child merge conflict repair smoke oracle failed",
                        oracle,
                    ) if smoke_blocks else ""
                except Exception as exc:  # noqa: BLE001 - smoke evaluation must stay structured
                    detail = (
                        "Child merge conflict repair smoke oracle evaluation crashed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    feedback = _child_merge_conflict_smoke_failed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        detail=detail,
                        oracle=oracle if isinstance(oracle, dict) else None,
                        exc=exc,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=detail,
                        origin="child_merge_conflict_smoke",
                        phase="child_merge_conflict_repair",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
                if smoke_blocks:
                    feedback = _child_merge_conflict_smoke_failed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        detail=detail,
                        oracle=oracle,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=detail,
                        origin="child_merge_conflict_smoke",
                        phase="child_merge_conflict_repair",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
            else:
                try:
                    retry = await _repair_stale_target_and_retry_merge(
                        project_dir=project_dir,
                        child_task_id=child_task_id,
                        child_worktree=child_worktree,
                        child_session_dir=child_session_dir,
                        parent_integration_branch=parent_integration_branch,
                        result=result,
                        config=config,
                        detail=detail,
                        prior_repair_detail=repair_detail,
                        origin="stale_target_merge_gate",
                        terminal_phase="merge",
                        source_branch=source_branch,
                        run_smoke_preflight=True,
                        on_event=on_event,
                    )
                except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
                    feedback = _child_repair_helper_crashed_feedback(
                        child_task_id=child_task_id,
                        parent_integration_branch=parent_integration_branch,
                        source_branch=source_branch,
                        pre_merge_ref=pre_merge_ref,
                        origin="stale_target_merge_gate",
                        phase="merge",
                        exc=exc,
                    )
                    _record_structured_merge_failed(
                        project_dir=project_dir,
                        task_id=child_task_id,
                        result=result,
                        reason=str(feedback["message"]),
                        origin="stale_target_merge_gate",
                        phase="merge",
                        structured_reason=feedback,
                        on_event=on_event,
                    )
                    return
                if retry.terminal_recorded:
                    return
                ok = retry.ok
                detail = retry.detail
                pre_merge_ref = retry.pre_merge_ref
        else:
            detail = f"{detail}; conflict repair attempt: {repair_detail}"
    if not ok and not _looks_like_merge_conflict(detail):
        try:
            repaired, repair_detail = await _repair_child_upward_merge_gate_once(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=result,
                config=config,
                original_detail=detail,
                on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
            feedback = _child_repair_helper_crashed_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                origin="upward_merge_gate",
                phase="upward_merge_gate",
                exc=exc,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="upward_merge_gate",
                phase="upward_merge_gate",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if repaired:
            pre_merge_ref = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
            feedback = _foundation_contract_write_feedback(
                project_dir=project_dir,
                acting_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                changed_paths=_git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
                operation="merge_after_upward_repair_delta",
            )
            if feedback is not None:
                detail = _foundation_contract_write_block_detail(feedback)
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=detail,
                    origin="foundation_contract_write_gate",
                    phase="upward_merge_gate",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            try:
                ok, detail = merge_child_into_integration(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                )
            except MergeWorktreeDirtyError as exc:
                ok = False
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
                ok = False
                detail = f"merge after upward repair crashed: {type(exc).__name__}: {exc}"
        if not ok:
            detail = f"{detail}; upward merge gate repair attempt: {repair_detail}"
    if not ok:
        logger.warning("merge_child_into_integration(%s) failed: %s", child_task_id, detail)
        feedback = {
            "kind": "upward_merge_gate_blocked",
            "step_id": "upward_merge_gate",
            "message": detail,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "pre_merge_ref": pre_merge_ref,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=detail,
            origin="upward_merge_gate",
            phase="merge",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    if not pre_merge_ref:
        reason = "integration union guard could not resolve pre-merge ref"
        feedback = _pre_merge_ref_unresolved_feedback(
            kind="integration_union_pre_merge_ref_unresolved",
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            detail=reason,
            prior_repair_detail="",
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=reason,
            origin="integration_union_guard",
            phase="integration_union_guard",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    try:
        union_feedback = _record_and_check_integration_union(
            project_dir=project_dir,
            parent_integration_branch=parent_integration_branch,
            child_task_id=child_task_id,
            source_branch=source_branch,
            pre_merge_ref=pre_merge_ref,
        )
    except Exception as exc:  # noqa: BLE001 - keep union guard failures structured
        feedback = _integration_union_guard_error_feedback(
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            source_branch=source_branch,
            pre_merge_ref=pre_merge_ref,
            exc=exc,
        )
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=str(feedback["message"]),
            origin="integration_union_guard",
            phase="integration_union_guard",
            structured_reason=feedback,
            on_event=on_event,
        )
        return
    if union_feedback is not None:
        detail = str(union_feedback.get("message") or _integration_union_reason_text(union_feedback))
        _emit(on_event, {
            "event": "integration_union_incomplete",
            "task_id": child_task_id,
            "into": parent_integration_branch,
            "detail": detail,
            "structured_reason": union_feedback,
        })
        parent_task_id = _parent_task_id_for_child(
            project_dir,
            child_task_id,
            parent_integration_branch,
        )
        amendment_contract = _foundation_contract_for_feedback_path(
            project_dir=project_dir,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            feedback=union_feedback,
        )
        if amendment_contract is not None:
            contract_path = _normalize_contract_path(str(amendment_contract.get("path") or ""))
            owner_id = str(amendment_contract.get("owner_task_id") or "").strip()
            current_attempts = _contract_amendment_attempt_count(
                get_task(project_dir, child_task_id) or {},
                contract_path,
            )
            if current_attempts >= MAX_CONTRACT_AMENDMENT_ATTEMPTS:
                feedback = _contract_amendment_exhausted_feedback(
                    child_task_id=child_task_id,
                    parent_task_id=parent_task_id,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    contract_path=contract_path,
                    owner_id=owner_id,
                    union_feedback=union_feedback,
                    attempt_count=current_attempts,
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=str(feedback["message"]),
                    origin="contract_amendment",
                    phase="foundation_contract_amendment",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            _schedule_foundation_contract_amendment(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_task_id=parent_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                union_feedback=union_feedback,
                contract=amendment_contract,
                on_event=on_event,
            )
            return
        try:
            repaired, repair_detail = await _repair_child_upward_merge_gate_once(
                project_dir=project_dir,
                child_task_id=child_task_id,
                child_worktree=child_worktree,
                child_session_dir=child_session_dir,
                parent_integration_branch=parent_integration_branch,
                result=result,
                config=config,
                original_detail=detail,
                on_event=on_event,
                gate_feedback=union_feedback,
                origin="integration_union_guard",
            )
        except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
            feedback = _child_repair_helper_crashed_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                origin="integration_union_guard",
                phase="integration_union_guard",
                exc=exc,
                previous_feedback=union_feedback,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if not repaired:
            reason = f"{detail}; union repair attempt: {repair_detail}"
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=reason,
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=union_feedback,
                on_event=on_event,
            )
            return

        pre_merge_ref = _git_capture(project_dir, ["rev-parse", parent_integration_branch])
        if not pre_merge_ref:
            reason = (
                "integration union repair retry could not resolve pre-merge ref; "
                f"union repair attempt: {repair_detail}; "
                f"original refusal: {union_feedback.get('message')}"
            )
            feedback = _pre_merge_ref_unresolved_feedback(
                kind="integration_union_pre_merge_ref_unresolved",
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                detail=reason,
                prior_repair_detail=repair_detail,
                previous_feedback=union_feedback,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=reason,
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        feedback = _foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            changed_paths=_git_changed_paths_between_refs(project_dir, pre_merge_ref, source_branch),
            operation="merge_after_integration_union_repair_delta",
        )
        if feedback is not None:
            detail = _foundation_contract_write_block_detail(feedback)
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=detail,
                origin="foundation_contract_write_gate",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        try:
            ok, detail = merge_child_into_integration(
                project_dir=project_dir,
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
            )
        except MergeWorktreeDirtyError as exc:
            ok = False
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001 - merge path must not escape post-commit
            ok = False
            detail = f"merge after integration union repair crashed: {type(exc).__name__}: {exc}"
        if not ok:
            try:
                retry = await _repair_stale_target_and_retry_merge(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    child_worktree=child_worktree,
                    child_session_dir=child_session_dir,
                    parent_integration_branch=parent_integration_branch,
                    result=result,
                    config=config,
                    detail=detail,
                    prior_repair_detail=repair_detail,
                    origin="integration_union_guard",
                    terminal_phase="integration_union_guard",
                    source_branch=source_branch,
                    previous_feedback=union_feedback,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
                feedback = _child_repair_helper_crashed_feedback(
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    exc=exc,
                    previous_feedback=union_feedback,
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=str(feedback["message"]),
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            if retry.terminal_recorded:
                return
            ok = retry.ok
            detail = retry.detail
            pre_merge_ref = retry.pre_merge_ref

        try:
            followup_feedback = _record_and_check_integration_union(
                project_dir=project_dir,
                parent_integration_branch=parent_integration_branch,
                child_task_id=child_task_id,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
            )
        except Exception as exc:  # noqa: BLE001 - keep union guard failures structured
            feedback = _integration_union_guard_error_feedback(
                child_task_id=child_task_id,
                parent_integration_branch=parent_integration_branch,
                source_branch=source_branch,
                pre_merge_ref=pre_merge_ref,
                exc=exc,
                previous_feedback=union_feedback,
            )
            _record_structured_merge_failed(
                project_dir=project_dir,
                task_id=child_task_id,
                result=result,
                reason=str(feedback["message"]),
                origin="integration_union_guard",
                phase="integration_union_guard",
                structured_reason=feedback,
                on_event=on_event,
            )
            return
        if followup_feedback is not None:
            followup_detail = str(
                followup_feedback.get("message")
                or _integration_union_reason_text(followup_feedback)
            )
            try:
                retry = await _repair_stale_target_and_retry_merge(
                    project_dir=project_dir,
                    child_task_id=child_task_id,
                    child_worktree=child_worktree,
                    child_session_dir=child_session_dir,
                    parent_integration_branch=parent_integration_branch,
                    result=result,
                    config=config,
                    detail=followup_detail,
                    prior_repair_detail=repair_detail,
                    origin="integration_union_guard",
                    terminal_phase="integration_union_guard",
                    source_branch=source_branch,
                    previous_feedback=followup_feedback,
                    check_union_after_merge=True,
                    emit_union_feedback=True,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001 - repair helper crash is terminal-structured
                feedback = _child_repair_helper_crashed_feedback(
                    child_task_id=child_task_id,
                    parent_integration_branch=parent_integration_branch,
                    source_branch=source_branch,
                    pre_merge_ref=pre_merge_ref,
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    exc=exc,
                    previous_feedback=followup_feedback,
                )
                _record_structured_merge_failed(
                    project_dir=project_dir,
                    task_id=child_task_id,
                    result=result,
                    reason=str(feedback["message"]),
                    origin="integration_union_guard",
                    phase="integration_union_guard",
                    structured_reason=feedback,
                    on_event=on_event,
                )
                return
            if retry.terminal_recorded:
                return
            ok = retry.ok
            detail = retry.detail
            pre_merge_ref = retry.pre_merge_ref

    if not ok:
        reason = f"child merge path ended without success: {detail}"
        feedback = {
            "kind": "child_merge_path_incomplete",
            "step_id": "child_merge",
            "message": reason,
            "task_id": child_task_id,
            "source_branch": source_branch,
            "parent_integration_branch": parent_integration_branch,
            "pre_merge_ref": pre_merge_ref,
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _record_structured_merge_failed(
            project_dir=project_dir,
            task_id=child_task_id,
            result=result,
            reason=reason,
            origin="child_merge",
            phase="merge",
            structured_reason=feedback,
            on_event=on_event,
        )
        return

    _emit(on_event, {
        "event": "merged",
        "task_id": child_task_id,
        "into": parent_integration_branch,
    })


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
    base_ref = _git_capture(project_dir, ["merge-base", target_branch, source_branch])
    packet = _build_repair_packet(
        session_dir=child_session_dir,
        repair_slug=repair_slug,
        worktree_path=child_worktree,
        task_id=child_task_id,
        phase="merge",
        repair_phase="merge",
        verify_scope="subtree",
        config=config,
        budget_prefix="merge_repair",
        default_agent_turns=1,
        default_oracle_invocations=3,
        latest_oracle_result=lambda oracle_command: _make_initial_oracle_payload(
            worktree=child_worktree,
            scope="subtree",
            oracle_command=oracle_command,
            issue_kind="merge_conflict",
            issue_message=original_detail,
            step_id="merge_retry",
            paths=paths,
        ),
        product_contract={
            **_worktree_product_contract(worktree=child_worktree),
            "contract_deltas": {
                "target_to_source_diff_stat": _git_diff_stat_for_ref_range(
                    project_dir,
                    target_branch,
                    source_branch,
                ),
                "source_to_target_diff_stat": _git_diff_stat_for_ref_range(
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
                "stat": _git_diff_stat_for_ref_range(project_dir, base_ref, source_branch),
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
    _emit(on_event, {
        "event": "merge_conflict_repair_agent_start",
        "task_id": child_task_id,
        "paths": list(paths),
        "conflict_packet": conflict_packet_path,
        "repair_packet": str(packet.packet_path),
    })

    async def commit_hook(_packet: RepairPacket, _oracle_result: Any) -> tuple[bool, str]:
        from otto.v5_branching import commit_worktree

        feedback = _foundation_contract_write_feedback(
            project_dir=project_dir,
            acting_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
            changed_paths=_git_diff_name_only(child_worktree),
            operation="merge_conflict_repair_commit",
        )
        if feedback is not None:
            return False, _foundation_contract_write_block_detail(feedback)
        return commit_worktree(
            worktree_path=child_worktree,
            message=f"v5 merge conflict repair: {child_task_id}",
        )

    repair = await run_oracle_repair_agent(
        packet,
        config=config,
        commit_hook=commit_hook,
    )
    _emit(on_event, {
        "event": "merge_conflict_repair_agent_done",
        "task_id": child_task_id,
        "ok": repair.verdict == "pass",
        "summary": repair.summary,
        "repair_packet": repair.packet_path,
    })
    return repair.verdict == "pass", repair.summary


async def _run_lead_with_fallback(
    *,
    task_id: str,
    intent: str,
    project_dir: Path,
    session_dir: Path,
    integration_branch: str | None,
    config: dict[str, Any],
    kind: str = "plan_or_inline",
    child_summaries: list[dict[str, Any]] | None = None,
    context_slice_note: str = "",
    decomp_runtime_context: dict[str, Any] | None = None,
    on_event: Any = None,
    execution_scope: ExecutionScope = "leaf",
) -> LeadResult:
    """Run a Lead with task-level provider fallback.

    First attempt uses the configured provider. If it returns
    verdict=catastrophic with a provider-exhausted-style failure_reason, AND
    a fallback_provider is configured, swap providers and retry once with
    the same task_id (preserving lineage).

    Per philosophy: never infinite-loop; cap fallback retries at 1.
    """
    import time as _time

    from otto.v5_provider_fallback import (
        append_attempt,
        fallback_provider as _fallback_provider,
        should_fallback,
    )

    started = _time.monotonic()
    attempt_started = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())

    # Attempt 1: configured provider.
    provider_a = (
        config.get("provider")
        or (config.get("defaults", {}) or {}).get("provider")
        or "claude"
    )
    result_a = await run_lead(
        task_id=task_id,
        intent=intent,
        project_dir=project_dir,
        session_dir=session_dir,
        integration_branch=integration_branch,
        config=config,
        kind=cast(LeadKind, kind),
        child_summaries=child_summaries,
        context_slice_note=context_slice_note,
        decomp_runtime_context=decomp_runtime_context,
        execution_scope=execution_scope,
    )
    duration_a = _time.monotonic() - started
    append_attempt(
        session_dir / "summary.json",
        provider=provider_a,
        cost_usd=result_a.cost_usd,
        outcome=result_a.verdict,
        duration_s=duration_a,
        started_at=attempt_started,
    )

    if result_a.verdict != "catastrophic":
        return result_a

    do_fallback, reason = should_fallback(result_a.failure_reason, config)
    if not do_fallback:
        return result_a

    fb = _fallback_provider(config)
    if not fb or fb == provider_a:
        return result_a

    _emit(on_event, {
        "event": "provider_fallback",
        "task_id": task_id,
        "from": provider_a,
        "to": fb,
        "reason": reason,
    })

    # Attempt 2: fallback provider (mutate a copy of config).
    fallback_config = dict(config)
    fallback_config["provider"] = fb
    overrides = dict(fallback_config.get("_cli_overrides") or {})
    overrides["provider"] = fb
    fallback_config["_cli_overrides"] = overrides

    fallback_started = _time.monotonic()
    fallback_started_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
    result_b = await run_lead(
        task_id=task_id,
        intent=intent,
        project_dir=project_dir,
        session_dir=session_dir,
        integration_branch=integration_branch,
        config=fallback_config,
        kind=cast(LeadKind, kind),
        child_summaries=child_summaries,
        context_slice_note=context_slice_note,
        decomp_runtime_context=decomp_runtime_context,
        execution_scope=execution_scope,
    )
    append_attempt(
        session_dir / "summary.json",
        provider=fb,
        cost_usd=result_b.cost_usd,
        outcome=result_b.verdict,
        duration_s=_time.monotonic() - fallback_started,
        started_at=fallback_started_iso,
        fallback_reason=reason,
    )
    return result_b


def _preflight_issue_payload(issue: Any) -> dict[str, Any]:
    return {
        "kind": getattr(issue, "kind", "unknown"),
        "severity": getattr(issue, "severity", "warn"),
        "message": getattr(issue, "message", ""),
        "task_id": getattr(issue, "task_id", None),
    }


def _run_integration_smoke_preflight(
    *,
    worktree_path: Path,
    task_id: str,
    phase: str,
    journey_scope: ExecutionScope = "subtree_integration",
    spec_path: Path | None = None,
    journey_artifact_dir: Path | None = None,
    on_event: Any = None,
) -> dict[str, Any]:
    """Run clean-deploy smoke for an integration worktree and serialize it."""
    payload: dict[str, Any] = {
        "check": "smoke_clean_deploy",
        "phase": phase,
        "task_id": task_id,
        "cwd": str(worktree_path),
        "passed": True,
        "issues": [],
        "error": None,
    }
    try:
        logger.info(
            "preflight: running %s integration clean-deploy check in %s",
            phase,
            worktree_path,
        )
        clean_oracle_result = verify_from_clean_oracle(
            worktree_path,
            scope="subtree",
            timeout_s=90,
            port_wait_s=12,
            logger_fn=lambda m: logger.info("preflight: %s", m),
            journey_scope=journey_scope,
            spec_path=spec_path,
            journey_artifact_dir=journey_artifact_dir,
        )
        payload["clean_oracle_result"] = clean_oracle_result.to_jsonable()
        smoke_issues = preflight_issues_from_clean_oracle(
            clean_oracle_result,
            surface="clean_deploy",
        )
        payload["issues"] = [_preflight_issue_payload(issue) for issue in smoke_issues]
        payload["passed"] = not smoke_issues
        for issue in smoke_issues:
            issue_payload = _preflight_issue_payload(issue)
            log_fn = (
                logger.error
                if issue_payload["severity"] in ("error", "block")
                else logger.warning
            )
            log_fn(
                "preflight %s [%s]: %s",
                issue_payload["kind"],
                issue_payload["severity"],
                issue_payload["message"],
            )
            _emit(on_event, {
                "event": "preflight_issue",
                "phase": phase,
                "worktree": str(worktree_path),
                "kind": issue_payload["kind"],
                "severity": issue_payload["severity"],
                "message": issue_payload["message"],
            })
    except Exception as exc:  # noqa: BLE001 - report to agent, do not crash runner
        payload["passed"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("integration %s smoke check raised: %s", phase, exc)
        payload["issues"] = [
            {
                "kind": "oracle_infra_error",
                "severity": "block",
                "message": payload["error"],
                "task_id": task_id,
            }
        ]
        _emit(on_event, {
            "event": "preflight_issue",
            "phase": phase,
            "worktree": str(worktree_path),
            "kind": "oracle_infra_error",
            "severity": "block",
            "message": payload["error"],
        })
    return payload


async def _run_integration_smoke_preflight_with_repair(
    *,
    project_dir: Path,
    worktree_path: Path,
    task_id: str,
    phase: str,
    session_dir: Path,
    config: dict[str, Any],
    integration_branch: str | None,
    journey_scope: ExecutionScope = "subtree_integration",
    on_event: Any = None,
) -> dict[str, Any]:
    """Run clean-deploy smoke and repair blocking issues before continuing."""
    spec_path = session_dir / "spec" / "spec.json"
    journey_artifact_dir = session_dir / "journeys" / safe_slug(phase, max_len=48)

    def run_once() -> dict[str, Any]:
        return _run_integration_smoke_preflight(
            worktree_path=worktree_path,
            task_id=task_id,
            phase=phase,
            journey_scope=journey_scope,
            spec_path=spec_path,
            journey_artifact_dir=journey_artifact_dir,
            on_event=on_event,
        )

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first

    return await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=worktree_path,
        session_dir=session_dir,
        config=config,
        task_id=task_id,
        repair_phase="integration_smoke",
        event_prefix="integration_smoke",
        integration_branch=integration_branch,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={
            "smoke_phase": phase,
        },
        journey_scope=journey_scope,
    )


def _integration_smoke_blocks(payload: dict[str, Any]) -> bool:
    if payload.get("error") and payload.get("passed") is False:
        return True
    issues = payload.get("issues") or []
    return any(
        isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
        for issue in issues
    )


def _preflight_repair_escalated(payload: dict[str, Any]) -> bool:
    repair = payload.get("repair")
    return isinstance(repair, dict) and repair.get("terminal_state") == "escalated"


def _preflight_blocked_result(
    *,
    task_id: str,
    preflight_result: dict[str, Any],
) -> LeadResult:
    reason = _preflight_blocking_summary(
        "Integration preflight repair escalated before agent dispatch",
        preflight_result,
    )
    return LeadResult(
        task_id=task_id,
        verdict="merge_blocked",
        verify_called=True,
        verify_result={
            "verdict": "merge_blocked",
            "summary": reason,
            "pre_integration_preflight": preflight_result,
        },
        failure_reason=reason,
    )


def _preflight_blocking_summary(prefix: str, payload: dict[str, Any]) -> str:
    messages = [
        str(issue.get("message") or issue.get("kind"))
        for issue in payload.get("issues", [])
        if isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
    ]
    return prefix + (": " + "; ".join(messages) if messages else "")


def _git_status_short(worktree_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _branch_checked_out(worktree_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _integration_worktree_setup_payload(
    *,
    project_dir: Path,
    task_id: str,
    integration_branch: str,
    worktree_path: Path | None,
    detail: str,
) -> dict[str, Any]:
    actual_branch = _branch_checked_out(worktree_path) if worktree_path is not None else ""
    passed = bool(worktree_path and worktree_path.exists() and actual_branch == integration_branch)
    issue_kind = "integration_worktree_wrong_branch" if worktree_path and actual_branch else "integration_worktree_setup_failed"
    message = detail
    if worktree_path and actual_branch != integration_branch:
        message = (
            f"integration worktree {worktree_path} is on {actual_branch or 'unknown'}; "
            f"expected {integration_branch}"
        )
    return {
        "check": "integration_worktree_setup",
        "task_id": task_id,
        "cwd": str(worktree_path or project_dir),
        "integration_branch": integration_branch,
        "actual_branch": actual_branch,
        "passed": passed,
        "issues": [] if passed else [
            {
                "kind": issue_kind,
                "severity": "block",
                "message": message or "integration worktree setup failed",
            }
        ],
        "error": None if passed else (message or "integration worktree setup failed"),
    }


def _setup_integration_worktree_once(
    *,
    project_dir: Path,
    task_id: str,
    own_integration_branch: str,
    integration_session_dir: Path,
) -> tuple[Path | None, str]:
    from otto.v5_branching import child_worktree_path, ensure_branch_exists
    from otto.worktree import add_worktree

    existing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project_dir), capture_output=True, text=True,
    )
    existing_path: Path | None = None
    if existing.returncode == 0:
        block_path: str | None = None
        for line in existing.stdout.splitlines():
            if line.startswith("worktree "):
                block_path = line[len("worktree "):].strip()
            elif line.startswith("branch ") and block_path:
                if line.endswith(f"/{own_integration_branch}") or line.endswith(own_integration_branch):
                    existing_path = Path(block_path)
                    break

    if existing_path is not None and existing_path.exists():
        integration_worktree = existing_path
    else:
        ensure_branch_exists(project_dir, own_integration_branch, base_ref="main")
        integration_worktree = child_worktree_path(project_dir, f"integ-{task_id}")
        if not (integration_worktree.exists() and (integration_worktree / ".git").exists()):
            add_worktree(
                project_dir=project_dir,
                worktree_path=integration_worktree,
                branch=own_integration_branch,
            )

    if not integration_worktree.exists():
        return None, f"integration worktree path does not exist: {integration_worktree}"
    branch = _branch_checked_out(integration_worktree)
    if branch != own_integration_branch:
        return integration_worktree, (
            f"integration worktree {integration_worktree} is on "
            f"{branch or 'unknown'}; expected {own_integration_branch}"
        )

    link_path = integration_session_dir / "worktree"
    if not link_path.exists():
        try:
            link_path.symlink_to(integration_worktree)
        except OSError as exc:
            logger.warning("symlink worktree failed: %s", exc)
    return integration_worktree, ""


async def _prepare_integration_worktree_with_repair(
    *,
    project_dir: Path,
    task_id: str,
    integration_branch: str,
    integration_session_dir: Path,
    config: dict[str, Any],
    on_event: Any = None,
) -> tuple[Path | None, dict[str, Any]]:
    prepared_path: Path | None = None

    def run_once() -> dict[str, Any]:
        nonlocal prepared_path
        try:
            prepared_path, detail = _setup_integration_worktree_once(
                project_dir=project_dir,
                task_id=task_id,
                own_integration_branch=integration_branch,
                integration_session_dir=integration_session_dir,
            )
        except Exception as exc:  # noqa: BLE001
            prepared_path = None
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("integration worktree setup failed for %s: %s", task_id, exc)
        return _integration_worktree_setup_payload(
            project_dir=project_dir,
            task_id=task_id,
            integration_branch=integration_branch,
            worktree_path=prepared_path,
            detail=detail,
        )

    first = run_once()
    if not _integration_smoke_blocks(first):
        return prepared_path, first

    payload = await _run_preflight_payload_repair_session(
        initial_payload=first,
        run_once=run_once,
        project_dir=project_dir,
        worktree_path=project_dir,
        session_dir=integration_session_dir,
        config=config,
        task_id=task_id,
        repair_phase="integration_worktree_setup",
        event_prefix="integration_worktree_setup",
        integration_branch=integration_branch,
        verify_scope="subtree",
        on_event=on_event,
        integration_context={
            "integration_branch": integration_branch,
        },
    )
    return prepared_path, payload


async def _run_integration(
    *,
    project_dir: Path,
    task_id: str,
    intent: str,
    config: dict[str, Any],
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    on_event: Any = None,
) -> LeadResult:
    """Run an integration Lead for ``task_id`` after children have resolved."""
    integration_session_id = _new_session_id()
    integration_session_dir = _paths.session_dir(project_dir, integration_session_id)
    integration_session_dir.mkdir(parents=True, exist_ok=True)

    # The integration Lead's verify call needs spec.json (same shape as build
    # children get via _run_child). Find any earlier session that has it and
    # copy. Fall back silently if no spec exists yet — verifier handles that.
    target_spec = integration_session_dir / "spec" / "spec.json"
    if not target_spec.exists():
        try:
            sessions_root = project_dir / "otto_logs" / "sessions"
            if sessions_root.exists():
                for sib in sorted(sessions_root.iterdir()):
                    candidate = sib / "spec" / "spec.json"
                    if candidate.exists():
                        target_spec.parent.mkdir(parents=True, exist_ok=True)
                        target_spec.write_text(
                            candidate.read_text(encoding="utf-8"),
                            encoding="utf-8",
                        )
                        break
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("could not copy spec for integration session: %s", exc)

    # Provide child summaries to the integration Lead's prompt.
    summaries = _build_child_summaries(project_dir, task_id, child_results, integration_results)

    # The integration Lead must run in a worktree that holds the merged
    # children's work — a worktree checked out to THIS task's integration
    # branch (where its children merged). Without this, the Lead defaults
    # to project_dir (typically `main`) and verify sees an empty workspace.
    #
    # Each task in the graph stores `integration_branch` = the branch the
    # task itself merges INTO (one level up). For the integration session
    # we instead want THIS task's OWN integration branch (where its children
    # merged), namespaced as `i2p/<task_id>/integration`.
    from otto.v5_branching import integration_branch_name as _integ
    own_integration_branch = _integ(task_id)
    parent_integration_branch = own_integration_branch
    integration_worktree, setup_preflight = await _prepare_integration_worktree_with_repair(
        project_dir=project_dir,
        task_id=task_id,
        integration_branch=parent_integration_branch,
        integration_session_dir=integration_session_dir,
        config=config,
        on_event=on_event,
    )
    if (
        integration_worktree is None
        or _preflight_repair_escalated(setup_preflight)
        or _integration_smoke_blocks(setup_preflight)
    ):
        result = _preflight_blocked_result(
            task_id=task_id,
            preflight_result=setup_preflight,
        )
        integration_results[task_id] = result
        _emit(on_event, {
            "event": "integration_done",
            "task_id": task_id,
            "verdict": result.verdict,
        })
        return result

    integration_cwd = integration_worktree
    preflight_result = await _run_integration_smoke_preflight_with_repair(
        project_dir=project_dir,
        worktree_path=integration_cwd,
        task_id=task_id,
        phase="pre_agent",
        session_dir=integration_session_dir,
        config=config,
        integration_branch=parent_integration_branch,
        on_event=on_event,
    )

    _emit(on_event, {"event": "integration_start", "task_id": task_id})
    if _preflight_repair_escalated(preflight_result):
        result = _preflight_blocked_result(
            task_id=task_id,
            preflight_result=preflight_result,
        )
    else:
        integration_packet_path = _write_integration_packet(
            project_dir=project_dir,
            parent_task_id=task_id,
            session_dir=integration_session_dir,
            child_results=child_results,
            integration_results=integration_results,
            child_summaries=summaries,
            preflight_result=preflight_result,
            integration_branch=parent_integration_branch,
            integration_worktree=integration_cwd,
        )
        result = await run_lead(
            task_id=task_id,
            intent=intent,
            project_dir=project_dir,
            session_dir=integration_session_dir,
            integration_branch=parent_integration_branch,
            config=config,
            kind="integration",
            child_summaries=summaries,
            preflight_result=preflight_result,
            integration_packet_path=str(integration_packet_path),
            execution_scope="subtree_integration",
        )
        _commit_integration_agent_changes(
            project_dir=project_dir,
            task_id=task_id,
            worktree_path=integration_cwd,
            result=result,
            on_event=on_event,
        )
    if _preflight_repair_escalated(preflight_result):
        post_preflight_result = preflight_result
    else:
        post_preflight_result = await _run_integration_smoke_preflight_with_repair(
            project_dir=project_dir,
            worktree_path=integration_cwd,
            task_id=task_id,
            phase="post_agent",
            session_dir=integration_session_dir,
            config=config,
            integration_branch=parent_integration_branch,
            on_event=on_event,
        )
    if result.verify_result is None:
        result.verify_result = {}
    if isinstance(result.verify_result, dict):
        result.verify_result["pre_integration_preflight"] = preflight_result
        result.verify_result["post_integration_preflight"] = post_preflight_result
    if result.verdict != "catastrophic" and _integration_smoke_blocks(post_preflight_result):
        result.verdict = "merge_blocked"
        result.failure_reason = (
            "Post-agent smoke_clean_deploy still has blocking issues: "
            + "; ".join(
                str(issue.get("message") or issue.get("kind"))
                for issue in post_preflight_result.get("issues", [])
                if isinstance(issue, dict)
                and issue.get("severity") in ("error", "block")
            )
        )
        if isinstance(result.verify_result, dict):
            result.verify_result["verdict"] = "merge_blocked"
            result.verify_result["summary"] = result.failure_reason
        set_verdict(project_dir, task_id, "merge_blocked", cost_usd=result.cost_usd)
        _emit(on_event, {
            "event": "integration_smoke_failed",
            "task_id": task_id,
            "verdict": "merge_blocked",
            "worktree": str(integration_cwd),
        })
    integration_results[task_id] = result
    _emit(on_event, {"event": "integration_done", "task_id": task_id, "verdict": result.verdict})
    restore_branch = _integration_restore_branch(project_dir, task_id, config)
    restore_result = await _checkout_v5_branch_clean_with_repair(
        project_dir=project_dir,
        branch=restore_branch,
        context=f"integration_return:{task_id}",
        session_dir=integration_session_dir,
        config=config,
        integration_branch=parent_integration_branch,
        task_id=task_id,
        on_event=on_event,
    )
    if _preflight_repair_escalated(restore_result) or _integration_smoke_blocks(restore_result):
        detail = _preflight_blocking_summary(
            f"could not restore project_dir after integration {task_id} to {restore_branch}",
            restore_result,
        )
        logger.warning(
            "could not restore project_dir after integration %s to %s: %s",
            task_id, restore_branch, detail,
        )
        _emit(on_event, {
            "event": "integration_restore_failed",
            "task_id": task_id,
            "target": restore_branch,
            "detail": detail,
        })
        set_verdict(project_dir, task_id, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"
    return result


def _write_integration_packet(
    *,
    project_dir: Path,
    parent_task_id: str,
    session_dir: Path,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    child_summaries: list[dict[str, Any]],
    preflight_result: dict[str, Any],
    integration_branch: str,
    integration_worktree: Path,
) -> Path:
    from otto.v5_branching import child_branch_name, integration_branch_name

    children: list[dict[str, Any]] = []
    for cid in children_of(project_dir, parent_task_id):
        entry = get_task(project_dir, cid) or {}
        result = integration_results.get(cid) or child_results.get(cid)
        build_branch = child_branch_name(cid)
        subtree_branch = integration_branch_name(cid)
        verdict_payload = (
            result.verify_result
            if result is not None and isinstance(result.verify_result, dict)
            else {}
        )
        children.append({
            "task_id": cid,
            "intent": entry.get("intent", ""),
            "task_graph": entry,
            "session_dir": _find_session_dir_for_task(project_dir, cid),
            "agent_session_id": result.agent_session_id if result else "",
            "branches": {"build": build_branch, "integration": subtree_branch},
            "changed_files": {
                "build": _changed_files_for_branch(project_dir, build_branch),
                "integration": _changed_files_for_branch(project_dir, subtree_branch),
            },
            "verdict": result.verdict if result else entry.get("verdict"),
            "verdict_json": verdict_payload,
            "intent_coverage": verdict_payload.get("intent_coverage") if verdict_payload else None,
            "decisions_appended": verdict_payload.get("decisions_appended") if verdict_payload else [],
            "runner_checks": verdict_payload.get("runner_checks") if verdict_payload else [],
        })
    packet = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parent_task_id": parent_task_id,
        "integration_branch": integration_branch,
        "integration_worktree": str(integration_worktree),
        "child_summaries": child_summaries,
        "children": children,
        "preflight_results": {"pre_agent": preflight_result},
        "applicable_journey_ids": _journey_ids_from_spec(session_dir / "spec" / "spec.json"),
    }
    path = session_dir / "integration_packet.json"
    path.write_text(json.dumps(packet, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def _find_session_dir_for_task(project_dir: Path, task_id: str) -> str:
    sessions_root = project_dir / "otto_logs" / "sessions"
    if not sessions_root.exists():
        return ""
    matches: list[Path] = []
    for summary in sessions_root.glob("*/summary.json"):
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            matches.append(summary.parent)
    return str(sorted(matches)[-1]) if matches else ""


def _changed_files_for_branch(project_dir: Path, branch: str) -> list[str]:
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return []
    base = "main"
    base_exists = subprocess.run(
        ["git", "rev-parse", "--verify", base],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if base_exists.returncode != 0:
        base = "HEAD"
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{branch}"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _journey_ids_from_spec(spec_path: Path) -> list[str]:
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    journeys = payload.get("behavior_journeys") if isinstance(payload, dict) else []
    return [
        str(journey.get("id"))
        for journey in journeys or []
        if isinstance(journey, dict) and journey.get("id")
    ]


def _build_child_summaries(
    project_dir: Path,
    parent_task_id: str,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult] | None = None,
) -> list[dict[str, Any]]:
    """Build the child summary list passed to integration Lead's prompt.

    For merge_blocked children, include the build branch name and a
    pointer so the integration Lead can choose to hand-merge instead of
    re-implementing from scratch. The work is preserved on the branch;
    only the mechanical merge failed.
    """
    from otto.v5_branching import child_branch_name
    integration_results = integration_results or {}
    out: list[dict[str, Any]] = []
    for cid in children_of(project_dir, parent_task_id):
        entry = get_task(project_dir, cid) or {}
        result = child_results.get(cid)
        # Task graph verdict is authoritative for merge outcomes — the
        # in-memory `result.verdict` stays at the agent's self-declared
        # value (e.g., "pass") even after `_merge_child_branch()` writes
        # "merge_blocked" to the graph. If we trust the stale result,
        # the integration agent never sees the merge failure and skips
        # Step 0b recovery. Prefer the graph verdict when it's terminal-
        # for-the-merge-path; fall back to result.verdict otherwise.
        graph_verdict = entry.get("verdict")
        if graph_verdict == "merge_blocked":
            verdict = "merge_blocked"
        elif result is not None:
            verdict = result.verdict
        else:
            verdict = graph_verdict or "unknown"
        record: dict[str, Any] = {
            "task_id": cid,
            "intent": entry.get("intent", ""),
            "verdict": verdict,
            "summary": (result.final_text if result else "")[:200],
            "cost_usd": result.cost_usd if result else float(entry.get("cost_usd", 0.0)),
        }
        if verdict == "pending_children":
            reconstructed = _reconstruct_decomposed_child_summary(
                project_dir=project_dir,
                task_id=cid,
                child_results=child_results,
                integration_results=integration_results,
            )
            if reconstructed is not None:
                record.update(reconstructed)
        # Surface the build branch for merge_blocked children so the
        # integration Lead can recover their work via git rather than
        # dispatching the build agent to rewrite it.
        if verdict == "merge_blocked":
            record["build_branch"] = child_branch_name(cid)
            record["recovery_hint"] = (
                f"Work passed verify but failed to merge. Try "
                f"`git merge {record['build_branch']}` in this worktree, "
                f"resolve any remaining conflicts by hand (most are likely "
                f"trivial), and commit. DO NOT re-implement the feature "
                f"from scratch — the source files exist on that branch."
            )
        out.append(record)
    return out


_SUMMARY_VERDICT_SEVERITY = {
    "pass": 0,
    "pending_children": 1,
    "partial": 2,
    "unverified": 3,
    "merge_blocked": 4,
    "catastrophic": 5,
}


def _coverage_from_result(result: LeadResult | None) -> dict[str, Any] | None:
    payload = result.verify_result if result is not None else None
    if not isinstance(payload, dict):
        return None
    coverage = payload.get("intent_coverage")
    return coverage if isinstance(coverage, dict) else None


def _merge_intent_coverage(items: list[dict[str, Any] | None]) -> dict[str, Any]:
    merged: dict[str, Any] = {"built": [], "partial": [], "skipped": []}
    for coverage in items:
        if not isinstance(coverage, dict):
            continue
        for key in ("built", "partial", "skipped"):
            values = coverage.get(key) or []
            if isinstance(values, list):
                merged[key].extend(values)
    return merged


def _summary_record_from_result(
    task_id: str,
    result: LeadResult,
    *,
    source: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "verdict": result.verdict,
        "summary": (result.final_text or "")[:200],
        "cost_usd": result.cost_usd,
        "reconstructed_from": source,
    }
    coverage = _coverage_from_result(result)
    if coverage is not None:
        record["intent_coverage"] = coverage
    if result.agent_session_id:
        record["agent_session_id"] = result.agent_session_id
    record["source_task_id"] = task_id
    return record


def _reconstruct_decomposed_child_summary(
    *,
    project_dir: Path,
    task_id: str,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
) -> dict[str, Any] | None:
    """Replace a stale planning verdict with resolved subtree integration.

    A parent Lead that decomposed has ``verdict=pending_children`` from its
    planning session. Once its own integration session runs, that integration
    verdict is the relevant summary for the parent/root integrator. If the
    integration is not present yet, preserve pending semantics for back-compat.
    """
    integration_result = integration_results.get(task_id)
    if integration_result is not None:
        return _summary_record_from_result(
            task_id,
            integration_result,
            source="subtree_integration",
        )

    descendant_records: list[dict[str, Any]] = []
    for child_id in children_of(project_dir, task_id):
        child_entry = get_task(project_dir, child_id) or {}
        child_result = integration_results.get(child_id) or child_results.get(child_id)
        child_verdict = (
            child_result.verdict
            if child_result is not None
            else child_entry.get("verdict")
        )
        if child_verdict == "pending_children":
            nested = _reconstruct_decomposed_child_summary(
                project_dir=project_dir,
                task_id=child_id,
                child_results=child_results,
                integration_results=integration_results,
            )
            if nested is None:
                return None
            nested = dict(nested)
            nested["task_id"] = child_id
            descendant_records.append(nested)
            continue
        if child_result is not None:
            record = _summary_record_from_result(
                child_id,
                child_result,
                source="descendant_result",
            )
        elif child_verdict:
            record = {
                "verdict": child_verdict,
                "summary": "",
                "cost_usd": float(child_entry.get("cost_usd", 0.0)),
                "reconstructed_from": "descendant_graph",
                "source_task_id": child_id,
            }
        else:
            return None
        record["task_id"] = child_id
        descendant_records.append(record)

    if not descendant_records:
        return None

    worst = max(
        (str(record.get("verdict") or "pending_children") for record in descendant_records),
        key=lambda verdict: _SUMMARY_VERDICT_SEVERITY.get(verdict, 0),
    )
    cost = sum(float(record.get("cost_usd") or 0.0) for record in descendant_records)
    coverage = _merge_intent_coverage([
        record.get("intent_coverage") if isinstance(record.get("intent_coverage"), dict) else None
        for record in descendant_records
    ])
    summary = (
        "Reconstructed from resolved descendant verdicts: "
        + ", ".join(
            f"{record.get('task_id')}={record.get('verdict')}"
            for record in descendant_records
        )
    )
    reconstructed: dict[str, Any] = {
        "verdict": worst,
        "summary": summary[:200],
        "cost_usd": cost,
        "reconstructed_from": "descendant_verdicts",
        "descendant_summaries": descendant_records,
    }
    if any(coverage.values()):
        reconstructed["intent_coverage"] = coverage
    return reconstructed


def _is_descendant_of(project_dir: Path, candidate_id: str, ancestor_id: str) -> bool:
    """Walk parent chain to confirm candidate_id is in ancestor_id's subtree."""
    if candidate_id == ancestor_id:
        return False
    cur = candidate_id
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        entry = get_task(project_dir, cur) or {}
        parent = entry.get("parent_task_id")
        if parent == ancestor_id:
            return True
        if parent is None:
            return ancestor_id == ROOT_TASK_ID and cur == ROOT_TASK_ID
        cur = parent
    return False


def _reconcile_recovered_children(
    project_dir: Path,
    parent_task_id: str,
    on_event: Any = None,
) -> int:
    """Update verdicts of merge_blocked children whose work the
    integration agent successfully recovered via Step 0b.

    Algorithm: for each direct child of ``parent_task_id`` currently
    marked ``merge_blocked``, check whether its build branch is an
    ancestor of the parent's integration branch. If yes, the
    integration session's ``git merge`` brought the work in — the
    child has effectively passed (via recovery). Update its verdict
    from ``merge_blocked`` to ``pass``.

    Without this, ``aggregate_verdict()`` keeps reporting the stale
    ``merge_blocked`` even though Step 0b succeeded.

    Returns the number of children whose verdict was updated.
    """
    from otto.v5_branching import child_branch_name, integration_branch_name

    parent = get_task(project_dir, parent_task_id) or {}
    integration_branch = str(parent.get("integration_branch") or "").strip()
    if not integration_branch:
        integration_branch = integration_branch_name(parent_task_id)
        logger.warning(
            "parent task %s has no recorded integration_branch; using deterministic %s",
            parent_task_id,
            integration_branch,
        )

    reconciled = 0
    for cid in children_of(project_dir, parent_task_id):
        child = get_task(project_dir, cid) or {}
        if child.get("verdict") != "merge_blocked":
            continue
        if _merge_blocked_by_verification(child):
            logger.info(
                "reconcile: child %s remains merge_blocked because blocker origin is verification",
                cid,
            )
            _emit(on_event, {
                "event": "child_recovery_not_reconciled",
                "task_id": cid,
                "reason": "verification_blocked",
            })
            continue
        build_branch = child_branch_name(cid)
        # is-ancestor: build branch is reachable from integration branch
        try:
            proc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", build_branch, integration_branch],
                cwd=str(project_dir),
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning(
                "reconcile: could not check is-ancestor for %s: %s", cid, exc,
            )
            continue
        if proc.returncode != 0:
            # Either not an ancestor (real failure that wasn't recovered)
            # or git couldn't resolve the branch (deleted, etc.). Don't
            # update the verdict — preserve the honest merge_blocked.
            continue
        # Ancestor confirmed: integration agent merged it.
        set_verdict(project_dir, cid, "pass", cost_usd=float(child.get("cost_usd", 0.0)))
        reconciled += 1
        logger.info(
            "reconcile: child %s recovered by integration (build=%s); "
            "verdict merge_blocked → pass",
            cid, build_branch,
        )
        _emit(on_event, {
            "event": "child_recovery_reconciled",
            "task_id": cid,
            "build_branch": build_branch,
            "integration_branch": integration_branch,
        })
    return reconciled


def _merge_blocked_by_verification(child: dict[str, Any]) -> bool:
    origin = str(child.get("merge_blocked_origin") or "").strip().lower()
    if origin in {"verification", "verify_repair", "runner_verification"}:
        return True
    reason = str(
        child.get("merge_blocked_reason")
        or child.get("failure_reason")
        or ""
    ).lower()
    return (
        "verify/repair" in reason
        or "runner verification" in reason
        or "verification downgraded" in reason
    )


def _emit(on_event: Any, payload: dict[str, Any]) -> None:
    """Best-effort event emission."""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass


def _build_decomp_runtime_context(
    *,
    project_dir: Path,
    config: dict[str, Any],
    max_parallel: int,
    run_started_at: float | None,
    spec: FlatSpec | None = None,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    spec_payload = _spec_payload(spec=spec, spec_path=spec_path)
    graph = read_graph(project_dir)
    pending = [entry for entry in read_pending(project_dir) if isinstance(entry, dict)]
    raw_tasks = graph.get("tasks")
    tasks: dict[str, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
    non_runnable_verdicts = {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}
    dependency_satisfied = {
        tid for tid, task in tasks.items()
        if isinstance(task, dict)
        and _verdict_satisfies_dependency(task.get("verdict"), task.get("review_state"))
    }
    non_runnable = {
        tid for tid, task in tasks.items()
        if isinstance(task, dict) and task.get("verdict") in non_runnable_verdicts
    }
    runnable = [
        entry for entry in pending
        if entry.get("task_id")
        and entry.get("task_id") not in non_runnable
        and entry.get("review_state") in ("approved", None)
    ]
    ready = [
        entry for entry in runnable
        if all(dep in dependency_satisfied for dep in (entry.get("depends_on") or []))
    ]
    elapsed = int(time.monotonic() - run_started_at) if run_started_at else 0
    budget = int(config.get("run_budget_seconds") or 3600)
    provider = (
        config.get("provider")
        or (config.get("defaults", {}) or {}).get("provider")
        or "claude"
    )
    return {
        "max_parallel": max(1, int(max_parallel or 1)),
        "run_budget_seconds": budget,
        "run_elapsed_seconds": max(0, elapsed),
        "cost_model_s": {
            "worktree_setup_s": 60,
            "prompt_render_s": 10,
            "min_leaf_runtime_s": 300,
        },
        "queue_state": {
            "active": 0,
            "queued": len(runnable),
            "ready": len(ready),
            "waiting_on_deps": max(0, len(runnable) - len(ready)),
            "free_slots": max(1, int(max_parallel or 1)),
        },
        "spec_profile": _spec_profile(spec_payload),
        "runtime_policy": {
            "tier": str(config.get("v5_tier") or "auto"),
            "review_first_decomp": bool(config.get("v5_review_first_decomp")),
            "context_slicing": _context_slicing_enabled(config),
            "provider": str(provider),
        },
    }


def _spec_payload(*, spec: FlatSpec | None, spec_path: Path | None) -> dict[str, Any]:
    if spec is not None:
        return {
            "project_kind": spec.project_kind,
            "intent_claims": spec.intent_claims,
            "core_entities": spec.core_entities,
            "behavior_journeys": spec.behavior_journeys,
        }
    if spec_path and spec_path.exists():
        try:
            payload = json.loads(spec_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _spec_profile(spec: dict[str, Any]) -> dict[str, Any]:
    raw_entities = spec.get("core_entities")
    raw_journeys = spec.get("behavior_journeys")
    raw_intent_claims = spec.get("intent_claims")
    entities: list[Any] = raw_entities if isinstance(raw_entities, list) else []
    journeys: list[Any] = raw_journeys if isinstance(raw_journeys, list) else []
    intent_claims: list[Any] = raw_intent_claims if isinstance(raw_intent_claims, list) else []
    actions = 0
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        raw_actions = entity.get("primary_actions") or entity.get("actions") or []
        if isinstance(raw_actions, list):
            actions += len(raw_actions)
    entry_routes = sorted({
        str(journey.get("entry_route"))
        for journey in journeys
        if isinstance(journey, dict) and journey.get("entry_route")
    })
    return {
        "project_kind": str(spec.get("project_kind") or "unknown"),
        "intent_claims": len(intent_claims),
        "core_entities": len(entities),
        "primary_actions": actions,
        "behavior_journeys": len(journeys),
        "entry_routes": entry_routes,
    }


def _context_slicing_enabled(config: dict[str, Any]) -> bool:
    """Return whether child context slicing is explicitly enabled.

    Default is full context. `--full-context` sets `v5_context_slicing=False`
    as a safe escape hatch even if config enables slicing later.
    """
    if not isinstance(config, dict):
        return False
    if config.get("v5_context_slicing") is False:
        return False
    if config.get("v5_full_context") is True:
        return False
    slicing = config.get("context_slicing")
    if isinstance(slicing, dict):
        return bool(slicing.get("enabled"))
    if isinstance(slicing, bool):
        return slicing
    return bool(config.get("v5_context_slicing"))


def _child_scope_from_entry(child_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Build the slicer's child scope payload from queue metadata."""
    return {
        "child_id": child_id,
        "task_intent": str(entry.get("intent") or ""),
        "owned_paths": list(entry.get("owned_paths") or []),
        "action_ids": list(entry.get("action_ids") or []),
    }


def _write_context_slice_failure_log(
    *,
    child_session_dir: Path,
    child_id: str,
    reason: str,
) -> Path:
    path = child_session_dir / "context_slice.json"
    payload = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "child_id": child_id,
        "included_entities": [],
        "excluded_entities": [],
        "included_intent_claims_n": 0,
        "excluded_intent_claims_n": 0,
        "fallback_to_full": True,
        "fallback_reason": reason,
        "artifacts": {},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _context_slice_needs_agent_resolution(audit: dict[str, Any]) -> bool:
    if not bool(audit.get("fallback_to_full")):
        return False
    resolution = audit.get("scope_resolution")
    if not isinstance(resolution, dict):
        return False
    status = str(resolution.get("status") or "")
    return status in {
        "unresolved_last_resort_full_context",
        "last_resort_full_context",
    }


async def _resolve_child_scope_with_agent(
    *,
    project_dir: Path,
    child_session_dir: Path,
    child_task_id: str,
    child_scope: dict[str, Any],
    parent_spec_path: Path,
    fallback_reason: str,
    config: dict[str, Any],
    on_event: Any = None,
) -> dict[str, Any] | None:
    """Ask the spec/scope agent to resolve ambiguous child scope ids."""

    log_dir = child_session_dir / "context-scope-resolver"
    log_dir.mkdir(parents=True, exist_ok=True)
    prompt = (
        "Resolve an Otto context-slice scope ambiguity. Do not edit files.\n"
        f"Child task id: {child_task_id}\n"
        f"Parent spec path: {parent_spec_path}\n"
        f"Original child scope JSON: {json.dumps(child_scope, sort_keys=True)}\n"
        f"Fallback reason: {fallback_reason}\n\n"
        "Read the parent spec if needed and return only one JSON object with "
        "keys: task_intent (string), owned_paths (array of strings), "
        "action_ids (array of strings). Use exact ids from the parent spec. "
        "If the scope cannot be resolved, return null."
    )
    _emit(on_event, {
        "event": "context_scope_resolution_agent_start",
        "task_id": child_task_id,
        "fallback_reason": fallback_reason,
        "log_dir": str(log_dir),
    })
    try:
        from otto.agent import make_agent_options, run_agent_with_timeout

        options = make_agent_options(project_dir, config, agent_type="spec")
        text, _cost, session_id, _breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=log_dir,
            phase_name="SCOPE_RESOLVE",
            phase_label="scope-resolve",
            timeout=int(config.get("scope_resolve_timeout_s") or 120),
            project_dir=project_dir,
        )
    except Exception as exc:  # noqa: BLE001
        _emit(on_event, {
            "event": "context_scope_resolution_agent_done",
            "task_id": child_task_id,
            "ok": False,
            "summary": str(exc),
            "log_dir": str(log_dir),
        })
        return None

    payload = _extract_scope_resolution_payload(text)
    _emit(on_event, {
        "event": "context_scope_resolution_agent_done",
        "task_id": child_task_id,
        "ok": payload is not None,
        "agent_session_id": session_id,
        "resolved_scope": payload or {},
        "log_dir": str(log_dir),
    })
    return payload


def _extract_scope_resolution_payload(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.lower() == "null":
        return None
    decoder = json.JSONDecoder()
    for start, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return {
                "task_intent": str(payload.get("task_intent") or ""),
                "owned_paths": _string_list(payload.get("owned_paths")),
                "action_ids": _string_list(payload.get("action_ids")),
            }
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _write_toolchain_preflight_log(
    *,
    project_dir: Path,
    architect_task_id: str,
    retry_count: int,
    result: dict[str, Any],
) -> Path:
    log_dir = project_dir / "otto_logs" / "preflight"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"toolchain-preflight-{architect_task_id}-attempt-{retry_count + 1}.json"
    payload = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "architect_task_id": architect_task_id,
        "retry_count": retry_count,
        **result,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# Names that mark a dir as install state we want to share across worktrees.
_INSTALL_DIR_NAMES = ("node_modules", ".venv")
# Dir names that should never have an install dir nested inside considered.
_NOISE_PARENTS = frozenset({
    ".git", ".worktrees", "node_modules", ".venv", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache",
})


def _iter_install_dirs(root: Path) -> Iterator[tuple[Path, Path]]:
    """Walk ``root`` for node_modules/.venv dirs, skipping nested noise.

    Yields ``(found_path, rel_to_root)`` pairs. A nested install dir
    (e.g., ``frontend/node_modules/foo/node_modules``) is skipped — only
    the top-level shared install dir per subsystem matters.
    """
    if not root.exists():
        return
    for name in _INSTALL_DIR_NAMES:
        for found in root.rglob(name):
            if not (found.is_dir() or found.is_symlink()):
                continue
            try:
                rel = found.relative_to(root)
            except ValueError:
                continue
            # If any parent part is itself an install/noise dir, this is
            # nested. Skip.
            if any(part in _NOISE_PARENTS for part in rel.parts[:-1]):
                continue
            yield found, rel


def _link_shared_install_dirs(
    project_dir: Path, child_worktree: Path, task_id: str
) -> int:
    """Symlink project_dir's install dirs (node_modules/.venv) into
    child_worktree at the same relative paths.

    Returns the count of symlinks created (for logging/tests).
    """
    created = 0
    for found, rel in _iter_install_dirs(project_dir):
        dst = child_worktree / rel
        if dst.exists() or dst.is_symlink():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(found.resolve())
            created += 1
        except OSError as exc:
            logger.warning(
                "could not symlink %s into child %s: %s", rel, task_id, exc,
            )
    return created


def _project_uses_playwright(project_dir: Path) -> bool:
    """Best-effort: does any package.json declare @playwright/test?"""
    for pkg in project_dir.rglob("package.json"):
        # Skip noise paths (don't read inside node_modules).
        if any(part in _NOISE_PARENTS for part in pkg.parts):
            continue
        try:
            text = pkg.read_text(encoding="utf-8")
        except OSError:
            continue
        if "@playwright/test" in text:
            return True
    return False


def _ensure_playwright_browsers(project_dir: Path) -> bool:
    """Run ``npx playwright install chromium`` once if needed.

    Agents reflexively run this command (22% of recent logs) and pay
    the ~30-60s download per run when chromium isn't cached. Playwright
    caches browsers at ``~/.cache/ms-playwright/`` by default; once
    chromium-* exists there, subsequent install calls become ~1-2s
    no-ops. Doing one install up-front (when chromium is genuinely
    missing) saves the agents that 30-60s.

    Skips entirely if the project doesn't use Playwright. Best-effort:
    failures are logged but don't block the pipeline.

    Returns True if browsers are now present (either pre-existing or
    freshly installed), False otherwise.
    """
    if not _project_uses_playwright(project_dir):
        return False

    import os
    cache_root = Path(os.path.expanduser("~/.cache/ms-playwright"))
    # Chromium dirs are named like `chromium-1140`. If any exists, we're set.
    if cache_root.exists():
        for entry in cache_root.iterdir():
            if entry.name.startswith("chromium-") and entry.is_dir():
                return True

    npx = shutil.which("npx")
    if not npx:
        logger.info("playwright preinstall skipped: npx not on PATH")
        return False
    logger.info("playwright preinstall: chromium not cached, installing once")
    try:
        proc = subprocess.run(
            [npx, "playwright", "install", "chromium"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-300:]
            logger.warning(
                "playwright preinstall failed (exit %d): %s",
                proc.returncode, tail,
            )
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("playwright preinstall raised: %s", exc)
        return False


def _propagate_install_dirs_from_architect(
    project_dir: Path, architect_worktree: Path
) -> int:
    """Symlink the architect's install dirs into project_dir.

    After the architect's session, its worktree typically holds
    ``frontend/node_modules`` and/or ``api/.venv`` (it ran them while
    verifying the scaffold). These don't merge into the integration
    branch — git ignores them. Without this propagation, every feature
    child re-runs ``npm install``/``uv sync`` from scratch (~30-60s
    each, ~5min on a 5-child tree).

    Returns the count of symlinks created.
    """
    if not architect_worktree.exists():
        return 0
    created = 0
    for found, rel in _iter_install_dirs(architect_worktree):
        target = project_dir / rel
        if target.exists() or target.is_symlink():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(found.resolve())
            created += 1
            logger.info(
                "propagated install dir from architect: %s → %s",
                target, found.resolve(),
            )
        except OSError as exc:
            logger.warning(
                "could not propagate %s from architect: %s", target, exc,
            )
    return created


async def _wait_for_review(
    project_dir: Path,
    *,
    parent_task_id: str,
    poll_interval_s: float = 1.0,
    timeout_s: float = 24 * 3600.0,
    on_event: Any = None,
) -> None:
    """Wait until no children of ``parent_task_id`` are in pending_review state.

    Best-effort: on timeout, auto-approve all remaining pending tasks (per
    plan-v5 §13: every layer terminates; no infinite waits).
    """
    from otto.v5_review import approve, list_pending_review

    deadline = time.monotonic() + timeout_s
    poll_count = 0
    while True:
        pending = list_pending_review(project_dir, parent_task_id=parent_task_id)
        if not pending:
            return
        poll_count += 1
        if poll_count == 1 or poll_count % 10 == 0:
            _emit(on_event, {
                "event": "review_waiting",
                "task_id": parent_task_id,
                "pending_count": len(pending),
                "elapsed_s": time.monotonic() - (deadline - timeout_s),
            })
        if time.monotonic() > deadline:
            # Auto-approve on timeout (per philosophy invariant).
            n = approve(project_dir, parent_task_id=parent_task_id)
            _emit(on_event, {
                "event": "review_timeout_auto_approve",
                "task_id": parent_task_id,
                "auto_approved": n,
            })
            return
        await asyncio.sleep(poll_interval_s)
