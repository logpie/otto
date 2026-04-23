from __future__ import annotations

from fastapi.testclient import TestClient


def test_quote_uses_billing_service(client: TestClient) -> None:
    response = client.get("/quote", params={"plan": "starter", "seats": 2})
    assert response.status_code == 200
    assert response.json() == {"amount_cents": 2000}
