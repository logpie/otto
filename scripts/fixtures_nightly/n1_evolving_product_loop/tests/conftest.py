from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path / "n1.db")
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/login", json={"username": username, "password": password})
    assert response.status_code == 200
