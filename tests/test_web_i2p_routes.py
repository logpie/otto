"""Tests for otto/web/i2p_routes.py — read-only i2p endpoints."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from otto.audit import AuditResult, AuditVerdict, GroupVerdict
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.render import render_run
from otto.spec_compile import (
    RepoTestCheck,
    Group,
    Spec,
    StructureDecisions,
    persist_spec,
)
from otto.spec_state import emit
from otto.web.app import create_app


def _init_project(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=tmp_path, check=True)
    return tmp_path


def _seed_session(project_dir: Path, session_id: str, *, with_proof: bool = False) -> Path:
    sessions_root = project_dir / "otto_logs" / "sessions"
    session_dir = sessions_root / session_id
    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = Spec(
        intent="A demo todo app",
        project_kind="webapp",
        structure=StructureDecisions(payload={"routes": [{"path": "/", "component": "Home", "key_text": "x"}], "components": [{"name": "Home", "key_text": "x"}]}),
        groups=[
            Group(id="shell", name="App shell", dependencies=[], owned_paths=["src/App.*"], feature_ids=["scaffold"],
                  checks=[RepoTestCheck(command=("npm", "run", "build"), timeout_s=60)]),
            Group(id="counter", name="Counter widget", dependencies=["shell"], owned_paths=["src/Counter.*"], feature_ids=["increment"],
                  checks=[RepoTestCheck(command=("npm", "test"), timeout_s=120)]),
        ],
        non_goals=["multi-user"],
        done_means=["counter persists"],
    )
    persist_spec(spec, spec_dir / "spec.json", allow_initial=True)

    # State journal — emit a few events
    emit(session_dir, "group.started", group_id="shell")
    emit(session_dir, "group.check.finished", group_id="shell", attempt=1, detail="pass")
    emit(session_dir, "group.merge.eligible", group_id="shell")
    emit(session_dir, "group.started", group_id="counter")

    if with_proof:
        # Render a real proof packet
        build_result = BuildResult(
            spec_session_dir=session_dir,
            group_results=[
                GroupResult(group_id="shell", status=GroupStatus.PASSING, attempts=1, branch="b", worktree=project_dir,
                            last_evidence=[Evidence(passed=True, started_at="x", duration_s=0.1, detail="exit=0")]),
                GroupResult(group_id="counter", status=GroupStatus.PASSING, attempts=1, branch="c", worktree=project_dir,
                            last_evidence=[Evidence(passed=True, started_at="x", duration_s=0.1, detail="exit=0")]),
            ],
        )
        merge_result = MergeQueueResult(
            landed_ids=["shell", "counter"],
            results=[
                MergeResult(group_id="shell", status=MergeStatus.LANDED, landed_commit="abc1234"),
                MergeResult(group_id="counter", status=MergeStatus.LANDED, landed_commit="def5678"),
            ],
        )
        audit_result = AuditResult(
            verdict=AuditVerdict.PASSED,
            narrative="audit complete",
            group_verdicts=[GroupVerdict(group_id="shell", passed=True), GroupVerdict(group_id="counter", passed=True)],
        )
        render_run(
            spec, session_dir=session_dir,
            build_result=build_result, merge_result=merge_result, audit_result=audit_result,
            wall_s=120.0, cost_usd=0.42,
        )

    return session_dir


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    project_dir = _init_project(tmp_path)
    app = create_app(project_dir, queue_compat=False)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/i2p/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_empty_when_no_runs(client: TestClient) -> None:
    response = client.get("/api/i2p/sessions")
    assert response.status_code == 200
    assert response.json() == {"sessions": []}


def test_list_sessions_returns_seeded_session(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "2026-05-03-100000-aaa111")
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    response = client.get("/api/i2p/sessions")
    assert response.status_code == 200
    data = response.json()
    assert len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert s["session_id"] == "2026-05-03-100000-aaa111"
    assert s["intent"] == "A demo todo app"
    assert s["project_kind"] == "webapp"
    assert s["group_count"] == 2
    assert s["has_proof_packet_html"] is False


def test_list_sessions_returns_queue_worktree_session(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    worktree = project_dir / ".worktrees" / "build-task"
    _seed_session(worktree, "2026-05-03-100000-worktree")
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    response = client.get("/api/i2p/sessions")
    assert response.status_code == 200
    data = response.json()
    assert [row["session_id"] for row in data["sessions"]] == [
        "2026-05-03-100000-worktree"
    ]

    detail = client.get("/api/i2p/sessions/2026-05-03-100000-worktree")
    assert detail.status_code == 200
    assert detail.json()["spec"]["intent"] == "A demo todo app"


def test_list_sessions_includes_verdict_when_proof_packet_exists(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "2026-05-03-100000-aaa111", with_proof=True)
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    response = client.get("/api/i2p/sessions")
    s = response.json()["sessions"][0]
    assert s["verdict"] == "passed"
    assert s["landed_count"] == 2
    assert s["blocked_count"] == 0
    assert s["wall_s"] == 120.0
    assert s["cost_usd"] == 0.42
    assert s["has_proof_packet_html"] is True


def test_list_sessions_skips_dirs_without_spec(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    # Make a session dir that is NOT an i2p run (no spec.json)
    (project_dir / "otto_logs" / "sessions" / "2026-05-03-non-i2p").mkdir(parents=True)
    _seed_session(project_dir, "2026-05-03-100000-aaa111")
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)
    data = client.get("/api/i2p/sessions").json()
    assert len(data["sessions"]) == 1


# ---------------------------------------------------------------------------
# /api/i2p/sessions/{id}
# ---------------------------------------------------------------------------


def test_get_session_returns_spec_and_state(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1")
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    response = client.get("/api/i2p/sessions/s1")
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "s1"
    assert data["spec"]["intent"] == "A demo todo app"
    assert len(data["spec"]["groups"]) == 2
    assert data["state"] is not None
    assert data["state"]["audit_started"] is False
    group_phases = {ss["group_id"]: ss["phase"] for ss in data["state"]["groups"]}
    # shell got group.merge.eligible → ELIGIBLE phase
    assert group_phases["shell"] == "eligible"
    # counter only got group.started → BUILDING phase
    assert group_phases["counter"] == "building"


def test_get_session_includes_proof_packet_when_present(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1", with_proof=True)
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    data = client.get("/api/i2p/sessions/s1").json()
    assert data["has_proof_packet_html"] is True
    assert data["proof_packet"] is not None
    assert data["proof_packet"]["verdict"] == "passed"


def test_get_session_404_when_missing(client: TestClient) -> None:
    response = client.get("/api/i2p/sessions/does-not-exist")
    assert response.status_code == 404


def test_get_session_rejects_path_traversal(client: TestClient) -> None:
    response = client.get("/api/i2p/sessions/..%2F..%2Fetc%2Fpasswd")
    # Either 400 (rejected) or 404 (not found). Both are acceptable; key is no 200.
    assert response.status_code in {400, 404}


# ---------------------------------------------------------------------------
# /api/i2p/sessions/{id}/proof-packet.{html,json}
# ---------------------------------------------------------------------------


def test_proof_packet_html_served_when_present(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1", with_proof=True)
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    response = client.get("/api/i2p/sessions/s1/proof-packet.html")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "A demo todo app" in response.text
    assert "verdict passed" in response.text


def test_proof_packet_json_served_when_present(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1", with_proof=True)
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    response = client.get("/api/i2p/sessions/s1/proof-packet.json")
    assert response.status_code == 200
    parsed = json.loads(response.text)
    assert parsed["verdict"] == "passed"


def test_proof_packet_404_when_not_yet_produced(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1", with_proof=False)
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)

    assert client.get("/api/i2p/sessions/s1/proof-packet.html").status_code == 404
    assert client.get("/api/i2p/sessions/s1/proof-packet.json").status_code == 404


# ---------------------------------------------------------------------------
# Evidence streaming + path-traversal guard
# ---------------------------------------------------------------------------


def test_evidence_serves_a_file_inside_session_dir(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    session_dir = _seed_session(project_dir, "s1")
    art_path = session_dir / "evidence" / "shot.png"
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)
    response = client.get("/api/i2p/sessions/s1/evidence/evidence/shot.png")
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")


def test_evidence_rejects_path_traversal_outside_session(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1")
    # Create a file OUTSIDE the session dir.
    (project_dir / "otto_logs" / "secrets.txt").write_text("nope", encoding="utf-8")

    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)
    response = client.get("/api/i2p/sessions/s1/evidence/../../secrets.txt")
    # Either 400 (path rejected by guard) or 404 (relpath resolves but file not found inside session)
    assert response.status_code in {400, 404}


def test_evidence_404_for_missing_file(tmp_path: Path) -> None:
    project_dir = _init_project(tmp_path)
    _seed_session(project_dir, "s1")
    app = create_app(project_dir, queue_compat=False)
    client = TestClient(app)
    response = client.get("/api/i2p/sessions/s1/evidence/nope.png")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# /i2p/ HTML page (Step 8a/8b: minimal MC view alongside the old client)
# ---------------------------------------------------------------------------


def test_i2p_index_page_serves_html(client: TestClient) -> None:
    response = client.get("/i2p/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # Minimum viable smoke: the page references /api/i2p/sessions and includes
    # the sessions table header. JS executes in the browser; here we only
    # validate the shell HTML.
    assert "Otto · intent-to-product sessions" in response.text
    assert "/api/i2p/sessions" in response.text


def test_i2p_session_page_serves_same_shell(client: TestClient) -> None:
    response = client.get("/i2p/some-session-id")
    assert response.status_code == 200
    # Same shell — client-side routing renders the detail view.
    assert "Otto · intent-to-product sessions" in response.text


def test_i2p_index_page_no_trailing_slash_also_works(client: TestClient) -> None:
    response = client.get("/i2p")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
