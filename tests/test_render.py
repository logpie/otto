"""Tests for otto/render.py — proof-packet HTML + JSON.

Coverage:
- compose_proof_packet: maps spec/build/merge/audit into ProofPacket
- render_json: round-trips through JSON, schema_version present
- render_html: contains required sections, escapes user input,
  thumbnails for image artifacts, video tag for video artifacts,
  blocked groups rendered with narrative not omitted
- write_proof_packet: writes both files, returns paths
- render_run: end-to-end with passing + blocked + landed groups
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.audit import AuditResult, AuditVerdict, GroupVerdict
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.render import (
    PROOF_PACKET_HTML,
    PROOF_PACKET_JSON,
    PROOF_PACKET_SCHEMA_VERSION,
    _TEMPLATES_DIR,
    compose_proof_packet,
    proof_packet_from_dict,
    rerender_proof_packet,
    render_html,
    render_json,
    render_run,
    write_proof_packet,
)
from otto.spec_compile import (
    BrowserJourney,
    FeatureProofBlock,
    RepoTestCheck,
    Group,
    Spec,
    StateInvariant,
    StructureDecisions,
    feature_proof_block_to_html,
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
        groups=[
            Group(
                id="shell",
                name="App shell",
                dependencies=[],
                owned_paths=["src/App.*"],
                feature_ids=["scaffold"],
                checks=[RepoTestCheck(command=("npm", "run", "build"), timeout_s=60)],
            ),
            Group(
                id="counter",
                name="Counter widget",
                dependencies=["shell"],
                owned_paths=["src/components/Counter.*"],
                feature_ids=["increment button"],
                checks=[
                    BrowserJourney(
                        command=("pytest", "tests/browser/test_counter.py"),
                        evidence_globs=("evidence/*.png",),
                        timeout_s=120,
                    )
                ],
            ),
        ],
        cross_group_checks=[StateInvariant(description="ok", expression="True")],
        non_goals=["multi-user support"],
        done_means=["counter increments and persists"],
    )


def _build_result_passing(tmp_path: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="shell",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="i2p/x/shell",
                worktree=tmp_path,
                last_evidence=[_evidence(True, "exit=0")],
            ),
            GroupResult(
                group_id="counter",
                status=GroupStatus.PASSING,
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
                group_id="shell",
                status=MergeStatus.LANDED,
                landed_commit="abc1234",
                group_recheck_evidence=[_evidence(True, "exit=0")],
                cross_slice_evidence=[_evidence(True, "True")],
            ),
            MergeResult(
                group_id="counter",
                status=MergeStatus.LANDED,
                landed_commit="def5678",
                group_recheck_evidence=[
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
        group_verdicts=[
            GroupVerdict(group_id="shell", passed=True, detail="ok"),
            GroupVerdict(group_id="counter", passed=True, detail="works"),
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
    assert len(packet.groups) == 2
    assert packet.landed_group_ids == ["shell", "counter"]
    assert packet.blocked_group_ids == []


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
    shell_packet = next(s for s in packet.groups if s.group_id == "shell")
    assert shell_packet.audit_verdict == {"passed": True, "detail": "ok"}


def test_compose_proof_packet_blocked_slice_carries_narrative(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    build_result = _build_result_passing(tmp_path)
    merge_result = MergeQueueResult(
        landed_ids=["shell"],
        blocked_ids=["counter"],
        results=[
            MergeResult(group_id="shell", status=MergeStatus.LANDED, landed_commit="abc1234"),
            MergeResult(
                group_id="counter",
                status=MergeStatus.BLOCKED,
                failure_narrative="cross-slice check failed: missing route",
                group_recheck_evidence=[_evidence(False, "exit=1")],
            ),
        ],
    )
    audit_result = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="counter failed",
        group_verdicts=[GroupVerdict(group_id="counter", passed=False, detail="missing route")],
    )
    packet = compose_proof_packet(
        spec, build_result, merge_result, audit_result,
        wall_s=10.0, cost_usd=0.0,
    )
    counter_packet = next(s for s in packet.groups if s.group_id == "counter")
    assert counter_packet.landed is False
    assert counter_packet.status == "passing"  # build was passing; merge blocked
    assert "cross-slice" in counter_packet.failure_narrative
    assert packet.blocked_group_ids == ["counter"]
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
    assert len(data["groups"]) == 2
    assert data["landed_group_ids"] == ["shell", "counter"]


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
    # Group sections
    assert "<h2>Groups</h2>" in html
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
        groups=[Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[])],
    )
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(group_id="s1", status=GroupStatus.PASSING, attempts=1, branch="b", worktree=tmp_path),
        ],
    )
    merge_result = MergeQueueResult(
        landed_ids=["s1"],
        results=[MergeResult(group_id="s1", status=MergeStatus.LANDED, landed_commit="aaa")],
    )
    audit = AuditResult(verdict=AuditVerdict.PASSED, narrative="<img onerror=alert(1)>", group_verdicts=[])
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
    build_result.group_results[1].last_evidence = [_evidence(True, "ok", artifacts=[str(art)])]
    merge_result = MergeQueueResult(
        landed_ids=["shell", "counter"],
        results=[
            MergeResult(group_id="shell", status=MergeStatus.LANDED, landed_commit="abc1234"),
            MergeResult(
                group_id="counter",
                status=MergeStatus.LANDED,
                landed_commit="def5678",
                group_recheck_evidence=[_evidence(True, "ok", artifacts=[str(art)])],
            ),
        ],
    )
    packet = compose_proof_packet(
        spec, build_result, merge_result, _audit_passed(), wall_s=10.0, cost_usd=0.5,
    )
    html = render_html(packet, session_dir=tmp_path)
    # Image rendered as thumbnail (relative path expected)
    assert '<img src="evidence/shot.png"' in html
    assert "grid-template-columns" in html


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


def test_proof_templates_exist_and_are_used(tmp_path: Path, monkeypatch) -> None:
    """A3.2: proof-packet and feature proof render through template files."""
    assert (_TEMPLATES_DIR / "proof-packet.html.j2").is_file()
    assert (_TEMPLATES_DIR / "feature-proof.html.j2").is_file()

    import otto.render as render_mod
    import otto.spec_compile as spec_compile_mod

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "proof-packet.html.j2").write_text(
        "<html><body><main data-template='proof'>{{ body }}</main></body></html>",
        encoding="utf-8",
    )
    feature_template = tmp_path / "feature-proof.html.j2"
    feature_template.write_text(
        "<section data-template='feature'>{{ name }} {{ walkthrough_html }}</section>",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_mod, "_TEMPLATES_DIR", template_dir)
    monkeypatch.setattr(spec_compile_mod, "_FEATURE_PROOF_TEMPLATE", feature_template)

    packet = compose_proof_packet(
        _two_slice_spec(tmp_path),
        _build_result_passing(tmp_path),
        _merge_result_landed(tmp_path),
        _audit_passed(),
        wall_s=1.0,
        cost_usd=0.0,
    )
    html = render_mod.render_html(packet, session_dir=tmp_path)
    feature_html = feature_proof_block_to_html(
        FeatureProofBlock(feature_id="f", name="Feature", verdict="passed")
    )

    assert "data-template='proof'" in html
    assert "data-template='feature'" in feature_html


def test_render_html_blocked_slice_renders_narrative(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(group_id="shell", status=GroupStatus.PASSING, attempts=1, branch="b", worktree=tmp_path),
            GroupResult(
                group_id="counter",
                status=GroupStatus.BLOCKED,
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
            MergeResult(group_id="shell", status=MergeStatus.LANDED, landed_commit="aaa"),
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
    assert len(parsed["groups"]) == 2


def test_render_run_embeds_compact_usage_telemetry(tmp_path: Path) -> None:
    messages = tmp_path / "audit" / "attempt-00" / "judge" / "messages.jsonl"
    messages.parent.mkdir(parents=True)
    messages.write_text(
        json.dumps({
            "type": "phase_end",
            "phase": "build",
            "duration_s": 2.5,
            "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10},
        }) + "\n",
        encoding="utf-8",
    )

    html_path, json_path = render_run(
        _two_slice_spec(tmp_path),
        session_dir=tmp_path,
        build_result=_build_result_passing(tmp_path),
        merge_result=_merge_result_landed(tmp_path),
        audit_result=_audit_passed(),
        wall_s=180.0,
        cost_usd=0.72,
    )

    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    html = html_path.read_text(encoding="utf-8")
    assert parsed["token_usage"] == {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "output_tokens": 10,
        "total_tokens": 110,
    }
    assert parsed["phase_usage"]["audit"]["duration_s"] == 2.5
    assert parsed["agent_usage_top"][0]["path"] == "audit/attempt-00/judge/messages.jsonl"
    assert "30 fresh + 80 cached" in html


def test_render_run_end_to_end_combines_landed_and_blocked_groups(tmp_path: Path) -> None:
    """Lifecycle fixture covers passed/landed and blocked groups together."""
    spec = _two_slice_spec(tmp_path)
    build_result = BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="shell",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="i2p/x/shell",
                worktree=tmp_path,
            ),
            GroupResult(
                group_id="counter",
                status=GroupStatus.BLOCKED,
                attempts=3,
                branch="i2p/x/counter",
                worktree=tmp_path,
                failure_narrative="checks failed on attempt 3: NameError",
            ),
        ],
    )
    merge_result = MergeQueueResult(
        landed_ids=["shell"],
        results=[MergeResult(group_id="shell", status=MergeStatus.LANDED, landed_commit="abc1234")],
    )
    audit_result = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="counter blocked",
        group_verdicts=[
            GroupVerdict(group_id="shell", passed=True, detail="ok"),
            GroupVerdict(group_id="counter", passed=False, detail="missing route"),
        ],
    )

    html_path, json_path = render_run(
        spec,
        session_dir=tmp_path,
        build_result=build_result,
        merge_result=merge_result,
        audit_result=audit_result,
        wall_s=180.0,
        cost_usd=0.72,
    )
    html = html_path.read_text(encoding="utf-8")
    parsed = json.loads(json_path.read_text(encoding="utf-8"))

    assert parsed["verdict"] == "partial"
    assert parsed["landed_group_ids"] == ["shell"]
    assert parsed["blocked_group_ids"] == ["counter"]
    statuses = {group["group_id"]: group["status"] for group in parsed["groups"]}
    assert statuses == {"shell": "landed", "counter": "blocked"}
    assert "Known limitations" in html
    assert "NameError" in html
    assert "App shell" in html and "Counter widget" in html


def test_proof_packet_from_dict_accepts_legacy_slice_keys() -> None:
    packet = proof_packet_from_dict(
        {
            "schema_version": 1,
            "intent": "legacy packet",
            "project_kind": "webapp",
            "verdict": "passed",
            "slices": [
                {
                    "slice_id": "shell",
                    "title": "App shell",
                    "status": "landed",
                    "landed": True,
                    "landed_commit": "abc",
                }
            ],
            "landed_slice_ids": ["shell"],
            "blocked_slice_ids": [],
            "capability_verdicts": [
                {"name": "App shell", "status": "passed", "detail": "ok"}
            ],
        }
    )

    assert packet.groups[0].group_id == "shell"
    assert packet.groups[0].name == "App shell"
    assert packet.landed_group_ids == ["shell"]
    assert packet.feature_audits[0]["name"] == "App shell"


def test_rerender_proof_packet_refreshes_html_without_rewriting_json(tmp_path: Path) -> None:
    spec = _two_slice_spec(tmp_path)
    packet = compose_proof_packet(
        spec,
        _build_result_passing(tmp_path),
        _merge_result_landed(tmp_path),
        _audit_passed(),
        wall_s=10.0,
        cost_usd=0.5,
    )
    _, json_path = write_proof_packet(packet, tmp_path)
    original_json = json_path.read_text(encoding="utf-8")
    (tmp_path / PROOF_PACKET_HTML).write_text("stale", encoding="utf-8")

    html_path, returned_json_path = rerender_proof_packet(tmp_path)

    assert html_path == tmp_path / PROOF_PACKET_HTML
    assert returned_json_path == json_path
    assert "stale" not in html_path.read_text(encoding="utf-8")
    assert "<h2>Groups</h2>" in html_path.read_text(encoding="utf-8")
    assert json_path.read_text(encoding="utf-8") == original_json
