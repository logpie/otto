from __future__ import annotations

from fastapi.testclient import TestClient


def test_login_still_works(client: TestClient) -> None:
    response = client.post("/login", json={"email": "alex@example.com", "password": "pw-alex"})
    assert response.status_code == 200
    me = client.get("/me")
    assert me.status_code == 200
    assert "alex@example.com" in me.text
