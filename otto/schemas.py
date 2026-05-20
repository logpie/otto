"""Documented schemas for the cross-module dict surfaces.

This module is **documentation-as-code**, not a runtime enforcer. The
TypedDicts here capture the shapes that flow across module boundaries
in `otto/`. They are declared with ``total=False`` so callers can still
pass raw dicts without type errors — the goal is to give an AI editor
(or a human) a single place to grep when they need to know what keys
exist and what they mean.

Per the audit (archive/audits/round4/audit-brittleness-types.md), the
3 highest-leverage boundaries are:

  1. Task-graph entries (.otto/task-graph.json :: tasks[task_id])
  2. Pipeline event payloads (the `on_event` callback in run_v5_pipeline)
  3. Repair packets (read+written by the repair agent across the
     subprocess boundary)

Adding `TypedDict` annotations to specific function signatures is a
follow-up item per call site; see BACKLOG.md.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# ---------------------------------------------------------------------------
# 1) TASK-GRAPH ENTRY  —  .otto/task-graph.json :: tasks[<task_id>]
# ---------------------------------------------------------------------------
#
# A task entry is created by `otto.queue.task_graph.record_task` and
# mutated by ~20 sites across v5_runner, otto/v5/*, otto/queue/. The
# contract amendment subsystem adds several `contract_amendment_*` keys
# during recovery. Cost / verdict / status flow in via the dispatch +
# merge layers.
#
# Most keys are NotRequired (optional) because entries grow new fields
# as a task moves through phases. The keys listed here are the ones
# read by 2+ consumers — single-reader keys live in the consumer.


TaskRole = Literal["root", "feature", "foundation", "contract_amendment"]
Decomposition = Literal["unknown", "inline", "emit"]
TaskVerdict = Literal[
    "pass", "partial", "unverified", "merge_blocked",
    "pending_children", "catastrophic",
]


class TaskGraphEntry(TypedDict, total=False):
    """Shape of one entry in `.otto/task-graph.json :: tasks[<task_id>]`.

    Not all keys are present at every read; entries grow fields across
    the dispatch → build → merge → verify lifecycle.
    """

    # Identity & topology (set at record_task time)
    task_id: str
    parent_task_id: str | None
    intent: str
    title: str
    task_role: TaskRole
    decomposition: Decomposition

    # Dispatch / dependency graph
    depends_on: list[str]
    action_ids: list[str]
    integration_branch: str | None
    owned_paths: list[str]
    foundation_contracts: list[dict[str, Any]]
    review_state: str  # "pending" | "approved" | "cancelled" | ""

    # Result fields (populated as the task runs / completes)
    verdict: TaskVerdict
    last_agent_verdict: TaskVerdict
    cost_usd: float
    started_at: str  # iso timestamp
    ended_at: str
    integration_union_guard: dict[str, Any]

    # Merge-failure feedback
    merge_blocked_reason: str
    merge_blocked_structured_reason: dict[str, Any]
    merge_blocked_origin: str
    failure_reason: str

    # Contract-amendment retry subsystem (audit3 + repair.py)
    contract_amendment: dict[str, Any]
    contract_amendment_attempts: dict[str, int]  # key: contract_path slug
    contract_amendment_paths: list[str]
    contract_amendment_path: str
    contract_amendment_owner_task_id: str
    contract_amendment_merge_context: dict[str, Any]
    contract_amendment_retry_merge: bool
    contract_path: str
    blocked_on_task_id: str
    blocked_pending_contract_amendment: bool
    owner_task_id: str


# ---------------------------------------------------------------------------
# 2) PIPELINE EVENT PAYLOAD  —  the `on_event(payload)` callback in run_v5_pipeline
# ---------------------------------------------------------------------------
#
# 40+ distinct `_emit(on_event, {"event": ..., ...})` sites in
# otto/v5_runner + otto/v5/*. The shape varies per `event` type.
# Consumers (UI Mission Control, CLI prints in cli_run._on_event) keyed
# off `event` to render. This TypedDict is the COMMON envelope; per-event
# shapes have their own keys not listed here.

EventName = Literal[
    "compile_start",
    "compile_done",
    "context_scope_resolution_agent_start",
    "context_scope_resolution_agent_done",
    "context_slice",
    "capability_inventory_injected",
    "feature_owned_paths_recorded",
    "lead_start",
    "lead_done",
    "child_dispatch",
    "child_done",
    "child_crash",
    "child_branch_ancestry_ok",
    "child_worktree_setup_failed",
    "child_merge_blocked",
    "child_verify_repair_start",
    "child_verify_repair_done",
    "child_recovery_reconciled",
    "child_recovery_not_reconciled",
    "integration_start",
    "integration_done",
    "checkout_preflight_issue",
    "architect_contract_invalid",
    "architect_contract_rescoped",
    "architect_contract_landed_partial",
    "architect_scaffold_repair_blocked",
    "coherence_finding",
    "budget_cap_hit",
    "budget_cap_drain_failure",
    "contract_amendment_leaf_merge_retry",
    "contract_amendment_leaf_merge_retry_claim_skipped",
    "contract_amendment_leaf_retry_verdict_restored",
    "contract_amendment_leaf_retry_claims_exhausted",
    "foundation_clean_boot_degraded_to_scaffold",
    "preflight_issue",
    "v5_resume_from_checkpoint",
]


class PipelineEvent(TypedDict, total=False):
    """Common envelope for `_emit()` event payloads.

    `event` is the dispatch key; downstream consumers (the on_event
    callback in cli_run, the Mission Control UI) switch on it. Per-event
    shapes carry additional keys not listed here — see the producer site
    for the exact shape of any one event.
    """

    event: EventName

    # Frequently-present keys across event variants
    task_id: str
    verdict: str
    decomposition: Decomposition
    cost_usd: float
    spent: float  # used by budget_cap_hit
    cap: float  # used by budget_cap_hit
    error: str  # crash / child_crash detail
    error_type: str
    reason: str
    detail: str
    message: str
    phase: str
    journey_count: int  # compile_done
    lint_warnings: int  # compile_done
    emitted: int  # lead_done / v5_resume_from_checkpoint
    kind: str  # preflight_issue / checkout_preflight_issue
    severity: str  # preflight_issue
    repair_packets_carried: int  # v5_resume_from_checkpoint


# ---------------------------------------------------------------------------
# 3) REPAIR PACKET  —  written by runner, read by the repair agent (subprocess boundary)
# ---------------------------------------------------------------------------
#
# Written as JSON to `otto_logs/sessions/<id>/queue/<task>/repair-packet.json`.
# Crosses the agent subprocess boundary, so shape drift means silent
# repair-agent failures. The audit calls this out as one of the highest-
# leverage typing targets.
#
# `_read_latest_conflict_packet` and `_carry_and_reset_prior_repair_packets` read
# the packet back; the repair agent's pass-model returns to it.

RepairPhase = Literal[
    "startup_port_cleanup",
    "pipeline_start",
    "preflight",
    "merge_conflict",
    "smoke",
    "integration",
    "foundation_contract",
]


class RepairPacketAttempt(TypedDict, total=False):
    """One entry in `repair-packet.json :: attempt_history`."""

    attempt_index: int
    started_at: str  # iso timestamp
    ended_at: str
    repair_verdict: str  # "pass" | "fail" | "no_repair_needed" | ...
    detail: str
    cost_usd: float
    duration_s: float
    agent_session_id: str


class RepairPacket(TypedDict, total=False):
    """Shape of `repair-packet.json` for one repair unit.

    Read by:
      - `otto.v5_preflight_repair._read_latest_conflict_packet`
      - `otto.v5_runner._carry_and_reset_prior_repair_packets`
      - the repair agent (via the SDK input file)
    Written by:
      - `otto.v5_runner._run_preflight_payload_repair_session`
      - `otto.v5/repair.py` various schedulers
    """

    schema_version: int
    repair_unit_id: str
    repair_phase: RepairPhase
    task_id: str
    parent_task_id: str | None
    integration_branch: str | None

    # The structured payload the repair agent reads
    payload: dict[str, Any]  # phase-specific (issues / conflict info / etc.)
    integration_context: dict[str, Any]

    # Branch + worktree state at packet creation
    source_branch: str
    target_branch: str
    unmerged_paths: list[str]
    pre_merge_ref: str

    # Retry history + budget
    attempt_history: list[RepairPacketAttempt]
    attempt_budget: int
    attempts_used: int

    # Terminal state once the unit completes
    terminal_state: Literal["", "pass", "exhausted", "escalated"]
    final_verdict: str
    final_detail: str
    final_cost_usd: float
    _written_at: str
