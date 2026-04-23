from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_payments_module_exists_after_merge(client: TestClient) -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "app" / "services" / "payments.py").exists()
    assert not (root / "app" / "services" / "billing.py").exists()


def test_new_logic_survives_after_rename(client: TestClient) -> None:
    from app.services.payments import calculate_charge

    assert calculate_charge("starter", 2, weekend=True) == 2600
    assert calculate_charge("enterprise", 1, weekend=True) == 3000


def test_quote_endpoint_uses_new_module(client: TestClient) -> None:
    response = client.get("/quote", params={"plan": "starter", "seats": 2, "weekend": "true"})
    assert response.status_code == 200
    assert response.json() == {"amount_cents": 2600}


def test_regression_tests_were_added_for_payments_module() -> None:
    root = Path(__file__).resolve().parents[2]
    candidate = root / "tests" / "visible" / "test_payments.py"
    assert candidate.exists()
    assert "app.services.payments" in candidate.read_text(encoding="utf-8")
