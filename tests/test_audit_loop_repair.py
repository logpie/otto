"""Tests for `otto.audit_loop.repair_failing_features` (A2.2, research §4).

Layer 2 retry loop. Stubs fix_agent and re_audit so no LLM cost.
"""

from __future__ import annotations

import asyncio
from typing import Any

from otto.audit_loop import (
    FailingFeature,
    RepairAttempt,
    RepairResult,
    repair_failing_features,
)
from otto.spec_compile import Feature, Group, Spec


def _spec(*feature_ids: str, group_id: str = "g") -> Spec:
    return Spec(
        intent="x",
        groups=[Group(id=group_id, title=group_id.title())],
        features=[
            Feature(id=fid, name=fid, group_id=group_id) for fid in feature_ids
        ],
    )


def _verdict(feature_id: str, verdict: str = "partial",
             detail: str = "") -> dict[str, Any]:
    return {"feature_id": feature_id, "verdict": verdict, "detail": detail}


def _make_fix_agent(*, succeed: bool = True):
    """Build a fix_agent that records calls and returns a fixed RepairAttempt."""
    calls: list[tuple[str, str]] = []

    async def stub(failing: FailingFeature, group: Group) -> RepairAttempt:
        calls.append((failing.feature_id, group.id))
        return RepairAttempt(
            feature_id=failing.feature_id,
            group_id=group.id,
            attempt_number=1,
            succeeded=succeed,
            new_verdict=None,
            detail="stub fix",
            cost_usd=0.01,
            wall_s=0.0,
        )

    return stub, calls


def _make_re_audit(updates: dict[str, str]):
    """Build a re_audit that returns updated verdicts for the given ids."""
    calls: list[list[str]] = []

    async def stub(feature_ids: list[str]) -> list[dict[str, Any]]:
        calls.append(list(feature_ids))
        return [
            _verdict(fid, updates.get(fid, "partial"))
            for fid in feature_ids
        ]

    return stub, calls


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_no_failing_features_returns_immediately() -> None:
    spec = _spec("f1")
    fix_agent, fix_calls = _make_fix_agent()

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "passed")],
            fix_agent=fix_agent,
        )
    )
    assert isinstance(result, RepairResult)
    assert result.attempts == []
    assert result.halted_reason == "no_failing_features"
    assert fix_calls == []


def test_features_without_group_skipped() -> None:
    """An orphan feature (group_id="") can't be repaired — features_to_repair
    excludes it, so repair_failing_features sees an empty selection."""
    spec = Spec(
        intent="x",
        groups=[Group(id="g", title="G")],
        features=[Feature(id="orphan", name="orphan", group_id="")],
    )
    fix_agent, fix_calls = _make_fix_agent()
    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("orphan", "partial")],
            fix_agent=fix_agent,
        )
    )
    assert result.attempts == []
    assert result.halted_reason == "no_failing_features"
    assert fix_calls == []


# ---------------------------------------------------------------------------
# Happy path: fix → re-audit → backfilled verdicts
# ---------------------------------------------------------------------------


def test_repair_dispatches_per_feature(test_max=10) -> None:
    spec = _spec("f1", "f2")
    fix_agent, fix_calls = _make_fix_agent(succeed=True)
    re_audit, re_audit_calls = _make_re_audit({"f1": "passed", "f2": "passed"})

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("f1", "partial"),
                _verdict("f2", "blocked"),
            ],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )
    assert len(result.attempts) == 2
    # fix_agent saw both features
    assert ("f1", "g") in fix_calls
    assert ("f2", "g") in fix_calls
    # re_audit was called with both feature ids
    assert re_audit_calls == [["f1", "f2"]]
    # Verdicts backfilled from re_audit
    by_id = {a.feature_id: a for a in result.attempts}
    assert by_id["f1"].new_verdict == "passed"
    assert by_id["f2"].new_verdict == "passed"
    # succeeded flipped True because re-audit said passed
    assert by_id["f1"].succeeded is True
    assert by_id["f2"].succeeded is True
    assert result.audit_passes_run == 2  # 1 original + 1 re-audit


def test_re_audit_still_partial_does_not_flip_succeeded() -> None:
    spec = _spec("f1")
    fix_agent, _ = _make_fix_agent(succeed=True)
    re_audit, _ = _make_re_audit({"f1": "partial"})

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )
    assert len(result.attempts) == 1
    a = result.attempts[0]
    assert a.new_verdict == "partial"
    # fix_agent reported succeeded=True; re-audit said partial — we
    # honor re-audit (the truthy verdict) by NOT flipping succeeded
    # to True (it stays at whatever fix_agent reported, in this case
    # True). The semantics: succeeded == "fix_agent's claim", new_verdict
    # == "ground truth from re-audit". Caller decides what to do with
    # the discrepancy.
    assert a.succeeded is True


# ---------------------------------------------------------------------------
# Cap behavior
# ---------------------------------------------------------------------------


def test_audit_passes_cap_skips_re_audit() -> None:
    spec = _spec("f1")
    fix_agent, fix_calls = _make_fix_agent(succeed=True)
    re_audit, re_audit_calls = _make_re_audit({"f1": "passed"})

    # max_audit_passes=1 means the original audit was the only pass we get
    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_audit_passes=1,
            audit_passes_so_far=1,
        )
    )
    # Fix attempted but re-audit skipped due to cap
    assert len(result.attempts) == 1
    assert fix_calls == [("f1", "g")]
    assert re_audit_calls == []  # never called
    assert result.attempts[0].new_verdict is None
    assert result.halted_reason == "audit_passes_cap_exhausted"


def test_max_attempts_per_run_caps_selection() -> None:
    """Three failing features but cap=2 → only 2 fix attempts."""
    spec = _spec("f1", "f2", "f3")
    fix_agent, fix_calls = _make_fix_agent()

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("f1", "partial"),
                _verdict("f2", "partial"),
                _verdict("f3", "partial"),
            ],
            fix_agent=fix_agent,
            max_attempts_per_run=2,
            max_audit_passes=10,
        )
    )
    assert len(result.attempts) == 2
    assert len(fix_calls) == 2
    # Order is the input verdict order
    assert fix_calls[0][0] == "f1"
    assert fix_calls[1][0] == "f2"


# ---------------------------------------------------------------------------
# Error-handling
# ---------------------------------------------------------------------------


def test_fix_agent_exception_recorded_as_failed_attempt() -> None:
    spec = _spec("f1")

    async def exploding_fix(_failing, _group):
        raise RuntimeError("agent timed out")

    re_audit, _ = _make_re_audit({"f1": "blocked"})

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=exploding_fix,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )
    assert len(result.attempts) == 1
    a = result.attempts[0]
    assert a.succeeded is False
    assert "RuntimeError" in a.detail
    assert "agent timed out" in a.detail


def test_re_audit_exception_halts_loop_with_reason() -> None:
    spec = _spec("f1")
    fix_agent, _ = _make_fix_agent()

    async def exploding_re_audit(_ids):
        raise ValueError("audit crashed")

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=fix_agent,
            re_audit=exploding_re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )
    assert len(result.attempts) == 1
    assert "re_audit_raised" in result.halted_reason
    assert "ValueError" in result.halted_reason


# ---------------------------------------------------------------------------
# Event observability
# ---------------------------------------------------------------------------


def test_on_event_invoked_for_lifecycle_events() -> None:
    spec = _spec("f1")
    fix_agent, _ = _make_fix_agent(succeed=True)
    re_audit, _ = _make_re_audit({"f1": "passed"})

    events: list[tuple[str, dict[str, Any]]] = []
    asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
            on_event=lambda kind, payload: events.append((kind, payload)),
        )
    )
    kinds = [k for k, _ in events]
    assert "audit.feature_repair.started" in kinds
    assert "audit.feature_repair.finished" in kinds
    assert "audit.re_audit.started" in kinds
    assert "audit.re_audit.finished" in kinds


def test_on_event_exception_does_not_crash_loop() -> None:
    """Observability callback that raises must not break the loop."""
    spec = _spec("f1")
    fix_agent, _ = _make_fix_agent(succeed=True)

    def bad_callback(_kind, _payload):
        raise RuntimeError("observer broken")

    # Should complete without raising
    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=fix_agent,
            on_event=bad_callback,
        )
    )
    assert len(result.attempts) == 1
