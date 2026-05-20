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
  than spawning fresh child-run subprocesses and works at the scale
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
import enum
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
    get_retry_count,
    get_retry_reason,
    get_task,
    mark_contract_amendment_retry_in_progress,
    mark_reviewed_partial,
    persist_contract_amendment_retry_success,
    refresh_contract_amendment_retry_heartbeat,
    read_graph,
    record_task,
    set_contract_amendment_blocked,
    set_verdict,
    set_verdict_and_metadata,
    terminalize_stale_contract_amendment_retry_if_exhausted,
    tree_total_cost,
    update_task_metadata,
)
from otto.spec_compile_flat import (
    SPEC_COMPILE_PER_ATTEMPT_BUDGET_S,
    FlatSpec,
    SpecContractRepairExhaustedError,
    compile_flat_spec,
    spec_compile_attempt_budget,
)
from otto.v5_branching import MergeWorktreeDirtyError
from otto.v5_common import git_capture as _git_capture

logger = logging.getLogger("otto.v5_runner")

ROOT_TASK_ID = "root"

# When scaffold preflight invalidates an architect's self-declared pass,
# the runner re-dispatches the architect with the failure summary
# prepended to its intent. This is the cap on those retries (architect
# is allowed 1 original attempt + ``MAX_ARCHITECT_RETRIES`` re-runs).
MAX_ARCHITECT_RETRIES = 2
MAX_CONTRACT_AMENDMENT_ATTEMPTS = 2
CONTRACT_AMENDMENT_RETRY_HEARTBEAT_INTERVAL_SECONDS = 60.0



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


class _V5RunDeadlineExceeded(TimeoutError):
    def __init__(
        self,
        *,
        phase: str,
        budget_s: int,
        fired_after_s: float | None = None,
        limit_kind: str = "run_budget",
    ) -> None:
        self.phase = phase
        self.budget_s = budget_s
        self.fired_after_s = fired_after_s
        self.limit_kind = limit_kind  # "run_budget" | "phase_cap"
        if limit_kind == "phase_cap" and fired_after_s is not None:
            msg = (
                f"{phase} phase cap exceeded "
                f"({fired_after_s:.0f}s; run_budget_seconds={budget_s}s)"
            )
        else:
            super_s = (
                f"{fired_after_s:.0f}s"
                if fired_after_s is not None
                else f"{budget_s}s"
            )
            msg = f"run_budget_seconds exceeded during {phase} ({super_s})"
        super().__init__(msg)


def _run_budget_seconds(config: dict[str, Any]) -> int:
    try:
        return max(1, int(config.get("run_budget_seconds") or 3600))
    except (TypeError, ValueError):
        return 3600


def _run_budget_remaining_s(
    *,
    config: dict[str, Any],
    started_at: float,
) -> float:
    return max(0.0, float(_run_budget_seconds(config)) - (time.monotonic() - started_at))


def _spec_compile_cap_s(config: dict[str, Any]) -> float:
    """Phase cap for spec_compile, derived from the phase's OWN bounded
    pass-model repair budget so the cap can never contradict the loop it
    wraps (v5-itracker-setupfix2-002240 died: a flat ~1200s cap killed a
    monotonically-converging 8-attempt repair loop at round 3, far under
    the run budget). Explicit operator override still wins;
    _await_with_run_deadline still clamps this to remaining run budget
    (the real global ceiling), so it is bounded and not gate-weakening."""

    override = config.get("spec_timeout") or config.get("spec_compile_timeout_s")
    if override:
        return float(override)
    return SPEC_COMPILE_PER_ATTEMPT_BUDGET_S * spec_compile_attempt_budget()


async def _await_with_run_deadline(
    awaitable: Any,
    *,
    config: dict[str, Any],
    started_at: float,
    phase: str,
    cap_s: float | None = None,
) -> Any:
    remaining = _run_budget_remaining_s(config=config, started_at=started_at)
    if remaining <= 0:
        raise _V5RunDeadlineExceeded(
            phase=phase,
            budget_s=_run_budget_seconds(config),
            fired_after_s=time.monotonic() - started_at,
            limit_kind="run_budget",
        )
    capped = cap_s is not None and float(cap_s) < remaining
    timeout = remaining if cap_s is None else min(remaining, max(0.001, float(cap_s)))
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise _V5RunDeadlineExceeded(
            phase=phase,
            budget_s=_run_budget_seconds(config),
            fired_after_s=timeout,
            limit_kind="phase_cap" if capped else "run_budget",
        ) from exc


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


_RUNNER_COMMITTABLE_OUTPUT_PATHS = frozenset({"CHARTER.md"})


def _porcelain_paths(status_lines: list[str]) -> list[str]:
    paths: list[str] = []
    for line in status_lines:
        path = _status_path(str(line))
        if path:
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _status_lines_from_detail(detail: str) -> list[str]:
    lines: list[str] = []
    capture = False
    for raw in (detail or "").splitlines():
        stripped = raw.strip()
        if stripped == "dirty_status:":
            capture = True
            continue
        if not capture:
            continue
        if not stripped:
            continue
        # MergeWorktreeDirtyError indents preview rows with two spaces.
        line = raw[2:] if raw.startswith("  ") else stripped
        if len(line) >= 3:
            lines.append(line)
    return lines


def _dirty_paths_are_runner_committable(paths: list[str]) -> bool:
    if not paths:
        return False
    try:
        from otto.setup_gitignore import is_otto_owned_path
    except Exception:  # noqa: BLE001
        def is_otto_owned_path(_path: str) -> bool:
            return False

    return all(
        path in _RUNNER_COMMITTABLE_OUTPUT_PATHS or is_otto_owned_path(path)
        for path in paths
    )


def _commit_runner_output_paths(
    *,
    worktree_path: Path,
    paths: list[str],
    message: str,
) -> tuple[bool, str]:
    """Commit only runner-owned output paths, preserving unrelated dirt."""
    clean_paths = [
        path
        for path in sorted(dict.fromkeys(paths))
        if path in _RUNNER_COMMITTABLE_OUTPUT_PATHS
    ]
    if not clean_paths:
        return True, "no-op (only ignored Otto runtime dirt)"
    try:
        reset = subprocess.run(
            ["git", "reset", "-q"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if reset.returncode != 0:
            return False, f"git reset failed: {(reset.stderr or '').strip()}"
        add = subprocess.run(
            ["git", "add", "--", *clean_paths],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if add.returncode != 0:
            return False, f"git add runner outputs failed: {(add.stderr or '').strip()}"
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(worktree_path),
            capture_output=True,
            timeout=15,
            check=False,
        )
        if diff.returncode == 0:
            return True, "no-op (nothing to commit)"
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if commit.returncode != 0:
            return False, f"git commit runner outputs failed: {(commit.stderr or '').strip()}"
        remaining = _git_status_short(worktree_path)
        if remaining:
            remaining_lines = [line for line in remaining.splitlines() if line.strip()]
            remaining_paths = _porcelain_paths(remaining_lines)
            if _dirty_paths_are_runner_committable(remaining_paths):
                return False, (
                    "runner output commit left committable dirty paths: "
                    + ", ".join(remaining_paths[:10])
                )
        return True, f"committed runner outputs: {', '.join(clean_paths)}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"runner output commit crashed: {type(exc).__name__}: {exc}"


def _commit_charter_injection_if_dirty(project_dir: Path) -> tuple[bool, str]:
    status = _git_status_short(project_dir)
    lines = [line for line in status.splitlines() if line.strip()]
    charter_lines = [line for line in lines if _status_path(line) == "CHARTER.md"]
    if not charter_lines:
        return True, "no-op (CHARTER.md clean)"
    paths = _porcelain_paths(charter_lines)
    return _commit_runner_output_paths(
        worktree_path=project_dir,
        paths=paths,
        message="chore(otto): record detected infrastructure in CHARTER",
    )


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
    bound_contract_paths = [
        _normalize_contract_path(str(path))
        for path in (
            bound_amendment.get("contract_paths")
            or task.get("contract_amendment_paths")
            or ([bound_contract_path] if bound_contract_path else [])
        )
        if _normalize_contract_path(str(path))
    ]
    bound_owner_id = str(
        bound_amendment.get("owner_task_id") or task.get("contract_amendment_owner_task_id") or ""
    ).strip()
    violations: list[dict[str, Any]] = []
    if role == "contract_amendment":
        outside_bound = [
            path
            for path in normalized_changes
            if not bound_contract_paths
            or not any(_path_overlaps(path, bound_path) for bound_path in bound_contract_paths)
        ]
        if outside_bound:
            violations.append({
                "contract_path": bound_contract_path,
                "contract_paths": bound_contract_paths,
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
            and contract_path in bound_contract_paths
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


def _allowed_paths_write_feedback(
    *,
    acting_task_id: str,
    changed_paths: list[str],
    allowed_paths: list[str] | None,
    scope_policy: str,
    operation: str,
) -> dict[str, Any] | None:
    if scope_policy != "allowed_paths":
        return None
    normalized_changes = [
        _normalize_contract_path(path)
        for path in changed_paths
        if _normalize_contract_path(path)
    ]
    normalized_allowed = [
        _normalize_contract_path(path)
        for path in allowed_paths or []
        if _normalize_contract_path(path)
    ]
    outside_scope = [
        path
        for path in normalized_changes
        if not normalized_allowed
        or not any(_path_overlaps(path, allowed) for allowed in normalized_allowed)
    ]
    if not outside_scope:
        return None
    return {
        "kind": "allowed_paths_write_blocked",
        "step_id": "allowed_paths_write_gate",
        "message": "scoped repair attempted to write outside allowed paths",
        "task_id": acting_task_id,
        "operation": operation,
        "scope_policy": scope_policy,
        "allowed_paths": normalized_allowed,
        "changed_paths": outside_scope,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }





















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
    foundation_child_id = ""
    for task_id, task in (tasks or {}).items():
        if not isinstance(task, dict):
            continue
        if task.get("parent_task_id") != parent_task_id:
            continue
        if not _is_foundation_task(task):
            continue
        if not foundation_child_id:
            foundation_child_id = str(task_id)
        raw_child = task.get("foundation_contracts")
        for item in raw_child or []:
            if isinstance(item, dict):
                contract = dict(item)
                contract.setdefault("owner_task_id", str(task_id))
                contracts.append(contract)
    if contracts:
        return contracts
    # The graph metadata is populated by persist_foundation_contracts_from_
    # charter, which runs in the architect-contract gate path — NOT before the
    # scheduler's post-pass contracts check. When a foundation child has passed
    # but persist has not yet written its contracts onto the parent, every
    # graph-only lookup here returns empty and the scheduler spuriously fires
    # `foundation_contracts_missing_after_pass`, costing a ~14min architect
    # re-dispatch every run (#6/#8/#10/#11). CHARTER.md is the source of truth
    # the scaffold actually produced and it is already on disk by this point —
    # parse it directly as the fallback so contracts are visible the moment
    # the scaffold exists, independent of when graph-persist happens.
    try:
        from otto.v5_capability_inventory import parse_foundation_contracts

        parsed, _parse_findings = parse_foundation_contracts(project_dir / "CHARTER.md")
    except Exception:  # noqa: BLE001
        return contracts
    # `parse_foundation_contracts` already EXCLUDES malformed/rejected
    # entries from `parsed` (it records a finding and `continue`s), so
    # `parsed` holds only structurally-valid contracts. Returning them even
    # when the parser also emitted advisory findings (e.g. a registry path
    # using check='semantic' that should be 'literal') keeps
    # `contracts_present` truthful: discarding all parsed contracts on ANY
    # advisory finding was a brittle all-or-nothing predicate that
    # spuriously failed the architect/foundation contract gate and
    # merge_blocked the whole foundation. Not gate-weakening: the gate's own
    # `_foundation_contract_findings` independently re-validates the returned
    # contracts (and still blocks genuinely-bad ones), genuine emptiness /
    # total invalidity still yields `[]`, and advisory parse findings remain
    # available to callers that surface architect feedback.
    owner_default = foundation_child_id
    for item in parsed or []:
        if isinstance(item, dict):
            contract = dict(item)
            if owner_default:
                contract.setdefault("owner_task_id", owner_default)
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









































def _foundation_clean_boot_probe_targets(
    *,
    scheduler_feedback: object,
    parent_task_id: str,
    already_probed: set[str],
    graph_tasks: dict[str, Any],
    ready: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Pure target-selection for the FOUNDATION-GATE CLEAN-BOOT PROBE.

    Returns ``(foundation_ids, ready_feature_entries)``; the probe runs iff
    BOTH are non-empty. Returns ``([], [])`` — i.e. NO probe, gate
    unaffected — when the foundation contracts are NOT valid
    (``scheduler_feedback is not None``: this is a contract re-entry that the
    existing scheduler-feedback gate owns) or this parent was already probed
    this run (dedupe). Selects a foundation sibling only when it is
    upward-mergeable (passed/reviewed-partial, not blocked) and at least one
    feature child is ready to dispatch. Extracting this keeps the
    regression-critical decision consistent-by-construction and unit-testable
    without driving the full async ``_process_children`` loop.
    """
    if scheduler_feedback is not None:
        return [], []
    if parent_task_id in already_probed:
        return [], []
    foundation_ids = [
        str(_tid)
        for _tid, _t in graph_tasks.items()
        if isinstance(_t, dict)
        and _t.get("parent_task_id") == parent_task_id
        and _t.get("task_role") == "foundation"
        and _task_entry_allows_upward_merge(_t)
    ]
    ready_features = [
        _e
        for _e in ready
        if str(
            (graph_tasks.get(str(_e.get("task_id") or "")) or _e).get("task_role")
            or "feature"
        )
        == "feature"
    ]
    if not foundation_ids or not ready_features:
        return [], []
    return foundation_ids, ready_features


def _foundation_failure_action(*, probe_blocks: bool) -> str:
    """Phase 4 (2026-05-19): the locked-design decision for when the
    foundation clean-boot probe blocks. Always DEGRADE to the
    deterministic P0 scaffold (materialize_seed) so feature children can
    still build/merge on a working base — never strip features from the
    ready queue. Pure decision; the actual materialize happens at the
    caller. Combines with Phase 5 (foundation's own verdict lands
    `partial`+annotation via the chokepoint, not merge_blocked)."""
    return "degrade_to_scaffold" if probe_blocks else "proceed"


def _seed_foundation_gate_spec(fg_session: Path) -> bool:
    """Make the FOUNDATION-GATE CLEAN-BOOT PROBE's session dir spec-bearing.

    ``_run_integration_smoke_preflight`` derives
    ``spec_path = session_dir / "spec" / "spec.json"`` and every OTHER caller of
    ``_run_integration_smoke_preflight_with_repair`` passes an integration
    session dir that already carries the compiled spec. The foundation-gate
    probe runs in an isolated ``<root_session>/foundation_gate`` dir; without
    seeding, ``spec_path`` does not exist, the clean-deploy oracle has no spec
    to drive the probe, and the preflight returns a vacuous non-blocking
    payload — the probe becomes a SILENT NO-OP (regression observed in the
    f2aa00b25 validation run: empty foundation_gate dir, ~10s, features
    dispatched with no clean-boot). The canonical compiled spec lives at the
    parent root session (``fg_session.parent / "spec" / "spec.json"`` — the
    foundation_gate dir is created directly under the root session dir). Copy
    it in so the probe honors the same spec-bearing-session_dir contract as
    every other caller (consistent-by-construction, NOT gate-weakening).

    Returns True iff the probe's spec is present (already there, or seeded).
    Returns False only when the canonical compiled spec is itself missing
    (degenerate run) — callers must make that observable, never silently skip.
    """
    dst = fg_session / "spec" / "spec.json"
    if dst.exists():
        return True
    src = fg_session.parent / "spec" / "spec.json"
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _seed_scaffold_profile(
    *,
    project_dir: Path,
    spec: Any,
    intent: str,
    config: dict[str, Any],
    root_branch: str,
    on_event: Any = None,
) -> tuple[str, str]:
    """P0 scaffold-profile SEED phase (plan-scaffold-profiles.md, Codex Plan
    Gate thread 019e3df2..., 17 findings folded).

    Runs AFTER flat compile and BEFORE root Lead so the architect decomposes
    around an Otto-owned, version-pinned env scaffold instead of guessing it
    (3 consecutive moving-target clean-boot cascades: tsc -> ports/bare-python
    -> python3.14). Hydrate-first (R4#1): a valid existing
    ``scaffold-contract.json`` wins over the greenfield guard; a corrupt /
    half-seeded state is a terminal ``invalid`` the caller MUST surface as
    catastrophic (never silent). When ``action == "seed"`` the seed is written
    AND committed on ``root_branch`` with a clean-tree assertion BEFORE the
    caller proceeds — child worktrees branch from the parent integration
    branch, so an uncommitted seed would not propagate (R3#3). Mirrors the
    pure-helper + thin-wiring pattern of ``_seed_foundation_gate_spec``.

    Returns ``(action, reason)`` where action is hydrate|seed|skip|invalid.
    The agent-facing surface note is derived downstream by
    ``_build_decomp_runtime_context`` from the committed contract (single
    source of truth — no state threading).
    """
    from otto.scaffold_profiles import materialize_seed, plan_scaffold_seed
    from otto.v5_branching import (
        commit_worktree,
        git_current_branch,
        git_status_porcelain,
    )

    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        repo_relpaths = [
            p for p in (tracked.stdout or "").splitlines() if p.strip()
        ]
    except Exception:  # noqa: BLE001
        repo_relpaths = []

    journeys = getattr(spec, "behavior_journeys", None)
    journeys = journeys if isinstance(journeys, list) else []
    has_ui = any(
        isinstance(j, dict) and str(j.get("verification_level") or "") == "ui"
        for j in journeys
    )
    override = config.get("scaffold_profile")
    plan = plan_scaffold_seed(
        project_dir=project_dir,
        intent_text=intent,
        project_kind=str(getattr(spec, "project_kind", "") or "webapp"),
        has_ui_journey=has_ui,
        scaffold_profile_override=override if isinstance(override, str) else None,
        repo_relpaths=repo_relpaths,
    )

    if plan.action == "hydrate":
        _emit(on_event, {
            "event": "scaffold_seed_hydrated",
            "profile_id": plan.profile_id,
            "reason": plan.reason,
        })
        return "hydrate", plan.reason
    if plan.action == "skip":
        _emit(on_event, {"event": "scaffold_seed_skipped", "reason": plan.reason})
        return "skip", plan.reason
    if plan.action == "invalid":
        _emit(on_event, {
            "event": "scaffold_seed_state_invalid", "reason": plan.reason,
        })
        return "invalid", plan.reason

    # action == "seed": hard-commit invariant (R3#3).
    current = git_current_branch(project_dir)
    if current != root_branch:
        reason = (
            f"branch_mismatch: on {current!r}, expected root_branch "
            f"{root_branch!r}"
        )
        _emit(on_event, {
            "event": "scaffold_seed_state_invalid", "reason": reason,
        })
        return "invalid", reason

    def _head() -> str | None:
        try:
            hp = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                check=False,
            )
            return (hp.stdout or "").strip() or None
        except Exception:  # noqa: BLE001
            return None

    mat = materialize_seed(
        project_dir=project_dir,
        profile_id=str(plan.profile_id),
        head_sha=_head(),
    )
    ok, detail = commit_worktree(
        worktree_path=project_dir,
        message=f"otto: seed scaffold profile {plan.profile_id}",
    )
    if not ok:
        reason = f"seed_commit_failed: {detail}"
        _emit(on_event, {
            "event": "scaffold_seed_state_invalid", "reason": reason,
        })
        return "invalid", reason
    leftover = git_status_porcelain(project_dir)
    if leftover:
        reason = f"seed_tree_not_clean: {leftover[:5]}"
        _emit(on_event, {
            "event": "scaffold_seed_state_invalid", "reason": reason,
        })
        return "invalid", reason

    _emit(on_event, {
        "event": "scaffold_seed_committed",
        "profile_id": plan.profile_id,
        "profile_hash": mat.contract.get("profile_hash"),
        "head_sha": _head(),
        "seeded_paths": list(mat.written_paths),
    })
    return "seed", plan.reason










def _resume_root_from_checkpoint(
    *,
    project_dir: Path,
    config: dict[str, Any],
    root_session_dir: Path,
    intent: str,
    on_event: Any = None,
) -> tuple[Any, LeadResult] | None:
    """If this project already has a completed root decomposition checkpoint,
    return ``(spec, root_result)`` so the pipeline can skip compile + root
    decomposition + child rebuild and resume straight at integration.

    The task graph (durable) + per-child git branches (durable) are the
    checkpoint. A v5 run interrupted/killed after decomposition (e.g. budget
    exceeded mid-integration) would otherwise have to rebuild compile +
    scaffold + every feature (~40min) just to retry integration; that work is
    already persisted, so resume from it instead of from scratch.

    Returns ``None`` (→ unchanged fresh-run behavior) unless ALL hold:
      * the task graph has the root task with ≥1 emitted child,
      * root is NOT a "done" terminal (``pass`` = success, nothing to retry;
        ``catastrophic`` = unrecoverable, refusing fresh logic). Phase 1.2-A
        (2026-05-19): ``partial`` and ``merge_blocked`` ARE resumable — the
        fast fix→resume→re-run-integration-only loop. The user fixes code,
        resumes, and the pipeline skips the ~40min rebuild and re-enters
        integration on the persisted task graph + per-child branches; the
        integration phase overwrites the persisted root verdict with its
        new outcome.
      * the persisted root intent matches (guards against intent drift —
        resuming an old decomposition for a changed intent would be wrong),
      * a persisted spec.json checkpoint exists.
    Opt-out with ``v5_resume_from_checkpoint: false`` in config/otto.yaml.
    """
    if config.get("v5_resume_from_checkpoint") is False:
        return None
    try:
        tasks = read_graph(project_dir).get("tasks") or {}
    except Exception:  # noqa: BLE001
        return None
    root_t = tasks.get(ROOT_TASK_ID)
    if not isinstance(root_t, dict):
        return None
    child_ids = [
        str(tid)
        for tid, v in tasks.items()
        if isinstance(v, dict) and v.get("parent_task_id") == ROOT_TASK_ID
    ]
    if not child_ids:
        return None
    # Phase 1.2-A: only "done" terminals block resume. `partial` /
    # `merge_blocked` are iteration targets (fix → resume → re-run integration).
    if root_t.get("verdict") in {"pass", "catastrophic"}:
        return None
    persisted_intent = str(root_t.get("intent") or "").strip()
    if persisted_intent and persisted_intent != str(intent).strip():
        return None
    specs = sorted(
        project_dir.glob("otto_logs/sessions/*/spec/spec.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not specs:
        return None
    dest = root_session_dir / "spec" / "spec.json"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(specs[0].read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        return None
    # The FlatSpec object is only consumed by Phase C (root decomposition),
    # which we skip on resume; Phase E reads the spec.json FILE we just
    # checkpointed into root_session_dir. So spec=None is correct here.
    root_result = LeadResult(
        task_id=ROOT_TASK_ID,
        verdict="pending_children",
        decomposition="emit",
        emitted_subtask_ids=list(child_ids),
        cost_usd=0.0,
        final_text="resumed from checkpoint",
    )
    # Phase 1.2-B (2026-05-19): also carry the prior session's
    # integration repair packets so the resumed run's repair agent
    # picks up its prior Claude SDK session (resume=agent_session_id),
    # not just the orchestrator. Best-effort: failures here logged but
    # don't block resume — agent simply starts fresh in that case.
    try:
        carried = _carry_prior_repair_packets(project_dir, root_session_dir)
    except Exception as exc:  # noqa: BLE001 - resume is best-effort
        logger.warning(
            "Phase 1.2-B: _carry_prior_repair_packets failed: %s; "
            "proceeding without carried packets",
            exc,
        )
        carried = 0
    _emit(on_event, {
        "event": "v5_resume_from_checkpoint",
        "task_id": ROOT_TASK_ID,
        "emitted": len(child_ids),
        "child_task_ids": child_ids,
        "spec_checkpoint": str(specs[0]),
        "repair_packets_carried": carried,
    })
    return None, root_result


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

        # ---- Phase B/C: compile + root decomposition (or resume checkpoint) ----
        # If this project already has a completed decomposition checkpoint
        # (task graph + per-child branches persisted from a prior interrupted
        # run), skip compile + decomposition + child rebuild and resume
        # straight at integration. Fresh projects have no such graph →
        # _resume is None → behavior is byte-identical to before.
        _resume = _resume_root_from_checkpoint(
            project_dir=project_dir,
            config=config,
            root_session_dir=root_session_dir,
            intent=intent,
            on_event=on_event,
        )
        if _resume is not None:
            spec, root_result = _resume
            result.spec = spec
            result.root_lead_result = root_result
            record_task(
                project_dir,
                task_id=ROOT_TASK_ID,
                intent=intent,
                integration_branch=None,
            )
            # Hydrate-first (R4#1): verify the seed state on resume. Never
            # reseed / 2nd commit; a corrupt state is terminal, not silent.
            _seed_action, _seed_reason = _seed_scaffold_profile(
                project_dir=project_dir,
                spec=spec,
                intent=intent,
                config=config,
                root_branch=root_branch,
                on_event=on_event,
            )
            if _seed_action == "invalid":
                result.verdict = "catastrophic"
                result.failure_reason = f"scaffold_seed_state_invalid: {_seed_reason}"
                return result
        else:
            # ---- Phase B: Compile flat spec ----
            _emit(on_event, {"event": "compile_start"})
            try:
                spec = await _await_with_run_deadline(
                    compile_flat_spec(
                        project_dir=project_dir,
                        session_dir=root_session_dir,
                        intent=intent,
                        config=config,
                    ),
                    config=config,
                    started_at=started,
                    phase="spec_compile",
                    cap_s=_spec_compile_cap_s(config),
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
            except _V5RunDeadlineExceeded:
                raise
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

            # ---- Phase B.5: Otto-owned scaffold seed (before decomposition)
            _seed_action, _seed_reason = _seed_scaffold_profile(
                project_dir=project_dir,
                spec=spec,
                intent=intent,
                config=config,
                root_branch=root_branch,
                on_event=on_event,
            )
            if _seed_action == "invalid":
                result.verdict = "catastrophic"
                result.failure_reason = f"scaffold_seed_state_invalid: {_seed_reason}"
                return result

            # ---- Phase C: Run root Lead ----
            _emit(on_event, {"event": "lead_start", "task_id": ROOT_TASK_ID})
            root_result = await _await_with_run_deadline(
                run_lead(
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
                ),
                config=config,
                started_at=started,
                phase="root_decomposition",
                cap_s=float(config.get("decomposition_timeout_s") or 900),
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
            await _await_with_run_deadline(
                _process_children(
                    project_dir=project_dir,
                    parent_task_id=ROOT_TASK_ID,
                    config=config,
                    max_parallel=max_parallel,
                    tree_budget_usd=tree_budget_usd,
                    child_results=result.child_results,
                    integration_results=result.integration_results,
                    on_event=on_event,
                    run_started_at=started,
                ),
                config=config,
                started_at=started,
                phase="children_and_subtree_integration",
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
                # Phase 1.2 / Task #8: the escalated path skips
                # _commit_integration_agent_changes, so a timed-out repair
                # agent's near-complete work would be discarded. Preserve
                # it (commit what it was killed mid-committing) so it LANDS.
                _pres_ok, _pres_det = _preserve_timed_out_repair_work(project_dir)
                _emit(on_event, {
                    "event": "integration_repair_work_preserved",
                    "task_id": ROOT_TASK_ID,
                    "preserved": bool(_pres_ok),
                    "detail": _pres_det,
                    "_written_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    ),
                })
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
                integration_result = await _await_with_run_deadline(
                    run_lead(
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
                    ),
                    config=config,
                    started_at=started,
                    phase="root_integration",
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
                _it_reason = (
                    "Post-agent smoke_clean_deploy still has blocking issues: "
                    + "; ".join(
                        str(issue.get("message") or issue.get("kind"))
                        for issue in post_preflight_result.get("issues", [])
                        if isinstance(issue, dict)
                        and issue.get("severity") in ("error", "block")
                    )
                )
                # Chokepoint: a post-agent smoke block is VERIFICATION →
                # LAND (partial)+annotation, never merge_blocked (Task #5).
                _it_verdict, _it_reason = _integration_terminal_verdict(
                    blocks=True,
                    current_verdict=integration_result.verdict,
                    reason=_it_reason,
                )
                integration_result.verdict = _it_verdict
                integration_result.failure_reason = _it_reason
                if isinstance(integration_result.verify_result, dict):
                    integration_result.verify_result["verdict"] = _it_verdict
                    integration_result.verify_result["summary"] = _it_reason
                    if _it_verdict != "merge_blocked":
                        integration_result.verify_result["landed_with_annotation"] = True
                        integration_result.verify_result.setdefault(
                            "annotations", []
                        ).append({
                            "origin": "integration_post_agent_smoke",
                            "detail": _it_reason,
                            "cause": "verification",
                        })
                set_verdict(
                    project_dir,
                    ROOT_TASK_ID,
                    _it_verdict,
                    cost_usd=integration_result.cost_usd,
                )
                _emit(on_event, {
                    "event": "integration_smoke_failed",
                    "task_id": ROOT_TASK_ID,
                    "verdict": _it_verdict,
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

    except _V5RunDeadlineExceeded as exc:
        logger.warning("v5 pipeline hit hard run deadline: %s", exc)
        result.verdict = "merge_blocked"
        result.failure_reason = str(exc)
        _emit(on_event, {
            "event": "run_deadline_exceeded",
            "phase": exc.phase,
            "run_budget_seconds": exc.budget_s,
            "elapsed_s": round(time.monotonic() - started, 3),
        })
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


def _unambiguous_foundation_contract_overlap_findings(
    feedback: dict[str, Any],
) -> list[dict[str, Any]]:
    findings = [
        finding
        for finding in (feedback.get("findings") or [])
        if isinstance(finding, dict)
    ]
    return [
        finding
        for finding in findings
        if str(finding.get("kind") or "") == "feature_overlaps_foundation_contract"
    ]


def _feature_overlap_findings(feedback: dict[str, Any]) -> list[dict[str, Any]]:
    findings = [
        finding
        for finding in (feedback.get("findings") or [])
        if isinstance(finding, dict)
    ]
    return [
        finding
        for finding in findings
        if str(finding.get("kind") or "") == "feature_owned_paths_overlap"
    ]


def _remove_feature_owned_foundation_contract_paths(
    *,
    project_dir: Path,
    feedback: dict[str, Any],
) -> dict[str, Any] | None:
    removals: list[dict[str, Any]] = []
    by_task: dict[str, set[str]] = {}
    for finding in _unambiguous_foundation_contract_overlap_findings(feedback):
        task_id = str(finding.get("task_id") or "").strip()
        contract_path = _normalize_contract_path(str(finding.get("contract_path") or ""))
        if not task_id or not contract_path:
            continue
        for owned in finding.get("owned_paths") or []:
            owned_path = _normalize_contract_path(str(owned))
            if owned_path and _path_overlaps(owned_path, contract_path):
                by_task.setdefault(task_id, set()).add(owned_path)
    if not by_task:
        return None

    for task_id, remove_paths in sorted(by_task.items()):
        task = get_task(project_dir, task_id) or {}
        before = _task_owned_paths(task)
        after = [path for path in before if path not in remove_paths]
        if after == before:
            continue
        update_task_metadata(project_dir, task_id, owned_paths=after)
        removals.append({
            "task_id": task_id,
            "removed_paths": sorted(remove_paths),
            "remaining_owned_paths": after,
        })
    if not removals:
        return None
    return {
        "kind": "foundation_contract_feature_rescoped",
        "message": "removed foundation contract paths from feature owned_paths",
        "removals": removals,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }




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





# ─────────────────────────────────────────────────────────────────────────
# Terminal-outcome chokepoint (v5 one-hard-gate keystone, 2026-05-19).
# Locked design: research-linkboard-overconstraint.md. The SINGLE place that
# decides land vs. terminal. Only INFRA_CORRUPT refuses (and only after
# bounded git recovery, decided at the merge layer — NOT here). Everything
# else lands and is annotated. An unmapped origin → PRODUCT (LAND) is the
# SAFE default in this inverted design: refusal does not live in these
# recording helpers, so landing can never hide a needed refusal.
# ─────────────────────────────────────────────────────────────────────────

















































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
    # The architect/scaffold child authors feature_owned_paths keyed by the
    # real sibling feature task_ids. It runs in an isolated worktree with NO
    # otto_logs/ (otto-owned, never propagated to agent surfaces), so it
    # cannot read task_graph.json. Supply the exact ids+titles here — the
    # sanctioned channel — so it never has to invent placeholder keys.
    feature_partition_targets = [
        {
            "task_id": str(tid),
            "title": str(task.get("title") or task.get("intent") or "").strip()[:120],
        }
        for tid, task in tasks.items()
        if isinstance(task, dict)
        and task.get("task_role") == "feature"
        and str(tid) != "root"
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
        "feature_partition_targets": feature_partition_targets,
        "scaffold_seed": _scaffold_seed_runtime_context(project_dir),
        "runtime_policy": {
            "tier": str(config.get("v5_tier") or "auto"),
            "root_only_decomposition": _root_only_decomposition_enabled(config),
            "review_first_decomp": bool(config.get("v5_review_first_decomp")),
            "context_slicing": _context_slicing_enabled(config),
            "provider": str(provider),
        },
    }


def _scaffold_seed_runtime_context(project_dir: Path) -> dict[str, Any] | None:
    """Surface the committed Otto-owned scaffold (R2#1) to the architect/lead
    AND to architect-contract re-entry — both go through
    ``_build_decomp_runtime_context``. Reads the persisted, committed
    ``scaffold-contract.json`` (single source of truth) so no state has to be
    threaded and it stays correct across resume / re-decomposition. Returns
    None when no profile was seeded (skip path) — the key is simply absent."""
    try:
        from otto.scaffold_profiles import (
            read_existing_contract,
            scaffold_surface_note,
        )

        contract = read_existing_contract(project_dir)
        if not contract:
            return None
        return {
            "profile_id": contract.get("profile_id"),
            "seeded_paths": contract.get("seeded_paths"),
            "services": contract.get("services"),
            "note": scaffold_surface_note(contract),
        }
    except Exception:  # noqa: BLE001
        return None


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


# ---------------------------------------------------------------------------
# Submodule re-exports
# ---------------------------------------------------------------------------
#
# The runner is split into ``otto/v5/*.py`` submodules to keep this file at
# a reviewable size. External callers (other otto modules and the test
# suite) still expect ``from otto.v5_runner import X`` and
# ``monkeypatch.setattr("otto.v5_runner.X", ...)`` to work for every X
# that used to live here, so we re-export every moved symbol below.
#
# Submodules access patched symbols (``subprocess``,
# ``verify_from_clean_oracle``, ``run_oracle_repair_agent``) and other
# runner-private helpers via ``from otto import v5_runner as _v5r`` and
# ``_v5r.X`` at call time. That keeps monkeypatches on
# ``otto.v5_runner.X`` visible from submodule code paths.

from otto.v5.preflight_oracle import (  # noqa: E402, F401
    _blocking_payload_issues,
    _branch_checked_out,
    _build_repair_packet,
    _checkout_issue_payload,
    _checkout_v5_branch_clean_with_repair,
    _clean_oracle_payload_from_preflight_payload,
    _failing_ui_journey_count,
    _git_status_short,
    _handle_mechanical_merge_blocker,
    _handle_mechanical_preflight_blocker,
    _integration_smoke_blocks,
    _integration_worktree_setup_payload,
    _make_initial_oracle_payload,
    _mechanical_fail_fast_payload,
    _merge_refusal_oracle_payload,
    _packet_attempt_history,
    _port_cleanup_payload,
    _preflight_blocked_result,
    _preflight_blocking_summary,
    _preflight_issue_payload,
    _preflight_repair_escalated,
    _prepare_integration_worktree_with_repair,
    _process_details_for_pids,
    _read_json_artifact,
    _read_text_artifact,
    _repair_budget_from_config,
    _repair_result_payload,
    _run_integration_smoke_preflight,
    _run_integration_smoke_preflight_with_repair,
    _run_preflight_payload_repair_session,
    _run_startup_port_cleanup_with_repair,
    _setup_integration_worktree_once,
    _status_path,
    _worktree_product_contract,
)

from otto.v5.repair import (  # noqa: E402, F401
    _StaleTargetRetryResult,
    _carry_prior_repair_packets,
    _child_repair_helper_crashed_feedback,
    _conflict_packet_for_refusal,
    _contract_amendment_attempt_count,
    _contract_amendment_attempt_key,
    _contract_amendment_exhausted_feedback,
    _enqueue_existing_task_for_merge_retry,
    _foundation_contract_for_feedback_path,
    _increment_contract_amendment_attempt,
    _looks_like_merge_conflict,
    _persist_successful_contract_amendment_retry,
    _preserve_timed_out_repair_work,
    _read_latest_conflict_packet,
    _reenter_or_block_architect_contract,
    _refresh_child_result_from_verdict_file,
    _refresh_contract_amendment_retry_heartbeat_until_stopped,
    _repair_child_merge_conflict_once,
    _repair_child_stale_target_gate_once,
    _repair_child_upward_merge_gate_once,
    _repair_stale_target_and_retry_merge,
    _repair_subtree_propagation_once,
    _route_out_of_scope_smoke_failure,
    _run_child_verify_repair_packet,
    _run_plan_amendment_repair_packet,
    _run_scaffold_repair_packet,
    _schedule_foundation_contract_amendment,
    _schedule_smoke_repair_needed,
    _settle_contract_amendment_dependents,
    _smoke_payload_paths,
    _smoke_payload_within_task_scope,
    _smoke_repair_feedback,
    _smoke_repair_paths_for_contract,
    _smoke_repair_unrouteable_feedback,
    _stale_target_gate_feedback,
    _tasks_blocked_on_amendment,
    _terminalize_stale_contract_amendment_retry_if_exhausted,
)

from otto.v5.merge import (  # noqa: E402, F401
    TerminalAction,
    TerminalCause,
    _ORIGIN_CAUSE_MAP,
    _block_child_before_upward_merge,
    _branch_is_ancestor,
    _cause_from_origin,
    _child_merge_conflict_smoke_failed_feedback,
    _child_result_allows_upward_merge,
    _commit_integration_agent_changes,
    _commit_root_inline_changes,
    _ensure_child_merge_ready,
    _foundation_contracts_by_path_from_union_state,
    _git_added_lines_by_path_between,
    _integration_restore_branch,
    _integration_terminal_verdict,
    _integration_union_contributor_snapshot,
    _integration_union_empty_state,
    _integration_union_feedback,
    _integration_union_guard_error_feedback,
    _integration_union_guard_lock,
    _integration_union_missing_contributions,
    _integration_union_reason_text,
    _integration_union_shared_paths,
    _integration_union_state_from_task,
    _line_hash,
    _merge_child_branch,
    _merge_integration_union_state,
    _parse_added_lines_by_path,
    _pre_merge_ref_unresolved_feedback,
    _propagate_subtree_integration,
    _record_and_check_integration_union,
    _record_reviewed_partial_if_present,
    _record_structured_merge_failed,
    _record_task_merge_blocked_reason,
    _result_has_reviewed_partial,
    _semantic_foundation_contract_satisfied,
    _semantic_union_contributor_allowed,
    _semantic_union_required_export_present,
    _semantic_union_text_contains_probe,
    _task_entry_allows_upward_merge,
    _task_id_for_integration_branch,
    _verify_child_branches_reached_parent,
    _worktree_for_branch,
    resolve_terminal_outcome,
)

from otto.v5.dispatch import (  # noqa: E402, F401
    _DispatchLease,
    _SUMMARY_VERDICT_SEVERITY,
    _ancestor_count,
    _build_child_summaries,
    _changed_files_for_branch,
    _coverage_from_result,
    _find_session_dir_for_task,
    _is_descendant_of,
    _journey_ids_from_spec,
    _merge_blocked_by_verification,
    _merge_intent_coverage,
    _process_children,
    _reconcile_recovered_children,
    _reconstruct_decomposed_child_summary,
    _root_only_decomposition_enabled,
    _run_child,
    _run_integration,
    _run_lead_with_fallback,
    _summary_record_from_result,
    _write_integration_packet,
)
