from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import login


def test_tasks_can_be_filtered_by_label(client: TestClient) -> None:
    login(client, "alice", "pw-alice")
    response = client.get("/tasks", params={"label": "work"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["active_label"] == "work"
    titles = [task["title"] for task in payload["tasks"]]
    assert titles == ["Write release notes"]
