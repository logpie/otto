"""Tests for otto/render.py — proof-packet HTML + JSON.

Coverage:
- compose_proof_packet: maps spec/build/merge/audit into ProofPacket
- render_json: round-trips through JSON, schema_version present
- render_html: contains required sections, escapes user input,
  thumbnails for image artifacts, video tag for video artifacts,
  blocked slices rendered with narrative not omitted
- write_proof_packet: writes both files, returns paths
- render_run: end-to-end with passing + blocked + landed slices
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.audit import AuditResult, AuditVerdict, SliceVerdict
from otto.build import BuildResult, SliceResult, SliceStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.render import (
    PROOF_PACKET_HTML,
    PROOF_PACKET_JSON,
    PROOF_PACKET_SCHEMA_VERSION,
    compose_proof_packet,
    render_html,
    render_json,
    render_run,
    write_proof_packet,
)
from otto.spec_compile import (
    BrowserJourney,
    RepoTestCheck,
    Slice,
    Spec,
    StateInvariant,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(passed: bool, detail: str, artifacts=None) -> Evidence:
    return Evidence(
        passed=passed,
        started_at="2026-05-03T00:00:00Z",
        duration_s=0.5,
        detail=detail,
        artifacts=[Path(a) for a in (artifacts or [])],
        raw={},
    )


def _two_slice_spec(tmp_path: Path) -> Spec:
    return Spec(
        intent="A demo todo app",
        project_kind="webapp",
        structure=StructureDecisions(
            payload={
                "routes": [{"path": "/", "component": "Home", "key_text": "Hi"}],
                "components": [{"name": "Home", "key_text": "Hi"}],
            }
        ),
        slices=[
            Slice(
                id="shell",
                title="App shell",
                deps=[],
                owned_paths=["src/App.*"],
                tasks=["scaffold"],
                checks=[RepoTestCheck(command=("npm", "run", "build"), timeout_s=60)],
            ),
            Slice(
                id="counter",
                title="Counter widget",
                deps=["shell"],
                owned_paths=["src/components/Counter.*"],
                tasks=["increment button"],
                checks=[
                    BrowserJourney(
                        command=("pytest", "tests/browser/test_counter.py"),
                        evidence_globs=("evidence/*.png",),
                        timeout_s=120,
                    )
                ],
            ),
        ],
        cross_slice_checks=[StateInvariant(description="ok", expression="True")],
        non_goals=["multi-user support"],
        done_means=["counter increments and persists"],
    )


def _build_result_passing(tmp_path: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=tmp_path,
        slice_results=[
            SliceResult(
                slice_id="shell",
                status=SliceStatus.PASSING,
                attempts=1,
                branch="i2p/x/shell",
                worktree=tmp_path,
                last_evidence=[_evidence(True, "exit=0")],
            ),
            SliceResult(
                slice_id="counter",
                status=SliceStatus.PASSING,
                attempts=2,
                branch="i2p/x/counter",
                worktree=tmp_path,
                last_evidence=[_evidence(True, "exit=0", artifacts=["/tmp/x/screenshot-1.png"])],
            ),
        ],
        total_cost_usd=0.42,
        total_wall_s=120.0,
    )


def _merge_result_landed(tmp_path: Path) -> MergeQueueResult:
    return MergeQueueResult(
        landed_ids=["shell", "counter"],
        results=[
            MergeResult(
                slice_id="shell",
                status=MergeStatus.LANDED,
                landed_commit="abc1234",
                slice_recheck_evidence=[_evidence(True, "exit=0")],
                cross_slice_evidence=[_evidence(True, "True")],
            ),
            MergeResult(
                slice_id="counter",
                status=MergeStatus.LANDED,
                landed_commit="def5678",
                slice_recheck_evidence=[
                    _evidence(True, "exit=0 artifacts=2", artifacts=["/tmp/x/shot1.png", "/tmp/x/shot2.png"])
                ],
                cross_slice_evidence=[_evidence(True, "True")],
            ),
        ],
        total_cost_usd=0.10,
        total_wall_s=30.0,
    )


def _audit_passed() -> AuditResult:
    return AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="Reviewed integrated app — all good.",
        slice_verdicts=[
            SliceVerdict(slice_id="shell", passed=True, detail="ok"),
            SliceVerdict(slice_id="counter", passed=True, detail="works"),
        ],
        cost_usd=0.20,
    )


# ---------------------------------------------------------------------------
# compose_proof_packet
# ---------------------------------------------------------------------------


def test_compose_proof_packet_basic_shape(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    packet = compose_proof_packet(
        spec,
        _build_result_passing(tmp_path),
        _merge_result_landed(tmp_path),
        _audit_passed(),
        wall_s=180.0,
        cost_usd=0.72,
    )
    assert packet.schema_version == PROOF_PACKET_SCHEMA_VERSION
    assert packet.intent == "A demo todo app"
    assert packet.project_kind == "webapp"
    assert packet.verdict == "passed"
    assert packet.wall_s == 180.0
    assert packet.cost_usd == 0.72
    assert len(packet.slices) == 2
    assert packet.landed_slice_ids == ["shell", "counter"]
    assert packet.blocked_slice_ids == []


def test_compose_proof_packet_includes_audit_verdicts(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    packet = compose_proof_packet(
        spec,
        _build_result_passing(tmp_path),
        _merge_result_landed(tmp_path),
        _audit_passed(),
        wall_s=1.0,
        cost_usd=0.0,
    )
    shell_packet = next(s for s in packet.slices if s.slice_id == "shell")
    assert shell_packet.audit_verdict == {"passed": True, "detail": "ok"}


def test_compose_proof_packet_blocked_slice_carries_narrative(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    build_result = _build_result_passing(tmp_path)
    merge_result = MergeQueueResult(
        landed_ids=["shell"],
        blocked_ids=["counter"],
        results=[
            MergeResult(slice_id="shell", status=MergeStatus.LANDED, landed_commit="abc1234"),
            MergeResult(
                slice_id="counter",
                status=MergeStatus.BLOCKED,
                failure_narrative="cross-slice check failed: missing route",
                slice_recheck_evidence=[_evidence(False, "exit=1")],
            ),
        ],
    )
    audit_result = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="counter failed",
        slice_verdicts=[SliceVerdict(slice_id="counter", passed=False, detail="missing route")],
    )
    packet = compose_proof_packet(
        spec, build_result, merge_result, audit_result,
        wall_s=10.0, cost_usd=0.0,
    )
    counter_packet = next(s for s in packet.slices if s.slice_id == "counter")
    assert counter_packet.landed is False
    assert counter_packet.status == "passing"  # build was passing; merge blocked
    assert "cross-slice" in counter_packet.failure_narrative
    assert packet.blocked_slice_ids == ["counter"]
    assert packet.verdict == "partial"


# ---------------------------------------------------------------------------
# render_json
# ---------------------------------------------------------------------------


def test_render_json_round_trip(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    packet = compose_proof_packet(
        spec, _build_result_passing(tmp_path), _merge_result_landed(tmp_path),
        _audit_passed(), wall_s=10.0, cost_usd=0.5,
    )
    text = render_json(packet)
    data = json.loads(text)
    assert data["schema_version"] == PROOF_PACKET_SCHEMA_VERSION
    assert data["verdict"] == "passed"
    assert data["intent"] == "A demo todo app"
    assert len(data["slices"]) == 2
    assert data["landed_slice_ids"] == ["shell", "counter"]


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------


def test_render_html_contains_required_sections(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    packet = compose_proof_packet(
        spec, _build_result_passing(tmp_path), _merge_result_landed(tmp_path),
        _audit_passed(), wall_s=10.0, cost_usd=0.5,
    )
    html = render_html(packet, session_dir=tmp_path)
    assert "<h1>" in html
    assert "A demo todo app" in html
    assert "verdict passed" in html
    # Spec sections
    assert "<h2>Spec</h2>" in html
    assert "Non-goals" in html
    assert "Done means" in html
    # Slice sections
    assert "<h2>Slices</h2>" in html
    assert "shell" in html
    assert "counter" in html
    # Audit
    assert "<h2>Audit</h2>" in html
    assert "Reviewed integrated app" in html
    # Merge state
    assert "<h2>Merge state</h2>" in html
    assert "abc1234" in html
    assert "def5678" in html


def test_render_html_escapes_user_input(tmp_path: Path) -> None:
    spec = Spec(
        intent="<script>alert('xss')</script>",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=[Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[])],
    )
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        slice_results=[
            SliceResult(slice_id="s1", status=SliceStatus.PASSING, attempts=1, branch="b", worktree=tmp_path),
        ],
    )
    merge_result = MergeQueueResult(
        landed_ids=["s1"],
        results=[MergeResult(slice_id="s1", status=MergeStatus.LANDED, landed_commit="aaa")],
    )
    audit = AuditResult(verdict=AuditVerdict.PASSED, narrative="<img onerror=alert(1)>", slice_verdicts=[])
    packet = compose_proof_packet(spec, build_result, merge_result, audit, wall_s=1.0, cost_usd=0.0)
    html = render_html(packet, session_dir=tmp_path)
    # Script tag should be escaped, not present as raw HTML
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    # Audit narrative escaped
    assert "<img onerror" not in html
    assert "&lt;img onerror" in html


def test_render_html_image_artifacts_become_thumbnails(tmp_path: Path) -> None:
    # Create a real artifact path in tmp_path so relative-path resolution works.
    art = tmp_path / "evidence" / "shot.png"
    art.parent.mkdir()
    art.write_bytes(b"x")
    spec = _two_slice_spec(tmp_path)
    build_result = _build_result_passing(tmp_path)
    # Replace counter's evidence with one that points at our real PNG.
    build_result.slice_results[1].last_evidence = [_evidence(True, "ok", artifacts=[str(art)])]
    merge_result = MergeQueueResult(
        landed_ids=["shell", "counter"],
        results=[
            MergeResult(slice_id="shell", status=MergeStatus.LANDED, landed_commit="abc1234"),
            MergeResult(
                slice_id="counter",
                status=MergeStatus.LANDED,
                landed_commit="def5678",
                slice_recheck_evidence=[_evidence(True, "ok", artifacts=[str(art)])],
            ),
        ],
    )
    packet = compose_proof_packet(
        spec, build_result, merge_result, _audit_passed(), wall_s=10.0, cost_usd=0.5,
    )
    html = render_html(packet, session_dir=tmp_path)
    # Image rendered as thumbnail (relative path expected)
    assert '<img src="evidence/shot.png"' in html


def test_render_html_video_walkthrough_artifact(tmp_path: Path) -> None:
    video = tmp_path / "audit" / "walk.webm"
    video.parent.mkdir()
    video.write_bytes(b"fake")
    spec = _two_slice_spec(tmp_path)
    build_result = _build_result_passing(tmp_path)
    merge_result = _merge_result_landed(tmp_path)
    audit = AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="walked through",
        walkthrough_artifacts=[video],
    )
    packet = compose_proof_packet(spec, build_result, merge_result, audit, wall_s=1.0, cost_usd=0.0)
    html = render_html(packet, session_dir=tmp_path)
    assert "<video controls" in html
    assert 'src="audit/walk.webm"' in html


def test_render_html_blocked_slice_renders_narrative(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        slice_results=[
            SliceResult(slice_id="shell", status=SliceStatus.PASSING, attempts=1, branch="b", worktree=tmp_path),
            SliceResult(
                slice_id="counter",
                status=SliceStatus.BLOCKED,
                attempts=3,
                branch="b2",
                worktree=tmp_path,
                failure_narrative="checks failed on attempt 3: NameError",
            ),
        ],
    )
    merge_result = MergeQueueResult(
        landed_ids=["shell"],
        blocked_ids=["counter"],
        results=[
            MergeResult(slice_id="shell", status=MergeStatus.LANDED, landed_commit="aaa"),
        ],
    )
    audit = AuditResult(verdict=AuditVerdict.PARTIAL, narrative="counter blocked")
    packet = compose_proof_packet(spec, build_result, merge_result, audit, wall_s=1.0, cost_usd=0.0)
    html = render_html(packet, session_dir=tmp_path)
    # Blocked section
    assert "Known limitations" in html
    assert "NameError" in html
    # Merge state shows ❌ for counter, ✅ for shell
    assert "✅" in html and "❌" in html


# ---------------------------------------------------------------------------
# write_proof_packet + render_run
# ---------------------------------------------------------------------------


def test_write_proof_packet_creates_both_files(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    packet = compose_proof_packet(
        spec, _build_result_passing(tmp_path), _merge_result_landed(tmp_path),
        _audit_passed(), wall_s=10.0, cost_usd=0.5,
    )
    html_path, json_path = write_proof_packet(packet, tmp_path)
    assert html_path == tmp_path / PROOF_PACKET_HTML
    assert json_path == tmp_path / PROOF_PACKET_JSON
    assert html_path.exists()
    assert json_path.exists()
    # JSON parses correctly
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == PROOF_PACKET_SCHEMA_VERSION


def test_render_run_end_to_end(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    html_path, json_path = render_run(
        spec,
        session_dir=tmp_path,
        build_result=_build_result_passing(tmp_path),
        merge_result=_merge_result_landed(tmp_path),
        audit_result=_audit_passed(),
        wall_s=180.0,
        cost_usd=0.72,
    )
    assert html_path.exists()
    assert json_path.exists()
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["verdict"] == "passed"
    assert parsed["wall_s"] == 180.0
    assert parsed["cost_usd"] == 0.72
    assert len(parsed["slices"]) == 2
