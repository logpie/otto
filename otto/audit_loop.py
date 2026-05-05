"""Layer 2 retry loop for failing Features (research §4 retry layers).

Per research §4: when audit's LLM judge flags a Feature as failing or
partial, the failing Feature is routed back to its Group's agent for
ONE repair attempt; audit re-runs only on the affected Features (not
the whole product).

Layer 1 (Build's check loop) handles deterministic Check failures
inside a Group. Layer 2 (this module) handles LLM-judged Feature
failures across Groups. No Layer 3 — after Layer 2 cap exhaustion,
the Run lands honestly with `verdict=partial` or `blocked`.

Caps come from `otto/defaults.py`:
- `retries.audit_loop.max_repair_attempts_per_run`: per-Feature repair cap (default 1)
- `retries.audit_loop.max_audit_passes_per_run`: total audit passes including original (default 2)

Quality findings with severity `critical` flip a Feature verdict to
`partial` and trigger Layer 2 repair. `important`/`polish` findings
do NOT trigger repair (research §4 severity ladder).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from otto.spec_compile import Feature, Group, Spec


# ---------------------------------------------------------------------------
# Result data shapes
# ---------------------------------------------------------------------------


@dataclass
class FailingFeature:
    """A Feature flagged by audit as failing or partial."""
    feature_id: str
    verdict: str  # "failed" | "partial" | "blocked" | "missing"
    detail: str
    severity_findings: list[str] = field(default_factory=list)


@dataclass
class RepairAttempt:
    """One repair attempt by a Group's agent against a failing Feature."""
    feature_id: str
    group_id: str
    attempt_number: int  # 1-based
    succeeded: bool
    new_verdict: str | None = None  # set after re-audit
    detail: str = ""
    cost_usd: float = 0.0
    wall_s: float = 0.0


@dataclass
class RepairResult:
    """Aggregate outcome of running the audit loop."""
    attempts: list[RepairAttempt] = field(default_factory=list)
    audit_passes_run: int = 1   # original audit pass counts as 1
    halted_reason: str = ""     # "" = normal completion; else explanation

    @property
    def repaired_feature_ids(self) -> list[str]:
        return [a.feature_id for a in self.attempts if a.succeeded]

    @property
    def still_failing_feature_ids(self) -> list[str]:
        return [a.feature_id for a in self.attempts if not a.succeeded]


# ---------------------------------------------------------------------------
# Caps + selection
# ---------------------------------------------------------------------------


def _repair_cap_default() -> int:
    """Default per-Run repair attempts cap, read from defaults."""
    from otto import defaults
    return int(defaults.get("retries.audit_loop.max_repair_attempts_per_run"))


def _audit_passes_cap_default() -> int:
    """Default total audit-passes cap (original + re-audits)."""
    from otto import defaults
    return int(defaults.get("retries.audit_loop.max_audit_passes_per_run"))


def select_failing_features(
    feature_verdicts: list[dict[str, Any]],
) -> list[FailingFeature]:
    """Extract failing/partial Features from `feature-verdicts.json`.

    A Feature is a repair candidate if its verdict is one of:
        "failed", "partial", "blocked", "missing".
    """
    failing: list[FailingFeature] = []
    for v in feature_verdicts:
        if not isinstance(v, dict):
            continue
        verdict = str(v.get("verdict") or "").strip().lower()
        if verdict not in {"failed", "partial", "blocked", "missing"}:
            continue
        failing.append(
            FailingFeature(
                feature_id=str(v.get("feature_id") or ""),
                verdict=verdict,
                detail=str(v.get("detail") or ""),
                severity_findings=[
                    str(s) for s in (v.get("severity_findings") or [])
                ],
            )
        )
    return failing


def group_for_feature(spec: Spec, feature_id: str) -> Group | None:
    """Find the Group that owns a Feature.

    Returns None if the Feature isn't found or has no group_id.
    """
    feature = next((f for f in spec.features if f.id == feature_id), None)
    if feature is None or not feature.group_id:
        return None
    return next((g for g in spec.groups if g.id == feature.group_id), None)


def features_to_repair(
    spec: Spec,
    feature_verdicts: list[dict[str, Any]],
    *,
    max_attempts_per_run: int | None = None,
) -> list[FailingFeature]:
    """Pick which failing Features to attempt repair on this audit pass.

    Bounded by `max_attempts_per_run` (defaults from
    retries.audit_loop.max_repair_attempts_per_run). Selection order
    is the failing-feature list order — first-found-first-attempted.

    Features without a known Group (orphan features) are excluded —
    repair has nowhere to route.
    """
    cap = (
        max_attempts_per_run
        if max_attempts_per_run is not None
        else _repair_cap_default()
    )
    failing = select_failing_features(feature_verdicts)
    candidates: list[FailingFeature] = []
    for f in failing:
        if group_for_feature(spec, f.feature_id) is None:
            continue
        candidates.append(f)
        if len(candidates) >= cap:
            break
    return candidates


def can_run_another_audit_pass(
    *, audit_passes_run: int, max_audit_passes: int | None = None
) -> bool:
    """Layer 2 cap check: have we exhausted audit passes for this Run?"""
    cap = (
        max_audit_passes
        if max_audit_passes is not None
        else _audit_passes_cap_default()
    )
    return audit_passes_run < cap


# ---------------------------------------------------------------------------
# A2.2 — Layer 2 orchestration loop
# ---------------------------------------------------------------------------
#
# `repair_failing_features` drives the per-Feature repair loop:
#   1. Select failing Features (via features_to_repair, bounded by cap).
#   2. For each, dispatch a fix attempt via the supplied `fix_agent`
#      callback (signature: (feature_id, group, detail) -> awaitable
#      RepairAttempt). Production wires this to a build-agent invocation
#      narrowed to that Group + Feature.
#   3. After all attempts in the pass, if the audit-passes cap allows,
#      invoke `re_audit` (callback) narrowed to the attempted feature ids.
#      Update each RepairAttempt.new_verdict from the re-audit's result.
#   4. Loop until the audit-passes cap is exhausted or no failing
#      features remain.
#
# The function is generic across fix-agent and re-audit shapes — both
# are passed as callbacks so tests can stub them deterministically.


import time as _time
from collections.abc import Awaitable, Callable

# A FixAgentCallable accepts (failing_feature, group) and returns a
# RepairAttempt-shaped result describing what it tried.
FixAgentCallable = Callable[
    [FailingFeature, Group],
    Awaitable[RepairAttempt],
]

# A ReAuditCallable accepts a list of feature ids to narrow the re-audit
# to and returns the updated feature_verdicts list (same shape as the
# input to features_to_repair).
ReAuditCallable = Callable[
    [list[str]],
    Awaitable[list[dict[str, Any]]],
]


async def repair_failing_features(
    *,
    spec: Spec,
    feature_verdicts: list[dict[str, Any]],
    fix_agent: FixAgentCallable,
    re_audit: ReAuditCallable | None = None,
    max_attempts_per_run: int | None = None,
    max_audit_passes: int | None = None,
    audit_passes_so_far: int = 1,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> RepairResult:
    """Drive the per-Feature repair loop (research §4 retry layers).

    Args:
        spec: the run's Spec (used to map feature_id → Group).
        feature_verdicts: feature-verdict dicts from the latest audit pass.
        fix_agent: async callback that performs one repair attempt for
            one (FailingFeature, Group). Returns a populated RepairAttempt
            with `succeeded` set; `new_verdict` will be filled by re_audit.
        re_audit: async callback that re-runs audit narrowed to the given
            feature ids. Returns the updated feature_verdicts list. If
            None, no re-audit happens — RepairAttempts stay with
            `new_verdict=None`. Caller is responsible for the next loop.
        max_attempts_per_run: per-Run repair cap (default from
            `retries.audit_loop.max_repair_attempts_per_run`).
        max_audit_passes: total audit-passes cap including the original
            (default from `retries.audit_loop.max_audit_passes_per_run`).
        audit_passes_so_far: 1 if the caller has run only the original
            audit pass; bump per re-audit.
        on_event: optional callback for structured event reporting
            (signature: kind, payload). Called for `audit.feature_repair.started`,
            `.finished`, `audit.re_audit.started`, `.finished`,
            `audit.repair_loop.halted`.

    Returns:
        A RepairResult with one RepairAttempt per attempted Feature.
    """
    result = RepairResult(audit_passes_run=audit_passes_so_far)

    def emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(kind, payload)
            except Exception:  # noqa: BLE001 — never let observability crash the loop
                pass

    selected = features_to_repair(
        spec,
        feature_verdicts,
        max_attempts_per_run=max_attempts_per_run,
    )
    if not selected:
        result.halted_reason = "no_failing_features"
        return result

    if not can_run_another_audit_pass(
        audit_passes_run=audit_passes_so_far,
        max_audit_passes=max_audit_passes,
    ):
        # No budget for re-audit: still attempt the fixes but record
        # honestly that we cannot verify them.
        result.halted_reason = "audit_passes_cap_exhausted"

    # ---- Phase 1: dispatch fix attempts ----
    for failing in selected:
        group = group_for_feature(spec, failing.feature_id)
        if group is None:
            # Should be filtered by features_to_repair, but defensive.
            continue
        emit("audit.feature_repair.started", {
            "feature_id": failing.feature_id,
            "group_id": group.id,
            "verdict_before": failing.verdict,
        })
        t0 = _time.monotonic()
        try:
            attempt = await fix_agent(failing, group)
        except Exception as exc:  # noqa: BLE001
            attempt = RepairAttempt(
                feature_id=failing.feature_id,
                group_id=group.id,
                attempt_number=1,
                succeeded=False,
                new_verdict=None,
                detail=f"fix_agent raised {type(exc).__name__}: {exc}",
                cost_usd=0.0,
                wall_s=_time.monotonic() - t0,
            )
        # Defensive: ensure feature_id + group_id are correct on the
        # returned attempt regardless of what the fix_agent populated.
        attempt.feature_id = failing.feature_id
        attempt.group_id = group.id
        result.attempts.append(attempt)
        emit("audit.feature_repair.finished", {
            "feature_id": failing.feature_id,
            "group_id": group.id,
            "succeeded": attempt.succeeded,
            "wall_s": attempt.wall_s,
            "cost_usd": attempt.cost_usd,
        })

    # ---- Phase 2: re-audit narrowed to attempted features ----
    if re_audit is None or result.halted_reason == "audit_passes_cap_exhausted":
        return result

    attempted_ids = [a.feature_id for a in result.attempts]
    if not attempted_ids:
        return result

    emit("audit.re_audit.started", {"feature_ids": attempted_ids})
    try:
        new_verdicts = await re_audit(attempted_ids)
    except Exception as exc:  # noqa: BLE001
        result.halted_reason = f"re_audit_raised: {type(exc).__name__}: {exc}"
        emit("audit.repair_loop.halted", {"reason": result.halted_reason})
        return result
    result.audit_passes_run += 1
    emit("audit.re_audit.finished", {
        "feature_ids": attempted_ids,
        "verdict_count": len(new_verdicts),
    })

    # Backfill each RepairAttempt.new_verdict from the re-audit output.
    by_id: dict[str, str] = {}
    for v in new_verdicts:
        if not isinstance(v, dict):
            continue
        fid = str(v.get("feature_id") or "")
        verdict = str(v.get("verdict") or "")
        if fid and verdict:
            by_id[fid] = verdict
    for a in result.attempts:
        if a.feature_id in by_id:
            a.new_verdict = by_id[a.feature_id]
            # If re-audit says it's now passed, mark the attempt succeeded.
            if a.new_verdict == "passed":
                a.succeeded = True

    return result


__all__ = [
    "FailingFeature",
    "RepairAttempt",
    "RepairResult",
    "select_failing_features",
    "group_for_feature",
    "features_to_repair",
    "can_run_another_audit_pass",
    "repair_failing_features",
    "FixAgentCallable",
    "ReAuditCallable",
]
