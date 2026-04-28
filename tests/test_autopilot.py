from __future__ import annotations

from pathlib import Path

import pytest

from otto.mission_control.service import MissionControlService
from otto.mission_control.autopilot import AutopilotController

from tests._web_mc_helpers import _append_queue_task, _client, _init_repo


@pytest.fixture(autouse=True)
def skip_web_bundle_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTTO_WEB_SKIP_FRESHNESS", "1")


def test_autopilot_defaults_to_assisted_and_surfaces_queued_work(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", resolved_intent="add export")

    state = _client(repo).get("/api/state").json()
    autopilot = state["autopilot"]

    assert autopilot["mode"] == "assisted"
    assert autopilot["enabled"] is True
    assert autopilot["health"] == "attention"
    assert autopilot["incidents"][0]["kind"] == "queued_without_runner"
    assert autopilot["pending_decisions"] == []
    assert autopilot["next_tick_hint"] == "Watching 1 issue and applying policy."


def test_autopilot_assisted_tick_creates_recovery_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", resolved_intent="add export")
    client = _client(repo)

    tick = client.post("/api/autopilot/tick", json={}).json()
    assert tick["ok"] is True
    assert tick["mode"] == "assisted"
    assert len(tick["pending"]) == 1
    assert tick["pending"][0]["action"] == "start_watcher"

    status = client.get("/api/autopilot").json()
    assert status["pending_decisions"][0]["action"] == "start_watcher"
    assert status["counters"]["decisions_pending"] == 1


def test_autopilot_full_executes_safe_recovery_once(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", resolved_intent="add export")
    calls: list[dict[str, object]] = []

    def fake_start_watcher(self: MissionControlService, *, concurrent=None, exit_when_empty=False):  # noqa: ANN001
        calls.append({"concurrent": concurrent, "exit_when_empty": exit_when_empty})
        return {"ok": True, "message": "watcher launch requested", "refresh": True}

    monkeypatch.setattr(MissionControlService, "start_watcher", fake_start_watcher)
    client = _client(repo)

    assert client.post("/api/autopilot/mode", json={"mode": "full"}).json()["ok"] is True
    tick = client.post("/api/autopilot/tick", json={}).json()

    assert tick["ok"] is True
    assert tick["mode"] == "full"
    assert calls == [{"concurrent": None, "exit_when_empty": False}]
    assert tick["executed"][0]["action"] == "start_watcher"
    assert tick["executed"][0]["status"] == "executed"


def test_autopilot_emergency_stop_turns_off_and_clears_pending(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", resolved_intent="add export")
    client = _client(repo)

    client.post("/api/autopilot/tick", json={})
    assert client.get("/api/autopilot").json()["pending_decisions"]

    stopped = client.post("/api/autopilot/emergency-stop", json={}).json()
    status = client.get("/api/autopilot").json()

    assert stopped["ok"] is True
    assert status["mode"] == "off"
    assert status["pending_decisions"] == []
    assert status["next_tick_hint"].startswith("Paused")


def test_autopilot_does_not_treat_ready_to_land_as_recovery(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    status = AutopilotController(repo).status({
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 1}},
        "live": {"items": []},
    })

    assert status["incidents"] == []
    assert status["pending_decisions"] == []
    assert status["health"] == "idle"
