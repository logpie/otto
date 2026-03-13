# tests/test_manager.py
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_tmp(tmp_path):
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("[]")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>v2</body></html>")

    from manager import create_app
    test_app = create_app(
        base_dir=tmp_path, tasks_file=tasks_file, logs_dir=logs_dir
    )
    return TestClient(test_app)


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


def test_sse_endpoint_returns_event_stream(tmp_path):
    """SSE endpoint should return text/event-stream content type.

    Uses a real uvicorn server because Starlette's TestClient buffers the entire
    response before returning, which hangs on infinite SSE generators.
    """
    import threading
    import time
    import httpx
    import uvicorn

    from manager import create_app
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("[]")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>v2</body></html>")

    sse_app = create_app(
        base_dir=tmp_path, tasks_file=tasks_file, logs_dir=logs_dir
    )

    # Start uvicorn on a free port
    port = 18421
    config = uvicorn.Config(sse_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    for _ in range(20):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/tasks", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    try:
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", timeout=5) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            # Read first event
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    assert "tasks" in data
                    assert "workers" in data
                    break
    finally:
        server.should_exit = True
        thread.join(timeout=3)


def test_full_flow_smoke(app_with_tmp):
    """Smoke test: create task, read it back, verify schema."""
    # Create
    resp = app_with_tmp.post("/api/tasks", json={
        "prompt": "Test task",
        "verify_prompt": "it works",
        "verify_cmd": "echo ok",
    })
    assert resp.status_code == 200
    task = resp.json()
    task_id = task["id"]

    # List
    resp = app_with_tmp.get("/api/tasks")
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["verify_prompt"] == "it works"
    assert tasks[0]["verify_cmd"] == "echo ok"
    assert tasks[0]["cost_usd"] == 0.0
    assert tasks[0]["session_id"] is None

    # Retry
    resp = app_with_tmp.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200

    # Delete
    resp = app_with_tmp.post("/api/tasks", json={"prompt": "Second task"})
    id2 = resp.json()["id"]
    resp = app_with_tmp.delete(f"/api/tasks/{id2}")
    assert resp.status_code == 200
    resp = app_with_tmp.get("/api/tasks")
    assert len(resp.json()) == 1


def test_index_serves_static_html(app_with_tmp):
    """Verify index serves the static HTML file."""
    resp = app_with_tmp.get("/")
    assert resp.status_code == 200
    assert "v2" in resp.text


def test_worker_start_uses_worker_py(app_with_tmp):
    """Verify worker start spawns worker.py, not ralph-loop.sh."""
    from unittest.mock import patch, MagicMock

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.poll.return_value = None

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        resp = app_with_tmp.post("/api/workers/start", json={
            "name": "test",
            "project_dir": "/tmp/test",
        })
        assert resp.status_code == 200
        cmd = mock_popen.call_args[0][0]
        assert "worker.py" in cmd[1]
        assert "ralph-loop.sh" not in " ".join(cmd)


def test_delete_in_progress_task_returns_409(app_with_tmp):
    tasks = [
        {
            "id": "busy",
            "prompt": "running",
            "status": "in_progress",
            "worker": "w1",
        }
    ]
    app_with_tmp.app.state.tasks_file.write_text(json.dumps(tasks, indent=2))

    resp = app_with_tmp.delete("/api/tasks/busy")

    assert resp.status_code == 409
    assert json.loads(app_with_tmp.app.state.tasks_file.read_text())[0]["status"] == "in_progress"


def test_retry_in_progress_task_returns_409(app_with_tmp):
    tasks = [
        {
            "id": "busy",
            "prompt": "running",
            "status": "in_progress",
            "worker": "w1",
            "attempts": 2,
            "cost_usd": 1.23,
            "session_id": "sess-1",
        }
    ]
    app_with_tmp.app.state.tasks_file.write_text(json.dumps(tasks, indent=2))

    resp = app_with_tmp.post("/api/tasks/busy/retry")

    assert resp.status_code == 409
    task = json.loads(app_with_tmp.app.state.tasks_file.read_text())[0]
    assert task["status"] == "in_progress"
    assert task["attempts"] == 2


def test_patch_task_updates_verify_fields(app_with_tmp):
    resp = app_with_tmp.post("/api/tasks", json={"prompt": "Build X"})
    task_id = resp.json()["id"]

    resp = app_with_tmp.patch(f"/api/tasks/{task_id}", json={
        "verify_prompt": "app loads without errors",
        "verify_cmd": "",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["verify_prompt"] == "app loads without errors"
    assert data["verify_cmd"] == ""

    # Verify persisted
    tasks = json.loads(app_with_tmp.app.state.tasks_file.read_text())
    assert tasks[0]["verify_prompt"] == "app loads without errors"


def test_patch_task_rejects_invalid_fields(app_with_tmp):
    resp = app_with_tmp.post("/api/tasks", json={"prompt": "Build X"})
    task_id = resp.json()["id"]

    resp = app_with_tmp.patch(f"/api/tasks/{task_id}", json={
        "status": "completed",
        "prompt": "hacked",
    })
    assert resp.status_code == 400


def test_patch_nonexistent_task_returns_404(app_with_tmp):
    resp = app_with_tmp.patch("/api/tasks/nonexistent", json={
        "verify_prompt": "test",
    })
    assert resp.status_code == 404


def test_stop_worker_requeues_its_in_progress_tasks(app_with_tmp):
    from unittest.mock import MagicMock

    tasks = [
        {
            "id": "busy",
            "prompt": "running",
            "status": "in_progress",
            "started_at": "2026-01-01T00:00:00",
            "finished_at": None,
            "heartbeat_at": "2026-01-01T00:05:00",
            "worker": "w1",
            "attempts": 2,
            "cost_usd": 1.23,
            "session_id": "sess-1",
            "last_error": "boom",
        },
        {
            "id": "other",
            "prompt": "running elsewhere",
            "status": "in_progress",
            "started_at": "2026-01-01T00:00:00",
            "finished_at": None,
            "heartbeat_at": "2026-01-01T00:05:00",
            "worker": "w2",
        },
    ]
    app_with_tmp.app.state.tasks_file.write_text(json.dumps(tasks, indent=2))

    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.return_value = 0
    app_with_tmp.app.state.workers["w1"] = proc

    resp = app_with_tmp.post("/api/workers/w1/stop")

    assert resp.status_code == 200
    assert resp.json()["requeued_tasks"] == 1
    proc.terminate.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=5)
    updated = json.loads(app_with_tmp.app.state.tasks_file.read_text())
    busy = next(task for task in updated if task["id"] == "busy")
    other = next(task for task in updated if task["id"] == "other")
    assert busy["status"] == "pending"
    assert busy["worker"] is None
    assert busy["started_at"] is None
    assert busy["heartbeat_at"] is None
    assert busy["attempts"] == 0
    assert busy["cost_usd"] == 0.0
    assert busy["session_id"] is None
    assert "last_error" not in busy
    assert other["status"] == "in_progress"
