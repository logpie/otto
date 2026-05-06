"""Tests for otto/web/spec_review_routes.py — A5 FastAPI spec-review surface.

Covers:
* `GET /api/specs/<id>/markdown` happy-path returns SpecMdView JSON.
* `POST /api/specs/<id>/edit` round-trips through parse_spec_md;
  archives prior version to spec-v<N>.json/.md.
* `POST /api/specs/<id>/edit` with stale intent_hash → 409.
* `POST /api/specs/<id>/approve` flips lifecycle, idempotent.
* Path-traversal blocked.
* 404 when spec dir missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from otto.spec_compile import (
    Feature,
    Group,
    Spec,
    lock_intent,
    render_spec_md,
    spec_to_dict,
)
from otto.web.spec_review_routes import install_spec_review_routes


def _make_spec() -> Spec:
    raw = Spec(
        intent="A doc editor for engineering teams",
        project_kind="webapp",
        groups=[Group(id="editor", name="Editor")],
        features=[
            Feature(
                id="md-render",
                name="Markdown rendering",
                description="Pages render .md as HTML.",
                evidence_kinds=["BrowserJourney"],
                group_id="editor",
            ),
        ],
    )
    return lock_intent(raw)


@pytest.fixture
def project_with_spec(tmp_path: Path) -> tuple[Path, str, Spec]:
    """Create a project + session with a populated spec/spec.json."""
    project = tmp_path / "proj"
    session_id = "2026-05-04-203000-abc123"
    sd = project / "otto_logs" / "sessions" / session_id / "spec"
    sd.mkdir(parents=True)
    spec = _make_spec()
    (sd / "spec.json").write_text(
        json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n"
    )
    (sd / "spec.md").write_text(render_spec_md(spec))
    return project, session_id, spec


def _client(project: Path) -> TestClient:
    app = FastAPI()
    install_spec_review_routes(app, project_dir=project)
    return TestClient(app)


def test_get_markdown_returns_view(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    resp = client.get(f"/api/specs/{sid}/markdown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["spec_id"] == sid
    assert body["intent_hash"] == spec.intent_hash
    assert body["lifecycle"] == "draft"
    assert "Markdown rendering" in body["markdown"]
    assert "<!-- feature: md-render" in body["markdown"]


def test_get_markdown_resolves_queue_worktree_session(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-203000-worktree"
    sd = project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / sid / "spec"
    sd.mkdir(parents=True)
    spec = _make_spec()
    (sd / "spec.json").write_text(json.dumps(spec_to_dict(spec)))
    (sd / "spec.md").write_text(render_spec_md(spec))

    client = _client(project)
    resp = client.get(f"/api/specs/{sid}/markdown")
    assert resp.status_code == 200
    assert resp.json()["intent_hash"] == spec.intent_hash
    journal = sd.parent / "spec-state.jsonl"
    assert journal.exists()
    assert "spec.review.opened" in journal.read_text()


def test_get_markdown_404_for_missing_session(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    client = _client(project)
    resp = client.get("/api/specs/no-such-session/markdown")
    assert resp.status_code == 404


def test_post_edit_round_trip_archives_prior_version(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    # Edit: rename the feature (id stays via metadata comment)
    md = render_spec_md(spec).replace(
        "Markdown rendering", "MD rendering (renamed)"
    )
    resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["view"]["lifecycle"] == "draft"
    assert "MD rendering (renamed)" in body["view"]["markdown"]
    assert isinstance(body["warnings"], list)

    # Archive present
    sd = project / "otto_logs" / "sessions" / sid / "spec"
    assert (sd / "spec-v1.json").exists()
    assert (sd / "spec-v1.md").exists()
    # Live spec.json updated
    new_spec = json.loads((sd / "spec.json").read_text())
    md_render = next(f for f in new_spec["features"] if f["id"] == "md-render")
    assert md_render["name"] == "MD rendering (renamed)"


def test_post_edit_stale_intent_hash_returns_409(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _spec = project_with_spec
    client = _client(project)
    resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": "deadbeef" * 8, "markdown": "# stale\n"},
    )
    assert resp.status_code == 409
    assert "stale" in resp.json()["detail"]


def test_post_edit_blocked_after_approval(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    # Approve first
    approve_resp = client.post(f"/api/specs/{sid}/approve")
    assert approve_resp.status_code == 200
    # Now edits should fail
    edit_resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": "# x\n"},
    )
    assert edit_resp.status_code == 409
    assert "approved" in edit_resp.json()["detail"]


def test_post_approve_idempotent(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _spec = project_with_spec
    client = _client(project)
    r1 = client.post(f"/api/specs/{sid}/approve")
    assert r1.status_code == 200
    assert r1.json()["view"]["lifecycle"] == "approved"
    r2 = client.post(f"/api/specs/{sid}/approve")
    assert r2.status_code == 200
    assert r2.json()["view"]["lifecycle"] == "approved"


def test_get_markdown_after_approve_shows_approved_lifecycle(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _spec = project_with_spec
    client = _client(project)
    client.post(f"/api/specs/{sid}/approve")
    resp = client.get(f"/api/specs/{sid}/markdown")
    assert resp.status_code == 200
    assert resp.json()["lifecycle"] == "approved"


def test_path_traversal_rejected(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "otto_logs" / "sessions").mkdir(parents=True)
    client = _client(project)
    resp = client.get("/api/specs/..%2F..%2Fetc/markdown")
    # FastAPI/Starlette routes don't normalize ".." but we still reject
    # via _resolve_spec_dir's relative_to guard.
    assert resp.status_code in (404, 400)


# ---------------------------------------------------------------------------
# spec.* state events (tick 36)
# ---------------------------------------------------------------------------


def _read_journal_kinds(project: Path, session_id: str) -> list[str]:
    journal = (
        project / "otto_logs" / "sessions" / session_id / "spec-state.jsonl"
    )
    if not journal.exists():
        return []
    return [json.loads(line)["kind"] for line in journal.read_text().splitlines() if line.strip()]


def test_get_markdown_emits_spec_review_opened(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _ = project_with_spec
    client = _client(project)
    assert _read_journal_kinds(project, sid) == []
    client.get(f"/api/specs/{sid}/markdown")
    assert "spec.review.opened" in _read_journal_kinds(project, sid)


def test_repeat_get_does_not_duplicate_review_opened(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _ = project_with_spec
    client = _client(project)
    for _ in range(3):
        client.get(f"/api/specs/{sid}/markdown")
    kinds = _read_journal_kinds(project, sid)
    assert kinds.count("spec.review.opened") == 1


def test_post_edit_emits_spec_edited(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    md = render_spec_md(spec)
    resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md},
    )
    assert resp.status_code == 200
    assert "spec.edited" in _read_journal_kinds(project, sid)


def test_post_approve_emits_spec_approved_once(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _ = project_with_spec
    client = _client(project)
    client.post(f"/api/specs/{sid}/approve")
    client.post(f"/api/specs/{sid}/approve")  # idempotent — no second event
    kinds = _read_journal_kinds(project, sid)
    assert kinds.count("spec.approved") == 1


def test_post_approve_emits_review_approved_for_gate(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """A13: POST /approve always emits spec.review_approved so a runner
    blocked at the review gate wakes up. Distinct from spec.approved
    which is lifecycle-transition-only.
    """
    project, sid, _ = project_with_spec
    client = _client(project)
    r1 = client.post(f"/api/specs/{sid}/approve")
    assert r1.status_code == 200
    kinds = _read_journal_kinds(project, sid)
    assert "spec.review_approved" in kinds
    # Repeat approve still emits review_approved so a transient runner
    # restart can re-read the signal — bounded by user clicks.
    client.post(f"/api/specs/{sid}/approve")
    kinds = _read_journal_kinds(project, sid)
    assert kinds.count("spec.review_approved") >= 1


def test_failed_edit_does_not_emit_spec_edited(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _ = project_with_spec
    client = _client(project)
    # Stale intent_hash → 409, no event
    client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": "stale" * 13, "markdown": "# x\n"},
    )
    assert "spec.edited" not in _read_journal_kinds(project, sid)


def test_versions_empty_when_no_edits(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _ = project_with_spec
    client = _client(project)
    resp = client.get(f"/api/specs/{sid}/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["versions"] == []


def test_versions_lists_archived_after_edits(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    md1 = render_spec_md(spec).replace(
        "Markdown rendering", "Markdown rendering v2"
    )
    r1 = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md1},
    )
    assert r1.status_code == 200, r1.text
    md2 = md1.replace("Markdown rendering v2", "Markdown rendering v3")
    r2 = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md2},
    )
    assert r2.status_code == 200, r2.text
    resp = client.get(f"/api/specs/{sid}/versions")
    assert resp.status_code == 200
    assert resp.json()["versions"] == [1, 2]


def test_diff_returns_paired_payload(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    md1 = render_spec_md(spec).replace(
        "Markdown rendering", "Markdown rendering v2"
    )
    client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md1},
    )
    md2 = md1.replace("Markdown rendering v2", "Markdown rendering v3")
    client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md2},
    )
    resp = client.get(f"/api/specs/{sid}/diff?from=1&to=2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from_version"] == 1
    assert body["to_version"] == 2
    assert "Markdown rendering" in body["from_md"]
    assert "Markdown rendering v2" in body["to_md"]
    assert isinstance(body["from_json"], dict)
    assert isinstance(body["to_json"], dict)
    # JSON structure should round-trip the spec.
    assert "features" in body["from_json"]
    assert "features" in body["to_json"]


def test_diff_404_for_missing_version(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, spec = project_with_spec
    client = _client(project)
    md1 = render_spec_md(spec).replace(
        "Markdown rendering", "Markdown rendering v2"
    )
    r1 = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": md1},
    )
    assert r1.status_code == 200
    # Only v1 exists; v9 should 404.
    resp = client.get(f"/api/specs/{sid}/diff?from=1&to=9")
    assert resp.status_code == 404
    assert "v9" in resp.json()["detail"]


def test_diff_rejects_zero_or_negative_versions(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    project, sid, _ = project_with_spec
    client = _client(project)
    resp = client.get(f"/api/specs/{sid}/diff?from=0&to=1")
    # B28: diff endpoint accepts integer or "current" sentinel; the int
    # range check now lives in the resolver and surfaces as 400.
    assert resp.status_code == 400


def test_edit_warnings_surface_in_response(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """parse_spec_md returns warnings; the route surfaces them."""
    project, sid, spec = project_with_spec
    client = _client(project)
    # Markdown with no Features section at all — parser should still
    # accept it (warnings will surface from parse_spec_md if any).
    resp = client.post(
        f"/api/specs/{sid}/edit",
        json={
            "intent_hash": spec.intent_hash,
            "markdown": "# Doc editor\n\n## Project kind\n\nwebapp\n",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "warnings" in body
    assert isinstance(body["warnings"], list)


# ---------------------------------------------------------------------------
# A6 — mid-build edit window via lifecycle=editing_in_flight
# ---------------------------------------------------------------------------


def test_post_edit_allowed_during_editing_in_flight_emits_invalidation(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """Runner sets lifecycle=editing_in_flight while build is running.
    A POST /edit during that window MUST be accepted AND must emit
    `group.invalidated_by_spec_edit` events for any Group whose Spec
    contributions changed.
    """
    project, sid, spec = project_with_spec
    # Simulate the runner having entered the build phase.
    sd = project / "otto_logs" / "sessions" / sid / "spec"
    (sd / "lifecycle.json").write_text(
        json.dumps({"lifecycle": "editing_in_flight"}, indent=2) + "\n"
    )

    client = _client(project)
    # Edit the spec — change the editor Group's name to trigger invalidation.
    resp = client.get(f"/api/specs/{sid}/markdown")
    assert resp.status_code == 200
    edited_md = resp.json()["markdown"].replace("Editor", "Editor v2")

    edit_resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": edited_md},
    )
    assert edit_resp.status_code == 200, edit_resp.json()

    # Journal contains a group.invalidated_by_spec_edit event for "editor".
    journal = project / "otto_logs" / "sessions" / sid / "spec-state.jsonl"
    assert journal.exists()
    events = [json.loads(line) for line in journal.read_text().splitlines()
              if line.strip()]
    invalidations = [
        e for e in events if e["kind"] == "group.invalidated_by_spec_edit"
    ]
    assert len(invalidations) >= 1
    assert any(e["group_id"] == "editor" for e in invalidations)


def test_post_edit_during_approved_still_blocks(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """Plain `approved` lifecycle (no in-flight build) still 409s."""
    project, sid, spec = project_with_spec
    client = _client(project)
    client.post(f"/api/specs/{sid}/approve")  # → lifecycle=approved
    edit_resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": "# nope\n"},
    )
    assert edit_resp.status_code == 409


# ---------------------------------------------------------------------------
# Round-3 audit gap 2 — lifecycle preconditions on /edit and /approve
# ---------------------------------------------------------------------------


def _emit_pause(project: Path, session_id: str) -> None:
    """Append a `run.paused_by_user` journal event for the session."""
    from otto.spec_state import emit as emit_state_event
    sd_journal = project / "otto_logs" / "sessions" / session_id
    emit_state_event(sd_journal, "run.paused_by_user", detail="test paused")


def test_post_edit_refuses_when_run_is_paused(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """An edit while the run is paused must be rejected — the runner is
    asleep at a phase boundary and would not see the invalidation
    events between phases.
    """
    project, sid, spec = project_with_spec
    _emit_pause(project, sid)

    client = _client(project)
    edit_resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": spec.intent_hash, "markdown": "# nope\n"},
    )
    assert edit_resp.status_code == 409
    assert "paused" in edit_resp.json()["detail"]


def test_post_approve_refuses_when_run_is_paused(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """Approve while paused must 409 so the operator resumes first."""
    project, sid, _spec = project_with_spec
    _emit_pause(project, sid)

    client = _client(project)
    resp = client.post(f"/api/specs/{sid}/approve")
    assert resp.status_code == 409
    assert "paused" in resp.json()["detail"]


def test_post_approve_refuses_when_lifecycle_not_in_allowed_set(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """Approve from `editing_in_flight` or `amended` must be rejected —
    those lifecycles indicate concurrent surgery the runner / amendment
    flow owns.
    """
    project, sid, _spec = project_with_spec
    sd = project / "otto_logs" / "sessions" / sid / "spec"
    (sd / "lifecycle.json").write_text(
        json.dumps({"lifecycle": "editing_in_flight"}, indent=2) + "\n"
    )

    client = _client(project)
    resp = client.post(f"/api/specs/{sid}/approve")
    assert resp.status_code == 409
    assert "lifecycle" in resp.json()["detail"]


def test_post_approve_normal_flow_still_works(
    project_with_spec: tuple[Path, str, Spec],
) -> None:
    """Sanity: the new guard does not block the canonical
    draft → approved transition."""
    project, sid, _spec = project_with_spec
    client = _client(project)
    resp = client.post(f"/api/specs/{sid}/approve")
    assert resp.status_code == 200
    assert resp.json()["view"]["lifecycle"] == "approved"
