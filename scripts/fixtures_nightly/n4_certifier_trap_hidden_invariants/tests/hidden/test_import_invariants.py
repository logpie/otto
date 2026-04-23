from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import login


CSV_TEXT = "import_key,title\nrow-1,Imported backlog item\nrow-2,Imported QA task\n"


def test_import_keeps_rows_in_current_tenant(client: TestClient) -> None:
    login(client, "ava", "pw-ava")
    response = client.post("/tasks/import", json={"csv": CSV_TEXT})
    assert response.status_code == 200

    acme_titles = {task["title"] for task in client.get("/tasks").json()["tasks"]}
    assert "Imported backlog item" in acme_titles

    with TestClient(client.app) as other:
        login(other, "gary", "pw-gary")
        globex_titles = {task["title"] for task in other.get("/tasks").json()["tasks"]}
        assert "Imported backlog item" not in globex_titles


def test_import_is_idempotent(client: TestClient) -> None:
    login(client, "ava", "pw-ava")
    first = client.post("/tasks/import", json={"csv": CSV_TEXT})
    second = client.post("/tasks/import", json={"csv": CSV_TEXT})
    assert first.status_code == 200
    assert second.status_code == 200
    tasks = client.get("/tasks").json()["tasks"]
    imported = [task for task in tasks if task["import_key"] in {"row-1", "row-2"}]
    assert len(imported) == 2
