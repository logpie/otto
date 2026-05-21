"""Single source of truth: "what would `otto run` (no --fresh) do
right now given the project's persisted state?"

Used by three sites that historically duplicated this logic (with
drift):
  - otto/v5_runner.py::_resume_root_from_checkpoint — actual resume decision
  - otto/cli_v5.py::status_cmd                       — read-only diagnostic
  - otto/cli_v5.py::plan_resume_cmd                  — read-only simulation (Phase 2)

Codex Plan Gate R2#7 + the user's "centralize APIs" mandate this
extraction. Without it, drift between the three checks could give
misleading advice (the original dup-drift class that caused the
iTracker Opus cascade).

Phase 2 of plan-checkpoint-resume-v2.md (Codex APPROVED at R5).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from otto.schemas import VERDICT_MERGE_BLOCKED


# Cost estimates are coarse — they're meant to set order-of-magnitude
# expectations before spending, not to be precise. Calibrated from the
# iTracker validation runs (Sonnet $17, Opus $87-205, retry $30-90).
COST_PER_OPUS_CHILD_REBUILD_USD = (15.0, 35.0, 60.0)  # low / p50 / high
COST_PER_SONNET_CHILD_REBUILD_USD = (3.0, 8.0, 15.0)
COST_PER_INTEGRATION_PHASE_USD = (5.0, 15.0, 40.0)
COST_PER_INTEGRATION_REPAIR_USD = (0.0, 10.0, 30.0)  # may not fire

# Wall time estimates (minutes).
WALL_PER_OPUS_CHILD_REBUILD_MIN = (10.0, 20.0, 40.0)
WALL_PER_SONNET_CHILD_REBUILD_MIN = (5.0, 10.0, 20.0)
WALL_PER_INTEGRATION_MIN = (5.0, 15.0, 40.0)


ResumeStatus = Literal[
    "RESUMABLE",        # would resume at integration phase
    "NOT_RESUMABLE",    # terminal-done (pass/catastrophic) or pre-decompose
    "FRESH_ONLY",       # no graph state — only fresh runs possible
]

ChildAction = Literal[
    "skip_pass",              # verdict satisfactory; no work needed
    "merge_unmerged",         # passed but branch not yet merged (re-merge attempt)
    "rebuild_via_retry",      # verdict=None + retry_count>0 → scheduler picks up
    "stays_merge_blocked",    # merge_blocked verdict; integration won't fix
    "stays_unverified",       # unverified; would re-enter through integration
    "pending_children",       # internal-node task; not directly actionable here
    "unknown_state",          # bogus / unhandled — surfaces as concern
]


@dataclass
class ChildPrediction:
    task_id: str
    intent_preview: str
    current_verdict: str
    retry_count: int
    action: ChildAction
    concern: str | None = None
    landed_with_annotation: bool = False
    has_blocker_metadata: bool = False


@dataclass
class ResumePlan:
    """The full picture of what `otto run` (no --fresh) would do
    right now. Pure data — caller renders or consumes."""

    status: ResumeStatus
    not_resumable_reason: str | None = None

    # Root task state
    root_verdict: str = "unknown"
    root_intent_preview: str = ""

    # Children predictions
    children: list[ChildPrediction] = field(default_factory=list)

    # Phase resume would re-enter (or none if not resumable)
    phase_to_enter: str | None = None  # "integration" | None
    skipped_phases: list[str] = field(default_factory=list)
    latest_spec_checkpoint: str | None = None
    repair_packets_carriable: int = 0

    # Cost + wall estimates (low/p50/high) — USD + minutes
    estimated_cost_usd_range: tuple[float, float, float] = (0.0, 0.0, 0.0)
    estimated_wall_minutes_range: tuple[float, float, float] = (0.0, 0.0, 0.0)
    model_assumed: str = "sonnet"  # the lazy default if no override

    # Concerns surfaced (advisory)
    concerns: list[str] = field(default_factory=list)

    # Suggested commands (CLI hints)
    suggested_next: list[str] = field(default_factory=list)


# ============================================================
# The planner
# ============================================================


def compute_resume_plan(
    *,
    project_dir: Path,
    intent_for_match: str | None = None,
    model: str = "sonnet",
    config: dict[str, Any] | None = None,
) -> ResumePlan:
    """Inspect the project's persisted state and predict what
    `otto run` (no --fresh) would do.

    Args:
        project_dir: project root.
        intent_for_match: if given, predict what would happen FOR
            this intent. If the persisted root intent differs, resume
            is refused. If None, no intent match is enforced (used by
            `status` which doesn't know the intent).
        model: which provider model to assume for cost estimates.
            "sonnet" | "opus" | other → uses Sonnet defaults.
        config: optional v5 config (mainly to check
            v5_resume_from_checkpoint=False opt-out).

    Returns a `ResumePlan` — pure data, no side effects.
    """
    from otto.queue.task_graph import (
        entry_is_satisfactory_terminal,
        read_graph,
    )
    from otto.v5_runner import ROOT_TASK_ID

    plan = ResumePlan(status="NOT_RESUMABLE", root_verdict="unknown")
    cfg = config or {}

    # 0. Opt-out config.
    if cfg.get("v5_resume_from_checkpoint") is False:
        plan.status = "NOT_RESUMABLE"
        plan.not_resumable_reason = (
            "v5_resume_from_checkpoint=False in config — explicit opt-out"
        )
        return plan

    # 1. Graph readable + has root?
    try:
        graph = read_graph(project_dir)
    except Exception as exc:  # noqa: BLE001 - any IO/parse error
        plan.status = "FRESH_ONLY"
        plan.not_resumable_reason = (
            f"could not read task graph ({type(exc).__name__}: {exc})"
        )
        return plan
    tasks = graph.get("tasks") or {}
    if isinstance(tasks, list):
        tasks = {t["id"]: t for t in tasks if isinstance(t, dict)}
    if ROOT_TASK_ID not in tasks:
        plan.status = "FRESH_ONLY"
        plan.not_resumable_reason = "no root task in graph — only fresh runs possible"
        return plan
    root = tasks[ROOT_TASK_ID]
    plan.root_verdict = str(root.get("verdict") or "unknown")
    plan.root_intent_preview = (root.get("intent") or "")[:120]

    # 2. Has children emitted?
    child_ids = list(root.get("child_task_ids") or [])
    if not child_ids:
        plan.status = "NOT_RESUMABLE"
        plan.not_resumable_reason = (
            "no children emitted — decomposition didn't complete; "
            "fresh run required"
        )
        return plan

    # 3. Terminal done state?
    if plan.root_verdict in {"pass", "catastrophic"}:
        plan.status = "NOT_RESUMABLE"
        plan.not_resumable_reason = (
            f"root verdict is '{plan.root_verdict}' (terminal done state); "
            f"use --fresh for a new run"
        )
        return plan

    # 4. Intent match (if caller provided one).
    if intent_for_match is not None:
        persisted = str(root.get("intent") or "").strip()
        if persisted and persisted != intent_for_match.strip():
            plan.status = "NOT_RESUMABLE"
            plan.not_resumable_reason = (
                "persisted root intent differs from the new intent; "
                "use --fresh (intent-drift guard)"
            )
            return plan

    # 5. Spec checkpoint exists?
    spec_dir = project_dir / "otto_logs" / "sessions"
    spec_candidates: list[tuple[str, Path]] = []
    if spec_dir.exists():
        for sdir in sorted(spec_dir.iterdir(), reverse=True):
            spec_path = sdir / "spec" / "spec.json"
            if spec_path.exists():
                spec_candidates.append((sdir.name, spec_path))
    if not spec_candidates:
        plan.status = "NOT_RESUMABLE"
        plan.not_resumable_reason = (
            "no spec.json checkpoint found under otto_logs/sessions/*/spec/"
        )
        return plan
    plan.latest_spec_checkpoint = spec_candidates[0][0]

    # 6. Resumable — predict actions per child.
    plan.status = "RESUMABLE"
    plan.phase_to_enter = "integration"
    plan.skipped_phases = ["compile", "decompose", "child-build (most)"]
    plan.model_assumed = model

    rebuild_count = 0
    blocked_count = 0
    has_unmerged_pass = False
    for cid in child_ids:
        child = tasks.get(cid) or {}
        if not isinstance(child, dict):
            continue
        pred = _predict_child(cid, child, entry_is_satisfactory_terminal)
        plan.children.append(pred)
        if pred.action == "rebuild_via_retry":
            rebuild_count += 1
        elif pred.action == "stays_merge_blocked":
            blocked_count += 1
        elif pred.action == "merge_unmerged":
            has_unmerged_pass = True
        if pred.concern:
            plan.concerns.append(f"{cid}: {pred.concern}")

    # 7. Cost / wall estimate.
    if model == "opus":
        per_child = COST_PER_OPUS_CHILD_REBUILD_USD
        per_wall = WALL_PER_OPUS_CHILD_REBUILD_MIN
    else:
        per_child = COST_PER_SONNET_CHILD_REBUILD_USD
        per_wall = WALL_PER_SONNET_CHILD_REBUILD_MIN

    cost_low = rebuild_count * per_child[0] + COST_PER_INTEGRATION_PHASE_USD[0]
    cost_p50 = rebuild_count * per_child[1] + COST_PER_INTEGRATION_PHASE_USD[1] + COST_PER_INTEGRATION_REPAIR_USD[1]
    cost_high = rebuild_count * per_child[2] + COST_PER_INTEGRATION_PHASE_USD[2] + COST_PER_INTEGRATION_REPAIR_USD[2]
    plan.estimated_cost_usd_range = (cost_low, cost_p50, cost_high)

    wall_low = rebuild_count * per_wall[0] / max(1, _max_parallel(cfg)) + WALL_PER_INTEGRATION_MIN[0]
    wall_p50 = rebuild_count * per_wall[1] / max(1, _max_parallel(cfg)) + WALL_PER_INTEGRATION_MIN[1]
    wall_high = rebuild_count * per_wall[2] / max(1, _max_parallel(cfg)) + WALL_PER_INTEGRATION_MIN[2]
    plan.estimated_wall_minutes_range = (wall_low, wall_p50, wall_high)

    # 8. Suggestions + repair-packets carriable.
    plan.repair_packets_carriable = _count_carriable_repair_packets(project_dir)
    if blocked_count > 0:
        blocked_ids = [c.task_id for c in plan.children if c.action == "stays_merge_blocked"]
        plan.suggested_next.append(
            f"otto v5 retry-children {' '.join('--task ' + cid for cid in blocked_ids)}"
            f"  # turn the {blocked_count} merge_blocked child(ren) into runnable retries"
        )
    if has_unmerged_pass:
        plan.concerns.append(
            "one or more children are pass-verdict but their branch has not "
            "merged into main — the runner's re-merge-unmerged pass should "
            "address this on the next run"
        )
    if plan.status == "RESUMABLE":
        plan.suggested_next.append('otto run "<original intent>"')
        if plan.root_verdict in {"merge_blocked", "partial"} and blocked_count == 0 and rebuild_count == 0:
            plan.concerns.append(
                f"root is '{plan.root_verdict}' but no children need rebuild; "
                f"the next run will re-enter integration only (cheap, ~$5-30)"
            )

    return plan


# ============================================================
# Internals
# ============================================================


def _predict_child(
    task_id: str,
    entry: dict[str, Any],
    sat_fn: Any,
) -> ChildPrediction:
    """Predict what happens to this child on the next `otto run`."""
    intent_preview = (entry.get("intent") or "")[:80]
    verdict = str(entry.get("verdict") or "") or "(none)"
    retry_count = int(entry.get("retry_count", 0) or 0)
    has_blocker = bool(
        entry.get("merge_blocked_reason")
        or entry.get("merge_blocked_structured_reason")
        or entry.get("merge_blocked_origin")
    )
    landed_annot = bool(entry.get("landed_with_annotation"))

    # Phase 1 retry-children path: verdict=None + retry_count>0 → runnable
    if entry.get("verdict") is None and retry_count > 0:
        return ChildPrediction(
            task_id=task_id,
            intent_preview=intent_preview,
            current_verdict="retry_pending",
            retry_count=retry_count,
            action="rebuild_via_retry",
            concern=None,
            landed_with_annotation=False,
            has_blocker_metadata=has_blocker,
        )

    # Internal node (decomposed itself)
    if entry.get("decomposition") == "emit" or entry.get("child_task_ids"):
        return ChildPrediction(
            task_id=task_id,
            intent_preview=intent_preview,
            current_verdict=verdict,
            retry_count=retry_count,
            action="pending_children",
            concern="non-leaf task; not directly actionable via retry-children",
            has_blocker_metadata=has_blocker,
        )

    # Satisfactory terminal — would skip
    if sat_fn(entry):
        return ChildPrediction(
            task_id=task_id,
            intent_preview=intent_preview,
            current_verdict=verdict,
            retry_count=retry_count,
            action="skip_pass",
            landed_with_annotation=landed_annot,
            has_blocker_metadata=False,
        )

    # Merge-blocked
    if verdict == VERDICT_MERGE_BLOCKED:
        return ChildPrediction(
            task_id=task_id,
            intent_preview=intent_preview,
            current_verdict=verdict,
            retry_count=retry_count,
            action="stays_merge_blocked",
            concern=(
                "merge_blocked verdict — would NOT be re-dispatched by "
                "resume alone. Use `otto v5 retry-children` first."
            ),
            has_blocker_metadata=has_blocker,
        )

    # Unverified, no retry pending
    if verdict in {"unverified", "(none)"}:
        return ChildPrediction(
            task_id=task_id,
            intent_preview=intent_preview,
            current_verdict=verdict,
            retry_count=retry_count,
            action="stays_unverified",
            concern=(
                "unverified verdict — would re-enter integration as-is; if "
                "the prior result was a false demotion, use retry-children "
                "to re-dispatch the build phase"
            ),
            has_blocker_metadata=has_blocker,
        )

    return ChildPrediction(
        task_id=task_id,
        intent_preview=intent_preview,
        current_verdict=verdict,
        retry_count=retry_count,
        action="unknown_state",
        concern=f"unhandled verdict '{verdict}' — inspect graph.json manually",
        has_blocker_metadata=has_blocker,
    )


def _max_parallel(cfg: dict[str, Any]) -> int:
    """Best-guess at how parallelism affects wall-time."""
    try:
        return max(1, int(cfg.get("max_parallel", 3) or 3))
    except (TypeError, ValueError):
        return 3


def _count_carriable_repair_packets(project_dir: Path) -> int:
    """How many prior integration-repair packets `_carry_prior_repair_packets`
    would pick up. Mirrors that helper's logic without coupling."""
    sessions = project_dir / "otto_logs" / "sessions"
    if not sessions.exists():
        return 0
    seen_units: set[str] = set()
    count = 0
    for sdir in sorted(sessions.iterdir(), reverse=True):
        if not sdir.is_dir():
            continue
        repair_dir = sdir / "integration" / "repair"
        if not repair_dir.exists():
            continue
        for unit_dir in repair_dir.iterdir():
            if not unit_dir.is_dir():
                continue
            packet = unit_dir / "repair_packet.json"
            if not packet.exists():
                continue
            if unit_dir.name in seen_units:
                continue
            seen_units.add(unit_dir.name)
            count += 1
    return count


# ============================================================
# Serialization for --json output
# ============================================================


def plan_to_json(plan: ResumePlan) -> str:
    """Stable JSON schema for plan-resume output. Versioned so MC /
    scripts can detect breaking changes."""
    return json.dumps({
        "schema_version": 1,
        "status": plan.status,
        "not_resumable_reason": plan.not_resumable_reason,
        "root_verdict": plan.root_verdict,
        "root_intent_preview": plan.root_intent_preview,
        "phase_to_enter": plan.phase_to_enter,
        "skipped_phases": plan.skipped_phases,
        "latest_spec_checkpoint": plan.latest_spec_checkpoint,
        "repair_packets_carriable": plan.repair_packets_carriable,
        "model_assumed": plan.model_assumed,
        "estimated_cost_usd_range": {
            "low": plan.estimated_cost_usd_range[0],
            "p50": plan.estimated_cost_usd_range[1],
            "high": plan.estimated_cost_usd_range[2],
        },
        "estimated_wall_minutes_range": {
            "low": plan.estimated_wall_minutes_range[0],
            "p50": plan.estimated_wall_minutes_range[1],
            "high": plan.estimated_wall_minutes_range[2],
        },
        "children": [
            {
                "task_id": c.task_id,
                "intent_preview": c.intent_preview,
                "current_verdict": c.current_verdict,
                "retry_count": c.retry_count,
                "action": c.action,
                "concern": c.concern,
                "landed_with_annotation": c.landed_with_annotation,
                "has_blocker_metadata": c.has_blocker_metadata,
            }
            for c in plan.children
        ],
        "concerns": plan.concerns,
        "suggested_next": plan.suggested_next,
    }, indent=2)
