"""Tests for otto/mission_control/run_view.py — A4 backend emitter.

Covers `build_run_view(session_dir, *, live_state=None)` returning RunView
shape from session dirs. Tests:
- happy path with proof-packet.json (post-Render)
- legacy session (no proof packet, no spec) → empty graceful
- legacy slices key → groups in RunView
- pre-Audit run (no verdict) → status derived from state events
- in-flight stages: status from event progression
- features fall through to spec when proof empty
- guardrails / components / findings shape
- malformed JSON tolerated
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.mission_control.run_view import build_run_view


def _setup_session(
    tmp_path: Path,
    *,
    proof: dict | None = None,
    spec: dict | None = None,
    state_events: list[dict] | None = None,
) -> Path:
    """Create a session dir with optional artifacts."""
    session_dir = tmp_path / "session-1"
    session_dir.mkdir()
    if proof is not None:
        (session_dir / "proof-packet.json").write_text(json.dumps(proof))
    if spec is not None:
        (session_dir / "spec").mkdir()
        (session_dir / "spec" / "spec.json").write_text(json.dumps(spec))
    if state_events is not None:
        (session_dir / "spec-state.jsonl").write_text(
            "\n".join(json.dumps(e) for e in state_events) + "\n"
        )
    return session_dir


def test_build_run_view_happy_path_post_render(tmp_path: Path) -> None:
    proof = {
        "schema_version": 1,
        "intent": "tiny webapp",
        "project_kind": "webapp",
        "verdict": "passed",
        "wall_s": 215.0,
        "cost_usd": 1.42,
        "groups": [
            {
                "id": "auth",
                "name": "Auth",
                "title": "Auth",
                "feature_ids": ["login"],
                "status": "passing",
                "branch": "auth-branch",
                "owned_paths": ["routes/auth.py"],
                "cost_usd": 0.5,
                "wall_s": 50.0,
            }
        ],
        "features": [
            {
                "feature_id": "login",
                "name": "Login",
                "verdict": "passed",
                "group_id": "auth",
                "evidence_completeness": "full",
                "coverage_confidence": "high",
            },
        ],
        "guardrails": [
            {"id": "no-oauth", "text": "No OAuth", "applies_to": "*", "verified": True},
        ],
        "quality_findings": [
            {"severity": "polish", "text": "color palette generic", "feature_id": ""}
        ],
    }
    session = _setup_session(tmp_path, proof=proof)
    view = build_run_view(session)

    assert view["intent"] == "tiny webapp"
    assert view["project_kind"] == "webapp"
    assert view["verdict"] == "passed"
    assert view["status"] == "passed"
    assert view["cost_usd"] == 1.42
    assert view["wall_s"] == 215.0
    assert len(view["features"]) == 1
    assert view["features"][0]["id"] == "login"
    assert view["features"][0]["verdict"] == "passed"
    assert view["features"][0]["evidence_completeness"] == "full"
    assert len(view["groups"]) == 1
    assert view["groups"][0]["id"] == "auth"
    assert view["groups"][0]["status"] == "passing"
    assert len(view["guardrails"]) == 1
    assert view["guardrails"][0]["verified"] is True
    assert len(view["findings"]) == 1
    assert view["findings"][0]["severity"] == "polish"


def test_build_run_view_legacy_session_no_artifacts(tmp_path: Path) -> None:
    """A session dir with neither proof packet nor spec → empty graceful."""
    session = _setup_session(tmp_path)
    view = build_run_view(session)
    assert view["intent"] == ""
    assert view["verdict"] is None
    assert view["status"] == "queued"
    assert view["features"] == []
    assert view["groups"] == []
    assert view["components"] == []
    assert view["guardrails"] == []
    assert view["findings"] == []


def test_build_run_view_legacy_slices_key_maps_to_groups(tmp_path: Path) -> None:
    """Pre-A0.3 specs that use 'slices' key still produce groups in RunView."""
    spec = {
        "intent": "legacy webapp",
        "project_kind": "webapp",
        "groups": [
            {"id": "g1", "title": "G1", "tasks": [], "deps": [], "owned_paths": [], "checks": []},
        ],
    }
    session = _setup_session(tmp_path, spec=spec)
    view = build_run_view(session)
    assert len(view["groups"]) == 1
    assert view["groups"][0]["id"] == "g1"
    assert view["groups"][0]["name"] == "G1"


def test_build_run_view_in_flight_status_from_state_events(tmp_path: Path) -> None:
    """Pre-verdict run: status derived from latest stage.started event."""
    spec = {"intent": "test", "project_kind": "webapp", "groups": []}
    state = [
        {"event": "run.started", "ts": "2026-05-04T20:00:00Z"},
        {"event": "stage.compile.started", "ts": "2026-05-04T20:00:01Z"},
        {"event": "stage.compile.finished", "ts": "2026-05-04T20:00:30Z", "duration_s": 29.0},
        {"event": "stage.build.started", "ts": "2026-05-04T20:00:31Z"},
    ]
    session = _setup_session(tmp_path, spec=spec, state_events=state)
    view = build_run_view(session)
    assert view["verdict"] is None
    assert view["status"] == "building"
    # Stage timeline reflects event sequence
    compile_stage = next(s for s in view["stages"] if s["name"] == "compile")
    build_stage = next(s for s in view["stages"] if s["name"] == "build")
    assert compile_stage["status"] == "done"
    assert compile_stage["duration_s"] == 29.0
    assert build_stage["status"] == "active"


def test_build_run_view_emits_canonical_stages(tmp_path: Path) -> None:
    session = _setup_session(tmp_path)
    view = build_run_view(session)
    stage_names = [s["name"] for s in view["stages"]]
    assert stage_names == ["compile", "spec_review", "build", "seed", "audit", "render", "land"]
    for s in view["stages"]:
        assert s["status"] == "pending"
        assert s["duration_s"] is None
        assert s["cost_usd"] is None


def test_build_run_view_features_from_spec_when_proof_empty(tmp_path: Path) -> None:
    """During in-flight, proof is absent — features fall through to spec."""
    spec = {
        "intent": "test",
        "project_kind": "webapp",
        "groups": [{"id": "g1", "title": "G1", "tasks": [], "deps": [], "owned_paths": []}],
        "features": [
            {
                "id": "f1",
                "name": "F1",
                "group_id": "g1",
                "evidence_completeness": "full",
                "coverage_confidence": "high",
            }
        ],
    }
    session = _setup_session(tmp_path, spec=spec)
    view = build_run_view(session)
    assert len(view["features"]) == 1
    assert view["features"][0]["id"] == "f1"
    assert view["features"][0]["verdict"] is None  # pre-Audit


def test_build_run_view_components_from_proof(tmp_path: Path) -> None:
    proof = {
        "verdict": "passed",
        "components": [
            {
                "id": "ws-hub",
                "name": "WebSocket hub",
                "owned_paths": ["realtime/"],
                "consumed_by": ["dm-delivery"],
                "status": "passing",
            }
        ],
    }
    session = _setup_session(tmp_path, proof=proof)
    view = build_run_view(session)
    assert len(view["components"]) == 1
    assert view["components"][0]["id"] == "ws-hub"
    assert view["components"][0]["consumed_by"] == ["dm-delivery"]


def test_build_run_view_meta_includes_session_paths(tmp_path: Path) -> None:
    spec = {"intent": "test", "intent_hash": "abc123", "project_kind": "webapp"}
    proof = {"verdict": "passed"}
    session = _setup_session(tmp_path, spec=spec, proof=proof)
    view = build_run_view(session)
    assert view["meta"]["session_id"] == session.name
    assert view["meta"]["intent_hash"] == "abc123"
    assert view["meta"]["proof_packet_json"] is not None


def test_build_run_view_malformed_proof_falls_back_to_spec(tmp_path: Path) -> None:
    """Corrupt proof-packet.json must not crash; falls back to spec."""
    session = tmp_path / "session-x"
    session.mkdir()
    (session / "proof-packet.json").write_text("{not valid json")
    (session / "spec").mkdir()
    (session / "spec" / "spec.json").write_text(json.dumps({"intent": "fallback"}))
    view = build_run_view(session)
    assert view["intent"] == "fallback"
    assert view["features"] == []


def test_build_run_view_legacy_string_findings_default_severity(tmp_path: Path) -> None:
    """Bare-string findings (legacy shape) default to severity=important."""
    proof = {
        "verdict": "partial",
        "quality_findings": ["page load >8s", "color palette generic"],
    }
    session = _setup_session(tmp_path, proof=proof)
    view = build_run_view(session)
    assert len(view["findings"]) == 2
    assert all(f["severity"] == "important" for f in view["findings"])


def test_build_run_view_seed_stage_resolves_on_seed_finished(tmp_path: Path) -> None:
    """seed.started + seed.finished events flip the seed stage out of pending.

    RUA W6-C: previously the canonical stage list included `seed` but no
    seed.* events ever flipped its status, so the timeline showed
    `pending —` even after the Seed stage ran. The journal-emitted
    seed.started/seed.finished events now resolve it.
    """
    state = [
        {"event": "stage.compile.started", "ts": "2026-05-04T20:00:00Z"},
        {"event": "stage.compile.finished", "ts": "2026-05-04T20:00:30Z"},
        {"kind": "seed.started", "ts": "2026-05-04T20:00:31Z"},
        {
            "kind": "seed.finished",
            "ts": "2026-05-04T20:00:33Z",
            "extra": {"succeeded": True, "applied": 1},
        },
    ]
    session = _setup_session(tmp_path, state_events=state)
    view = build_run_view(session)
    seed_stage = next(s for s in view["stages"] if s["name"] == "seed")
    assert seed_stage["status"] == "done"
    assert seed_stage["started_at"] == "2026-05-04T20:00:31Z"
    assert seed_stage["finished_at"] == "2026-05-04T20:00:33Z"


def test_build_run_view_seed_stage_failed_when_succeeded_false(tmp_path: Path) -> None:
    """seed.finished with extra.succeeded=False marks the stage failed."""
    state = [
        {"kind": "seed.started", "ts": "2026-05-04T20:00:31Z"},
        {
            "kind": "seed.finished",
            "ts": "2026-05-04T20:00:33Z",
            "extra": {"succeeded": False},
        },
    ]
    session = _setup_session(tmp_path, state_events=state)
    view = build_run_view(session)
    seed_stage = next(s for s in view["stages"] if s["name"] == "seed")
    assert seed_stage["status"] == "failed"
    assert seed_stage["finished_at"] == "2026-05-04T20:00:33Z"


def test_build_run_view_status_recognises_seed_started(tmp_path: Path) -> None:
    """Pre-verdict run with seed.started but no later stage → status=building."""
    state = [
        {"event": "stage.compile.finished", "ts": "2026-05-04T20:00:30Z"},
        {"kind": "seed.started", "ts": "2026-05-04T20:00:31Z"},
    ]
    session = _setup_session(tmp_path, state_events=state)
    view = build_run_view(session)
    # seed maps to "building" in the RunStatus mapping
    assert view["status"] == "building"


def test_build_run_view_derives_features_from_group_feature_ids(tmp_path: Path) -> None:
    spec = {
        "intent": "build a micro twitter",
        "project_kind": "webapp",
        "features": [],
        "groups": [
            {
                "id": "timeline",
                "name": "Timeline",
                "feature_ids": ["post short messages", "view latest posts"],
                "checks": [{"kind": "state_invariant"}],
            }
        ],
    }
    session = _setup_session(tmp_path, spec=spec)
    view = build_run_view(session)
    assert [f["id"] for f in view["features"]] == [
        "post short messages",
        "view latest posts",
    ]
    assert view["features"][0]["group_id"] == "timeline"
    assert view["features"][0]["evidence_kinds"] == ["StateInvariant"]


def test_build_run_view_group_started_drives_group_and_build_status(tmp_path: Path) -> None:
    spec = {
        "intent": "test",
        "project_kind": "webapp",
        "groups": [{"id": "g1", "name": "G1", "feature_ids": ["f1"]}],
    }
    state = [
        {"kind": "seed.finished", "ts": "2026-05-04T20:00:01Z"},
        {"kind": "group.started", "group_id": "g1", "ts": "2026-05-04T20:00:02Z"},
    ]
    session = _setup_session(tmp_path, spec=spec, state_events=state)
    view = build_run_view(session)
    assert view["status"] == "building"
    assert view["groups"][0]["status"] == "in_progress"
    assert next(s for s in view["stages"] if s["name"] == "compile")["status"] == "done"
    assert next(s for s in view["stages"] if s["name"] == "build")["status"] == "active"


def test_in_flight_features_inherit_group_build_state(tmp_path: Path) -> None:
    spec = {
        "intent": "test",
        "project_kind": "webapp",
        "groups": [
            {"id": "shell", "name": "App shell", "feature_ids": ["route", "nav"]},
            {"id": "posts", "name": "Posts", "feature_ids": ["create post"]},
        ],
    }
    state = [
        {
            "kind": "group.started",
            "group_id": "shell",
            "ts": "2026-05-04T20:00:02Z",
            "extra": {"branch": "i2p/session-1/shell"},
        },
        {
            "kind": "group.merge.eligible",
            "group_id": "shell",
            "ts": "2026-05-04T20:01:02Z",
            "extra": {"wall_s": 60, "cost_usd": 0.2},
        },
        {
            "kind": "group.started",
            "group_id": "posts",
            "ts": "2026-05-04T20:01:03Z",
            "extra": {"branch": "i2p/session-1/posts"},
        },
    ]
    session = _setup_session(tmp_path, spec=spec, state_events=state)

    view = build_run_view(session)

    features = {feature["id"]: feature for feature in view["features"]}
    assert features["route"]["build_status"] == "passing"
    assert features["route"]["group_name"] == "App shell"
    assert features["create post"]["build_status"] == "in_progress"
    groups = {group["id"]: group for group in view["groups"]}
    assert groups["shell"]["branch"] == "i2p/session-1/shell"
    assert groups["shell"]["wall_s"] == 60.0
    assert groups["shell"]["cost_usd"] == 0.2
    assert groups["posts"]["branch"] == "i2p/session-1/posts"


def test_proof_group_id_maps_to_group_view_and_blocked_event_wins(
    tmp_path: Path,
) -> None:
    proof = {
        "intent": "test",
        "project_kind": "webapp",
        "verdict": "blocked",
        "groups": [
            {
                "group_id": "foundation",
                "name": "Foundation",
                "status": "passing",
                "landed": False,
                "branch": "i2p/session-1/foundation",
                "failure_narrative": "checkout main failed",
            }
        ],
    }
    spec = {
        "intent": "test",
        "project_kind": "webapp",
        "groups": [
            {
                "id": "foundation",
                "name": "Foundation",
                "feature_ids": ["scaffold", "routing"],
                "dependencies": ["seed"],
            }
        ],
    }
    state = [
        {
            "kind": "group.blocked",
            "group_id": "foundation",
            "detail": "checkout main failed",
            "ts": "2026-05-04T20:01:02Z",
        }
    ]
    session = _setup_session(tmp_path, proof=proof, spec=spec, state_events=state)

    view = build_run_view(session)

    assert view["groups"][0]["id"] == "foundation"
    assert view["groups"][0]["status"] == "blocked"
    assert view["groups"][0]["branch"] == "i2p/session-1/foundation"
    assert view["groups"][0]["feature_ids"] == ["scaffold", "routing"]
    assert view["groups"][0]["dependencies"] == ["seed"]


def test_build_run_view_initializing_live_state_does_not_hide_started_group(
    tmp_path: Path,
) -> None:
    spec = {
        "intent": "test",
        "project_kind": "webapp",
        "groups": [{"id": "g1", "name": "G1", "feature_ids": ["f1"]}],
    }
    state = [{"kind": "group.started", "group_id": "g1", "ts": "2026-05-04T20:00:02Z"}]
    session = _setup_session(tmp_path, spec=spec, state_events=state)
    view = build_run_view(
        session,
        live_state={
            "status": "initializing",
            "started_at": "2026-05-04T20:00:00Z",
            "duration_s": 90,
        },
    )
    assert view["status"] == "building"
    assert view["wall_s"] == 90.0
    assert view["groups"][0]["status"] == "in_progress"
    assert next(s for s in view["stages"] if s["name"] == "build")["status"] == "active"


def test_build_run_view_interrupted_queue_state_is_terminal(tmp_path: Path) -> None:
    spec = {
        "intent": "test",
        "project_kind": "webapp",
        "groups": [{"id": "g1", "name": "G1", "feature_ids": ["f1"]}],
    }
    state = [{"kind": "group.started", "group_id": "g1", "ts": "2026-05-04T20:00:02Z"}]
    session = _setup_session(tmp_path, spec=spec, state_events=state)
    view = build_run_view(
        session,
        live_state={
            "status": "interrupted",
            "started_at": "2026-05-04T20:00:00Z",
            "finished_at": "2026-05-04T20:05:00Z",
            "duration_s": 300,
            "cost_usd": 0.25,
        },
    )
    assert view["status"] == "interrupted"
    assert view["wall_s"] == 300.0
    assert view["cost_usd"] == 0.25
    assert view["meta"]["started_at"] == "2026-05-04T20:00:00Z"
    assert view["meta"]["finished_at"] == "2026-05-04T20:05:00Z"
    assert next(s for s in view["stages"] if s["name"] == "build")["status"] == "failed"
