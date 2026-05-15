"""Layer 2 retry loop for failing Features (research §4 retry layers).

Per research §4: when audit's LLM judge flags a Feature as failing or
partial, the failing Feature is routed back to its Group's agent for a
bounded repair attempt; audit re-runs only on the affected Features
(not the whole product). If the narrowed re-audit still reports an
actionable failure and the run has repair/audit budget left, the loop
continues instead of stopping after the first repair pass.

Layer 1 (Build's check loop) handles deterministic Check failures
inside a Group. Layer 2 (this module) handles LLM-judged Feature
failures across Groups. No Layer 3 — after Layer 2 cap exhaustion,
the Run lands honestly with `verdict=partial` or `blocked`.

Caps come from `otto/defaults.py`:
- `retries.audit_loop.max_repair_attempts_per_run`: maximum actionable Feature
  repairs in one run
- `retries.audit_loop.max_audit_passes_per_run`: total audit passes including original

Per-Feature quality findings with severity `critical` flip that Feature verdict
to `partial` and trigger Layer 2 repair. Product-wide quality findings can also
be routed to relevant Features by the runner when they keep the aggregate audit
verdict non-PASS even though all explicit Feature audits passed.
"""

from __future__ import annotations

import time as _time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from otto.repair_gates import repair_gate_for_verdict
from otto.spec_compile import Group, Spec


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
    related_feature_ids: list[str] = field(default_factory=list)


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
        if not _is_actionable_repair_verdict(v, verdict=verdict):
            continue
        failing.append(
            FailingFeature(
                feature_id=str(v.get("feature_id") or ""),
                verdict=verdict,
                detail=str(v.get("detail") or ""),
                severity_findings=[
                    str(s) for s in (v.get("severity_findings") or [])
                ],
                related_feature_ids=[
                    str(s) for s in (v.get("related_feature_ids") or [])
                    if str(s).strip()
                ],
            )
        )
    return failing


_UNACTIONABLE_BLOCKED_DETAIL_MARKERS: tuple[str, ...] = (
    "not evaluated",
    "not audited",
    "not tested",
    "not verified",
    "not in scope",
    "out of scope",
    "no direct test evidence",
    "no test evidence",
    "no evidence collected",
    "tests were not audited",
)
_UNACTIONABLE_REPAIR_DETAIL_MARKERS: tuple[str, ...] = (
    *_UNACTIONABLE_BLOCKED_DETAIL_MARKERS,
    "no changes were needed",
    "no changes needed",
    "not affected",
    "does not mention this function",
    "not mention this function",
    "outside the requested scope",
    "outside of the requested scope",
)


def _is_actionable_repair_verdict(
    verdict_payload: dict[str, Any],
    *,
    verdict: str,
) -> bool:
    """Return whether a verdict should be routed to a repair agent.

    Audit agents sometimes mark unrelated or uninspected Features as
    ``blocked`` simply because they did not gather evidence. Those are
    audit-coverage gaps, not implementation bugs. Sending them to repair
    burns provider budget and can crowd out the real failing features in
    the same audit pass.
    """
    detail = str(verdict_payload.get("detail") or "").casefold()
    if any(marker in detail for marker in _UNACTIONABLE_REPAIR_DETAIL_MARKERS):
        return False
    has_strength_metadata = any(
        str(verdict_payload.get(key) or "").strip()
        for key in ("surface", "methodology", "evidence_completeness", "coverage_confidence")
    )
    if verdict != "blocked":
        return repair_gate_for_verdict(verdict_payload).actionable if has_strength_metadata else True
    evidence_refs = verdict_payload.get("evidence_refs") or []
    if isinstance(evidence_refs, list) and any(str(ref).strip() for ref in evidence_refs):
        return repair_gate_for_verdict(verdict_payload).actionable if has_strength_metadata else True
    if not detail:
        return repair_gate_for_verdict(verdict_payload).actionable if has_strength_metadata else True
    if any(marker in detail for marker in _UNACTIONABLE_BLOCKED_DETAIL_MARKERS):
        return False
    return repair_gate_for_verdict(verdict_payload).actionable if has_strength_metadata else True


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
    ensure_each_group_first_attempt: bool = True,
) -> list[FailingFeature]:
    """Pick which failing Features to attempt repair on this audit pass.

    Selection order is the failing-feature list order, coalesced to one repair
    dispatch per Group. Every failing Group gets one first repair attempt
    before the per-run cap is allowed to truncate follow-up work.

    Features without a known Group are spec/verdict contract mismatches and
    raise instead of being silently dropped.
    """
    cap = (
        max_attempts_per_run
        if max_attempts_per_run is not None
        else _repair_cap_default()
    )
    failing = select_failing_features(feature_verdicts)
    by_group: dict[str, list[FailingFeature]] = {}
    group_order: list[str] = []
    for feature in failing:
        group = group_for_feature(spec, feature.feature_id)
        if group is None:
            raise ValueError(
                "failing audit verdict references feature without repair group: "
                f"{feature.feature_id}"
            )
        if group.id not in by_group:
            group_order.append(group.id)
            by_group[group.id] = []
        by_group[group.id].append(feature)

    effective_cap = (
        max(int(cap), len(group_order))
        if ensure_each_group_first_attempt
        else int(cap)
    )
    candidates: list[FailingFeature] = []
    for group_id in group_order:
        candidates.append(_coalesce_group_failures(group_id, by_group[group_id]))
        if len(candidates) >= effective_cap:
            break
    return candidates


def _coalesce_group_failures(
    group_id: str,
    failures: list[FailingFeature],
) -> FailingFeature:
    """Return one repair dispatch for all current failures in a Group.

    The implementation unit is the Group. When several Features in the
    same Group fail the same audit pass, sending one over-narrow repair
    per Feature burns the repair budget and can falsely tell the agent
    that sibling failures already passed. Coalescing keeps the first
    Feature as the representative id for legacy result wiring, while the
    detail and related ids carry the full group repair scope.
    """
    if not failures:
        return FailingFeature(feature_id="", verdict="missing", detail="")

    representative = failures[0]
    related_ids: list[str] = []
    severity_findings: list[str] = []
    for failure in failures:
        for feature_id in failure.related_feature_ids or [failure.feature_id]:
            if feature_id and feature_id not in related_ids:
                related_ids.append(feature_id)
        severity_findings.extend(failure.severity_findings)

    if len(failures) == 1:
        return FailingFeature(
            feature_id=representative.feature_id,
            verdict=representative.verdict,
            detail=representative.detail,
            severity_findings=list(severity_findings),
            related_feature_ids=related_ids or [representative.feature_id],
        )

    detail_lines = [
        f"Multiple actionable audit failures share group `{group_id}`. "
        "Repair the shared group behavior in one coherent pass, then Otto "
        "will re-audit the integrated product before spending more repair attempts.",
        "",
        "Failing features in this group:",
    ]
    for failure in failures:
        detail = failure.detail.strip() or "(no detail)"
        detail_lines.append(f"- {failure.feature_id} [{failure.verdict}]: {detail}")

    return FailingFeature(
        feature_id=representative.feature_id,
        verdict=representative.verdict,
        detail="\n".join(detail_lines),
        severity_findings=list(severity_findings),
        related_feature_ids=related_ids or [failure.feature_id for failure in failures],
    )


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
#      invoke `re_audit` (callback) with the attempted feature ids as focus.
#      The callback may return either product-wide verdicts or only the
#      focused subset. Update each RepairAttempt.new_verdict from the
#      re-audit result and carry any unresolved/unreturned failures forward.
#   4. Loop until the audit-passes cap is exhausted or no failing
#      features remain.
#
# The function is generic across fix-agent and re-audit shapes — both
# are passed as callbacks so tests can stub them deterministically.

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

    cap = (
        max_attempts_per_run
        if max_attempts_per_run is not None
        else _repair_cap_default()
    )
    remaining_attempts = max(1, int(cap))
    audit_cap = (
        max_audit_passes
        if max_audit_passes is not None
        else _audit_passes_cap_default()
    )
    effective_audit_cap = (
        max(int(audit_cap), int(audit_passes_so_far) + 1)
        if re_audit is not None
        else int(audit_cap)
    )
    current_verdicts = feature_verdicts
    attempt_numbers_by_feature: dict[str, int] = {}

    while remaining_attempts > 0:
        selected = features_to_repair(
            spec,
            current_verdicts,
            max_attempts_per_run=remaining_attempts,
            ensure_each_group_first_attempt=not result.attempts,
        )
        if not selected:
            if not result.attempts:
                result.halted_reason = "no_failing_features"
            return result

        if not can_run_another_audit_pass(
            audit_passes_run=result.audit_passes_run,
            max_audit_passes=effective_audit_cap,
        ):
            # No budget for re-audit: do not dispatch hidden fixes that
            # cannot be verified in this run. Earlier behavior attempted
            # repairs anyway, which created slow "maybe fixed" loops and
            # no fresh audit evidence for the operator.
            result.halted_reason = "audit_passes_cap_exhausted"
            emit("audit.repair_loop.halted", {"reason": result.halted_reason})
            return result

        # ---- Phase 1: dispatch fix attempts ----
        pass_attempts: list[RepairAttempt] = []
        related_ids_by_attempt: dict[str, list[str]] = {}
        for failing in selected:
            group = group_for_feature(spec, failing.feature_id)
            if group is None:
                # Should be filtered by features_to_repair, but defensive.
                continue
            related_feature_ids = failing.related_feature_ids or [failing.feature_id]
            next_attempt_number = attempt_numbers_by_feature.get(
                failing.feature_id, 0
            ) + 1
            attempt_numbers_by_feature[failing.feature_id] = next_attempt_number
            emit("audit.feature_repair.started", {
                "feature_id": failing.feature_id,
                "group_id": group.id,
                "related_feature_ids": related_feature_ids,
                "attempt_number": next_attempt_number,
                "verdict_before": failing.verdict,
            })
            t0 = _time.monotonic()
            try:
                attempt = await fix_agent(failing, group)
            except Exception as exc:  # noqa: BLE001
                attempt = RepairAttempt(
                    feature_id=failing.feature_id,
                    group_id=group.id,
                    attempt_number=next_attempt_number,
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
            attempt.attempt_number = next_attempt_number
            result.attempts.append(attempt)
            pass_attempts.append(attempt)
            related_ids_by_attempt[attempt.feature_id] = related_feature_ids
            emit("audit.feature_repair.finished", {
                "feature_id": failing.feature_id,
                "group_id": group.id,
                "related_feature_ids": related_feature_ids,
                "attempt_number": next_attempt_number,
                "succeeded": attempt.succeeded,
                "wall_s": attempt.wall_s,
                "cost_usd": attempt.cost_usd,
            })

        remaining_attempts -= len(pass_attempts)
        if not pass_attempts:
            return result

        # ---- Phase 2: re-audit narrowed to this pass's attempted features ----
        if re_audit is None or result.halted_reason == "audit_passes_cap_exhausted":
            return result

        successful_attempts = [attempt for attempt in pass_attempts if attempt.succeeded]
        if not successful_attempts:
            return result

        attempted_ids = _unique_feature_ids(
            feature_id
            for attempt in successful_attempts
            for feature_id in related_ids_by_attempt.get(attempt.feature_id, [attempt.feature_id])
        )
        succeeded_before_reaudit = {
            feature_id: bool(attempt.succeeded)
            for attempt in successful_attempts
            for feature_id in related_ids_by_attempt.get(attempt.feature_id, [attempt.feature_id])
        }
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
        for a in pass_attempts:
            related_ids = related_ids_by_attempt.get(a.feature_id, [a.feature_id])
            related_verdicts = [
                by_id[feature_id] for feature_id in related_ids
                if feature_id in by_id
            ]
            if related_verdicts:
                a.new_verdict = (
                    "passed"
                    if all(verdict == "passed" for verdict in related_verdicts)
                    else next(
                        verdict for verdict in related_verdicts
                        if verdict != "passed"
                    )
                )
                # Once re-audit exists, it is the source of truth for
                # whether the repair actually succeeded.
                a.succeeded = a.new_verdict == "passed"
            else:
                a.succeeded = False

        # Merge re-audit payloads back into the latest known verdict set.
        # A re-audit callback is allowed to return only the attempted
        # feature subset. In that case, still-failing features that were
        # not selected in this pass must remain visible to the loop; losing
        # them can turn a partial product into a false pass when the retry
        # cap is exhausted.
        merged_payload_by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        for v in current_verdicts:
            if not isinstance(v, dict):
                continue
            fid = str(v.get("feature_id") or "")
            if not fid:
                continue
            if fid not in merged_payload_by_id:
                ordered_ids.append(fid)
            merged_payload_by_id[fid] = v
        for v in new_verdicts:
            if not isinstance(v, dict):
                continue
            fid = str(v.get("feature_id") or "")
            if not fid:
                continue
            if fid not in merged_payload_by_id:
                ordered_ids.append(fid)
            merged_payload_by_id[fid] = v

        # Retry only:
        # - features that were not selected in this pass, and still fail, or
        # - attempted features where the repair agent produced a successful
        #   attempt and the re-audit still found a gap.
        #
        # If the fix agent crashed or returned failure, immediately asking
        # the same agent again usually repeats the same runtime failure and
        # burns the whole cap without new evidence.
        attempted_id_set = set(attempted_ids)
        current_verdicts = [
            merged_payload_by_id[fid]
            for fid in ordered_ids
            if (
                fid not in attempted_id_set
                or succeeded_before_reaudit.get(fid, False)
            )
        ]
        if not features_to_repair(spec, current_verdicts, max_attempts_per_run=1):
            return result
        if remaining_attempts <= 0:
            result.halted_reason = "repair_attempts_cap_exhausted"
            emit("audit.repair_loop.halted", {"reason": result.halted_reason})
            return result

    return result


def _unique_feature_ids(feature_ids: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    for feature_id in feature_ids:
        clean = str(feature_id or "").strip()
        if clean and clean not in ordered:
            ordered.append(clean)
    return ordered


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
