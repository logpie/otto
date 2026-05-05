"""A0.4 propagation tests — canonical `feature_audits` wire key flows
through render.py and the audit-output parser.

Originally these tests pinned the temporary back-compat alias
(`capability_verdicts`). After the back-compat removal they assert the
canonical key is the ONLY surface — both for emission (render.py) and
ingestion (`_parse_audit_output`).
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.audit import (
    AuditResult,
    AuditVerdict,
    FeatureAudit,
    _parse_audit_output,
)
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.render import (
    ProofPacket,
    compose_proof_packet,
    render_json,
)
from otto.spec_compile import (
    RepoTestCheck,
    Group,
    Spec,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(passed: bool = True) -> Evidence:
    return Evidence(
        passed=passed,
        started_at="2026-05-04T00:00:00Z",
        duration_s=0.1,
        detail="ok",
        artifacts=[],
        raw={},
    )


def _spec() -> Spec:
    return Spec(
        intent="demo",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="core",
                name="Core",
                dependencies=[],
                owned_paths=["src/**"],
                feature_ids=["build"],
                checks=[RepoTestCheck(command=("true",), timeout_s=30)],
            ),
        ],
        non_goals=[],
        done_means=["does the thing"],
    )


def _build_result(tmp_path: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="core",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="i2p/x/core",
                worktree=tmp_path,
                last_evidence=[_evidence()],
            ),
        ],
    )


def _merge_result() -> MergeQueueResult:
    return MergeQueueResult(
        landed_ids=["core"],
        results=[
            MergeResult(
                group_id="core",
                status=MergeStatus.LANDED,
                landed_commit="abc1234",
                group_recheck_evidence=[_evidence()],
            ),
        ],
    )


def _audit_with_two_features() -> AuditResult:
    return AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="all good",
        feature_audits=[
            FeatureAudit(
                name="signup",
                status="passed",
                detail="ok",
                evidence_refs=["app/signup.tsx:1"],
            ),
            FeatureAudit(
                name="login",
                status="partial",
                detail="flaky",
                evidence_refs=["app/login.tsx:1"],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# render.py — emits ONLY `feature_audits` (legacy key removed)
# ---------------------------------------------------------------------------


def test_proof_packet_dataclass_has_feature_audits_only() -> None:
    """`ProofPacket.feature_audits` is the canonical accessor. The legacy
    `capability_verdicts` field is gone.
    """
    fa = [{"name": "x", "status": "passed", "detail": "", "evidence_refs": []}]
    packet = ProofPacket(
        schema_version=1,
        intent="x",
        project_kind="webapp",
        verdict="passed",
        wall_s=0.0,
        cost_usd=0.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
        feature_audits=list(fa),
    )
    assert packet.feature_audits == fa
    assert not hasattr(packet, "capability_verdicts")


def test_render_json_emits_feature_audits_only(tmp_path: Path) -> None:
    """`render_json` emits only `feature_audits`. The legacy
    `capability_verdicts` JSON key is gone.
    """
    spec = _spec()
    packet = compose_proof_packet(
        spec,
        _build_result(tmp_path),
        _merge_result(),
        _audit_with_two_features(),
        wall_s=1.0,
        cost_usd=0.01,
    )
    payload = json.loads(render_json(packet))
    assert "feature_audits" in payload, "canonical key must be emitted"
    assert "capability_verdicts" not in payload, "legacy key must be dropped"
    names = sorted(entry["name"] for entry in payload["feature_audits"])
    assert names == ["login", "signup"]


# ---------------------------------------------------------------------------
# otto/audit.py — _parse_audit_output reads `feature_audits`
# ---------------------------------------------------------------------------


def test_parse_audit_output_accepts_feature_audits_key() -> None:
    """The canonical wire format. The audit agent's reply uses
    `feature_audits`.
    """
    raw = """```json
{
  "verdict": "passed",
  "narrative": "all good",
  "group_verdicts": [],
  "feature_audits": [
    {"name": "signup", "status": "passed", "detail": "ok", "evidence_refs": ["a:1"]},
    {"name": "login", "status": "partial", "detail": "meh", "evidence_refs": []}
  ],
  "quality_score": 4,
  "quality_findings": ["small thing"]
}
```"""
    out = _parse_audit_output(raw)
    assert out.verdict == AuditVerdict.PASSED
    assert len(out.feature_audits) == 2
    names = [fa.name for fa in out.feature_audits]
    assert names == ["signup", "login"]
    assert out.feature_audits[0].evidence_refs == ["a:1"]


def test_parse_audit_output_ignores_legacy_capability_verdicts_key() -> None:
    """The legacy `capability_verdicts` wire key is no longer parsed.
    Responses that only emit the legacy key produce empty
    `feature_audits` — the back-compat parser branch is gone.
    """
    raw = """```json
{
  "verdict": "partial",
  "narrative": "old-format reply",
  "group_verdicts": [],
  "capability_verdicts": [
    {"name": "legacy", "status": "blocked", "detail": "no good", "evidence_refs": []}
  ],
  "quality_score": 2,
  "quality_findings": []
}
```"""
    out = _parse_audit_output(raw)
    assert out.verdict == AuditVerdict.PARTIAL
    assert out.feature_audits == []


def test_parse_audit_output_empty_feature_audits_yields_empty() -> None:
    """An explicit empty `feature_audits: []` is honored as-is."""
    raw = """```json
{
  "verdict": "passed",
  "narrative": "",
  "group_verdicts": [],
  "feature_audits": [],
  "quality_score": 3,
  "quality_findings": []
}
```"""
    out = _parse_audit_output(raw)
    assert out.feature_audits == []


def test_audit_prompt_advertises_feature_audits_key() -> None:
    """Sanity: the audit prompt asks the agent for `feature_audits` (the
    canonical wire key).
    """
    from otto.audit import AuditAgentInput, _audit_prompt

    spec = _spec()
    agent_input = AuditAgentInput(
        spec=spec,
        integrated_worktree=Path("/tmp/integrated"),
        project_dir=Path("/tmp/project"),
        build_summary={},
        merge_summary={},
        cross_slice_evidence=[],
        walkthrough_artifacts=[],
    )
    prompt = _audit_prompt(agent_input)
    assert "feature_audits" in prompt, "prompt must request canonical key"
    schema_block = prompt.split("Output as a single fenced JSON block")[-1]
    assert "feature_audits" in schema_block
