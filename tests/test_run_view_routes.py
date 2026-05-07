"""Tests for otto/web/run_view_routes.py — A4 FastAPI route mount.

Covers GET /api/run-view (list) and GET /api/run-view/<session_id> (RunView JSON).
Path-traversal rejection. 404 for missing sessions.
"""

from __future__ import annotations

import json
import subprocess
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


def test_get_run_does_not_show_queue_concurrency_as_group_concurrency(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-abc123"
    _write_minimal_session(
        project / "otto_logs" / "sessions" / sid,
        intent="tiny webapp",
        project_kind="webapp",
    )
    (project / "otto.yaml").write_text("queue:\n  concurrent: 4\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)

    body = _app_with_project(project).get(f"/api/run-view/{sid}").json()

    assert body["dispatch"]["max_concurrent"] == 1


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


def test_get_run_merges_queue_state_for_worktree_session(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-worktree"
    _write_minimal_session(
        project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / sid,
        intent="worktree micro twitter",
        project_kind="webapp",
    )
    (project / ".otto-queue-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "watcher": None,
                "tasks": {
                    "build-task": {
                        "status": "interrupted",
                        "attempt_run_id": sid,
                        "started_at": "2026-05-04T20:00:00Z",
                        "finished_at": "2026-05-04T20:01:00Z",
                        "duration_s": 60,
                        "cost_usd": 0.12,
                    }
                },
            }
        )
    )

    client = _app_with_project(project)
    resp = client.get(f"/api/run-view/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    # Proof packets still win terminal verdicts, but queue metrics must merge.
    assert body["status"] == "passed"
    assert body["wall_s"] == 200.0
    assert body["control_plane"]["status"] == "interrupted"
    assert body["control_plane"]["conflict"] is True
    assert "Proof says the product passed" in body["control_plane"]["conflict_reason"]


def test_get_run_derives_active_queue_duration(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-worktree"
    session = project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / sid
    _write_minimal_session(session, intent="worktree micro twitter", project_kind="webapp")
    proof = session / "proof-packet.json"
    proof.unlink()
    (session / "spec").mkdir()
    (session / "spec" / "spec.json").write_text(
        json.dumps(
            {
                "intent": "worktree micro twitter",
                "project_kind": "webapp",
                "groups": [{"id": "g1", "name": "G1", "feature_ids": ["f1"]}],
            }
        )
    )
    (session / "spec-state.jsonl").write_text(
        json.dumps(
            {
                "kind": "group.started",
                "group_id": "g1",
                "ts": "2026-05-04T20:00:02Z",
            }
        )
        + "\n"
    )
    (project / ".otto-queue-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": {
                    "build-task": {
                        "status": "initializing",
                        "attempt_run_id": sid,
                        "started_at": "2000-01-01T00:00:00Z",
                    }
                },
            }
        )
    )

    client = _app_with_project(project)
    resp = client.get(f"/api/run-view/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "building"
    assert body["wall_s"] > 1.0
    assert body["groups"][0]["status"] == "in_progress"


def test_run_view_logs_and_files_resolve_worktree_session(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-worktree"
    session = project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / sid
    _write_minimal_session(session, intent="worktree micro twitter", project_kind="webapp")
    (session / "spec-state.jsonl").write_text('{"kind":"group.started"}\n')
    compile_agent = session / "spec" / "compile-agent"
    compile_agent.mkdir(parents=True)
    (compile_agent / "narrative.log").write_text("compile log\n")
    (compile_agent / "messages.jsonl").write_text('{"message":"compile"}\n')

    client = _app_with_project(project)
    logs = client.get(f"/api/run-view/{sid}/logs")
    assert logs.status_code == 200
    log_paths = [item["path"] for item in logs.json()["logs"]]
    assert log_paths[0] == "spec-state.jsonl"
    assert "spec/compile-agent/narrative.log" in log_paths
    assert "spec/compile-agent/messages.jsonl" in log_paths

    files = client.get(f"/api/run-view/{sid}/files")
    assert files.status_code == 200
    assert "proof-packet.json" in {item["path"] for item in files.json()["files"]}


def test_run_view_group_logs_filter_to_group_build_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-worktree"
    session = project / ".worktrees" / "build-task" / "otto_logs" / "sessions" / sid
    _write_minimal_session(session, intent="worktree micro twitter", project_kind="webapp")
    foundation_log = session / "build" / "foundation" / "attempt-01" / "live.log"
    timeline_log = session / "build" / "timeline" / "attempt-01" / "live.log"
    foundation_log.parent.mkdir(parents=True)
    timeline_log.parent.mkdir(parents=True)
    foundation_log.write_text("building foundation\n", encoding="utf-8")
    timeline_log.write_text("building timeline\n", encoding="utf-8")

    client = _app_with_project(project)
    logs = client.get(f"/api/run-view/{sid}/logs", params={"group_id": "foundation"})

    assert logs.status_code == 200
    paths = {item["path"] for item in logs.json()["logs"]}
    assert paths == {"build/foundation/attempt-01/live.log"}
    assert "building foundation" in logs.json()["logs"][0]["text"]


def test_proof_packet_rewrites_and_serves_worktree_artifact_links(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-worktree"
    worktree = project / ".worktrees" / "build-task"
    session = worktree / "otto_logs" / "sessions" / sid
    _write_minimal_session(session, intent="worktree micro twitter", project_kind="webapp")
    artifact = worktree / "otto_artifacts" / "browser" / "shot.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fake-png")
    (session / "proof-packet.html").write_text(
        '<a href="../../../otto_artifacts/browser/shot.png">'
        '<img src="../../../otto_artifacts/browser/shot.png" alt="shot"></a>',
        encoding="utf-8",
    )

    client = _app_with_project(project)
    html = client.get(f"/api/run-view/{sid}/proof-packet.html")
    assert html.status_code == 200
    assert f"/api/run-view/{sid}/evidence?path=..%2F..%2F..%2Fotto_artifacts%2Fbrowser%2Fshot.png" in html.text

    image = client.get(
        f"/api/run-view/{sid}/evidence",
        params={"path": "../../../otto_artifacts/browser/shot.png"},
    )
    assert image.status_code == 200
    assert image.content == b"fake-png"


def test_run_view_diff_returns_worktree_diff_against_main(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@otto.local"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Otto Tester"], cwd=project, check=True)
    (project / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=project, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "build/task", ".worktrees/build-task", "main"],
        cwd=project,
        check=True,
    )
    worktree = project / ".worktrees" / "build-task"
    (worktree / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "add app"], cwd=worktree, check=True)
    sid = "2026-05-04-200000-worktree"
    _write_minimal_session(
        worktree / "otto_logs" / "sessions" / sid,
        intent="worktree diff",
        project_kind="webapp",
    )

    client = _app_with_project(project)
    diff = client.get(f"/api/run-view/{sid}/diff")

    assert diff.status_code == 200
    assert "Base: main" in diff.text
    assert "app.py" in diff.text


def test_run_view_diff_includes_live_untracked_group_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@otto.local"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Otto Tester"], cwd=project, check=True)
    (project / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=project, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "build/task", ".worktrees/build-task", "main"],
        cwd=project,
        check=True,
    )
    worktree = project / ".worktrees" / "build-task"
    sid = "2026-05-04-200000-worktree"
    session = worktree / "otto_logs" / "sessions" / sid
    (session / "spec").mkdir(parents=True)
    (session / "spec" / "spec.json").write_text(
        json.dumps(
            {
                "intent": "build a micro twitter",
                "project_kind": "webapp",
                "groups": [
                    {
                        "id": "foundation",
                        "name": "Foundation",
                        "owned_paths": ["src/App.tsx", "server.js"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    app_file = worktree / "src" / "App.tsx"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("export default function App() { return <main>Microfeed</main>; }\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("not in this group\n", encoding="utf-8")

    client = _app_with_project(project)
    diff = client.get(f"/api/run-view/{sid}/diff", params={"group_id": "foundation"})

    assert diff.status_code == 200
    assert "Scope: group foundation" in diff.text
    assert "Untracked files:" in diff.text
    assert "src/App.tsx" in diff.text
    assert "+export default function App()" in diff.text
    assert "outside.txt" not in diff.text


def test_group_logs_and_diff_are_real_endpoints(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-groups"
    session = project / "otto_logs" / "sessions" / sid
    session.mkdir(parents=True)
    (session / "spec").mkdir()
    (session / "spec" / "spec.json").write_text(
        json.dumps(
            {
                "intent": "group endpoints",
                "project_kind": "webapp",
                "groups": [{"id": "g1", "name": "G1", "feature_ids": ["f1"]}],
            }
        )
    )
    (session / "spec-state.jsonl").write_text(
        json.dumps(
            {
                "kind": "group.started",
                "group_id": "g1",
                "extra": {"branch": "i2p/session/g1"},
            }
        )
        + "\n"
    )
    g1_logs = session / "build" / "g1" / "attempt-01"
    g1_logs.mkdir(parents=True)
    (g1_logs / "narrative.log").write_text("g1 log\n")
    g2_logs = session / "build" / "g2" / "attempt-01"
    g2_logs.mkdir(parents=True)
    (g2_logs / "narrative.log").write_text("g2 log\n")

    project.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "test@otto.local"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Otto Tester"], check=True)
    (project / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(project), "checkout", "-qb", "i2p/session/g1"], check=True)
    (project / "README.md").write_text("base\ng1\n")
    subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "g1"], check=True)

    client = _app_with_project(project)
    logs = client.get(f"/api/run-view/{sid}/groups/g1/logs")
    assert logs.status_code == 200
    log_paths = {item["path"] for item in logs.json()["logs"]}
    assert "build/g1/attempt-01/narrative.log" in log_paths
    assert "build/g2/attempt-01/narrative.log" not in log_paths

    diff = client.get(f"/api/run-view/{sid}/groups/g1/diff")
    assert diff.status_code == 200
    body = diff.json()
    assert body["error"] is None
    assert body["branch"] == "i2p/session/g1"
    assert "+g1" in body["diff"]


def test_run_view_skips_symlinked_session_escape(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-escape"
    sessions_root = project / "otto_logs" / "sessions"
    sessions_root.mkdir(parents=True)
    outside_session = tmp_path / "outside-session"
    _write_minimal_session(outside_session, intent="escaped", project_kind="webapp")
    try:
        (sessions_root / sid).symlink_to(outside_session, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    client = _app_with_project(project)
    listed = client.get("/api/run-view")
    assert listed.status_code == 200
    assert sid not in listed.json()["runs"]

    detail = client.get(f"/api/run-view/{sid}")
    assert detail.status_code == 404


def test_run_view_files_truncated_only_when_more_files_exist(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    sid = "2026-05-04-200000-files"
    session = project / "otto_logs" / "sessions" / sid
    _write_minimal_session(session, intent="file list", project_kind="webapp")
    for index in range(299):
        (session / f"extra-{index:03d}.txt").write_text("x")

    client = _app_with_project(project)
    exact = client.get(f"/api/run-view/{sid}/files")
    assert exact.status_code == 200
    assert len(exact.json()["files"]) == 300
    assert exact.json()["truncated"] is False

    (session / "extra-over-limit.txt").write_text("x")
    over = client.get(f"/api/run-view/{sid}/files")
    assert over.status_code == 200
    assert len(over.json()["files"]) == 300
    assert over.json()["truncated"] is True


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
