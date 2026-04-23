from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.outbox import clear


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    clear()
    app = create_app(tmp_path / "n2.db")
    with TestClient(app) as test_client:
        yield test_client
