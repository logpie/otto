from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto import paths
from otto.checkpoint import write_checkpoint
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
from otto.runs.history import append_history_snapshot, build_terminal_snapshot
from otto.runs.registry import make_run_record, update_record, write_record
from otto.spec import spec_hash

from tests._web_mc_helpers import (
    _app,
    _client,
    _client_for_app,
    _init_repo,
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
    assert row["cost_display"] == "1.3K tokens"
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

def test_web_live_run_detail_avoids_full_history_refresh(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)

    def fail_if_history_is_loaded(_project_dir: Path) -> list[dict]:
        raise AssertionError("live run detail should not rebuild history")

    monkeypatch.setattr(
        "otto.mission_control.model.load_project_history_rows",
        fail_if_history_is_loaded,
    )

    detail = _client(repo).get("/api/runs/build-web").json()

    assert detail["run_id"] == "build-web"
    assert detail["source"] == "live"
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
    assert row["cost_display"] == "1.3K tokens"
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

def test_web_state_marks_queued_compat_task_waiting_not_active(tmp_path: Path) -> None:
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

    row = state["live"]["items"][0]
    assert row["run_id"] == "queue-compat:queued-task"
    assert row["status"] == "queued"
    assert row["display_status"] == "queued"
    assert row["active"] is False
    assert state["live"]["active_count"] == 0
    assert state["live"]["refresh_interval_s"] == 1.5
    assert state["watcher"]["counts"]["queued"] == 1

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
