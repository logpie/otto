from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

from otto import paths
from otto.mission_control.supervisor import record_watcher_launch
from otto.queue.runner import acquire_lock
from otto.queue.schema import write_state as write_queue_state

from tests._web_mc_helpers import _client, _init_repo


def test_web_state_includes_watcher_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = _client(repo)
    state = client.get("/api/state").json()
    assert state["watcher"]["alive"] is False
    assert state["watcher"]["counts"]["queued"] == 0
    assert state["watcher"]["health"]["state"] == "stopped"
    assert state["runtime"]["status"] == "healthy"
    assert state["runtime"]["supervisor"]["mode"] == "local-single-user"
    assert state["runtime"]["supervisor"]["can_start"] is True
    assert state["runtime"]["supervisor"]["can_stop"] is False


def test_web_runtime_surfaces_state_and_command_recovery_issues(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".otto-queue.yml").write_text("schema_version: [\n", encoding="utf-8")
    paths.queue_commands_processing_path(repo).write_text(
        json.dumps({"command_id": "cmd-1", "run_id": "run-1", "kind": "cancel", "args": {"task_id": "task-a"}}) + "\n",
        encoding="utf-8",
    )

    state = _client(repo).get("/api/state").json()
    labels = [issue["label"] for issue in state["runtime"]["issues"]]

    assert state["runtime"]["status"] == "attention"
    assert state["runtime"]["command_backlog"]["processing"] == 1
    assert state["runtime"]["command_backlog"]["items"][0]["state"] == "processing"
    assert state["runtime"]["command_backlog"]["items"][0]["task_id"] == "task-a"
    assert "Queue file unreadable" in labels
    assert "Command drain is unfinished" in labels


def test_web_can_stop_stale_but_live_watcher_process(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pid = os.getpid()
    lock = acquire_lock(repo)
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": {
                "pid": pid,
                "pgid": pid,
                "started_at": "2026-04-24T00:00:00Z",
                "heartbeat": "2026-04-24T00:00:00Z",
            },
            "tasks": {},
        },
    )
    signals: list[tuple[int, int]] = []
    killed = False

    def fake_kill(target_pid: int, sig: int) -> None:
        nonlocal killed
        if sig == 0:
            return
        killed = True
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service._safe_getpgid", lambda _pid: None)
    monkeypatch.setattr("otto.mission_control.service._pid_alive", lambda _pid: not killed)

    try:
        client = _client(repo)
        state = client.get("/api/state").json()
        assert state["watcher"]["alive"] is False
        assert state["watcher"]["health"]["state"] == "stale"
        assert state["watcher"]["health"]["blocking_pid"] == pid

        response = client.post("/api/watcher/stop", json={})
    finally:
        lock.close()

    assert response.status_code == 200
    assert response.json()["message"] == "stale watcher stop requested"
    assert signals == [(pid, signal.SIGTERM)]


def test_web_refuses_to_stop_unverified_live_watcher_pid(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pid = os.getpid()
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": {"pid": pid, "pgid": pid, "started_at": now, "heartbeat": now},
            "tasks": {},
        },
    )
    signals: list[tuple[int, int]] = []
    killed = False

    def fake_kill(target_pid: int, sig: int) -> None:
        nonlocal killed
        if sig == 0:
            return
        killed = True
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.queue.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service._safe_getpgid", lambda _pid: None)
    monkeypatch.setattr("otto.mission_control.service._pid_alive", lambda _pid: not killed)

    response = _client(repo).post("/api/watcher/stop", json={})

    assert response.status_code == 409
    assert "could not verify" in response.json()["message"]
    assert signals == []


def test_web_allows_stop_for_supervised_live_watcher_pid(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pid = os.getpid()
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": {"pid": pid, "pgid": pid, "started_at": now, "heartbeat": now},
            "tasks": {},
        },
    )
    record_watcher_launch(
        repo,
        watcher_pid=pid,
        argv=["otto", "queue", "run"],
        log_path=paths.logs_dir(repo) / "web" / "watcher.log",
        concurrent=1,
        exit_when_empty=False,
    )
    signals: list[tuple[int, int]] = []
    killed = False

    def fake_kill(target_pid: int, sig: int) -> None:
        nonlocal killed
        if sig == 0:
            return
        killed = True
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.queue.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service._safe_getpgid", lambda _pid: None)
    monkeypatch.setattr("otto.mission_control.service._pid_alive", lambda _pid: not killed)

    response = _client(repo).post("/api/watcher/stop", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "watcher stop requested"
    assert signals == [(pid, signal.SIGTERM)]


def test_web_does_not_stop_stale_watcher_pid_without_held_lock(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pid = os.getpid()
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": {
                "pid": pid,
                "pgid": pid,
                "started_at": "2026-04-24T00:00:00Z",
                "heartbeat": "2026-04-24T00:00:00Z",
            },
            "tasks": {},
        },
    )
    signals: list[tuple[int, int]] = []

    def fake_kill(target_pid: int, sig: int) -> None:
        if sig == 0:
            return
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)

    client = _client(repo)
    state = client.get("/api/state").json()
    assert state["watcher"]["health"]["state"] == "stopped"
    assert state["watcher"]["health"]["watcher_pid"] == pid
    assert state["watcher"]["health"]["blocking_pid"] is None

    response = client.post("/api/watcher/stop", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "watcher is not running"
    assert signals == []


def test_web_ignores_unheld_queue_lock_pid(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = acquire_lock(repo)
    lock.close()

    client = _client(repo)
    state = client.get("/api/state").json()

    assert state["watcher"]["health"]["state"] == "stopped"
    assert state["watcher"]["health"]["lock_pid"] is None
    assert state["watcher"]["health"]["blocking_pid"] is None


def test_web_reports_held_queue_lock_as_stale_runtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = acquire_lock(repo)
    try:
        client = _client(repo)
        state = client.get("/api/state").json()
    finally:
        lock.close()

    assert state["watcher"]["alive"] is False
    assert state["watcher"]["health"]["state"] == "stale"
    assert state["watcher"]["health"]["lock_pid"] == os.getpid()
    assert state["watcher"]["health"]["blocking_pid"] == os.getpid()
    assert state["runtime"]["supervisor"]["can_start"] is False
    assert state["runtime"]["supervisor"]["can_stop"] is True
    assert state["runtime"]["supervisor"]["stop_target_pid"] == os.getpid()


def test_web_start_watcher_blocks_when_runtime_is_stale(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = acquire_lock(repo)
    calls: list[str] = []

    class _UnexpectedPopen:
        def __init__(self, argv, **kwargs) -> None:
            calls.append("called")

    monkeypatch.setattr("otto.mission_control.service.subprocess.Popen", _UnexpectedPopen)
    try:
        client = _client(repo)
        response = client.post("/api/watcher/start", json={"concurrent": 2})
    finally:
        lock.close()

    assert response.status_code == 409
    assert "Stop the stale watcher" in response.json()["message"]
    assert calls == []
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "watcher.start.blocked"


def test_web_start_watcher_launches_background_process(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text("default_branch: main\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class _FakePopen:
        pid = 12345
        returncode = None

        def __init__(self, argv, **kwargs) -> None:
            calls.append({"argv": argv, "kwargs": kwargs})

        def poll(self):
            return None

    monkeypatch.setattr("otto.mission_control.service.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("otto.mission_control.service.time.sleep", lambda _seconds: None)

    client = _client(repo)
    response = client.post("/api/watcher/start", json={"concurrent": 2, "exit_when_empty": True})

    assert response.status_code == 200
    assert response.json()["message"] == "watcher launch requested"
    events = client.get("/api/events").json()["items"]
    assert events[0]["kind"] == "watcher.launch.requested"
    assert events[0]["details"]["pid"] == 12345
    argv = calls[0]["argv"]
    assert "queue" in argv
    assert "run" in argv
    assert "--no-dashboard" in argv
    assert "--exit-when-empty" in argv
    assert calls[0]["kwargs"]["cwd"] == str(repo.resolve())


def test_web_start_watcher_uses_configured_default_concurrency(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text("default_branch: main\nqueue:\n  concurrent: 4\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class _FakePopen:
        pid = 12345
        returncode = None

        def __init__(self, argv, **kwargs) -> None:
            calls.append({"argv": argv, "kwargs": kwargs})

        def poll(self):
            return None

    monkeypatch.setattr("otto.mission_control.service.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("otto.mission_control.service.time.sleep", lambda _seconds: None)

    client = _client(repo)
    response = client.post("/api/watcher/start", json={})

    assert response.status_code == 200
    argv = calls[0]["argv"]
    assert argv[argv.index("--concurrent") + 1] == "4"


def test_web_start_watcher_reports_started_when_state_becomes_alive(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text("default_branch: main\n", encoding="utf-8")
    pid = os.getpid()

    class _FakePopen:
        returncode = None

        def __init__(self, argv, **kwargs) -> None:
            self.pid = pid

        def poll(self):
            return None

    def fake_sleep(_seconds: float) -> None:
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_queue_state(
            repo,
            {
                "schema_version": 1,
                "watcher": {"pid": pid, "pgid": pid, "started_at": now, "heartbeat": now},
                "tasks": {},
            },
        )

    monkeypatch.setattr("otto.mission_control.service.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("otto.mission_control.service.time.sleep", fake_sleep)

    client = _client(repo)
    response = client.post("/api/watcher/start", json={"concurrent": 2})

    assert response.status_code == 200
    assert response.json()["message"] == "watcher started"
    assert response.json()["supervisor"]["watcher_pid"] == pid
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "watcher.started"


def test_web_start_watcher_records_immediate_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text("default_branch: main\n", encoding="utf-8")

    class _FailedPopen:
        pid = 12345
        returncode = 42

        def __init__(self, argv, **kwargs) -> None:
            pass

        def poll(self):
            return self.returncode

    monkeypatch.setattr("otto.mission_control.service.subprocess.Popen", _FailedPopen)

    client = _client(repo)
    response = client.post("/api/watcher/start", json={"concurrent": 2})

    assert response.status_code == 500
    assert "watcher exited immediately with 42" in response.json()["message"]
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "watcher.start.failed"
    assert event["severity"] == "error"
