from __future__ import annotations

import subprocess
from pathlib import Path

from tests._web_mc_helpers import _client, _init_repo


def test_web_project_launcher_starts_without_selected_project(tmp_path: Path) -> None:
    host = tmp_path / "host"
    projects_root = tmp_path / "managed"
    _init_repo(host)

    client = _client(host, project_launcher=True, projects_root=projects_root)

    projects = client.get("/api/projects").json()
    assert projects["launcher_enabled"] is True
    assert projects["projects_root"] == str(projects_root.resolve())
    assert projects["current"] is None
    assert projects["projects"] == []

    state = client.get("/api/state")
    assert state.status_code == 409
    assert "No project selected" in state.json()["message"]


def test_web_projects_endpoint_has_no_root_side_effect_without_launcher(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    projects_root = tmp_path / "managed"
    _init_repo(repo)

    response = _client(repo, projects_root=projects_root).get("/api/projects")

    assert response.status_code == 200
    payload = response.json()
    assert payload["launcher_enabled"] is False
    assert payload["current"]["path"] == str(repo.resolve())
    assert payload["projects"] == []
    assert not projects_root.exists()


def test_web_project_launcher_creates_managed_git_project(tmp_path: Path) -> None:
    host = tmp_path / "host"
    projects_root = tmp_path / "managed"
    _init_repo(host)

    client = _client(host, project_launcher=True, projects_root=projects_root)
    response = client.post("/api/projects/create", json={"name": "Expense Approval Portal"})

    assert response.status_code == 200
    payload = response.json()
    project_path = projects_root / "expense-approval-portal"
    assert payload["project"]["path"] == str(project_path.resolve())
    assert payload["project"]["branch"] == "main"
    assert payload["project"]["dirty"] is False
    assert (project_path / ".git").exists()
    assert (project_path / "README.md").read_text(encoding="utf-8").startswith("# Expense Approval Portal")
    assert (project_path / "otto.yaml").exists()
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=project_path, text=True).strip()
    assert payload["project"]["head_sha"] == head

    state = client.get("/api/state").json()
    assert state["project"]["path"] == str(project_path.resolve())
    assert payload["projects"][0]["path"] == str(project_path.resolve())


def test_web_project_launcher_can_clear_selected_project(tmp_path: Path) -> None:
    host = tmp_path / "host"
    projects_root = tmp_path / "managed"
    _init_repo(host)

    client = _client(host, project_launcher=True, projects_root=projects_root)
    created = client.post("/api/projects/create", json={"name": "Expense Approval Portal"}).json()
    assert created["project"]["name"] == "expense-approval-portal"

    cleared = client.post("/api/projects/clear", json={})

    assert cleared.status_code == 200
    payload = cleared.json()
    assert payload["current"] is None
    assert payload["projects"][0]["name"] == "expense-approval-portal"
    state = client.get("/api/state")
    assert state.status_code == 409
    assert "No project selected" in state.json()["message"]


def test_web_project_launcher_rejects_selection_outside_managed_root(tmp_path: Path) -> None:
    host = tmp_path / "host"
    outside = tmp_path / "outside"
    projects_root = tmp_path / "managed"
    _init_repo(host)
    _init_repo(outside)

    client = _client(host, project_launcher=True, projects_root=projects_root)
    response = client.post("/api/projects/select", json={"path": str(outside)})

    assert response.status_code == 403
    assert "Managed projects must live under" in response.json()["message"]
