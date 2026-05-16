"""Tests for `otto.audit_loop.repair_failing_features` (A2.2, research §4).

Layer 2 retry loop. Stubs fix_agent and re_audit so no LLM cost.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

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
        groups=[Group(id=group_id, name=group_id.title())],
        features=[
            Feature(id=fid, name=fid, group_id=group_id) for fid in feature_ids
        ],
    )


def _spec_by_group(feature_to_group: dict[str, str]) -> Spec:
    groups = [
        Group(id=group_id, name=group_id.title())
        for group_id in dict.fromkeys(feature_to_group.values())
    ]
    return Spec(
        intent="x",
        groups=groups,
        features=[
            Feature(id=feature_id, name=feature_id, group_id=group_id)
            for feature_id, group_id in feature_to_group.items()
        ],
    )


def _verdict(
    feature_id: str,
    verdict: str = "partial",
    detail: str = "",
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "feature_id": feature_id,
        "verdict": verdict,
        "detail": detail,
        "evidence_refs": evidence_refs or [],
    }


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


def _make_re_audit_sequence(
    verdicts_by_call: list[dict[str, str]],
):
    """Build a re_audit that returns different verdicts on each pass."""
    calls: list[list[str]] = []

    async def stub(feature_ids: list[str]) -> list[dict[str, Any]]:
        calls.append(list(feature_ids))
        index = min(len(calls) - 1, len(verdicts_by_call) - 1)
        updates = verdicts_by_call[index]
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


def test_visual_only_feature_verdict_triggers_agent_repair() -> None:
    spec = _spec("f1")
    fix_agent, fix_calls = _make_fix_agent()

    asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict(
                    "f1",
                    "partial",
                    detail="Screenshot looks sparse but no workflow failed.",
                    evidence_refs=["home.png"],
                )
                | {"surface": "screenshot", "methodology": "visual-only"},
            ],
            fix_agent=fix_agent,
        )
    )

    assert fix_calls == [("f1", "g")]


def test_live_ui_feature_verdict_triggers_repair() -> None:
    spec = _spec("f1")
    fix_agent, fix_calls = _make_fix_agent()

    asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict(
                    "f1",
                    "partial",
                    detail="Clicking Save leaves the form dirty and no row appears.",
                    evidence_refs=["walkthrough.jsonl#L4"],
                )
                | {"surface": "DOM", "methodology": "live-ui-events"},
            ],
            fix_agent=fix_agent,
            re_audit=None,
            max_audit_passes=2,
        )
    )

    assert fix_calls == [("f1", "g")]


def test_features_without_group_skipped() -> None:
    """A failing orphan feature is a spec/verdict mismatch, not a silent skip."""
    spec = Spec(
        intent="x",
        groups=[Group(id="g", name="G")],
        features=[Feature(id="orphan", name="orphan", group_id="")],
    )
    fix_agent, fix_calls = _make_fix_agent()

    with pytest.raises(ValueError, match="without repair group"):
        asyncio.run(
            repair_failing_features(
                spec=spec,
                feature_verdicts=[_verdict("orphan", "partial")],
                fix_agent=fix_agent,
            )
        )
    assert fix_calls == []


def test_out_of_scope_missing_features_do_not_consume_repair_cap() -> None:
    spec = _spec_by_group({
        "target-1": "g1",
        "unrelated": "g1",
        "target-2": "g2",
    })
    fix_agent, fix_calls = _make_fix_agent()

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("target-1", "blocked", evidence_refs=["walkthrough#1"]),
                _verdict(
                    "unrelated",
                    "missing",
                    detail="No changes were needed as the user intent does not mention this function.",
                ),
                _verdict("target-2", "blocked", evidence_refs=["walkthrough#2"]),
            ],
            fix_agent=fix_agent,
            max_attempts_per_run=3,
        )
    )

    assert [attempt.feature_id for attempt in result.attempts] == [
        "target-1",
        "target-2",
    ]
    assert fix_calls == [("target-1", "g1"), ("target-2", "g2")]


# ---------------------------------------------------------------------------
# Happy path: fix → re-audit → backfilled verdicts
# ---------------------------------------------------------------------------


def test_repair_dispatches_per_feature(test_max=10) -> None:
    spec = _spec_by_group({"f1": "g1", "f2": "g2"})
    fix_agent, fix_calls = _make_fix_agent(succeed=True)
    re_audit, re_audit_calls = _make_re_audit({"f1": "passed", "f2": "passed"})

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("f1", "partial"),
                _verdict("f2", "blocked", evidence_refs=["walkthrough#L2"]),
            ],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )
    assert len(result.attempts) == 2
    # fix_agent saw both features
    assert ("f1", "g1") in fix_calls
    assert ("f2", "g2") in fix_calls
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


def test_repair_coalesces_same_group_failures_before_reaudit() -> None:
    """One group-level repair should cover sibling feature failures together."""
    spec = _spec("f1", "f2")
    fix_agent, fix_calls = _make_fix_agent(succeed=True)
    re_audit, re_audit_calls = _make_re_audit({"f1": "passed", "f2": "passed"})

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("f1", "partial", "form is missing"),
                _verdict(
                    "f2",
                    "blocked",
                    "filters are missing",
                    evidence_refs=["walkthrough#L3"],
                ),
            ],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )

    assert [attempt.feature_id for attempt in result.attempts] == ["f1"]
    assert fix_calls == [("f1", "g")]
    assert re_audit_calls == [["f1", "f2"]]
    assert result.attempts[0].new_verdict == "passed"
    assert result.attempts[0].succeeded is True


def test_re_audit_still_partial_does_not_flip_succeeded() -> None:
    spec = _spec("f1")
    fix_agent, _ = _make_fix_agent(succeed=True)
    re_audit, _ = _make_re_audit({"f1": "partial"})
    events: list[tuple[str, dict[str, Any]]] = []

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[_verdict("f1", "partial")],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=1,
            max_audit_passes=10,
            on_event=lambda kind, payload: events.append((kind, payload)),
        )
    )
    assert len(result.attempts) == 1
    a = result.attempts[0]
    assert a.new_verdict == "partial"
    # Once re-audit exists, it is the source of truth. A fix-agent claim
    # is not enough to mark the repair succeeded when audit still says
    # partial.
    assert a.succeeded is False
    assert result.halted_reason == "no_progress:oracle_state_unchanged"
    assert any(kind == "audit.repair_session.no_progress" for kind, _payload in events)
    assert any(kind == "audit.repair_session.oracle_gate" for kind, _payload in events)


def test_repair_loop_retries_feature_that_reaudit_keeps_partial() -> None:
    spec = _spec_by_group({"f1": "g1", "f2": "g2"})
    fix_agent, fix_calls = _make_fix_agent(succeed=True)
    re_audit, re_audit_calls = _make_re_audit_sequence([
        {"f1": "passed", "f2": "partial"},
        {"f2": "passed"},
    ])

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("f1", "partial"),
                _verdict("f2", "partial"),
            ],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=3,
            max_audit_passes=4,
        )
    )

    assert [feature_id for feature_id, _ in fix_calls] == ["f1", "f2", "f2"]
    assert re_audit_calls == [["f1", "f2"], ["f2"]]
    assert result.audit_passes_run == 3
    assert len(result.attempts) == 3
    f2_attempts = [a for a in result.attempts if a.feature_id == "f2"]
    assert [a.attempt_number for a in f2_attempts] == [1, 2]
    assert f2_attempts[0].succeeded is False
    assert f2_attempts[0].new_verdict == "partial"
    assert f2_attempts[1].succeeded is True
    assert f2_attempts[1].new_verdict == "passed"
    assert result.halted_reason == ""


def test_repair_loop_preserves_unattempted_failures_when_reaudit_is_scoped() -> None:
    spec = _spec_by_group({"f1": "g1", "f2": "g2"})
    fix_agent, fix_calls = _make_fix_agent(succeed=True)
    re_audit, re_audit_calls = _make_re_audit_sequence([
        {"f1": "partial", "f2": "partial"},
        {"f1": "passed"},
    ])

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict("f1", "partial"),
                _verdict("f2", "partial"),
            ],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=3,
            max_audit_passes=4,
        )
    )

    assert [feature_id for feature_id, _ in fix_calls] == ["f1", "f2"]
    assert re_audit_calls == [["f1", "f2"]]
    assert result.audit_passes_run == 2
    assert result.halted_reason == "no_progress:oracle_state_unchanged"
    by_feature = {a.feature_id: a for a in result.attempts}
    assert by_feature["f2"].new_verdict == "partial"
    assert by_feature["f2"].succeeded is False


# ---------------------------------------------------------------------------
# Cap behavior
# ---------------------------------------------------------------------------


def test_audit_passes_cap_reserves_one_fix_and_re_audit() -> None:
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
    assert [attempt.feature_id for attempt in result.attempts] == ["f1"]
    assert fix_calls == [("f1", "g")]
    assert re_audit_calls == [["f1"]]
    assert result.audit_passes_run == 2


def test_max_attempts_per_run_does_not_truncate_first_failing_group_attempts() -> None:
    """Three failing groups and cap=2 still get one first fix attempt each."""
    spec = _spec_by_group({"f1": "g1", "f2": "g2", "f3": "g3"})
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
    assert len(result.attempts) == 3
    assert len(fix_calls) == 3
    # Order is the input verdict order
    assert fix_calls[0][0] == "f1"
    assert fix_calls[1][0] == "f2"
    assert fix_calls[2][0] == "f3"


def test_no_evidence_blocked_verdicts_do_not_crowd_out_real_repairs() -> None:
    spec = _spec_by_group({
        "not_seen": "g1",
        "crashing_api": "g2",
        "wrong_output": "g3",
    })
    fix_agent, fix_calls = _make_fix_agent()
    re_audit, re_audit_calls = _make_re_audit({
        "not_seen": "passed",
        "crashing_api": "passed",
        "wrong_output": "passed",
    })

    result = asyncio.run(
        repair_failing_features(
            spec=spec,
            feature_verdicts=[
                _verdict(
                    "not_seen",
                    "blocked",
                    "No direct test evidence collected; not evaluated in this audit.",
                ),
                _verdict(
                    "crashing_api",
                    "blocked",
                    "raises ValueError for the required input",
                    evidence_refs=["walkthrough.jsonl#L4"],
                ),
                _verdict(
                    "wrong_output",
                    "partial",
                    "returns the old value instead of the required value",
                ),
            ],
            fix_agent=fix_agent,
            re_audit=re_audit,
            max_attempts_per_run=10,
            max_audit_passes=10,
        )
    )

    assert [feature_id for feature_id, _ in fix_calls] == [
        "crashing_api",
        "wrong_output",
    ]
    assert re_audit_calls == [["crashing_api", "wrong_output"]]
    assert [a.feature_id for a in result.attempts] == [
        "crashing_api",
        "wrong_output",
    ]


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


def test_failed_repair_attempt_does_not_trigger_re_audit() -> None:
    spec = _spec("f1")
    fix_agent, _ = _make_fix_agent(succeed=False)
    re_audit, re_audit_calls = _make_re_audit({"f1": "passed"})

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
    assert result.attempts[0].succeeded is False
    assert re_audit_calls == []
    assert result.audit_passes_run == 1


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
