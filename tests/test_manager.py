# tests/test_manager.py
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_tmp(tmp_path):
    """Create a test app with temporary tasks file."""
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("[]")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    import manager
    manager.TASKS_FILE = tasks_file
    manager.LOGS_DIR = logs_dir
    manager.BASE_DIR = tmp_path
    return TestClient(manager.app)


def test_create_task_with_new_fields(app_with_tmp):
    resp = app_with_tmp.post("/api/tasks", json={
        "prompt": "Fix the bug",
        "verify_prompt": "tests must pass",
        "verify_cmd": "pytest -x",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["prompt"] == "Fix the bug"
    assert data["verify_prompt"] == "tests must pass"
    assert data["verify_cmd"] == "pytest -x"
    assert data["cost_usd"] == 0.0
    assert data["session_id"] is None


def test_create_task_backward_compat(app_with_tmp):
    """Old clients sending 'verify' field should still work."""
    resp = app_with_tmp.post("/api/tasks", json={
        "prompt": "Do something",
        "verify": "latency < 500ms",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["verify_prompt"] == "latency < 500ms"


def test_verify_script_endpoints_removed(app_with_tmp):
    resp = app_with_tmp.get("/api/verify-script")
    assert resp.status_code == 404

    resp = app_with_tmp.post("/api/verify-script", json={})
    assert resp.status_code in (404, 405)

    resp = app_with_tmp.post("/api/verify-run", json={})
    assert resp.status_code in (404, 405)
