"""A5 exit-criteria integration test (research §4.8, A5 Exit criteria).

End-to-end flow against the real FastAPI app, no live LLM:
  1. Compose a session on disk with a draft Spec.
  2. GET /api/specs/<sid>/markdown            → SpecMdView (lifecycle=draft).
  3. POST /api/specs/<sid>/edit               → user renames a Feature.
  4. POST /api/specs/<sid>/approve            → lifecycle flips to approved.
  5. Verify on-disk: spec.json reflects the rename, lifecycle.json says
     approved, spec-state.jsonl has the three expected events.

This is the "Pause Run at gate, edit via API, approve, Build proceeds
with edited Spec" exit criterion for A5: the build system reads
spec.json + lifecycle.json on resume; if both reflect user edits +
approval, build proceeds with the edited spec. Asserting the on-disk
artifacts is the load-bearing check.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _seed_session(project_dir: Path, session_id: str) -> Spec:
    spec = lock_intent(
        Spec(
            intent="A doc editor for engineering teams",
            project_kind="webapp",
            groups=[Group(id="editor", title="Editor")],
            features=[
                Feature(
                    id="md-render",
                    name="Markdown rendering",
                    description="Pages render .md as HTML.",
                    evidence_kinds=["BrowserJourney"],
                    group_id="editor",
                ),
                Feature(
                    id="save-load",
                    name="Save / load drafts",
                    description="Persist drafts.",
                    evidence_kinds=["ApiProbe"],
                    group_id="editor",
                ),
            ],
        )
    )
    sd = project_dir / "otto_logs" / "sessions" / session_id / "spec"
    sd.mkdir(parents=True)
    (sd / "spec.json").write_text(
        json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n"
    )
    (sd / "spec.md").write_text(render_spec_md(spec))
    return spec


def test_a5_full_review_flow(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-210000-abc123"
    initial_spec = _seed_session(project, sid)

    app = FastAPI()
    install_spec_review_routes(app, project_dir=project)
    client = TestClient(app)

    # ---- 1. GET /markdown ----
    get_resp = client.get(f"/api/specs/{sid}/markdown")
    assert get_resp.status_code == 200, get_resp.text
    initial_view = get_resp.json()
    assert initial_view["lifecycle"] == "draft"
    assert initial_view["intent_hash"] == initial_spec.intent_hash
    initial_md = initial_view["markdown"]

    # ---- 2. POST /edit (rename a Feature) ----
    edited_md = initial_md.replace(
        "Markdown rendering", "Markdown rendering (improved)"
    )
    edit_resp = client.post(
        f"/api/specs/{sid}/edit",
        json={
            "intent_hash": initial_spec.intent_hash,
            "markdown": edited_md,
        },
    )
    assert edit_resp.status_code == 200, edit_resp.text
    edit_body = edit_resp.json()
    assert "Markdown rendering (improved)" in edit_body["view"]["markdown"]
    assert isinstance(edit_body["warnings"], list)

    # ---- 3. POST /approve ----
    approve_resp = client.post(f"/api/specs/{sid}/approve")
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["view"]["lifecycle"] == "approved"

    # ---- 4. Verify on-disk artifacts ----
    sd = project / "otto_logs" / "sessions" / sid / "spec"
    on_disk_spec = json.loads((sd / "spec.json").read_text())
    md_render = next(
        f for f in on_disk_spec["features"] if f["id"] == "md-render"
    )
    # Edit landed: name updated; id stable.
    assert md_render["name"] == "Markdown rendering (improved)"
    assert md_render["id"] == "md-render"
    # Other features untouched.
    save_load = next(
        f for f in on_disk_spec["features"] if f["id"] == "save-load"
    )
    assert save_load["name"] == "Save / load drafts"
    # Tier-1 invariants intact: intent + intent_hash unchanged.
    assert on_disk_spec["intent"] == initial_spec.intent
    assert on_disk_spec["intent_hash"] == initial_spec.intent_hash
    # Version archive present from the edit.
    assert (sd / "spec-v1.json").exists()
    assert (sd / "spec-v1.md").exists()
    # Lifecycle flipped.
    lifecycle = json.loads((sd / "lifecycle.json").read_text())
    assert lifecycle["lifecycle"] == "approved"

    # ---- 5. Verify state-event journal ----
    journal_path = (
        project / "otto_logs" / "sessions" / sid / "spec-state.jsonl"
    )
    kinds = [
        json.loads(line)["kind"]
        for line in journal_path.read_text().splitlines()
        if line.strip()
    ]
    # All three review events present, in order.
    assert kinds == ["spec.review.opened", "spec.edited", "spec.approved"]

    # ---- 6. Subsequent GET surfaces the approved view ----
    final_get = client.get(f"/api/specs/{sid}/markdown")
    assert final_get.status_code == 200
    assert final_get.json()["lifecycle"] == "approved"
    # Repeat GETs do not re-emit spec.review.opened.
    final_kinds = [
        json.loads(line)["kind"]
        for line in journal_path.read_text().splitlines()
        if line.strip()
    ]
    assert final_kinds.count("spec.review.opened") == 1


def test_a5_stale_edit_blocked_during_concurrent_session(
    tmp_path: Path,
) -> None:
    """If two reviewers race, the second edit (with stale intent_hash)
    must be blocked — verifies the Tier-1 concurrency guard end-to-end."""
    project = tmp_path / "proj"
    sid = "2026-05-04-210500-xyz999"
    spec = _seed_session(project, sid)
    app = FastAPI()
    install_spec_review_routes(app, project_dir=project)
    client = TestClient(app)

    md = render_spec_md(spec)
    # Stale hash — simulates a stale browser tab posting against an
    # already-bumped spec.
    resp = client.post(
        f"/api/specs/{sid}/edit",
        json={"intent_hash": "stale" * 13, "markdown": md},
    )
    assert resp.status_code == 409
    # On-disk spec untouched.
    on_disk = json.loads(
        (project / "otto_logs" / "sessions" / sid / "spec" / "spec.json").read_text()
    )
    assert on_disk["intent_hash"] == spec.intent_hash
    md_render = next(f for f in on_disk["features"] if f["id"] == "md-render")
    assert md_render["name"] == "Markdown rendering"
