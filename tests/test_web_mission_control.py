from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto import paths
from otto.checkpoint import write_checkpoint
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
from otto.mission_control.actions import ActionResult
from otto.mission_control.events import append_event, events_path
from otto.mission_control.supervisor import record_watcher_launch
from otto.queue.runner import acquire_lock
from otto.queue.schema import QueueTask, append_task, load_queue, write_state as write_queue_state
from otto.runs.history import append_history_snapshot, build_terminal_snapshot
from otto.runs.registry import make_run_record, update_record, write_record
from otto.spec import spec_hash

from tests._web_mc_helpers import (
    _app,
    _client,
    _client_for_app,
    _create_branch_file,
    _init_repo,
    _set_origin_head,
    _write_run,
)


def test_web_detail_exposes_split_phase_routing_and_timeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "split-routing"
    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("done\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build split routing",
        status="done",
        cwd=repo,
        source={
            "argv": [
                "build",
                "split routing",
                "--split",
                "--provider",
                "claude",
                "--build-provider",
                "codex",
                "--certifier-provider",
                "codex",
                "--fix-provider",
                "codex",
                "--fix-effort",
                "high",
            ],
        },
        git={"branch": "main", "worktree": None},
        intent={"summary": "split routing"},
        artifacts={"summary_path": str(summary_path), "primary_log_path": str(primary_log)},
        metrics={
            "breakdown": {
                "build": {"duration_s": 10, "total_tokens": 100},
                "certify": {"duration_s": 5, "rounds": 1},
                "fix": {"duration_s": 3, "cost_usd": 0.12},
            },
        },
        adapter_key="atomic.build",
        last_event="completed",
    )
    record.terminal_outcome = "success"
    write_record(repo, record)

    detail = _client(repo).get(f"/api/runs/{run_id}").json()

    assert detail["build_config"]["split_mode"] is True
    assert detail["build_config"]["agents"]["build"]["provider"] == "codex"
    assert detail["build_config"]["agents"]["certifier"]["provider"] == "codex"
    assert detail["build_config"]["agents"]["fix"]["provider"] == "codex"
    assert detail["build_config"]["agents"]["fix"]["reasoning_effort"] == "high"
    phases = {item["phase"]: item for item in detail["phase_timeline"]}
    assert phases["build"]["provider"] == "codex"
    assert phases["build"]["token_usage"]["total_tokens"] == 100
    assert phases["certify"]["provider"] == "codex"
    assert phases["certify"]["rounds"] == 1
    assert phases["fix"]["cost_usd"] == 0.12


def test_web_detail_exposes_improve_split_as_evaluate_and_improve(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "improve-routing"
    primary_log = paths.improve_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("done\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="improve",
        command="improve feature",
        display_name="improve feature",
        status="done",
        cwd=repo,
        source={
            "argv": [
                "improve",
                "feature",
                "search UX",
                "--split",
                "--provider",
                "claude",
                "--certifier-provider",
                "claude",
                "--improver-provider",
                "codex",
                "--improver-effort",
                "high",
            ],
        },
        git={"branch": "improve/test", "worktree": None},
        intent={"summary": "improve search UX"},
        artifacts={"summary_path": str(summary_path), "primary_log_path": str(primary_log)},
        metrics={
            "breakdown": {
                "certify": {"duration_s": 5, "rounds": 1},
                "fix": {"duration_s": 8, "total_tokens": 200},
            },
        },
        adapter_key="atomic.improve",
        last_event="completed",
    )
    record.terminal_outcome = "success"
    write_record(repo, record)

    detail = _client(repo).get(f"/api/runs/{run_id}").json()

    assert detail["build_config"]["command_family"] == "improve"
    assert detail["build_config"]["provider"] == "codex"
    assert detail["build_config"]["agents"]["fix"]["provider"] == "codex"
    assert detail["build_config"]["agents"]["fix"]["reasoning_effort"] == "high"
    phases = detail["phase_timeline"]
    assert [item["phase"] for item in phases] == ["certify", "fix"]
    assert [item["label"] for item in phases] == ["Evaluate", "Improve / fix"]
    assert phases[1]["provider"] == "codex"
    assert phases[1]["token_usage"]["total_tokens"] == 200


def test_web_state_detail_logs_and_artifact_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)

    client = _client(repo)
    state = client.get("/api/state").json()
    assert state["project"]["branch"] == "main"
    assert state["live"]["active_count"] == 1
    row = state["live"]["items"][0]
    assert row["provider"] == "codex"
    assert row["model"] == "gpt-5.4"
    assert row["reasoning_effort"] == "medium"
    assert row["cost_display"] == "1.2K in / 56 out"
    assert row["token_usage"]["cached_input_tokens"] == 1000
    assert state["project_stats"]["total_tokens"] == 1290
    assert state["project_stats"]["token_display"] == "1.3K tokens"
    assert row["progress"] == "STORY_RESULT: web PASS"

    detail = client.get("/api/runs/build-web").json()
    assert detail["title"].startswith("build:")
    assert any(action["key"] == "c" for action in detail["legal_actions"])

    logs = client.get("/api/runs/build-web/logs?offset=0").json()
    assert "STORY_RESULT: web PASS" in logs["text"]
    assert logs["next_offset"] > 0

    artifacts = client.get("/api/runs/build-web/artifacts").json()["artifacts"]
    summary = next(item for item in artifacts if item["label"] == "summary")
    content = client.get(f"/api/runs/build-web/artifacts/{summary['index']}/content").json()
    assert '"passed"' in content["content"]


def test_web_run_detail_is_not_hidden_by_list_filters(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)

    client = _client(repo)

    detail = client.get("/api/runs/build-web?type=merge&query=no-match").json()

    assert detail["run_id"] == "build-web"
    assert detail["title"].startswith("build:")


def test_web_state_marks_abandoned_live_runs_stale_not_active(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo, run_id="stale-web")
    heartbeat_at = "2026-04-24T00:00:00Z"
    update_record(
        repo,
        "stale-web",
        heartbeat=False,
        updates={
            "writer": {"pid": 999999, "pgid": 999999, "writer_id": "dead"},
            "timing": {
                "started_at": heartbeat_at,
                "updated_at": heartbeat_at,
                "heartbeat_at": heartbeat_at,
                "heartbeat_interval_s": 2.0,
                "heartbeat_seq": 1,
            },
        },
    )
    app = _app(repo)
    clock = {"now": datetime(2026, 4, 24, tzinfo=timezone.utc), "monotonic": 0.0}
    app.state.service.model._now_fn = lambda: clock["now"]
    app.state.service.model._monotonic_fn = lambda: clock["monotonic"]
    client = _client_for_app(app)

    client.get("/api/state")
    clock["now"] += timedelta(seconds=16)
    clock["monotonic"] += 16
    state = client.get("/api/state").json()

    row = state["live"]["items"][0]
    assert state["live"]["active_count"] == 0
    assert state["live"]["refresh_interval_s"] == 1.5
    assert row["status"] == "running"
    assert row["display_status"] == "stale"
    assert row["active"] is False
    assert row["elapsed_display"] == "-"
    assert row["cost_display"] == "1.2K in / 56 out"
    assert row["last_event"] == "heartbeat stalled and writer identity is gone"

    detail = client.get("/api/runs/stale-web").json()
    assert detail["display_status"] == "stale"
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert actions["c"]["enabled"] is False
    assert actions["x"]["enabled"] is True


def test_web_state_marks_abandoned_legacy_queue_runs_stale(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="stale-task",
            command_argv=["build", "stale task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="stale task",
            branch="build/stale-task",
            worktree=".worktrees/stale-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "stale-task": {
                    "status": "running",
                    "attempt_run_id": "stale-task-run",
                    "started_at": "2026-04-24T00:00:00Z",
                    "child": {"pid": 999999, "pgid": 999999},
                }
            },
        },
    )
    client = _client(repo)
    state = client.get("/api/state").json()

    row = state["live"]["items"][0]
    assert row["run_id"] == "stale-task-run"
    assert row["status"] == "running"
    assert row["display_status"] == "stale"
    assert row["active"] is False
    assert state["watcher"]["counts"]["stale"] == 1
    landing = {item["task_id"]: item for item in state["landing"]["items"]}
    assert landing["stale-task"]["queue_status"] == "stale"
    assert landing["stale-task"]["label"] == "Needs attention"

    detail = client.get("/api/runs/stale-task-run").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert actions["c"]["enabled"] is False
    assert actions["x"]["label"] == "remove"
    assert actions["x"]["enabled"] is True


def test_web_state_marks_abandoned_starting_queue_runs_stale(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="starting-task",
            command_argv=["build", "starting task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="starting task",
            branch="build/starting-task",
            worktree=".worktrees/starting-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "starting-task": {
                    "status": "starting",
                    "attempt_run_id": "starting-task-run",
                    "started_at": "2026-04-24T00:00:00Z",
                    "child": None,
                }
            },
        },
    )

    state = _client(repo).get("/api/state").json()

    row = state["live"]["items"][0]
    assert row["run_id"] == "starting-task-run"
    assert row["display_status"] == "stale"
    assert row["active"] is False
    assert state["watcher"]["counts"]["stale"] == 1


def test_web_keeps_failed_queue_tasks_inspectable_for_requeue(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="failed-task",
            command_argv=["build", "failed task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="failed task",
            branch="build/failed-task",
            worktree=".worktrees/failed-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "failed-task": {
                    "status": "failed",
                    "attempt_run_id": "failed-task-run",
                    "started_at": "2026-04-24T00:00:00Z",
                    "finished_at": "2026-04-24T00:01:00Z",
                    "failure_reason": "old failure",
                }
            },
        },
    )
    watcher_log = repo / "otto_logs" / "web" / "watcher.log"
    watcher_log.parent.mkdir(parents=True, exist_ok=True)
    watcher_log.write_text(
        "[failed-task] Fatal Python error: init_sys_streams: can't initialize sys standard streams\n"
        "[failed-task] OSError: [Errno 9] Bad file descriptor\n"
        "[01:00:01] reaped failed-task: failed (exit_code=1)\n",
        encoding="utf-8",
    )

    client = _client(repo)
    state = client.get("/api/state").json()

    assert [(item["display_id"], item["display_status"]) for item in state["live"]["items"]] == [("failed-task", "failed")]
    assert state["live"]["active_count"] == 0
    assert state["landing"]["items"][0]["run_id"] == "failed-task-run"

    detail = client.get("/api/runs/failed-task-run?type=merge&query=unmatched").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert detail["run_id"] == "failed-task-run"
    assert detail["display_status"] == "failed"
    assert actions["R"]["label"] == "requeue"
    assert actions["R"]["enabled"] is True
    assert detail["review_packet"]["next_action"]["label"] == "requeue"
    assert detail["review_packet"]["failure"]["reason"] == "Fatal Python error: init_sys_streams: can't initialize sys standard streams"
    assert "Bad file descriptor" in detail["review_packet"]["failure"]["excerpt"]
    assert any(artifact["label"] == "watcher log" for artifact in detail["artifacts"])

    logs = client.get("/api/runs/failed-task-run/logs?offset=0").json()
    assert logs["exists"] is True
    assert logs["path"] == str(watcher_log)
    assert "Primary session log was not created" in logs["text"]
    assert "Bad file descriptor" in logs["text"]


def test_web_failed_queue_run_with_checkpoint_prefers_resume(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    task = QueueTask(
        id="failed-task",
        command_argv=["build", "ship it"],
        added_at="2026-04-24T00:00:00Z",
        resolved_intent="ship it",
        branch="build/failed-task",
        worktree=".worktrees/failed-task",
        resumable=True,
    )
    append_task(repo, task)
    session_id = "failed-task-run"
    worktree = repo / ".worktrees" / "failed-task"
    paths.ensure_session_scaffold(worktree, session_id)
    paths.session_checkpoint(worktree, session_id).write_text(
        json.dumps({"status": "in_progress", "updated_at": "2026-04-24T00:01:00Z"}),
        encoding="utf-8",
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "failed-task": {
                    "status": "failed",
                    "attempt_run_id": session_id,
                    "started_at": "2026-04-24T00:00:00Z",
                    "finished_at": "2026-04-24T00:30:00Z",
                    "failure_reason": "timed out after 1800s (limit 1800s)",
                }
            },
        },
    )

    detail = _client(repo).get("/api/runs/failed-task-run?type=merge").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}

    assert detail["display_status"] == "failed"
    assert actions["r"]["label"] == "resume from checkpoint"
    assert actions["r"]["enabled"] is True
    assert detail["review_packet"]["next_action"]["label"] == "resume from checkpoint"
    assert detail["review_packet"]["next_action"]["action_key"] == "r"


def test_web_paused_spec_review_exposes_approve_and_regenerate_actions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "spec-review-run"
    spec_path = paths.spec_dir(repo, run_id) / "spec.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_content = (
        "**Intent:** add reports\n\n"
        "## Must Have\n- Report list\n\n"
        "## Must NOT Have Yet\n- Billing\n\n"
        "## Success Criteria\n- Tests pass\n"
    )
    spec_path.write_text(spec_content, encoding="utf-8")
    write_checkpoint(
        repo,
        run_id=run_id,
        command="build",
        phase="spec_review",
        status="paused",
        split_mode=True,
        intent="add reports",
        spec_path=str(spec_path),
        spec_hash=spec_hash(spec_content),
    )
    append_task(
        repo,
        QueueTask(
            id="reports",
            command_argv=["build", "add reports", "--spec", "--spec-review-mode", "web"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="add reports",
            branch="build/reports",
            worktree=".",
            resumable=True,
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "reports": {
                    "status": "paused",
                    "attempt_run_id": run_id,
                    "started_at": "2026-04-24T00:00:00Z",
                    "finished_at": "2026-04-24T00:01:00Z",
                    "failure_reason": "approve or request changes in Mission Control",
                }
            },
        },
    )

    client = _client(repo)
    state = client.get("/api/state").json()
    paused_item = next(item for item in state["live"]["items"] if item["run_id"] == run_id)
    assert state["live"]["active_count"] == 0
    assert paused_item["active"] is False

    detail = client.get(f"/api/runs/{run_id}").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}

    assert detail["display_status"] == "paused"
    assert detail["review_packet"]["headline"] == "Spec review required"
    assert detail["review_packet"]["next_action"]["action_key"] == "a"
    assert actions["a"]["enabled"] is True
    assert actions["g"]["enabled"] is True
    assert actions["r"]["enabled"] is False
    assert any(artifact["label"] == "spec" for artifact in detail["artifacts"])

    response = client.post(f"/api/runs/{run_id}/actions/regenerate-spec", json={"note": "Add admin report criteria"})
    assert response.status_code == 200
    decision = json.loads((spec_path.parent / "review-decision.json").read_text(encoding="utf-8"))
    assert decision["action"] == "regenerate"
    assert decision["note"] == "Add admin report criteria"


def test_web_failed_queue_run_prefers_existing_primary_log(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "failed-task-run"
    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("Current task failed because tests failed.\n", encoding="utf-8")
    watcher_log = repo / "otto_logs" / "web" / "watcher.log"
    watcher_log.parent.mkdir(parents=True, exist_ok=True)
    watcher_log.write_text(
        "[failed-task] Fatal Python error: stale old attempt\n"
        "[failed-task] OSError: [Errno 9] stale descriptor\n",
        encoding="utf-8",
    )
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="queue",
        run_type="queue",
        command="build failed task",
        display_name="failed-task",
        status="failed",
        cwd=repo,
        identity={"queue_task_id": "failed-task"},
        git={"branch": "build/failed-task"},
        intent={"summary": "failed task"},
        artifacts={"primary_log_path": str(primary_log)},
        adapter_key="queue.attempt",
        last_event="pytest failed",
    )
    write_record(repo, record)

    client = _client(repo)
    detail = client.get(f"/api/runs/{run_id}").json()
    logs = client.get(f"/api/runs/{run_id}/logs?offset=0").json()

    assert detail["review_packet"]["failure"]["reason"] == "pytest failed"
    assert detail["review_packet"]["failure"]["excerpt"] is None
    assert logs["path"] == str(primary_log)
    assert "Current task failed because tests failed" in logs["text"]


def test_web_failed_queue_fallback_uses_latest_exact_task_block(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="failed-task",
            command_argv=["build", "failed task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="failed task",
            branch="build/failed-task",
            worktree=".worktrees/failed-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "failed-task": {
                    "status": "failed",
                    "attempt_run_id": "failed-task-run",
                    "started_at": "2026-04-24T00:00:00Z",
                    "finished_at": "2026-04-24T00:01:00Z",
                    "failure_reason": "exit_code=1",
                }
            },
        },
    )
    watcher_log = repo / "otto_logs" / "web" / "watcher.log"
    watcher_log.parent.mkdir(parents=True, exist_ok=True)
    watcher_log.write_text(
        "[failed-task] Fatal Python error: stale old attempt\n"
        "[failed-task-extra] Fatal Python error: prefix collision\n"
        "[failed-task] OSError: [Errno 9] current descriptor\n"
        "[01:00:01] reaped failed-task: failed (exit_code=1)\n",
        encoding="utf-8",
    )

    detail = _client(repo).get("/api/runs/failed-task-run").json()

    assert detail["review_packet"]["failure"]["reason"] == "OSError: [Errno 9] current descriptor"
    assert "prefix collision" not in detail["review_packet"]["failure"]["excerpt"]


def test_web_cleaned_failed_queue_history_is_audit_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="run-cleaned-failed",
            domain="queue",
            run_type="queue",
            command="build failed task",
            intent_meta={"summary": "failed task"},
            status="failed",
            terminal_outcome="failure",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            source={"resumable": True},
            identity={"queue_task_id": "cleaned-task"},
            extra_fields={"last_event": "failure"},
        ),
    )

    client = _client(repo)
    state = client.get("/api/state").json()
    detail = client.get("/api/runs/run-cleaned-failed").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}

    assert state["live"]["items"] == []
    assert state["landing"]["items"] == []
    assert state["runtime"]["status"] == "healthy"
    assert actions["x"]["enabled"] is False
    assert actions["x"]["reason"] == "queue task already cleaned up"
    assert detail["review_packet"]["next_action"]["label"] == "No action"
    assert detail["review_packet"]["next_action"]["enabled"] is False


def test_web_state_exposes_landing_queue_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    append_task(
        repo,
        QueueTask(
            id="merged-task",
            command_argv=["build", "merged task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="merged task",
            branch="build/merged-task",
            worktree=".worktrees/merged-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 2,
                    "stories_tested": 2,
                },
                "merged-task": {
                    "status": "done",
                    "attempt_run_id": "run-merged",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged")],
        ),
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["counts"] == {"ready": 1, "merged": 1, "blocked": 0, "total": 2}
    by_id = {item["task_id"]: item for item in state["landing"]["items"]}
    assert by_id["ready-task"]["landing_state"] == "ready"
    assert by_id["ready-task"]["label"] == "Ready to land"
    assert by_id["ready-task"]["run_id"] == "run-ready"
    assert by_id["ready-task"]["stories_passed"] == 2
    assert by_id["merged-task"]["landing_state"] == "merged"
    assert by_id["merged-task"]["label"] == "Landed"
    assert by_id["merged-task"]["merge_id"] == "merge-merged"

    detail = _client(repo).get("/api/runs/run-merged").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert detail["landing_state"] == "merged"
    assert detail["review_packet"]["headline"] == "Already merged into main"
    assert detail["review_packet"]["readiness"]["state"] == "merged"
    assert detail["review_packet"]["checks"][-1]["detail"] == "Task is already landed."
    assert detail["review_packet"]["next_action"]["enabled"] is False
    assert actions["m"]["enabled"] is False
    assert actions["m"]["reason"] == "Already merged into main."


def test_web_landed_task_uses_merge_state_diff_after_source_branch_deleted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/merged-task"], cwd=repo, check=True)
    (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
    subprocess.run(["git", "add", "merged.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add merged task"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "--no-ff", "-m", "land merged task", "build/merged-task"], cwd=repo, check=True)
    merge_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "branch", "-D", "build/merged-task"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="merged-task",
            command_argv=["build", "merged task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="merged task",
            branch="build/merged-task",
            worktree=".worktrees/merged-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "merged-task": {
                    "status": "done",
                    "attempt_run_id": "run-merged",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged", merge_commit=merge_commit)],
        ),
    )

    client = _client(repo)
    item = client.get("/api/state").json()["landing"]["items"][0]
    packet = client.get("/api/runs/run-merged").json()["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert item["landing_state"] == "merged"
    assert item["diff_error"] is None
    assert item["changed_file_count"] == 1
    assert item["changed_files"] == ["merged.txt"]
    assert packet["readiness"]["state"] == "merged"
    assert packet["changes"]["diff_error"] is None
    assert packet["changes"]["diff_command"].startswith("git diff ")
    assert packet["changes"]["files"] == ["merged.txt"]
    assert checks["changes"]["status"] == "pass"
    assert checks["changes"]["detail"] == "1 file landed into main."

    diff = client.get("/api/runs/run-merged/diff").json()
    assert diff["file_count"] == 1
    assert diff["files"] == ["merged.txt"]
    assert "+merged" in diff["text"]


def test_web_merge_action_rejects_already_merged_task(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="run-merged",
        domain="queue",
        run_type="queue",
        command="build merged",
        display_name="merged-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "merged-task"},
        git={"branch": "build/merged-task"},
        intent={"summary": "merged"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged")],
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr("otto.mission_control.service.execute_action", lambda *args, **kwargs: calls.append("called"))
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )

    response = _client(repo).post("/api/runs/run-merged/actions/merge", json={})

    assert response.status_code == 409
    assert response.json()["message"] == "Already merged into main."
    assert calls == []


def test_web_merge_action_reports_already_merged_before_dirty_repo(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="run-merged",
        domain="queue",
        run_type="queue",
        command="build merged",
        display_name="merged-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "merged-task"},
        git={"branch": "build/merged-task"},
        intent={"summary": "merged"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged")],
        ),
    )
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": True, "merge_blockers": ["dirty"], "dirty_files": ["README.md"]},
    )

    response = _client(repo).post("/api/runs/run-merged/actions/merge", json={})

    assert response.status_code == 409
    assert response.json()["message"] == "Already merged into main."


def test_web_landing_ignores_merge_state_for_different_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {"ready-task": {"status": "done", "attempt_run_id": "run-ready"}},
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-other-target",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="release",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/ready-task"],
            outcomes=[BranchOutcome(branch="build/ready-task", status="merged")],
        ),
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["target"] == "main"
    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["items"][0]["landing_state"] == "ready"


def test_web_landing_ignores_unreachable_merge_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
    branch_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {"ready-task": {"status": "done", "attempt_run_id": "run-ready"}},
        },
    )
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-unreachable",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/ready-task"],
            outcomes=[BranchOutcome(branch="build/ready-task", status="merged", merge_commit=branch_sha)],
        ),
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["counts"]["merged"] == 0
    assert state["landing"]["items"][0]["landing_state"] == "ready"


def test_web_landing_and_detail_show_review_packet_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )

    client = _client(repo)
    state = client.get("/api/state").json()
    item = state["landing"]["items"][0]

    assert item["changed_file_count"] == 1
    assert item["changed_files"] == ["feature.txt"]

    detail = client.get("/api/runs/run-ready").json()
    packet = detail["review_packet"]
    assert packet["headline"] == "Ready for review"
    assert packet["readiness"] == {
        "state": "ready",
        "label": "Ready to land in main",
        "tone": "success",
        "blockers": [],
        "next_step": "Review evidence and land the task.",
    }
    checks = {check["key"]: check for check in packet["checks"]}
    assert checks["run"]["status"] == "pass"
    assert checks["certification"]["detail"] == "1/1 stories passed."
    assert checks["changes"]["status"] == "pass"
    assert checks["landing"]["detail"] == "Safe to land into main."
    assert packet["certification"]["stories_passed"] == 1
    assert packet["certification"]["stories_tested"] == 1
    assert packet["changes"]["files"] == ["feature.txt"]
    assert packet["changes"]["diff_command"] == "git diff main...build/ready-task"
    assert packet["next_action"]["label"] == "Land selected"
    assert packet["next_action"]["action_key"] == "m"

    diff = client.get("/api/runs/run-ready/diff").json()
    assert diff["command"] == "git diff main...build/ready-task"
    assert diff["files"] == ["feature.txt"]
    assert diff["file_count"] == 1
    assert diff["error"] is None
    assert "+ready" in diff["text"]


def test_web_landing_surfaces_diff_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/missing",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {"ready-task": {"status": "done", "attempt_run_id": "run-ready"}},
        },
    )
    client = _client(repo)
    state = client.get("/api/state").json()
    detail = client.get("/api/runs/run-ready").json()

    assert state["landing"]["counts"]["ready"] == 0
    assert state["landing"]["counts"]["blocked"] == 1
    assert state["landing"]["items"][0]["landing_state"] == "blocked"
    assert state["landing"]["items"][0]["label"] == "Review blocked"
    assert "build/missing" in state["landing"]["items"][0]["diff_error"]
    assert "build/missing" in detail["review_packet"]["changes"]["diff_error"]
    assert detail["review_packet"]["headline"] == "Review blocked before landing"
    assert detail["review_packet"]["readiness"]["state"] == "blocked"
    assert detail["review_packet"]["readiness"]["tone"] == "danger"
    assert detail["review_packet"]["next_action"] == {
        "label": "Land blocked",
        "action_key": None,
        "enabled": False,
        "reason": "Resolve review blockers before landing.",
    }
    checks = {check["key"]: check for check in detail["review_packet"]["checks"]}
    assert checks["changes"]["status"] == "fail"
    assert "build/missing" in checks["landing"]["detail"]


def test_web_landing_target_preserves_detected_branch_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_origin_head(repo, "fix/codex-provider-i2p")
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["target"] == "fix/codex-provider-i2p"
    assert state["landing"]["counts"]["ready"] == 1


def test_web_landing_blocks_merge_when_project_has_tracked_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_file(repo, "build/ready-task", "ready.txt")
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    state = _client(repo).get("/api/state").json()

    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["merge_blocked"] is True
    assert "working tree has unstaged changes" in state["landing"]["merge_blockers"]
    assert state["landing"]["dirty_files"] == ["README.md"]


def test_web_review_packet_blocks_landing_when_project_has_tracked_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/ready-task"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add feature"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch="build/ready-task",
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": "run-ready",
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    detail = _client(repo).get("/api/runs/run-ready").json()
    packet = detail["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert packet["headline"] == "Repository cleanup required before landing"
    assert packet["readiness"]["state"] == "blocked"
    assert packet["readiness"]["tone"] == "danger"
    assert packet["next_action"] == {
        "label": "Land blocked",
        "action_key": None,
        "enabled": False,
        "reason": "Commit, stash, or revert local project changes before landing.",
    }
    assert "Repository has local changes: README.md." in packet["readiness"]["blockers"]
    assert checks["landing"]["status"] == "fail"
    assert "README.md" in checks["landing"]["detail"]


def test_web_merge_all_rejects_dirty_project_before_launch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    response = _client(repo).post("/api/actions/merge-all", json={})

    assert response.status_code == 409
    assert "Merge blocked by local repository state" in response.json()["message"]
    assert "README.md" in response.json()["message"]


def test_web_runtime_issue_prefers_recovery_for_interrupted_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {
            "merge_blocked": True,
            "merge_blockers": ["repository has unmerged paths: app.py", "repository has merge in progress"],
            "dirty_files": ["app.py"],
        },
    )

    state = _client(repo).get("/api/state").json()

    assert state["runtime"]["issues"][0]["label"] == "Landing recovery available"
    issue = next(item for item in state["runtime"]["issues"] if item["label"] == "Landing recovery available")
    assert "Recover landing" in issue["next_action"]


def test_web_merge_recovery_routes_record_actions(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[str] = []

    def _fake_abort(project_dir):
        calls.append(f"abort:{project_dir}")
        return ActionResult(ok=True, message="aborted", refresh=True)

    def _fake_recover(project_dir, *, post_result=None):
        del post_result
        calls.append(f"recover:{project_dir}")
        return ActionResult(ok=True, message="recovery launched", refresh=True)

    monkeypatch.setattr("otto.mission_control.service.execute_merge_abort", _fake_abort)
    monkeypatch.setattr("otto.mission_control.service.execute_merge_recover", _fake_recover)
    client = _client(repo)

    abort = client.post("/api/actions/merge-abort", json={})
    recover = client.post("/api/actions/merge-recover", json={})

    assert abort.status_code == 200
    assert abort.json()["message"] == "aborted"
    assert recover.status_code == 200
    assert recover.json()["message"] == "recovery launched"
    assert calls == [f"abort:{repo.resolve()}", f"recover:{repo.resolve()}"]


def test_web_resolve_release_recovers_interrupted_merge(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[str] = []

    monkeypatch.setattr(
        "otto.mission_control.service.MissionControlService.landing_status",
        lambda self: {
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
            "items": [],
            "merge_blocked": True,
            "merge_blockers": ["repository has merge in progress"],
            "dirty_files": ["app.py"],
        },
    )
    monkeypatch.setattr(
        "otto.mission_control.service.execute_merge_recover",
        lambda project_dir, *, post_result=None: calls.append(str(project_dir)) or ActionResult(ok=True, message="recovery launched", refresh=True),
    )

    response = _client(repo).post("/api/actions/resolve-release", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "recovery launched"
    assert calls == [str(repo.resolve())]


def test_web_resolve_release_cleans_superseded_failed_tasks(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "otto.mission_control.service.MissionControlService.landing_status",
        lambda self: {
            "counts": {"ready": 0, "merged": 1, "blocked": 1, "total": 2},
            "items": [
                {
                    "task_id": "old-task",
                    "queue_status": "failed",
                    "landing_state": "blocked",
                    "summary": "Add CSV export",
                },
                {
                    "task_id": "redo-task",
                    "queue_status": "done",
                    "landing_state": "merged",
                    "summary": "Add CSV export",
                },
            ],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
        },
    )
    monkeypatch.setattr(
        "otto.mission_control.service.execute_queue_cleanup",
        lambda project_dir, task_ids, *, post_result=None: calls.append(list(task_ids)) or ActionResult(ok=True, message="cleanup launched", refresh=True),
    )

    response = _client(repo).post("/api/actions/resolve-release", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "cleanup launched"
    assert calls == [["old-task"]]


def test_web_artifact_content_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo, outside_artifact="/etc/passwd")

    client = _client(repo)
    artifacts = client.get("/api/runs/build-web/artifacts").json()["artifacts"]
    outside = next(item for item in artifacts if item["label"] == "summary")
    response = client.get(f"/api/runs/build-web/artifacts/{outside['index']}/content")
    assert response.status_code == 403
    assert response.json()["message"] == "artifact path is outside the project"


def test_web_queue_build_enqueues_without_click_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = _client(repo)
    response = client.post(
        "/api/queue/build",
        json={
            "intent": "add saved searches",
            "as": "saved-searches",
            "extra_args": ["--provider", "codex", "--model", "gpt-5.4", "--effort", "medium"],
        },
    )
    assert response.status_code == 200
    assert response.json()["task"]["id"] == "saved-searches"
    tasks = load_queue(repo)
    assert [task.id for task in tasks] == ["saved-searches"]
    assert tasks[0].command_argv == [
        "build",
        "add saved searches",
        "--provider",
        "codex",
        "--model",
        "gpt-5.4",
        "--effort",
        "medium",
    ]

    state = client.get("/api/state?type=queue").json()
    row = state["live"]["items"][0]
    assert row["queue_task_id"] == "saved-searches"
    assert row["provider"] == "codex"
    assert row["model"] == "gpt-5.4"
    assert row["reasoning_effort"] == "medium"
    assert row["build_config"]["provider"] == "codex"
    assert row["build_config"]["certifier_mode"] == "fast"
    assert row["build_config"]["queue"]["task_timeout_s"] == 4200.0

    hidden = client.get("/api/state?type=queue&query=unmatched").json()
    assert hidden["live"]["items"] == []

    matching = client.get("/api/state?type=queue&query=saved").json()
    assert matching["live"]["items"][0]["queue_task_id"] == "saved-searches"


def test_web_queue_build_spec_defaults_to_web_review_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/build",
        json={
            "intent": "add reports",
            "as": "reports",
            "extra_args": ["--spec", "--split"],
        },
    )

    assert response.status_code == 200
    task = load_queue(repo)[0]
    assert task.command_argv == [
        "build",
        "add reports",
        "--spec",
        "--split",
        "--spec-review-mode",
        "web",
    ]
    config = _client(repo).get("/api/state").json()["live"]["items"][0]["build_config"]
    assert config["planning"] == "spec_review"


def test_web_queue_accepts_split_mode_and_phase_provider_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/build",
        json={
            "intent": "add saved searches",
            "as": "saved-searches",
            "extra_args": [
                "--split",
                "--provider",
                "claude",
                "--build-provider",
                "codex",
                "--certifier-provider",
                "claude",
                "--fix-provider",
                "codex",
                "--fix-effort",
                "high",
            ],
        },
    )

    assert response.status_code == 200
    task = load_queue(repo)[0]
    assert "--split" in task.command_argv
    state = _client(repo).get("/api/state").json()
    config = state["live"]["items"][0]["build_config"]
    assert config["split_mode"] is True
    assert config["agents"]["build"]["provider"] == "codex"
    assert config["agents"]["certifier"]["provider"] == "claude"
    assert config["agents"]["fix"]["provider"] == "codex"
    assert config["agents"]["fix"]["reasoning_effort"] == "high"


def test_web_queue_accepts_improve_improver_provider_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/improve",
        json={
            "subcommand": "feature",
            "focus": "improve search UX",
            "as": "improve-search",
            "extra_args": [
                "--split",
                "--provider",
                "claude",
                "--certifier-provider",
                "claude",
                "--improver-provider",
                "codex",
                "--improver-effort",
                "high",
            ],
        },
    )

    assert response.status_code == 200
    task = load_queue(repo)[0]
    assert "--improver-provider" in task.command_argv
    state = _client(repo).get("/api/state").json()
    config = state["live"]["items"][0]["build_config"]
    assert config["command_family"] == "improve"
    assert config["provider"] == "codex"
    assert config["agents"]["fix"]["provider"] == "codex"
    assert config["agents"]["fix"]["reasoning_effort"] == "high"


def test_web_queue_rejects_unknown_after_dependency(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/improve",
        json={
            "subcommand": "feature",
            "focus": "add saved views",
            "as": "saved-views",
            "after": ["missing-task"],
        },
    )

    assert response.status_code == 400
    assert "after references unknown task(s): ['missing-task']" in response.json()["message"]
    assert load_queue(repo) == []


def test_web_queue_rejects_invalid_inner_command_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    response = _client(repo).post(
        "/api/queue/improve",
        json={
            "subcommand": "feature",
            "focus": "add saved views",
            "extra_args": ["--fast"],
        },
    )

    assert response.status_code == 400
    assert "Unsupported options for `otto improve feature`" in response.json()["message"]
    assert "--fast" in response.json()["message"]
    assert load_queue(repo) == []


def test_web_state_exposes_effective_project_defaults(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text(
        "\n".join([
            "provider: codex",
            "model: gpt-5.4",
            "effort: high",
            "certifier_mode: standard",
            "skip_product_qa: true",
            "run_budget_seconds: 2400",
            "spec_timeout: 300",
            "max_certify_rounds: 5",
            "max_turns_per_call: 120",
            "strict_mode: true",
            "split_mode: true",
            "allow_dirty_repo: true",
            "default_branch: main",
            "test_command: uv run pytest",
            "queue:",
            "  concurrent: 4",
            "  worktree_dir: .otto-trees",
            "  on_watcher_restart: fail",
            "  task_timeout_s: 1200",
            "  merge_certifier_mode: thorough",
            "",
        ]),
        encoding="utf-8",
    )

    state = _client(repo).get("/api/state").json()
    defaults = state["project"]["defaults"]

    assert defaults["provider"] == "codex"
    assert defaults["model"] == "gpt-5.4"
    assert defaults["reasoning_effort"] == "high"
    assert defaults["certifier_mode"] == "standard"
    assert defaults["skip_product_qa"] is True
    assert defaults["run_budget_seconds"] == 2400
    assert defaults["spec_timeout"] == 300
    assert defaults["max_certify_rounds"] == 5
    assert defaults["max_turns_per_call"] == 120
    assert defaults["strict_mode"] is True
    assert defaults["split_mode"] is True
    assert defaults["allow_dirty_repo"] is True
    assert defaults["default_branch"] == "main"
    assert defaults["test_command"] == "uv run pytest"
    assert defaults["queue_concurrent"] == 4
    assert defaults["queue_task_timeout_s"] == 1200.0
    assert defaults["queue_worktree_dir"] == ".otto-trees"
    assert defaults["queue_on_watcher_restart"] == "fail"
    assert defaults["queue_merge_certifier_mode"] == "thorough"
    assert defaults["config_file_exists"] is True
    assert defaults["config_error"] is None


def test_web_state_exposes_queue_task_build_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "otto.yaml").write_text(
        "\n".join([
            "provider: claude",
            "model: sonnet",
            "effort: medium",
            "certifier_mode: standard",
            "run_budget_seconds: 3600",
            "max_certify_rounds: 4",
            "queue:",
            "  concurrent: 2",
            "  task_timeout_s: 1500",
            "",
        ]),
        encoding="utf-8",
    )
    append_task(
        repo,
        QueueTask(
            id="configured-task",
            command_argv=[
                "build",
                "configured task",
                "--provider",
                "codex",
                "--model",
                "gpt-5.4",
                "--effort",
                "high",
                "--thorough",
                "--rounds",
                "6",
                "--budget",
                "900",
                "--max-turns",
                "80",
                "--strict",
                "--split",
                "--allow-dirty",
            ],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="configured task",
            branch="build/configured-task",
            worktree=".worktrees/configured-task",
        ),
    )
    write_queue_state(repo, {"schema_version": 1, "watcher": None, "tasks": {}})

    client = _client(repo)
    state = client.get("/api/state").json()
    live_config = state["live"]["items"][0]["build_config"]
    landing_config = state["landing"]["items"][0]["build_config"]

    for config in (live_config, landing_config):
        assert config["provider"] == "codex"
        assert config["model"] == "gpt-5.4"
        assert config["reasoning_effort"] == "high"
        assert config["certifier_mode"] == "thorough"
        assert config["skip_product_qa"] is False
        assert config["run_budget_seconds"] == 900
        assert config["max_certify_rounds"] == 6
        assert config["max_turns_per_call"] == 80
        assert config["strict_mode"] is True
        assert config["split_mode"] is True
        assert config["allow_dirty_repo"] is True
        assert config["queue"]["concurrent"] == 2
        assert config["queue"]["task_timeout_s"] == 1500.0
        assert config["agents"]["build"]["provider"] == "codex"
        assert config["agents"]["certifier"]["provider"] == "codex"

    detail = client.get(f"/api/runs/{state['live']['items'][0]['run_id']}").json()
    assert detail["build_config"]["certifier_mode"] == "thorough"
    assert detail["build_config"]["queue"]["task_timeout_s"] == 1500.0


def test_web_landing_does_not_show_diff_errors_for_queued_future_branches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="queued-task",
            command_argv=["build", "queued task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="queued task",
            branch="build/queued-task",
            worktree=".worktrees/queued-task",
        ),
    )
    write_queue_state(repo, {"schema_version": 1, "watcher": None, "tasks": {}})

    state = _client(repo).get("/api/state").json()
    item = state["landing"]["items"][0]

    assert item["queue_status"] == "queued"
    assert item["diff_error"] is None
    assert item["changed_file_count"] == 0


def test_web_review_packet_does_not_diff_queued_future_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    append_task(
        repo,
        QueueTask(
            id="queued-task",
            command_argv=["build", "queued task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="queued task",
            branch="build/queued-task",
            worktree=".worktrees/queued-task",
        ),
    )
    write_queue_state(repo, {"schema_version": 1, "watcher": None, "tasks": {}})

    client = _client(repo)
    state = client.get("/api/state").json()
    run_id = state["live"]["items"][0]["run_id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    packet = detail["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert detail["display_status"] == "queued"
    assert packet["headline"] == "Waiting for watcher"
    assert packet["readiness"]["label"] == "Queued"
    assert packet["readiness"]["next_step"] == "Start the watcher when you want this queued task to run."
    assert packet["next_action"]["label"] == "Start watcher"
    assert packet["next_action"]["enabled"] is False
    assert packet["changes"]["diff_error"] is None
    assert packet["changes"]["diff_command"] is None
    assert packet["changes"]["file_count"] == 0
    assert packet["changes"]["files"] == []
    assert checks["changes"]["status"] == "pending"
    assert checks["certification"]["status"] == "pending"
    assert checks["evidence"]["status"] == "pending"


def test_web_records_queue_events_and_exposes_operator_timeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = _client(repo)
    response = client.post(
        "/api/queue/build",
        json={
            "intent": "add saved searches",
            "as": "saved-searches",
            "extra_args": ["--provider", "codex", "--effort", "high"],
        },
    )

    assert response.status_code == 200
    state = client.get("/api/state").json()
    event = state["events"]["items"][0]
    assert state["events"]["total_count"] == 1
    assert event["kind"] == "queue.build"
    assert event["severity"] == "success"
    assert event["task_id"] == "saved-searches"
    assert event["message"] == "queued saved-searches"

    endpoint = client.get("/api/events?limit=1").json()
    assert endpoint["items"] == state["events"]["items"][:1]
    assert endpoint["path"].endswith("otto_logs/mission-control/events.jsonl")


def test_web_events_endpoint_reports_malformed_rows_without_breaking_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('not-json\n{"schema_version":[],"message":"old event"}\n', encoding="utf-8")
    append_event(repo, kind="watcher.stop.skipped", message="watcher is not running")

    state = _client(repo).get("/api/state").json()

    assert state["events"]["malformed_count"] == 1
    assert state["events"]["total_count"] == 2
    assert state["events"]["items"][0]["message"] == "watcher is not running"


def test_web_events_tail_preserves_boundary_aligned_rows(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr("otto.mission_control.events.MAX_EVENT_TAIL_BYTES", 120)
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    first = json.dumps({"schema_version": 1, "event_id": "a", "message": "first", "kind": "test", "severity": "info"})
    second = json.dumps({"schema_version": 1, "event_id": "b", "message": "second", "kind": "test", "severity": "info"})
    path.write_text(first + "\n" + second + "\n", encoding="utf-8")
    monkeypatch.setattr("otto.mission_control.events.MAX_EVENT_TAIL_BYTES", len(second) + 1)

    events = _client(repo).get("/api/events").json()

    assert events["truncated"] is True
    assert events["items"][0]["message"] == "second"
    assert events["total_count"] == 1


def test_web_history_detail_recovers_provider_from_manifest_argv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    manifest_path = repo / "otto_logs" / "queue" / "hello-web" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"argv": ["build", "hello", "--provider", "codex", "--effort", "high"]}),
        encoding="utf-8",
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="run-history",
            domain="queue",
            run_type="queue",
            command="build hello",
            intent_meta={"summary": "hello"},
            status="done",
            terminal_outcome="success",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            metrics={"cost_usd": 0.0},
            artifacts={"manifest_path": str(manifest_path)},
            source={"resumable": True},
            identity={"queue_task_id": "hello-web"},
        ),
    )

    detail = _client(repo).get("/api/runs/run-history").json()

    assert detail["provider"] == "codex"
    assert detail["reasoning_effort"] == "high"


def test_web_history_usage_reads_merge_summary_extra_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    summary_path = repo / "otto_logs" / "sessions" / "merge-cert" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "breakdown": {
                    "certify": {
                        "input_tokens": 2048,
                        "cached_input_tokens": 1024,
                        "output_tokens": 300,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="merge-run",
            domain="merge",
            run_type="merge",
            command="merge",
            intent_meta={"summary": "merge 1 branch"},
            status="done",
            terminal_outcome="success",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            artifacts={"extra_log_paths": [str(summary_path)]},
            identity={"merge_id": "merge-run"},
        ),
    )

    state = _client(repo).get("/api/state?type=merge").json()

    assert state["history"]["items"][0]["cost_display"] == "2.0K in / 300 out"


def test_web_project_stats_include_claude_cache_token_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    summary_path = repo / "otto_logs" / "sessions" / "claude-build" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "breakdown": {
                    "build": {
                        "input_tokens": 51,
                        "cache_creation_input_tokens": 84864,
                        "cache_read_input_tokens": 2434281,
                        "output_tokens": 25347,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="claude-build",
            domain="queue",
            run_type="queue",
            command="build",
            intent_meta={"summary": "build with claude"},
            status="done",
            terminal_outcome="success",
            timing={"finished_at": "2026-04-24T00:00:00Z"},
            artifacts={"summary_path": str(summary_path)},
            identity={"queue_task_id": "claude-build"},
        ),
    )

    state = _client(repo).get("/api/state").json()

    usage = state["history"]["items"][0]["token_usage"]
    assert usage["cache_creation_input_tokens"] == 84864
    assert usage["cache_read_input_tokens"] == 2434281
    assert state["project_stats"]["total_tokens"] == 2544543
    assert state["project_stats"]["token_display"] == "2.5M tokens"


def test_web_merge_run_review_packet_is_landing_audit_not_landable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="merge-audit",
        domain="merge",
        run_type="merge",
        command="merge",
        display_name="merge: 1 branch",
        status="done",
        cwd=repo,
        identity={"merge_id": "merge-audit"},
        git={"branch": "main", "target_branch": "main"},
        intent={"summary": "merge 1 branch"},
        adapter_key="merge.run",
    )
    record.terminal_outcome = "success"
    write_record(repo, record)

    detail = _client(repo).get("/api/runs/merge-audit").json()
    packet = detail["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert packet["headline"] == "Landed in main"
    assert packet["readiness"]["state"] == "merged"
    assert packet["readiness"]["next_step"] == "Audit the landing record, artifacts, and final logs if needed."
    assert packet["next_action"] == {
        "label": "No action",
        "action_key": None,
        "enabled": False,
        "reason": "Landing runs are audit records.",
    }
    assert packet["changes"]["file_count"] == 0
    assert packet["changes"]["diff_command"] is None
    assert checks["landing"]["detail"] == "No further landing action is needed."
    assert checks["certification"]["status"] == "info"


def test_web_merge_history_review_packet_uses_persisted_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-release",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="release/1.0",
            target_head_before="abc123",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/release-task"],
            outcomes=[BranchOutcome(branch="build/release-task", status="merged")],
        ),
    )
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id="merge-release",
            domain="merge",
            run_type="merge",
            command="merge",
            intent_meta={"summary": "merge release", "intent_path": None, "spec_path": None},
            status="done",
            terminal_outcome="success",
            identity={"merge_id": "merge-release"},
            artifacts={"session_dir": str(paths.merge_dir(repo) / "merge-release")},
        ),
    )

    packet = _client(repo).get("/api/runs/merge-release?type=merge").json()["review_packet"]

    assert packet["headline"] == "Landed in release/1.0"
    assert packet["changes"]["target"] == "release/1.0"


def test_web_merge_action_uses_fast_merge_and_reports_immediate_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="queue-done",
        domain="queue",
        run_type="queue",
        command="build hello",
        display_name="hello-web",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "hello-web"},
        git={"branch": "build/hello-web"},
        intent={"summary": "hello"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    calls: list[list[str]] = []

    class _FailedPopen:
        returncode = 1

        def __init__(self, argv, **kwargs) -> None:
            calls.append(list(argv))

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", "merge failed"

    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FailedPopen)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )

    client = _client(repo)
    response = client.post("/api/runs/queue-done/actions/merge", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "merge failed" in response.json()["message"]
    assert any(call[-4:] == ["merge", "--fast", "--no-certify", "hello-web"] for call in calls)
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "run.merge"
    assert event["severity"] == "error"
    assert event["run_id"] == "queue-done"


def test_web_merge_action_records_late_background_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record = make_run_record(
        project_dir=repo,
        run_id="queue-done",
        domain="queue",
        run_type="queue",
        command="build hello",
        display_name="hello-web",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "hello-web"},
        git={"branch": "build/hello-web"},
        intent={"summary": "hello"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)
    completed = threading.Event()

    class _LateFailedPopen:
        pid = 23456
        returncode = None

        def __init__(self, argv, **kwargs) -> None:
            pass

        def poll(self):
            return None

        def communicate(self):
            self.returncode = 1
            completed.set()
            return "", "late merge failed"

    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _LateFailedPopen)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )

    client = _client(repo)
    response = client.post("/api/runs/queue-done/actions/merge", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert completed.wait(2)
    events = client.get("/api/events").json()["items"]
    completion = next(event for event in events if event["kind"] == "run.merge.completed")
    assert completion["severity"] == "error"
    assert completion["message"] == "late merge failed"


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
