"""Tests for otto/web/run_view_routes.py — A4 FastAPI route mount.

Covers GET /api/run-view (list) and GET /api/run-view/<session_id> (RunView JSON).
Path-traversal rejection. 404 for missing sessions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from otto.web.run_view_routes import install_run_view_routes


def _write_minimal_session(session_dir: Path, *, intent: str, project_kind: str) -> None:
    session_dir.mkdir(parents=True)
    proof = {
        "intent": intent,
        "project_kind": project_kind,
        "verdict": "passed",
        "wall_s": 200.0,
        "cost_usd": 1.0,
        "groups": [{"id": "g", "title": "G"}],
        "features": [{"feature_id": "f1", "name": "F1", "verdict": "passed"}],
    }
    (session_dir / "proof-packet.json").write_text(json.dumps(proof))


@pytest.fixture
def project_with_session(tmp_path: Path) -> tuple[Path, str]:
    """Create a project with one session containing a minimal proof packet."""
    project = tmp_path / "proj"
    sessions = project / "otto_logs" / "sessions"
    sessions.mkdir(parents=True)
    session_id = "2026-05-04-200000-abc123"
    sdir = sessions / session_id
    _write_minimal_session(sdir, intent="tiny webapp", project_kind="webapp")
    return project, session_id


def _app_with_project(project: Path) -> TestClient:
    app = FastAPI()
    install_run_view_routes(app, project_dir=project)
    return TestClient(app)


def test_list_runs_returns_session_ids(project_with_session: tuple[Path, str]) -> None:
    project, sid = project_with_session
    client = _app_with_project(project)
    resp = client.get("/api/run-view")
    assert resp.status_code == 200
    body = resp.json()
    assert sid in body["runs"]


def test_list_runs_empty_when_no_sessions(tmp_path: Path) -> None:
    project = tmp_path / "empty-proj"
    project.mkdir()
    client = _app_with_project(project)
    resp = client.get("/api/run-view")
    assert resp.status_code == 200
    assert resp.json() == {"runs": [], "sessions": []}


def test_get_run_returns_run_view(project_with_session: tuple[Path, str]) -> None:
    project, sid = project_with_session
    client = _app_with_project(project)
    resp = client.get(f"/api/run-view/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    # Shape match: RunView fields
    assert body["run_id"] == sid
    assert body["intent"] == "tiny webapp"
    assert body["project_kind"] == "webapp"
    assert body["verdict"] == "passed"
    assert len(body["features"]) == 1
    assert body["features"][0]["id"] == "f1"
    assert "stages" in body
    assert "groups" in body
    assert "guardrails" in body


def test_list_runs_includes_queue_worktree_sessions(tmp_path: Path) -> None:
    """Queue/i2p sessions live inside per-task worktrees and must be visible."""

    project = tmp_path / "proj"
    root_sid = "2026-05-04-200000-root"
    worktree_sid = "2026-05-04-200000-worktree"
    _write_minimal_session(
        project / "otto_logs" / "sessions" / root_sid,
        intent="root session",
        project_kind="cli",
    )
    _write_minimal_session(
        project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / worktree_sid,
        intent="worktree session",
        project_kind="webapp",
    )

    client = _app_with_project(project)
    resp = client.get("/api/run-view")
    assert resp.status_code == 200
    body = resp.json()
    assert root_sid in body["runs"]
    assert worktree_sid in body["runs"]
    assert {row["id"] for row in body["sessions"]} >= {root_sid, worktree_sid}


def test_get_run_returns_queue_worktree_session(tmp_path: Path) -> None:
    """Task rows from `/api/state` link to sessions stored under `.worktrees`."""

    project = tmp_path / "proj"
    sid = "2026-05-04-200000-worktree"
    _write_minimal_session(
        project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / sid,
        intent="worktree micro twitter",
        project_kind="webapp",
    )

    client = _app_with_project(project)
    resp = client.get(f"/api/run-view/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == sid
    assert body["intent"] == "worktree micro twitter"
    assert body["project_kind"] == "webapp"


def test_get_run_404_for_missing_session(project_with_session: tuple[Path, str]) -> None:
    project, _ = project_with_session
    client = _app_with_project(project)
    resp = client.get("/api/run-view/nonexistent-session-id")
    assert resp.status_code == 404


def test_get_run_rejects_path_traversal(project_with_session: tuple[Path, str]) -> None:
    project, _ = project_with_session
    client = _app_with_project(project)
    # Try to escape sessions dir; FastAPI URL-decodes %2F so "..%2Fetc"
    # becomes "../etc" in the path param. Our resolver must reject it.
    resp = client.get("/api/run-view/..%2Fetc")
    assert resp.status_code == 404


def test_install_requires_project_dir_or_provider() -> None:
    app = FastAPI()
    with pytest.raises(ValueError):
        install_run_view_routes(app)


def test_dynamic_project_dir_provider(tmp_path: Path) -> None:
    """Per-request project resolver works for launcher mode."""
    project1 = tmp_path / "p1"
    project1_sessions = project1 / "otto_logs" / "sessions" / "s1"
    project1_sessions.mkdir(parents=True)
    (project1_sessions / "proof-packet.json").write_text(
        json.dumps({"intent": "p1", "project_kind": "webapp"})
    )
    project2 = tmp_path / "p2"
    project2_sessions = project2 / "otto_logs" / "sessions" / "s2"
    project2_sessions.mkdir(parents=True)
    (project2_sessions / "proof-packet.json").write_text(
        json.dumps({"intent": "p2", "project_kind": "cli"})
    )

    current = {"project": project1}
    app = FastAPI()
    install_run_view_routes(app, project_dir_provider=lambda: current["project"])
    client = TestClient(app)

    # Request 1: project1's session
    resp = client.get("/api/run-view/s1")
    assert resp.status_code == 200
    assert resp.json()["intent"] == "p1"

    # Switch project at runtime
    current["project"] = project2
    resp = client.get("/api/run-view/s2")
    assert resp.status_code == 200
    assert resp.json()["intent"] == "p2"


def test_no_project_selected_returns_409(tmp_path: Path) -> None:
    """Provider returns None → 409."""
    app = FastAPI()
    install_run_view_routes(app, project_dir_provider=lambda: None)
    client = TestClient(app)
    resp = client.get("/api/run-view")
    assert resp.status_code == 409
