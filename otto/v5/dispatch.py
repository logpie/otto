"""Parallel child dispatch loop, lease, and per-child orchestration.

Extracted from ``otto/v5_runner.py``. Public surface stays on
``otto.v5_runner`` via re-exports. Patched and cross-module runner
symbols are dereferenced lazily via ``_v5r.X`` to honour test-time
``monkeypatch.setattr("otto.v5_runner.X", ...)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from otto import paths as _paths
from otto.journey_scope_policy import ExecutionScope
from otto.lead import LeadKind, LeadResult
from otto.safe_slug import safe_slug
from otto.schemas import (
    VERDICT_CATASTROPHIC,
    VERDICT_MERGE_BLOCKED,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_PENDING_CHILDREN,
    VERDICT_UNVERIFIED,
)
from otto.v5_preflight import filter_blocked_descendants, run_preflight
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
    read_graph,
    record_task,
    set_contract_amendment_blocked,
    set_verdict,
    set_verdict_and_metadata,
    terminalize_stale_contract_amendment_retry_if_exhausted,
    tree_total_cost,
    update_task_metadata,
)
from otto.spec_compile_flat import FlatSpec
from otto.defaults import DEFAULT_ORACLE_STAGE_TIMEOUT_S
from otto.observability import iso_timestamp

logger = logging.getLogger("otto.v5_runner")


_SUMMARY_VERDICT_SEVERITY = {
    "pass": 0,
    "pending_children": 1,
    "partial": 2,
    "unverified": 3,
    "merge_blocked": 4,
    "catastrophic": 5,
}


# Lazy parent-module reference for patchability.
from otto import v5_runner as _v5r  # noqa: E402


class _DispatchLease:
    """Shared per-run child dispatch capacity.

    ``_v5r._process_children`` can be entered recursively, and tests can exercise
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
    dispatch_lease: _v5r._DispatchLease | None = None,
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
    # FOUNDATION-GATE CLEAN-BOOT PROBE: parents whose merged foundation has
    # already been clean-boot-probed this run (probe once, before dispatching
    # the dependent feature children — see the gate after scheduler feedback).
    foundation_clean_boot_done: set[str] = set()
    if dispatch_lease is None:
        dispatch_lease = _v5r._DispatchLease(max_parallel)

    while True:
        # Check tree budget cap.
        if tree_total_cost(project_dir, _v5r.ROOT_TASK_ID) > tree_budget_usd:
            logger.warning("tree budget cap exceeded; refusing new dispatches")
            _v5r._emit(on_event, {
                "event": "budget_cap_hit",
                "spent": tree_total_cost(project_dir, _v5r.ROOT_TASK_ID),
                "cap": tree_budget_usd,
            })
            # Wait for in-flight to drain, then exit.
            if in_flight:
                drained = list(in_flight.items())
                drain_results = await asyncio.gather(
                    *(task for _tid, task in drained),
                    return_exceptions=True,
                )
                for leased_tid, drain_result in zip(
                    (task_id for task_id, _task in drained),
                    drain_results,
                ):
                    if not isinstance(drain_result, BaseException):
                        continue
                    reason = (
                        "budget_cap_drain: "
                        f"{type(drain_result).__name__}: {drain_result}"
                    )
                    logger.error(
                        "budget cap drain failed for child %s",
                        leased_tid,
                        exc_info=(
                            type(drain_result),
                            drain_result,
                            drain_result.__traceback__,
                        ),
                    )
                    set_verdict(project_dir, leased_tid, "merge_blocked", cost_usd=0.0)
                    update_task_metadata(
                        project_dir,
                        leased_tid,
                        failure_reason=reason,
                        merge_blocked_origin="budget_cap_drain",
                        merge_blocked_reason=reason,
                    )
                    _v5r._emit(on_event, {
                        "event": "budget_cap_drain_failure",
                        "task_id": leased_tid,
                        "error_type": type(drain_result).__name__,
                        "error": str(drain_result),
                        "reason": "budget_cap_drain",
                        "verdict": "merge_blocked",
                    })
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
            _v5r._emit(on_event, {
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
                and architect_task.get("verdict") == VERDICT_PASS
                and not (architect_task.get("depends_on") or [])
            ):
                continue
            retry_count = get_retry_count(project_dir, architect_tid)
            preflight_key = (architect_tid, retry_count)
            if preflight_key in architect_preflight_done:
                continue
            architect_preflight_done.add(preflight_key)
            logger.info("preflight: running scaffold clean oracle after architect-pass (task=%s)", architect_tid)
            scaffold_result = _v5r.verify_from_clean_oracle(project_dir, scope="scaffold")
            blocking_messages = _v5r._emit_scaffold_oracle_issues(
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
                and _v5r._scaffold_oracle_contract_structurally_invalid(scaffold_result)
            ):
                # Phase 5 (2026-05-19): NO fresh-Lead re-decomposition.
                # Killed the p0fix2/3/4 cascade — `clear_verdict_for_retry +
                # child_results.pop + retry_architect=True + break` would
                # re-dispatch a fresh Architect that re-decomposes from
                # zero, discarding all prior child work and starving the
                # budget. Land the architect `partial`+annotation via the
                # chokepoint (Part A) instead; features land best-effort
                # and the contract issue is annotated for human review.
                _arch_reason = (
                    "Scaffold oracle found a structured product-contract "
                    "contradiction; landing architect partial+annotated "
                    "rather than fresh-Lead re-decomposing (Phase 5):\n\n"
                    + "\n".join(f"  - {m}" for m in blocking_messages)
                )
                _arch_result = child_results.get(architect_tid) or LeadResult(
                    task_id=architect_tid, verdict="partial",
                )
                _v5r._record_task_merge_blocked_reason(
                    project_dir=project_dir,
                    task_id=architect_tid,
                    result=_arch_result,
                    reason=_arch_reason,
                    origin="contract",  # → VERIFICATION → LAND partial
                )
                child_results[architect_tid] = _arch_result
                _v5r._emit(on_event, {
                    "event": "architect_contract_landed_partial",
                    "task_id": architect_tid,
                    "reason_tail": blocking_messages[-1][:200],
                })
                continue

            if blocking_messages:
                architect_result = child_results.get(architect_tid) or LeadResult(
                    task_id=architect_tid,
                    verdict=str(architect_task.get("verdict") or VERDICT_PARTIAL),
                )
                repair = await _v5r._run_scaffold_repair_packet(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    architect_task=architect_task,
                    latest_result=scaffold_result,
                    result=architect_result,
                    config=config,
                    on_event=on_event,
                )
                child_results[architect_tid] = architect_result
                if repair.verdict != VERDICT_PASS:
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
                    _v5r._emit(on_event, {
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

            parent_id = _v5r._parent_task_id_for_child(
                project_dir,
                architect_tid,
                str(architect_task.get("integration_branch") or "main"),
            )
            try:
                from otto.v5_capability_inventory import (
                    persist_feature_owned_paths_from_charter,
                    persist_foundation_contracts_from_charter,
                )

                foundation_contracts, foundation_findings = (
                    persist_foundation_contracts_from_charter(
                        project_dir,
                        parent_task_id=parent_id,
                    )
                )
                feature_owned_paths, feature_findings = (
                    persist_feature_owned_paths_from_charter(
                        project_dir,
                        parent_task_id=parent_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                foundation_contracts = []
                foundation_findings = []
                feature_owned_paths = {}
                feature_findings = []
                logger.warning("architect partition parse failed: %s", exc)
            partition_findings = list(foundation_findings) + list(feature_findings)
            if partition_findings:
                foundation_feedback = {
                    "kind": "foundation_contracts_contract_invalid",
                    "step_id": "architect_foundation_contracts",
                    "message": "architect Foundation Contracts or feature ownership partition is invalid",
                    "architect_task_id": architect_tid,
                    "parent_task_id": parent_id,
                    "contract_findings": [
                        {"kind": f.kind, "reference": f.reference, "detail": f.detail}
                        for f in partition_findings
                    ],
                    "_written_at": iso_timestamp(),
                }
                _v5r._emit(on_event, {
                    "event": "architect_contract_invalid",
                    "task_id": architect_tid,
                    "reason": foundation_feedback.get("kind"),
                    "structured_reason": foundation_feedback,
                })
                retry_architect = await _v5r._reenter_or_block_architect_contract(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    child_results=child_results,
                    completed=completed,
                    feedback=foundation_feedback,
                    origin="architect_contract",
                    config=config,
                    on_event=on_event,
                )
                break
            if foundation_contracts:
                _v5r._emit(on_event, {
                    "event": "foundation_contracts_recorded",
                    "architect_task_id": architect_tid,
                    "count": len(foundation_contracts),
                })
            else:
                foundation_contracts = _v5r._foundation_contracts_for_parent(
                    project_dir,
                    parent_id,
                    read_graph(project_dir).get("tasks") or {},
                )
            if feature_owned_paths:
                _v5r._emit(on_event, {
                    "event": "feature_owned_paths_recorded",
                    "architect_task_id": architect_tid,
                    "count": len(feature_owned_paths),
                })

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
                _v5r._emit(on_event, {
                    "event": "architect_contract_invalid",
                    "task_id": architect_tid,
                    "reason": route_isolation_feedback.get("kind"),
                    "structured_reason": route_isolation_feedback,
                })
                retry_architect = await _v5r._reenter_or_block_architect_contract(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    child_results=child_results,
                    completed=completed,
                    feedback=route_isolation_feedback,
                    origin="architect_contract",
                    config=config,
                    on_event=on_event,
                )
                break

            isolation_feedback = _v5r._foundation_isolation_feedback(
                parent_task_id=parent_id,
                architect_task_id=architect_tid,
                tasks=read_graph(project_dir).get("tasks") or {},
                contracts=foundation_contracts,
            )
            if isolation_feedback is not None:
                retry_architect = await _v5r._reenter_or_block_architect_contract(
                    project_dir=project_dir,
                    architect_tid=architect_tid,
                    child_results=child_results,
                    completed=completed,
                    feedback=isolation_feedback,
                    origin="architect_contract",
                    config=config,
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
                    timeout_s=DEFAULT_ORACLE_STAGE_TIMEOUT_S,
                    logger_fn=lambda m: logger.info("preflight: %s", m),
                )
                toolchain_duration_s = round(time.monotonic() - toolchain_started, 3)
                toolchain_payload = toolchain_result.to_jsonable()
                toolchain_payload["duration_s"] = toolchain_duration_s
                log_path = _v5r._write_toolchain_preflight_log(
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
                _v5r._emit(on_event, {
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
                n = _v5r._propagate_install_dirs_from_architect(
                    project_dir, arch_worktree
                )
                logger.info(
                    "preflight: architect %s install-dir propagation complete (count=%d)",
                    architect_tid,
                    n,
                )
                _v5r._emit(on_event, {
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
                    commit_ok, commit_detail = _v5r._commit_charter_injection_if_dirty(project_dir)
                    logger.info(
                        "Detected Infrastructure section injected into CHARTER.md "
                        "(%d package.jsons, %d pyprojects, %d configs)",
                        len(inv.package_jsons),
                        len(inv.pyprojects),
                        len(inv.known_configs),
                    )
                    _v5r._emit(on_event, {
                        "event": "capability_inventory_injected",
                        "package_json_count": len(inv.package_jsons),
                        "pyproject_count": len(inv.pyprojects),
                        "known_config_count": len(inv.known_configs),
                        "commit": {
                            "ok": commit_ok,
                            "detail": commit_detail,
                        },
                    })
                    if not commit_ok:
                        logger.warning(
                            "Detected Infrastructure CHARTER commit failed: %s",
                            commit_detail,
                        )
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
                        _v5r._emit(on_event, {
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
        contracts = _v5r._foundation_contracts_for_parent(project_dir, parent_task_id, tasks)
        scheduler_feedback = _v5r._foundation_scheduler_feedback(
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
                    _v5r._emit(on_event, {
                        "event": "architect_contract_invalid",
                        "task_id": reenter_foundation_id,
                        "reason": scheduler_feedback.get("kind"),
                        "structured_reason": scheduler_feedback,
                    })
                    await _v5r._reenter_or_block_architect_contract(
                        project_dir=project_dir,
                        architect_tid=reenter_foundation_id,
                        child_results=child_results,
                        completed=completed,
                        feedback=scheduler_feedback,
                        origin="foundation_scheduler",
                        config=config,
                        on_event=on_event,
                    )
                    foundation_after_reenter = get_task(project_dir, reenter_foundation_id) or {}
                    if str(foundation_after_reenter.get("verdict") or "") != VERDICT_MERGE_BLOCKED:
                        continue
                block_reason = dict(scheduler_feedback)
                for feature_id in affected_feature_ids:
                    result = child_results.get(feature_id) or LeadResult(
                        task_id=feature_id,
                        verdict="merge_blocked",
                        decomposition="inline",
                    )
                    _v5r._record_task_merge_blocked_reason(
                        project_dir=project_dir,
                        task_id=feature_id,
                        result=result,
                        reason=block_reason["message"],
                        origin="foundation_scheduler",
                        structured_reason=block_reason,
                    )
                    child_results[feature_id] = result
                _v5r._emit(on_event, {
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
                    _v5r._record_task_merge_blocked_reason(
                        project_dir=project_dir,
                        task_id=feature_id,
                        result=result,
                        reason=block_reason["message"],
                        origin="foundation_scheduler",
                        structured_reason=block_reason,
                    )
                    child_results[feature_id] = result
                _v5r._emit(on_event, {
                    "event": "foundation_feature_dispatch_blocked",
                    "parent_task_id": parent_task_id,
                    "structured_reason": block_reason,
                })
            ready = [
                entry
                for entry in ready
                if str(entry.get("task_id") or "") not in (ready_feature_ids | set(affected_feature_ids))
            ]
            _v5r._emit(on_event, {
                "event": "foundation_feature_dispatch_held",
                "parent_task_id": parent_task_id,
                "structured_reason": scheduler_feedback,
            })

        # FOUNDATION-GATE CLEAN-BOOT PROBE: once the foundation sibling has
        # passed+merged and its contracts are valid (scheduler_feedback is
        # None), verify the MERGED foundation scaffold boots clean BEFORE
        # dispatching the dependent feature children. Shifts the "assembled
        # product won't boot/build clean" class left — caught here as a cheap
        # bounded foundation-repair instead of at the single terminal
        # clean_deploy where one bounded repair turn must absorb it.
        if scheduler_feedback is None and parent_task_id not in foundation_clean_boot_done:
            _fg_foundation_ids, _fg_ready_features = (
                _v5r._foundation_clean_boot_probe_targets(
                    scheduler_feedback=scheduler_feedback,
                    parent_task_id=parent_task_id,
                    already_probed=foundation_clean_boot_done,
                    graph_tasks=(read_graph(project_dir).get("tasks") or {}),
                    ready=ready,
                )
            )
            if _fg_foundation_ids and _fg_ready_features:
                foundation_clean_boot_done.add(parent_task_id)
                _fg_fid = _fg_foundation_ids[0]
                _fg_session = (
                    Path(_find_session_dir_for_task(project_dir, _v5r.ROOT_TASK_ID))
                    / "foundation_gate"
                )
                _fg_session.mkdir(parents=True, exist_ok=True)
                if not _v5r._seed_foundation_gate_spec(_fg_session):
                    # Canonical compiled spec missing => degenerate run; the
                    # probe cannot meaningfully clean-boot. Make it OBSERVABLE
                    # (the original f2aa00b25 bug was a SILENT no-op) and
                    # proceed to feature dispatch without a false block.
                    _v5r._emit(on_event, {
                        "event": "foundation_clean_boot_skipped",
                        "parent_task_id": parent_task_id,
                        "task_id": _fg_fid,
                        "reason": "canonical compiled spec.json not found "
                        "at root session; clean-boot probe not run",
                    })
                else:
                    _v5r._emit(on_event, {
                        "event": "foundation_clean_boot_probe_start",
                        "parent_task_id": parent_task_id,
                        "task_id": _fg_fid,
                    })
                    _fg_cb = await _v5r._run_integration_smoke_preflight_with_repair(
                        project_dir=project_dir,
                        worktree_path=project_dir,
                        task_id=_fg_fid,
                        phase="foundation_clean_boot",
                        session_dir=_fg_session,
                        config=config,
                        integration_branch=None,
                        journey_scope="root_integration",
                        on_event=on_event,
                    )
                    if _v5r._integration_smoke_blocks(_fg_cb):
                        _fg_feedback = {
                            "kind": "foundation_clean_boot_failed",
                            "step_id": "foundation_gate_clean_boot",
                            "message": (
                                "merged foundation scaffold failed the "
                                "clean-boot probe before feature dispatch; "
                                "degrading to P0 scaffold so features can "
                                "still build/merge (Phase 4); foundation "
                                "verdict lands partial+annotation (Phase 5)"
                            ),
                            "parent_task_id": parent_task_id,
                            "foundation_task_ids": _fg_foundation_ids,
                            "_written_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                        }
                        _v5r._emit(on_event, {
                            "event": "foundation_clean_boot_failed",
                            "task_id": _fg_fid,
                            "structured_reason": _fg_feedback,
                        })
                        # Phase 4 (2026-05-19): degrade to P0 scaffold so
                        # features can still build/merge on a working base.
                        # The clean-boot probe revealed the merged foundation
                        # doesn't boot; replace it with the deterministic
                        # scaffold and let feature dispatch proceed.
                        if (
                            _v5r._foundation_failure_action(probe_blocks=True)
                            == "degrade_to_scaffold"
                        ):
                            try:
                                from otto.scaffold_profiles import (
                                    PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312,
                                    materialize_seed,
                                )
                                from otto.v5_branching import commit_worktree
                                materialize_seed(
                                    project_dir=project_dir,
                                    profile_id=PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312,
                                )
                                commit_worktree(
                                    worktree_path=project_dir,
                                    message=(
                                        "otto: degrade foundation to P0 "
                                        "scaffold after clean-boot failure "
                                        "(Phase 4)"
                                    ),
                                )
                                _v5r._emit(on_event, {
                                    "event": "foundation_clean_boot_degraded_to_scaffold",
                                    "task_id": _fg_fid,
                                    "_written_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                    ),
                                })
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "Phase 4 degrade-to-scaffold failed for "
                                    "%s: %s; foundation will land partial "
                                    "without scaffold replacement",
                                    _fg_fid,
                                    exc,
                                )
                        # Phase 5: foundation's own verdict lands `partial`
                        # +annotation via the chokepoint (no fresh-Lead
                        # cascade). Feature dispatch then proceeds on the
                        # degraded scaffold.
                        await _v5r._reenter_or_block_architect_contract(
                            project_dir=project_dir,
                            architect_tid=_fg_fid,
                            child_results=child_results,
                            completed=completed,
                            feedback=_fg_feedback,
                            origin="foundation_clean_boot",
                            config=config,
                            on_event=on_event,
                        )
                        # The previous feature-blocking loop (mark every
                        # ready feature merge_blocked + strip them from
                        # the ready queue) was the discard-pathology at
                        # the foundation-gate scope. After Phase 5 the
                        # foundation's verdict is `partial` not
                        # `merge_blocked`, so the prior block branch was
                        # already unreachable; explicitly removed for
                        # clarity. Features stay in `ready` and proceed.
                        continue

        # Spawn ready tasks up to max_parallel.
        spawned_any = False
        for entry in ready:
            tid = entry["task_id"]
            if not await dispatch_lease.try_acquire(tid):
                continue
            in_flight[tid] = asyncio.create_task(
                _v5r._run_child(
                    project_dir=project_dir,
                    entry=entry,
                    config=config,
                    max_parallel=max_parallel,
                    run_started_at=run_started_at,
                    on_event=on_event,
                )
            )
            spawned_any = True
            _v5r._emit(on_event, {"event": "child_dispatch", "task_id": tid})

        # If nothing in flight and nothing ready, we're done.
        if not in_flight and not ready:
            _v5r._verify_child_branches_reached_parent(
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
                tid = None
                for candidate_tid, candidate_fut in in_flight.items():
                    if candidate_fut is fut:
                        tid = candidate_tid
                        break
                if tid is None:
                    logger.warning("orphaned future in dispatch loop")
                    continue
                in_flight.pop(tid, None)
                released = False
                try:
                    result: LeadResult = fut.result()
                    await dispatch_lease.release(tid)
                    released = True
                    child_results[tid] = result
                    _v5r._record_reviewed_partial_if_present(project_dir, tid, result)
                    _v5r._settle_contract_amendment_dependents(
                        project_dir=project_dir,
                        amendment_id=tid,
                        amendment_result=result,
                        completed=completed,
                        child_results=child_results,
                        on_event=on_event,
                    )
                    if _v5r._child_result_allows_upward_merge(project_dir, tid, result):
                        completed.add(tid)
                    _v5r._emit(on_event, {
                        "event": "child_done",
                        "task_id": tid,
                        "verdict": result.verdict,
                    })

                    # If this child itself emitted grandchildren, recursively process.
                    if result.decomposition == "emit" and result.emitted_subtask_ids:
                        if (
                            _root_only_decomposition_enabled(config)
                            and _ancestor_count(project_dir, tid) >= 1
                        ):
                            reason = (
                                "non-root Lead emitted subtasks, but this run uses "
                                "root-only decomposition; child scopes must build inline"
                            )
                            result.verdict = "merge_blocked"
                            result.failure_reason = reason
                            set_verdict(project_dir, tid, "merge_blocked", cost_usd=result.cost_usd)
                            update_task_metadata(
                                project_dir,
                                tid,
                                merge_blocked_origin="decomposition_depth",
                                merge_blocked_reason=reason,
                                non_root_decomposition_rejected=True,
                            )
                            _v5r._emit(on_event, {
                                "event": "non_root_decomposition_rejected",
                                "task_id": tid,
                                "emitted_subtask_ids": list(result.emitted_subtask_ids),
                                "structured_reason": {
                                    "kind": "inline_only_at_depth",
                                    "message": reason,
                                    "task_id": tid,
                                    "_written_at": iso_timestamp(),
                                },
                            })
                            continue
                        await _v5r._process_children(
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
                        integ_result = await _v5r._run_integration(
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
                        _v5r._record_reviewed_partial_if_present(
                            project_dir,
                            tid,
                            integ_result,
                        )
                        if _v5r._child_result_allows_upward_merge(project_dir, tid, integ_result):
                            try:
                                ok, detail, source, target = _v5r._propagate_subtree_integration(
                                    project_dir=project_dir,
                                    task_id=tid,
                                )
                                _v5r._emit(on_event, {
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
                                    repaired, repair_detail = await _v5r._repair_subtree_propagation_once(
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
                                        ok, detail, source, target = _v5r._propagate_subtree_integration(
                                            project_dir=project_dir,
                                            task_id=tid,
                                        )
                                        _v5r._emit(on_event, {
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
                                        _v5r._record_task_merge_blocked_reason(
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
                        elif integ_result.verdict in (VERDICT_PARTIAL, VERDICT_UNVERIFIED):
                            _v5r._block_child_before_upward_merge(
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
                    _v5r._settle_contract_amendment_dependents(
                        project_dir=project_dir,
                        amendment_id=tid,
                        amendment_result=crash_result,
                        completed=completed,
                        child_results=child_results,
                        on_event=on_event,
                    )
                    completed.add(tid)
                    _v5r._emit(on_event, {
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
    # Child dispatch allocates sessions without the project lock; the canonical
    # allocator still retries existing session-dir collisions.
    child_session_id = _paths.new_session_id(project_dir)
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
        _v5r._emit(on_event, {
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
            linked_install_dirs = _v5r._link_shared_install_dirs(project_dir, child_worktree, tid)
            if linked_install_dirs:
                logger.info(
                    "linked %d shared install dir(s) into child %s",
                    linked_install_dirs,
                    tid,
                )
                _v5r._emit(on_event, {
                    "event": "install_dirs_linked",
                    "task_id": tid,
                    "count": linked_install_dirs,
                })
            _v5r._emit(on_event, {
                "event": "worktree_created",
                "task_id": tid,
                "path": str(child_worktree),
            })
    except Exception as exc:  # noqa: BLE001
        reason = f"child worktree setup failed before dispatch: {type(exc).__name__}: {exc}"
        logger.error("%s", reason)
        set_verdict(project_dir, tid, "merge_blocked")
        _v5r._emit(on_event, {
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
        _v5r._emit(on_event, {
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
        claimed = mark_contract_amendment_retry_in_progress(
            project_dir,
            tid,
            owner_id=child_session_id,
        )
        if not claimed:
            if _v5r._terminalize_stale_contract_amendment_retry_if_exhausted(
                project_dir=project_dir,
                task_id=tid,
                result=retry_result,
                on_event=on_event,
            ):
                return retry_result
            retry_result.verdict = "unverified"
            retry_result.failure_reason = (
                "contract amendment merge-only retry is already claimed by another runner"
            )
            retry_result.verify_result = {
                "verdict": "unverified",
                "summary": retry_result.failure_reason,
                "phase": "contract_amendment_retry_claim",
            }
            _v5r._emit(on_event, {
                "event": "contract_amendment_leaf_merge_retry_claim_skipped",
                "task_id": tid,
            })
            return retry_result
        _v5r._emit(on_event, {
            "event": "contract_amendment_leaf_merge_retry",
            "task_id": tid,
            "blocked_on_task_id": task_entry.get("blocked_on_task_id"),
        })
        if _v5r._child_result_allows_upward_merge(project_dir, tid, retry_result):
            heartbeat_task = asyncio.create_task(
                _v5r._refresh_contract_amendment_retry_heartbeat_until_stopped(
                    project_dir=project_dir,
                    task_id=tid,
                    owner_id=child_session_id,
                )
            )
            try:
                retry_task_role = str((task_entry or {}).get("task_role") or "feature").lower()
                if retry_task_role == "foundation":
                    await _v5r._merge_child_branch(
                        project_dir=project_dir,
                        child_task_id=tid,
                        child_worktree=child_worktree,
                        child_session_dir=retry_session_dir,
                        parent_integration_branch=parent_integration_branch,
                        result=retry_result,
                        config=config,
                        on_event=on_event,
                    )
                else:
                    await _v5r._commit_child_for_integration(
                        project_dir=project_dir,
                        child_task_id=tid,
                        child_worktree=child_worktree,
                        parent_integration_branch=parent_integration_branch,
                        result=retry_result,
                        on_event=on_event,
                    )
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            _v5r._persist_successful_contract_amendment_retry(
                project_dir=project_dir,
                task_id=tid,
                verdict=retry_result.verdict,
                cost_usd=retry_result.cost_usd,
                on_event=on_event,
            )
        return retry_result

    context_slice_note = ""
    if _v5r._context_slicing_enabled(config):
        try:
            from otto.v5_context_slicer import write_context_slice_for_child

            full_charter_path = child_worktree / "CHARTER.md"
            if not full_charter_path.exists():
                full_charter_path = project_dir / "CHARTER.md"
            child_scope = _v5r._child_scope_from_entry(tid, entry)
            slice_result = write_context_slice_for_child(
                project_dir=project_dir,
                child_session_dir=child_session_dir,
                child_scope=child_scope,
                parent_spec_path=parent_spec,
                full_charter_path=full_charter_path,
                child_spec_path=child_spec_path,
            )
            if _v5r._context_slice_needs_agent_resolution(slice_result.audit):
                resolved_scope = await _v5r._resolve_child_scope_with_agent(
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
            _v5r._emit(on_event, {
                "event": "context_slice",
                "task_id": tid,
                "fallback_to_full": slice_result.audit.get("fallback_to_full"),
                "included_entities": slice_result.audit.get("included_entities", []),
                "excluded_entities": slice_result.audit.get("excluded_entities", []),
                "log_path": str(child_session_dir / "context_slice.json"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("context slicing failed for child %s: %s", tid, exc)
            log_path = _v5r._write_context_slice_failure_log(
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
    result = await _v5r._run_lead_with_fallback(
        task_id=tid,
        intent=intent,
        project_dir=project_dir,
        session_dir=child_session_dir,
        integration_branch=parent_integration_branch,
        config=config,
        kind="plan_or_inline",
        context_slice_note=context_slice_note,
        decomp_runtime_context=_v5r._build_decomp_runtime_context(
            project_dir=project_dir,
            config=config,
            max_parallel=max_parallel,
            run_started_at=run_started_at,
            spec_path=child_spec_path,
            task_id=tid,
        ),
        on_event=on_event,
    )

    # Merge child's branch into parent's integration branch only after an
    # oracle-backed result. Raw partial/unverified results get one focused
    # verify/repair dispatch; if that still does not produce pass or explicit
    # reviewed-partial, the child becomes merge_blocked instead of best-effort
    # merging upward.
    if child_worktree is not None:
        result = await _v5r._ensure_child_merge_ready(
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
        if _v5r._child_result_allows_upward_merge(project_dir, tid, result):
            # Foundation must merge eagerly so sibling features can build on
            # top of its contracts (their dispatch worktree branches off the
            # parent integration branch). Features defer to integration —
            # they're independent of each other and integration is the single
            # merge authority for cross-feature reconciliation.
            task_role = str((entry or {}).get("task_role") or "feature").lower()
            if task_role == "foundation":
                await _v5r._merge_child_branch(
                    project_dir=project_dir,
                    child_task_id=tid,
                    child_worktree=child_worktree,
                    child_session_dir=child_session_dir,
                    parent_integration_branch=parent_integration_branch,
                    result=result,
                    config=config,
                    on_event=on_event,
                )
            else:
                await _v5r._commit_child_for_integration(
                    project_dir=project_dir,
                    child_task_id=tid,
                    child_worktree=child_worktree,
                    parent_integration_branch=parent_integration_branch,
                    result=result,
                    on_event=on_event,
                )

    return result

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
    result_a = await _v5r.run_lead(
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
        session_dir / _paths.SUMMARY_FILENAME,
        provider=provider_a,
        cost_usd=result_a.cost_usd,
        outcome=result_a.verdict,
        duration_s=duration_a,
        started_at=attempt_started,
    )

    if result_a.verdict != VERDICT_CATASTROPHIC:
        return result_a

    do_fallback, reason = should_fallback(result_a.failure_reason, config)
    if not do_fallback:
        return result_a

    fb = _fallback_provider(config)
    if not fb or fb == provider_a:
        return result_a

    _v5r._emit(on_event, {
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
    result_b = await _v5r.run_lead(
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
        session_dir / _paths.SUMMARY_FILENAME,
        provider=fb,
        cost_usd=result_b.cost_usd,
        outcome=result_b.verdict,
        duration_s=_time.monotonic() - fallback_started,
        started_at=fallback_started_iso,
        fallback_reason=reason,
    )
    return result_b

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
    # Integration dispatch allocates sessions without the project lock; the
    # canonical allocator still retries existing session-dir collisions.
    integration_session_id = _paths.new_session_id(project_dir)
    integration_session_dir = _paths.session_dir(project_dir, integration_session_id)
    integration_session_dir.mkdir(parents=True, exist_ok=True)

    # The integration Lead's verify call needs spec.json (same shape as build
    # children get via _v5r._run_child). Find any earlier session that has it and
    # copy. Fall back silently if no spec exists yet — verifier handles that.
    target_spec = integration_session_dir / "spec" / "spec.json"
    if not target_spec.exists():
        try:
            sessions_root = _paths.sessions_root(project_dir)
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
    integration_worktree, setup_preflight = await _v5r._prepare_integration_worktree_with_repair(
        project_dir=project_dir,
        task_id=task_id,
        integration_branch=parent_integration_branch,
        integration_session_dir=integration_session_dir,
        config=config,
        on_event=on_event,
    )
    if (
        integration_worktree is None
        or _v5r._preflight_repair_escalated(setup_preflight)
        or _v5r._integration_smoke_blocks(setup_preflight)
    ):
        result = _v5r._preflight_blocked_result(
            task_id=task_id,
            preflight_result=setup_preflight,
        )
        integration_results[task_id] = result
        _v5r._emit(on_event, {
            "event": "integration_done",
            "task_id": task_id,
            "verdict": result.verdict,
        })
        return result

    integration_cwd = integration_worktree
    # Phase 2 follow-up (T1-1, audit-pre-flight-duplication.md Finding 1):
    # the pre_agent integration smoke preflight is deferred to the
    # integration Lead's Step 2 (`./start.sh` + journey verification). The
    # Lead's first action in its own session runs the same checks this
    # preflight would have run, with the same tools (Bash, chrome-devtools).
    # Pre-flighting the same work pre-emptively wastes ~$0.20+ per
    # integration on a repair-agent dispatch that usually no-ops because by
    # the time it runs, integration is about to do the same thing anyway.
    preflight_result = {
        "check": "smoke_clean_deploy",
        "passed": True,
        "deferred_to_integration_step_2": True,
        "summary": (
            "pre_agent smoke preflight deferred to integration Lead's "
            "Step 2 start.sh (single-source-of-truth verifier)"
        ),
    }

    _v5r._emit(on_event, {"event": "integration_start", "task_id": task_id})
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
    result = await _v5r.run_lead(
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
    _v5r._commit_integration_agent_changes(
        project_dir=project_dir,
        task_id=task_id,
        worktree_path=integration_cwd,
        result=result,
        on_event=on_event,
    )
    # Phase-1 unified verifier: the integration Lead drove the journeys
    # itself via chrome-devtools MCP in the same session it made edits.
    # We trust its verdict; no separate post-agent journey runner +
    # repair-agent loop. The pre-integration preflight stays attached
    # to verify_result for proof-packet rendering, but there's no
    # post_integration_preflight anymore. See plan-unified-self-verifying-agent.md.
    if result.verify_result is None:
        result.verify_result = {}
    if isinstance(result.verify_result, dict):
        result.verify_result["pre_integration_preflight"] = preflight_result
    integration_results[task_id] = result
    _v5r._emit(on_event, {"event": "integration_done", "task_id": task_id, "verdict": result.verdict})
    restore_branch = _v5r._integration_restore_branch(project_dir, task_id, config)
    restore_result = await _v5r._checkout_v5_branch_clean_with_repair(
        project_dir=project_dir,
        branch=restore_branch,
        context=f"integration_return:{task_id}",
        session_dir=integration_session_dir,
        config=config,
        integration_branch=parent_integration_branch,
        task_id=task_id,
        on_event=on_event,
    )
    if _v5r._preflight_repair_escalated(restore_result) or _v5r._integration_smoke_blocks(restore_result):
        detail = _v5r._preflight_blocking_summary(
            f"could not restore project_dir after integration {task_id} to {restore_branch}",
            restore_result,
        )
        logger.warning(
            "could not restore project_dir after integration %s to %s: %s",
            task_id, restore_branch, detail,
        )
        _v5r._emit(on_event, {
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
        "_written_at": iso_timestamp(),
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
    sessions_root = _paths.sessions_root(project_dir)
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
    exists = _v5r.subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return []
    base = "main"
    base_exists = _v5r.subprocess.run(
        ["git", "rev-parse", "--verify", base],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if base_exists.returncode != 0:
        base = "HEAD"
    proc = _v5r.subprocess.run(
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
        # value (e.g., "pass") even after `_v5r._merge_child_branch()` writes
        # "merge_blocked" to the graph. If we trust the stale result,
        # the integration agent never sees the merge failure and skips
        # Step 0b recovery. Prefer the graph verdict when it's terminal-
        # for-the-merge-path; fall back to result.verdict otherwise.
        graph_verdict = entry.get("verdict")
        if graph_verdict == VERDICT_MERGE_BLOCKED:
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
        if verdict == VERDICT_PENDING_CHILDREN:
            reconstructed = _reconstruct_decomposed_child_summary(
                project_dir=project_dir,
                task_id=cid,
                child_results=child_results,
                integration_results=integration_results,
            )
            if reconstructed is not None:
                record.update(reconstructed)
        # Post-refactor (2026-05-21): integration Lead is the single merge
        # authority. The orchestrator no longer pre-merges children at
        # child-finish time. EVERY child's `i2p/build/<id>` branch is
        # surfaced here so the integration Lead can merge them all.
        # Previously only merge_blocked children carried `build_branch`
        # because the others had been pre-merged; that's no longer true.
        record["build_branch"] = child_branch_name(cid)
        if verdict == VERDICT_MERGE_BLOCKED:
            record["recovery_hint"] = (
                f"Work passed verify but failed to merge. Try "
                f"`git merge {record['build_branch']}` in this worktree, "
                f"resolve any remaining conflicts by hand (most are likely "
                f"trivial), and commit. DO NOT re-implement the feature "
                f"from scratch — the source files exist on that branch."
            )
        out.append(record)
    return out

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
        if child_verdict == VERDICT_PENDING_CHILDREN:
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
            return ancestor_id == _v5r.ROOT_TASK_ID and cur == _v5r.ROOT_TASK_ID
        cur = parent
    return False

def _ancestor_count(project_dir: Path, task_id: str) -> int:
    count = 0
    cur = task_id
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        entry = get_task(project_dir, cur) or {}
        parent = entry.get("parent_task_id")
        if parent is None:
            return count
        count += 1
        cur = str(parent)
    return count

def _root_only_decomposition_enabled(config: dict[str, Any]) -> bool:
    if bool(config.get("v5_allow_recursive_decomposition")):
        return False
    return str(config.get("v5_tier") or "") in {"auto", "lead", "modular"}

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
        if child.get("verdict") != VERDICT_MERGE_BLOCKED:
            continue
        if _merge_blocked_by_verification(child):
            logger.info(
                "reconcile: child %s remains merge_blocked because blocker origin is verification",
                cid,
            )
            _v5r._emit(on_event, {
                "event": "child_recovery_not_reconciled",
                "task_id": cid,
                "reason": "verification_blocked",
            })
            continue
        build_branch = child_branch_name(cid)
        # is-ancestor: build branch is reachable from integration branch
        try:
            proc = _v5r.subprocess.run(
                ["git", "merge-base", "--is-ancestor", build_branch, integration_branch],
                cwd=str(project_dir),
                capture_output=True,
                timeout=10,
            )
        except (_v5r.subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
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
        _v5r._emit(on_event, {
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
