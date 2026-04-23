from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import login


CSV_TEXT = "import_key,title\nrow-1,Import first task\nrow-2,Import second task\n"


def test_csv_import_happy_path(client: TestClient) -> None:
    login(client, "ava", "pw-ava")
    response = client.post("/tasks/import", json={"csv": CSV_TEXT})
    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 2
