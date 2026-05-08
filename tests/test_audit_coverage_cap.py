"""Tests for the walkthrough Feature-tag coverage verdict cap.

A2.1 follow-up (deferred from tick 58, research §A2 honesty contract):
when `walkthrough.jsonl` Feature-tag coverage falls below the
`CoverageReport.meets_threshold()` floor (0.90), the audit verdict
MUST be capped at PARTIAL regardless of the LLM judge's verdict —
the audit cannot legitimately certify Features it didn't observe.

Cases covered (mirror the tick deferral spec):
  1. coverage 100% + LLM PASSED                     → PASSED (no cap)
  2. coverage 80%  + LLM PASSED                     → PARTIAL + cap reason
  3. coverage 0%   (vacuous: zero entries)          → PASSED (vacuous-passes)
  4. no walkthrough.jsonl emitted (coverage=None)   → PASSED (cap doesn't apply)
  5. coverage 80%  + LLM BLOCKED                    → BLOCKED (cap doesn't downgrade)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from otto.audit import (
    AuditAgentInput,
    AuditAgentOutput,
    AuditVerdict,
    FeatureAudit,
    WalkthroughResult,
    run_audit,
)
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.spec_compile import (
    Feature,
    Group,
    RepoTestCheck,
    Spec,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_with_features(*ids: str) -> Spec:
    return Spec(
        intent="x",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="g",
                name="G",
                dependencies=[],
                owned_paths=["src/**"],
                feature_ids=["build"],
                checks=[RepoTestCheck(command=("true",), timeout_s=30)],
            ),
        ],
        features=[
            Feature(
                id=fid,
                name=fid.replace("-", " ").title(),
                description=f"feature {fid}",
                group_id="g",
            )
            for fid in ids
        ],
        non_goals=[],
        done_means=[f"{fid} works" for fid in ids],
    )


def _build_result(tmp_path: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="g",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="i2p/x/g",
                worktree=tmp_path,
            ),
        ],
    )


def _merge_result() -> MergeQueueResult:
    return MergeQueueResult(
        landed_ids=["g"],
        results=[
            MergeResult(group_id="g", status=MergeStatus.LANDED, landed_commit="abc1234"),
        ],
    )


def _agent_returning(verdict: AuditVerdict):
    """Stub audit_agent that returns the given verdict, no findings."""

    async def _agent(_input: AuditAgentInput) -> AuditAgentOutput:
        status = "passed" if verdict == AuditVerdict.PASSED else verdict.value
        return AuditAgentOutput(
            verdict=verdict,
            narrative=f"llm-judge says {verdict.value}",
            group_verdicts=[],
            feature_audits=[
                FeatureAudit(
                    feature_id=feature.id,
                    name=feature.name,
                    status=status,
                    detail=f"{feature.id} {status}",
                )
                for feature in _input.spec.features
            ],
            cost_usd=0.0,
        )

    return _agent


def _agent_writing_walkthrough(verdict: AuditVerdict, entries: list[dict]):
    """Stub audit_agent that writes the JSONL during judging."""

    async def _agent(input_: AuditAgentInput) -> AuditAgentOutput:
        assert input_.walkthrough_jsonl_path is not None
        input_.walkthrough_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        input_.walkthrough_jsonl_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + ("\n" if entries else "")
        )
        status = "passed" if verdict == AuditVerdict.PASSED else verdict.value
        return AuditAgentOutput(
            verdict=verdict,
            narrative=f"llm-judge says {verdict.value}",
            group_verdicts=[],
            feature_audits=[
                FeatureAudit(
                    feature_id=feature.id,
                    name=feature.name,
                    status=status,
                    detail=f"{feature.id} {status}",
                )
                for feature in input_.spec.features
            ],
            cost_usd=0.0,
        )

    return _agent


def _walkthrough_writing(entries: list[dict] | None):
    """Stub walkthrough that writes `entries` to walkthrough.jsonl in walk_log_dir.

    `entries=None` simulates a no-op walkthrough that emits NO `walkthrough.jsonl`
    file (so `_validate_walkthrough_jsonl` returns coverage=None).
    """

    def _walk(_project_dir: Path, walk_log_dir: Path, _timeout_s: int) -> WalkthroughResult:
        walk_log_dir.mkdir(parents=True, exist_ok=True)
        if entries is not None:
            (walk_log_dir / "walkthrough.jsonl").write_text(
                "\n".join(json.dumps(e) for e in entries) + ("\n" if entries else "")
            )
        return WalkthroughResult(succeeded=True, detail="stub walkthrough", artifacts=[])

    return _walk


def _run(spec: Spec, tmp_path: Path, *, agent_verdict: AuditVerdict, walk_entries):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    return asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(tmp_path),
            merge_result=_merge_result(),
            audit_agent=_agent_returning(agent_verdict),
            walkthrough=_walkthrough_writing(walk_entries),
        )
    )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_full_coverage_passed_stays_passed(tmp_path: Path) -> None:
    """100% Feature-tag coverage + LLM PASSED → final verdict PASSED.

    The cap only fires when meets_threshold is False, so the LLM verdict
    flows through unchanged."""
    spec = _spec_with_features("login")
    entries = [
        {"t": "0:01", "action_kind": "browser_navigation", "feature_ids": ["login"]},
        {"t": "0:02", "action_kind": "api_request", "feature_ids": ["login"]},
    ]
    result = _run(spec, tmp_path, agent_verdict=AuditVerdict.PASSED, walk_entries=entries)
    assert result.verdict == AuditVerdict.PASSED
    assert result.walkthrough_coverage is not None
    assert result.walkthrough_coverage["meets_threshold"] is True
    assert result.verdict_cap_reasons == []
    assert "coverage cap" not in result.narrative


def test_below_threshold_caps_passed_to_partial(tmp_path: Path) -> None:
    """80% coverage (below 90%) + LLM PASSED → final PARTIAL with cap reason."""
    spec = _spec_with_features("login")
    # 8 non-exploration entries; 6 tagged → 6/8 = 75% < 90%
    entries: list[dict] = [
        {"t": f"0:{i:02d}", "action_kind": "browser_navigation", "feature_ids": ["login"]}
        for i in range(6)
    ]
    entries.extend(
        [
            {"t": "0:90", "action_kind": "browser_navigation", "feature_ids": []},
            {"t": "0:91", "action_kind": "browser_navigation", "feature_ids": []},
        ]
    )
    result = _run(spec, tmp_path, agent_verdict=AuditVerdict.PASSED, walk_entries=entries)

    assert result.verdict == AuditVerdict.PARTIAL
    assert result.walkthrough_coverage is not None
    assert result.walkthrough_coverage["meets_threshold"] is False
    assert len(result.verdict_cap_reasons) == 1
    cap = result.verdict_cap_reasons[0]
    assert "walkthrough Feature-tag coverage" in cap
    assert "below" in cap
    assert "capped at partial" in cap
    # Narrative carries the audit-trail too — operators reading the
    # rendered packet must see WHY the verdict differs from the LLM's.
    assert "[walkthrough coverage cap]" in result.narrative
    assert "75.0%" in result.narrative


def test_zero_entries_vacuous_pass(tmp_path: Path) -> None:
    """Empty walkthrough.jsonl → coverage_ratio=1.0 (vacuous) → PASSED.

    Per `CoverageReport.meets_threshold()`: zero non-exploration entries
    means there's nothing to fail. The cap doesn't fire."""
    spec = _spec_with_features("login")
    result = _run(spec, tmp_path, agent_verdict=AuditVerdict.PASSED, walk_entries=[])
    assert result.verdict == AuditVerdict.PASSED
    assert result.walkthrough_coverage is not None
    assert result.walkthrough_coverage["total_entries"] == 0
    assert result.walkthrough_coverage["meets_threshold"] is True
    assert result.verdict_cap_reasons == []


def test_no_jsonl_file_means_no_cap(tmp_path: Path) -> None:
    """Walkthrough emits no `walkthrough.jsonl` (e.g. no-op walkthrough) →
    coverage is None → cap doesn't apply → LLM verdict honored."""
    spec = _spec_with_features("login")
    result = _run(spec, tmp_path, agent_verdict=AuditVerdict.PASSED, walk_entries=None)
    assert result.verdict == AuditVerdict.PASSED
    assert result.walkthrough_coverage is None
    assert result.verdict_cap_reasons == []


def test_walkthrough_written_by_audit_agent_is_validated(tmp_path: Path) -> None:
    """run_audit validates walkthrough.jsonl again after the judge.

    Without the post-agent read, an agent-created walkthrough trace would
    be invisible and coverage would stay None.
    """
    spec = _spec_with_features("login")
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(tmp_path),
            merge_result=_merge_result(),
            audit_agent=_agent_writing_walkthrough(
                AuditVerdict.PASSED,
                [
                    {
                        "t": "0:01",
                        "action_kind": "browser_navigation",
                        "feature_ids": ["login"],
                    }
                ],
            ),
            walkthrough=_walkthrough_writing(None),
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert result.walkthrough_coverage is not None
    assert result.walkthrough_coverage["tagged_entries"] == 1
    assert result.walkthrough_entries


def test_blocked_verdict_not_downgraded_by_cap(tmp_path: Path) -> None:
    """80% coverage + LLM BLOCKED → final BLOCKED.

    `_strictest` ensures the cap can never relax a stricter verdict —
    a low-coverage walkthrough means we couldn't observe enough to
    certify, but it must NOT downgrade BLOCKED → PARTIAL. The cap
    reason is still recorded for the audit trail."""
    spec = _spec_with_features("login")
    entries: list[dict] = [
        {"t": f"0:{i:02d}", "action_kind": "browser_navigation", "feature_ids": ["login"]}
        for i in range(6)
    ]
    entries.extend(
        [
            {"t": "0:90", "action_kind": "browser_navigation", "feature_ids": []},
            {"t": "0:91", "action_kind": "browser_navigation", "feature_ids": []},
        ]
    )
    result = _run(spec, tmp_path, agent_verdict=AuditVerdict.BLOCKED, walk_entries=entries)

    assert result.verdict == AuditVerdict.BLOCKED
    assert result.walkthrough_coverage is not None
    assert result.walkthrough_coverage["meets_threshold"] is False
    # Cap reason still recorded — audit-trail invariant: every active
    # cap surfaces, even when it didn't change the final verdict.
    assert len(result.verdict_cap_reasons) == 1
    assert "walkthrough Feature-tag coverage" in result.verdict_cap_reasons[0]
