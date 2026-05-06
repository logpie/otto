from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone, timedelta
import os

import pytest

from otto.mission_control.service import MissionControlService
from otto.mission_control.autopilot import AutopilotController
from otto.mission_control.runtime import watcher_health
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state

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
    assert autopilot["pending_decisions"][0]["action"] == "start_watcher"
    assert autopilot["next_tick_hint"] == "1 recovery decision awaiting approval."


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


def test_autopilot_approve_returns_top_level_ok(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_queue_task(repo, "queued-task", resolved_intent="add export")
    client = _client(repo)

    def fake_start_watcher(self: MissionControlService, *, concurrent=None, exit_when_empty=False):  # noqa: ANN001
        return {"ok": True, "message": "watcher launch requested", "refresh": True}

    monkeypatch.setattr(MissionControlService, "start_watcher", fake_start_watcher)

    client.post("/api/autopilot/tick", json={})
    decision_id = client.get("/api/autopilot").json()["pending_decisions"][0]["id"]
    approved = client.post(f"/api/autopilot/decisions/{decision_id}/approve", json={}).json()

    assert approved["ok"] is True
    assert approved["message"] == "watcher launch requested"
    assert approved["status"] == "executed"
    assert approved["result"]["message"] == "watcher launch requested"


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


def test_autopilot_proposes_requeue_for_interrupted_landing_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    status = AutopilotController(repo).status({
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 1},
            "items": [{
                "task_id": "certify-existing",
                "run_id": "run-interrupted",
                "summary": "Certify the existing app loads.",
                "build_config": {"command_family": "certify"},
                "queue_status": "interrupted",
                "landing_state": "blocked",
                "changed_file_count": 0,
            }],
        },
        "live": {"items": []},
    })

    assert status["incidents"][0]["kind"] == "landing_interrupted"
    assert status["incidents"][0]["action"] == "requeue"
    assert status["pending_decisions"][0]["action"] == "requeue"
    assert status["pending_decisions"][0]["run_id"] == "run-interrupted"
    assert status["pending_decisions"][0]["action_label"] == "Recover task"
    assert status["pending_decisions"][0]["includes_actions"] == ["requeue", "start_watcher"]
    assert [step["action"] for step in status["pending_decisions"][0]["plan_steps"]] == [
        "requeue",
        "start_watcher",
        "watch_retry",
    ]
    assert status["pending_decisions"][0]["plan_steps"][0]["label"] == "Requeue interrupted task"


def test_autopilot_requeue_failed_copy_is_truthful(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    status = AutopilotController(repo).status({
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 1},
            "items": [{
                "task_id": "build-microfeed",
                "run_id": "run-failed",
                "summary": "Build a microfeed.",
                "build_config": {"command_family": "build"},
                "queue_status": "failed",
                "landing_state": "blocked",
                "changed_file_count": 0,
            }],
        },
        "live": {"items": []},
    })

    decision = status["pending_decisions"][0]
    assert decision["title"] == "Recover failed task"
    assert decision["plan_steps"][0]["label"] == "Requeue failed task"
    assert "failed before verified changes were ready to land" in decision["reason"]
    assert "before producing code changes" not in decision["reason"]
    assert "was failed" not in decision["reason"]


def test_autopilot_ignores_superseded_interrupted_landing_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    status = AutopilotController(repo).status({
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 0, "reviewed": 1},
            "items": [{
                "task_id": "certify-existing",
                "run_id": "run-interrupted",
                "summary": "Certify the existing app loads.",
                "build_config": {"command_family": "certify"},
                "queue_status": "interrupted",
                "landing_state": "blocked",
                "changed_file_count": 0,
                "superseded": True,
                "superseded_by": {"task_id": "certify-existing-2", "run_id": "run-retry"},
            }],
        },
        "live": {"items": []},
    })

    assert status["incidents"] == []
    assert status["pending_decisions"] == []
    assert status["health"] == "idle"


def test_autopilot_ignores_in_progress_landing_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    status = AutopilotController(repo).status({
        "watcher": {"health": {"state": "running"}, "counts": {"queued": 0, "running": 1}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": False}},
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 1},
            "items": [{
                "task_id": "certify-existing-2",
                "run_id": "run-running",
                "summary": "Certify the existing app loads.",
                "build_config": {"command_family": "certify"},
                "queue_status": "running",
                "landing_state": "blocked",
                "changed_file_count": 0,
            }],
        },
        "live": {
            "items": [{
                "display_status": "running",
                "run_id": "run-running",
                "queue_task_id": "certify-existing-2",
                "summary": "Certify the existing app loads.",
            }]
        },
    })

    assert status["incidents"] == []
    assert status["pending_decisions"] == []
    assert status["health"] == "idle"


def test_autopilot_surfaces_failed_post_merge_verification_from_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-proof-failed",
            started_at="2026-04-29T10:00:00Z",
            finished_at="2026-04-29T10:05:00Z",
            target="main",
            target_head_before="abc123",
            status="failed",
            terminal_outcome="failure",
            note="cert FAILED; missing demo proof",
            branches_in_order=["build/a-2026-04-29", "build/b-2026-04-29"],
            outcomes=[
                BranchOutcome(branch="build/a-2026-04-29", status="merged"),
                BranchOutcome(branch="build/b-2026-04-29", status="conflict_resolved"),
            ],
            cert_passed=False,
        ),
    )

    status = AutopilotController(repo).status({
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {"items": []},
        "history": {
            "items": [{
                "domain": "merge",
                "run_type": "merge",
                "status": "failed",
                "terminal_outcome": "failure",
                "run_id": "merge-proof-failed",
                "merge_id": "merge-proof-failed",
                "summary": "merge 2 branch(es)",
            }]
        },
    })

    assert status["health"] == "attention"
    assert status["incidents"][0]["kind"] == "merge_verification_failed"
    assert status["pending_decisions"][0]["action"] == "rerun_merge_verification"
    assert status["pending_decisions"][0]["action_label"] == "Rerun merge verification"
    assert status["pending_decisions"][0]["run_id"] == "merge-proof-failed"
    assert status["pending_decisions"][0]["plan_steps"][0]["action"] == "rerun_merge_verification"


def test_autopilot_approves_failed_merge_verification_rerun(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-proof-failed",
            started_at="2026-04-29T10:00:00Z",
            finished_at="2026-04-29T10:05:00Z",
            target="main",
            target_head_before="abc123",
            status="failed",
            terminal_outcome="failure",
            branches_in_order=["build/a-2026-04-29"],
            outcomes=[BranchOutcome(branch="build/a-2026-04-29", status="merged")],
            cert_passed=False,
        ),
    )
    snapshot = {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {"items": []},
        "history": {
            "items": [{
                "domain": "merge",
                "run_type": "merge",
                "status": "failed",
                "run_id": "merge-proof-failed",
                "merge_id": "merge-proof-failed",
            }]
        },
    }

    class FakeExecutor:
        calls: list[tuple[str, str]] = []

        def rerun_merge_verification(self, merge_id: str, *, verification_policy: str | None = "smart"):  # noqa: ANN001
            self.calls.append((merge_id, str(verification_policy)))
            return {"ok": True, "message": "merge verification rerun requested", "refresh": True}

    controller = AutopilotController(repo)
    decision_id = controller.status(snapshot)["pending_decisions"][0]["id"]
    executor = FakeExecutor()

    approved = controller.approve(decision_id, snapshot, executor)  # type: ignore[arg-type]

    assert approved["ok"] is True
    assert approved["action"] == "rerun_merge_verification"
    assert approved["message"] == "merge verification rerun requested"
    assert executor.calls == [("merge-proof-failed", "smart")]


def test_autopilot_surfaces_stale_running_merge_verification(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-stale-cert",
            started_at="2026-04-29T10:00:00Z",
            finished_at=None,
            target="main",
            target_head_before="abc123",
            status="running",
            terminal_outcome=None,
            branches_in_order=["build/a-2026-04-29"],
            outcomes=[BranchOutcome(branch="build/a-2026-04-29", status="merged")],
            cert_passed=None,
        ),
    )
    controller = AutopilotController(repo)

    status = controller.status({
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {"items": []},
        "history": {
            "items": [{
                "domain": "merge",
                "run_type": "merge",
                "status": "failed",
                "run_id": "merge-stale-cert",
                "merge_id": "merge-stale-cert",
            }]
        },
    })

    assert status["health"] == "attention"
    assert status["incidents"][0]["kind"] == "merge_verification_stale"
    assert status["pending_decisions"][0]["action"] == "rerun_merge_verification"


def test_autopilot_surfaces_recently_executed_requeue_as_blocked_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    snapshot = {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 1},
            "items": [{
                "task_id": "certify-existing",
                "run_id": "run-interrupted",
                "summary": "Certify the existing app loads.",
                "build_config": {"command_family": "certify"},
                "queue_status": "interrupted",
                "landing_state": "blocked",
                "changed_file_count": 0,
            }],
        },
        "live": {"items": []},
    }
    controller = AutopilotController(repo)
    pending = controller.status(snapshot)["pending_decisions"][0]
    controller._append_audit("decision.executed", "success", "requeued", {"decision": pending})

    status = controller.status(snapshot)

    assert status["pending_decisions"] == []
    assert status["decisions"][0]["status"] == "blocked"
    assert status["decisions"][0]["action"] == "requeue"
    assert status["decisions"][0]["incident_id"] == status["incidents"][0]["id"]
    assert status["decisions"][0]["reason"] == "Autopilot already tried this recovery action recently."
    assert status["counters"]["decisions_pending"] == 0


def test_autopilot_keeps_start_runner_actionable_after_prior_attempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    snapshot = {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 1, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {"items": []},
    }
    controller = AutopilotController(repo)
    pending = controller.status(snapshot)["pending_decisions"][0]
    controller._append_audit("decision.executed", "success", "watcher started", {"decision": pending})

    status = controller.status(snapshot)

    assert status["pending_decisions"][0]["action"] == "start_watcher"
    assert status["pending_decisions"][0]["status"] == "pending"


def test_autopilot_approves_synthesized_requeue_decision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    snapshot = {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {
            "merge_blocked": False,
            "counts": {"ready": 0, "blocked": 1},
            "items": [{
                "task_id": "certify-existing",
                "run_id": "run-interrupted",
                "summary": "Certify the existing app loads.",
                "build_config": {"command_family": "certify"},
                "queue_status": "interrupted",
                "landing_state": "blocked",
                "changed_file_count": 0,
            }],
        },
        "live": {"items": []},
    }
    controller = AutopilotController(repo)
    decision_id = controller.status(snapshot)["pending_decisions"][0]["id"]

    class FakeExecutor:
        calls: list[tuple[str, str]] = []

        def execute(self, run_id: str, action: str, **kwargs):  # noqa: ANN001
            self.calls.append((run_id, action))
            return {"ok": True, "message": "requeue requested", "refresh": True}

        def start_watcher(self, *, concurrent=None, exit_when_empty=False):  # noqa: ANN001
            self.calls.append(("queue", "start_watcher"))
            return {"ok": True, "message": "watcher launch requested", "refresh": True}

    executor = FakeExecutor()
    approved = controller.approve(decision_id, snapshot, executor)  # type: ignore[arg-type]

    assert approved["ok"] is True
    assert approved["action"] == "requeue"
    assert approved["message"] == "Requeued task and started the queue runner."
    assert executor.calls == [("run-interrupted", "requeue"), ("queue", "start_watcher")]
    assert [step["action"] for step in approved["result"]["steps"]] == ["requeue", "start_watcher"]


def test_autopilot_ask_pilot_returns_running_without_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    snapshot = {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {
            "items": [{
                "display_status": "failed",
                "run_id": "run-failed",
                "summary": "A run needs recovery.",
                "queue_task_id": "task-failed",
            }]
        },
    }
    started: list[str] = []

    def fake_start(self: AutopilotController, decision: dict, policy: object, *, approved: bool) -> None:  # noqa: ANN001
        started.append(str(decision["id"]))

    monkeypatch.setattr(AutopilotController, "_start_pilot_background", fake_start)
    controller = AutopilotController(repo)
    decision_id = controller.status(snapshot)["pending_decisions"][0]["id"]

    approved = controller.approve(decision_id, snapshot, object())  # type: ignore[arg-type]
    status = controller.status(snapshot)

    assert approved["ok"] is True
    assert approved["status"] == "running"
    assert approved["message"] == "Pilot diagnosis started."
    assert started == [decision_id]
    assert status["pending_decisions"][0]["status"] == "running"
    assert status["next_tick_hint"] == "1 Pilot diagnosis in progress."


def test_autopilot_pilot_proposes_action_before_assisted_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    snapshot = {
        "watcher": {"health": {"state": "stopped"}, "counts": {"queued": 0, "running": 0}},
        "runtime": {"command_backlog": {"processing": 0}, "supervisor": {"can_start": True}},
        "landing": {"merge_blocked": False, "counts": {"ready": 0}, "items": []},
        "live": {
            "items": [{
                "display_status": "failed",
                "run_id": "run-failed",
                "summary": "A run needs recovery.",
                "queue_task_id": "task-failed",
            }]
        },
    }
    controller = AutopilotController(repo)
    decision = controller.status(snapshot)["pending_decisions"][0]
    controller._store_pending_decisions([{**decision, "status": "running"}], controller._classify(snapshot))
    monkeypatch.setattr(
        AutopilotController,
        "_call_pilot",
        lambda self, decision, policy: {  # noqa: ARG005, ANN001
            "action": "requeue",
            "reason": "The failed run produced no code and can be safely retried.",
            "required_verification": "Confirm a fresh run starts.",
        },
    )

    controller._run_pilot_triage_background(
        {**decision, "status": "running"},
        controller._policy(controller._read_state()),
        approved=True,
    )
    status = controller.status(snapshot)
    pending = status["pending_decisions"][0]

    assert pending["id"] == decision["id"]
    assert pending["status"] == "pending"
    assert pending["action"] == "requeue"
    assert pending["action_label"] == "Requeue task"
    assert pending["pilot_plan"]["action"] == "requeue"
    assert "safely retried" in pending["reason"]


def test_autopilot_blocks_unverified_stale_watcher_stop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    status = AutopilotController(repo).status({
        "watcher": {
            "health": {
                "state": "stale",
                "watcher_pid": os.getpid(),
                "blocking_pid": os.getpid(),
                "next_action": "Stop the stale queue runner before starting another one.",
            },
            "counts": {"queued": 0, "running": 0},
        },
        "runtime": {
            "command_backlog": {"processing": 0},
            "supervisor": {"can_stop": False, "can_start": False},
        },
        "landing": {"merge_blocked": False, "counts": {"ready": 0}},
        "live": {"items": []},
        "stale_time": old,
    })

    assert status["incidents"][0]["kind"] == "stale_watcher"
    assert status["incidents"][0]["action"] == "human_required"
    assert status["pending_decisions"] == []
    assert status["decisions"][0]["status"] == "blocked"
    assert status["decisions"][0]["action"] == "human_required"


def test_watcher_health_marks_live_pid_with_stale_heartbeat_as_stale(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "schema_version": 1,
        "watcher": {"pid": os.getpid(), "pgid": os.getpgid(0), "started_at": old, "heartbeat": old},
        "tasks": {},
    }

    health = watcher_health(repo, state)

    assert health["state"] == "stale"
    assert health["blocking_pid"] == os.getpid()
    assert health["watcher_process_alive"] is True
