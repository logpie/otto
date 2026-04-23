from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import login


def test_tasks_do_not_leak_between_users(client: TestClient) -> None:
    login(client, "alice", "pw-alice")
    alice_tasks = client.get("/tasks").json()["tasks"]
    titles = {task["title"] for task in alice_tasks}
    assert "Bob private roadmap" not in titles


def test_label_summary_is_not_n_plus_one(client: TestClient) -> None:
    login(client, "alice", "pw-alice")
    payload = client.get("/labels").json()
    assert payload["query_count"] <= 3
