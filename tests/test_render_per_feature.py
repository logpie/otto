"""A3 — per-Feature proof-packet rendering tests.

Coverage (research §3 atomic-units + §7 cross-link rule):
- render_html emits a `<h2>Features</h2>` section with one block per Feature
- render_html escapes Feature names/descriptions
- render_json's `feature_proofs` array carries serialised blocks for every Feature
- Multi-Feature walkthrough cross-link: an entry tagged with multiple
  feature_ids appears in EACH Feature's proof block (not deduplicated)
- Empty `spec.features` → render still works, no Features header is emitted
- Per-Feature findings are filtered by feature_id and rendered with severity
- Legacy packets (no spec.features) keep rendering unchanged — slice section
  remains intact
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.audit import AuditResult, AuditVerdict, FeatureAudit
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.render import (
    PROOF_PACKET_JSON,
    ProofPacket,
    compose_proof_packet,
    render_html,
    render_json,
    write_proof_packet,
)
from otto.spec_compile import (
    Feature,
    FeatureProofBlock,
    Finding,
    RepoTestCheck,
    Group,
    Spec,
    StructureDecisions,
    WalkthroughEntry,
    build_feature_proof_blocks,
    feature_proof_blocks_to_dicts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(passed: bool, detail: str = "ok") -> Evidence:
    return Evidence(
        passed=passed,
        started_at="2026-05-04T00:00:00Z",
        duration_s=0.1,
        detail=detail,
        artifacts=[],
        raw={},
    )


def _two_feature_spec() -> Spec:
    """Spec with one Group and two Features tied to that Group."""
    return Spec(
        intent="A demo todo app",
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
        features=[
            Feature(
                id="signup",
                name="User signup",
                description="A new visitor can register an account.",
                group_id="core",
            ),
            Feature(
                id="login",
                name="User login",
                description="A registered user can log back in.",
                group_id="core",
            ),
        ],
        non_goals=[],
        done_means=["signup works", "login works"],
    )


def _passing_build(tmp_path: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="core",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="i2p/x/core",
                worktree=tmp_path,
                last_evidence=[_evidence(True)],
            ),
        ],
    )


def _landed_merge() -> MergeQueueResult:
    return MergeQueueResult(
        landed_ids=["core"],
        results=[
            MergeResult(
                group_id="core",
                status=MergeStatus.LANDED,
                landed_commit="abc1234",
                group_recheck_evidence=[_evidence(True)],
            ),
        ],
    )


def _audit_with_feature_audits() -> AuditResult:
    return AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="All good.",
        feature_audits=[
            FeatureAudit(name="User signup", status="passed", detail="signup ok"),
            FeatureAudit(name="User login", status="partial", detail="login flaky"),
        ],
    )


# ---------------------------------------------------------------------------
# render_html — per-Feature section
# ---------------------------------------------------------------------------


def test_render_html_emits_features_section_with_one_block_per_feature(
    tmp_path: Path,
) -> None:
    spec = _two_feature_spec()
    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        _audit_with_feature_audits(),
        wall_s=10.0,
        cost_usd=0.1,
    )
    html = render_html(packet, session_dir=tmp_path)

    assert "<h2>Features</h2>" in html
    # One <section class="feature-proof"> per Feature, anchored by id
    assert 'id="feature-signup"' in html
    assert 'id="feature-login"' in html
    # Feature names rendered
    assert "User signup" in html
    assert "User login" in html
    # Verdicts mapped from FeatureAudit.status
    assert "passed" in html
    assert "partial" in html


def test_render_html_features_section_appears_before_groups(tmp_path: Path) -> None:
    spec = _two_feature_spec()
    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        _audit_with_feature_audits(),
        wall_s=1.0,
        cost_usd=0.0,
    )
    html = render_html(packet, session_dir=tmp_path)
    feature_pos = html.find("<h2>Features</h2>")
    group_pos = html.find("<h2>Groups</h2>")
    assert feature_pos > 0, "Features section missing"
    assert group_pos > 0, "Groups section missing"
    assert feature_pos < group_pos, (
        "Features section must precede Groups (research §3 — Features lead)"
    )


def test_render_html_no_features_section_for_legacy_packet(tmp_path: Path) -> None:
    """Legacy spec without `features=...` populated: no Features header."""
    spec = Spec(
        intent="legacy run",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
        ],
    )
    build = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="s1",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="b",
                worktree=tmp_path,
            ),
        ],
    )
    merge = MergeQueueResult(
        landed_ids=["s1"],
        results=[
            MergeResult(group_id="s1", status=MergeStatus.LANDED, landed_commit="aaa"),
        ],
    )
    audit = AuditResult(verdict=AuditVerdict.PASSED, narrative="ok")
    packet = compose_proof_packet(spec, build, merge, audit, wall_s=1.0, cost_usd=0.0)
    html = render_html(packet, session_dir=tmp_path)
    assert "<h2>Groups</h2>" in html
    # No Features section since spec.features is empty
    assert "<h2>Features</h2>" not in html


# ---------------------------------------------------------------------------
# render_json — feature_proofs array
# ---------------------------------------------------------------------------


def test_render_json_includes_features_array(tmp_path: Path) -> None:
    spec = _two_feature_spec()
    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        _audit_with_feature_audits(),
        wall_s=1.0,
        cost_usd=0.0,
    )
    text = render_json(packet)
    data = json.loads(text)
    assert "features" in data
    feature_ids = [f["feature_id"] for f in data["features"]]
    assert feature_ids == ["signup", "login"]
    # Verdicts populated from FeatureAudit.status
    by_id = {f["feature_id"]: f for f in data["features"]}
    assert by_id["signup"]["verdict"] == "passed"
    assert by_id["login"]["verdict"] == "partial"
    # Back-compat: legacy slices array still present
    assert "groups" in data
    assert len(data["groups"]) == 1


def test_feature_audit_feature_id_wins_over_display_name(tmp_path: Path) -> None:
    """Per-Feature proof joins on feature_id, not fragile display names."""
    spec = _two_feature_spec()
    audit = AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        feature_audits=[
            FeatureAudit(
                feature_id="signup",
                name="Renamed by audit agent",
                status="passed",
                detail="matched by id",
            )
        ],
    )
    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        audit,
        wall_s=1.0,
        cost_usd=0.0,
    )
    by_id = {f["feature_id"]: f for f in packet.features}
    assert by_id["signup"]["verdict"] == "passed"
    assert by_id["signup"]["detail"] == "matched by id"


# ---------------------------------------------------------------------------
# Multi-Feature walkthrough cross-link
# ---------------------------------------------------------------------------


def test_multi_feature_walkthrough_entry_appears_in_each_feature(
    tmp_path: Path,
) -> None:
    """Research §7: multi-Feature entries cross-link, do NOT dedup."""
    spec = _two_feature_spec()
    # Build per-Feature proof blocks directly with a multi-tagged entry,
    # then inject into a ProofPacket so render layer ingests it.
    multi_entry = WalkthroughEntry(
        t="0:01",
        feature_ids=["signup", "login"],
        action_kind="browser_navigation",
        narrative="user signs up then logs in",
        extras={"url": "/signup", "screenshot": "s.png"},
    )
    blocks = build_feature_proof_blocks(
        spec,
        walkthrough_entries=[multi_entry],
        feature_verdicts=[
            {"feature_id": "signup", "verdict": "passed", "detail": ""},
            {"feature_id": "login", "verdict": "passed", "detail": ""},
        ],
    )
    feature_dicts = feature_proof_blocks_to_dicts(blocks)
    # Each block must include the entry (no dedup).
    by_id = {d["feature_id"]: d for d in feature_dicts}
    assert len(by_id["signup"]["walkthrough_entries"]) == 1
    assert len(by_id["login"]["walkthrough_entries"]) == 1
    # Cross-link metadata: each names the other under shared_with.
    assert "login" in by_id["signup"]["shared_with"]
    assert "signup" in by_id["login"]["shared_with"]

    # Packet rendered by render_html surfaces the cross-link.
    packet = ProofPacket(
        schema_version=1,
        intent=spec.intent,
        project_kind=spec.project_kind,
        verdict="passed",
        wall_s=1.0,
        cost_usd=0.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
        features=feature_dicts,
    )
    html = render_html(packet, session_dir=tmp_path)
    # Cross-link anchor appears in each section
    assert html.count("user signs up then logs in") == 2
    assert 'href="#feature-login"' in html
    assert 'href="#feature-signup"' in html


# ---------------------------------------------------------------------------
# Per-Feature findings filter
# ---------------------------------------------------------------------------


def test_per_feature_findings_filtered_by_feature_id(tmp_path: Path) -> None:
    spec = _two_feature_spec()
    findings = [
        Finding(severity="critical", text="signup form crashes", feature_id="signup"),
        Finding(severity="polish", text="login button color", feature_id="login"),
        Finding(severity="important", text="orphan finding", feature_id=""),
    ]
    blocks = build_feature_proof_blocks(
        spec,
        walkthrough_entries=[],
        feature_verdicts=[
            {"feature_id": "signup", "verdict": "blocked"},
            {"feature_id": "login", "verdict": "passed"},
        ],
        findings=findings,
    )
    feature_dicts = feature_proof_blocks_to_dicts(blocks)
    by_id = {d["feature_id"]: d for d in feature_dicts}
    # Only the matching feature_id finding lands in each block.
    signup_finding_texts = [f["text"] for f in by_id["signup"]["findings"]]
    login_finding_texts = [f["text"] for f in by_id["login"]["findings"]]
    assert signup_finding_texts == ["signup form crashes"]
    assert login_finding_texts == ["login button color"]
    # Orphan finding (feature_id="") is NOT attached to any feature block.
    assert "orphan finding" not in signup_finding_texts
    assert "orphan finding" not in login_finding_texts

    packet = ProofPacket(
        schema_version=1,
        intent=spec.intent,
        project_kind=spec.project_kind,
        verdict="partial",
        wall_s=1.0,
        cost_usd=0.0,
        structure={},
        non_goals=[],
        done_means=[],
        groups=[],
        audit_narrative="",
        walkthrough_artifacts=[],
        blocked_group_ids=[],
        landed_group_ids=[],
        features=feature_dicts,
    )
    html = render_html(packet, session_dir=tmp_path)
    # Severity classes rendered
    assert "finding critical" in html
    assert "signup form crashes" in html
    assert "finding polish" in html
    assert "login button color" in html


# ---------------------------------------------------------------------------
# Escape + write round-trip
# ---------------------------------------------------------------------------


def test_render_html_escapes_feature_name_and_description(tmp_path: Path) -> None:
    spec = Spec(
        intent="x",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(id="g", name="g", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
        ],
        features=[
            Feature(
                id="evil",
                name="<script>alert(1)</script>",
                description="<img onerror=x>",
                group_id="g",
            ),
        ],
    )
    build = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="g",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="b",
                worktree=tmp_path,
            ),
        ],
    )
    merge = MergeQueueResult(
        landed_ids=["g"],
        results=[MergeResult(group_id="g", status=MergeStatus.LANDED, landed_commit="aaa")],
    )
    audit = AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        feature_audits=[
            FeatureAudit(name="<script>alert(1)</script>", status="passed", detail=""),
        ],
    )
    packet = compose_proof_packet(spec, build, merge, audit, wall_s=1.0, cost_usd=0.0)
    html = render_html(packet, session_dir=tmp_path)
    # Verify Feature name escaped (no raw <script>)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html
    assert "<img onerror" not in html
    assert "&lt;img onerror" in html


def test_write_proof_packet_round_trips_feature_proofs(tmp_path: Path) -> None:
    spec = _two_feature_spec()
    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        _audit_with_feature_audits(),
        wall_s=1.0,
        cost_usd=0.0,
    )
    write_proof_packet(packet, tmp_path)
    parsed = json.loads((tmp_path / PROOF_PACKET_JSON).read_text(encoding="utf-8"))
    feature_ids = [f["feature_id"] for f in parsed["features"]]
    assert feature_ids == ["signup", "login"]


# Pin imports the dispatch flow needs but doesn't reference at module top level.
_ = (FeatureProofBlock,)
