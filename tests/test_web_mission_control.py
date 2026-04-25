from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from otto import paths
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
from otto.mission_control.events import append_event, events_path
from otto.mission_control.supervisor import record_watcher_launch
from otto.queue.runner import acquire_lock
from otto.queue.schema import QueueTask, append_task, load_queue, write_state as write_queue_state
from otto.runs.history import append_history_snapshot, build_terminal_snapshot
from otto.runs.registry import make_run_record, update_record, write_record
from otto.web.app import create_app


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "web@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Web Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# web\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _set_origin_head(repo: Path, branch: str) -> None:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "update-ref", f"refs/remotes/origin/{branch}", sha], cwd=repo, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"],
        cwd=repo,
        check=True,
    )


def _write_run(repo: Path, *, run_id: str = "build-web", outside_artifact: str | None = None) -> None:
    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("BUILD starting\nSTORY_RESULT: web PASS\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build web",
        status="running",
        cwd=repo,
        source={
            "argv": ["build", "web"],
            "provider": "codex",
            "model": "gpt-5.4",
            "reasoning_effort": "medium",
        },
        git={"branch": "main", "worktree": None},
        intent={"summary": "build the web surface"},
        artifacts={
            "summary_path": outside_artifact or str(summary_path),
            "primary_log_path": str(primary_log),
        },
        metrics={
            "cost_usd": 0.0,
            "input_tokens": 1234,
            "cached_input_tokens": 1000,
            "output_tokens": 56,
        },
        adapter_key="atomic.build",
        last_event="running tests",
    )
    write_record(repo, record)


def test_web_state_detail_logs_and_artifact_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)

    client = TestClient(create_app(repo))
    state = client.get("/api/state").json()
    assert state["project"]["branch"] == "main"
    assert state["live"]["active_count"] == 1
    row = state["live"]["items"][0]
    assert row["provider"] == "codex"
    assert row["model"] == "gpt-5.4"
    assert row["reasoning_effort"] == "medium"
    assert row["cost_display"] == "1.2K in / 56 out"

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

    client = TestClient(create_app(repo))

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
    app = create_app(repo)
    clock = {"now": datetime(2026, 4, 24, tzinfo=timezone.utc), "monotonic": 0.0}
    app.state.service.model._now_fn = lambda: clock["now"]
    app.state.service.model._monotonic_fn = lambda: clock["monotonic"]
    client = TestClient(app)

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

    client = TestClient(create_app(repo))
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

    state = TestClient(create_app(repo)).get("/api/state").json()

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

    client = TestClient(create_app(repo))
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
    assert detail["review_packet"]["failure"]["reason"] == "old failure"


def test_web_state_exposes_landing_queue_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
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

    state = TestClient(create_app(repo)).get("/api/state").json()

    assert state["landing"]["counts"] == {"ready": 1, "merged": 1, "blocked": 0, "total": 2}
    by_id = {item["task_id"]: item for item in state["landing"]["items"]}
    assert by_id["ready-task"]["landing_state"] == "ready"
    assert by_id["ready-task"]["label"] == "Ready to land"
    assert by_id["ready-task"]["run_id"] == "run-ready"
    assert by_id["ready-task"]["stories_passed"] == 2
    assert by_id["merged-task"]["landing_state"] == "merged"
    assert by_id["merged-task"]["label"] == "Landed"
    assert by_id["merged-task"]["merge_id"] == "merge-merged"

    detail = TestClient(create_app(repo)).get("/api/runs/run-merged").json()
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert detail["landing_state"] == "merged"
    assert detail["review_packet"]["headline"] == "Already merged into main"
    assert detail["review_packet"]["readiness"]["state"] == "merged"
    assert detail["review_packet"]["checks"][-1]["detail"] == "Task is already landed."
    assert detail["review_packet"]["next_action"]["enabled"] is False
    assert actions["m"]["enabled"] is False
    assert actions["m"]["reason"] == "Already merged into main."


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

    response = TestClient(create_app(repo)).post("/api/runs/run-merged/actions/merge", json={})

    assert response.status_code == 409
    assert response.json()["message"] == "Already merged into main."
    assert calls == []


def test_web_landing_ignores_merge_state_for_different_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
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

    state = TestClient(create_app(repo)).get("/api/state").json()

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

    state = TestClient(create_app(repo)).get("/api/state").json()

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

    client = TestClient(create_app(repo))
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
    assert packet["next_action"]["label"] == "merge selected"


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
    client = TestClient(create_app(repo))
    state = client.get("/api/state").json()
    detail = client.get("/api/runs/run-ready").json()

    assert "build/missing" in state["landing"]["items"][0]["diff_error"]
    assert "build/missing" in detail["review_packet"]["changes"]["diff_error"]
    assert detail["review_packet"]["readiness"]["state"] == "blocked"
    assert detail["review_packet"]["readiness"]["tone"] == "danger"
    checks = {check["key"]: check for check in detail["review_packet"]["checks"]}
    assert checks["changes"]["status"] == "fail"
    assert "build/missing" in checks["landing"]["detail"]


def test_web_landing_target_preserves_detected_branch_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_origin_head(repo, "fix/codex-provider-i2p")
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

    state = TestClient(create_app(repo)).get("/api/state").json()

    assert state["landing"]["target"] == "fix/codex-provider-i2p"
    assert state["landing"]["counts"]["ready"] == 1


def test_web_landing_blocks_merge_when_project_has_tracked_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
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

    state = TestClient(create_app(repo)).get("/api/state").json()

    assert state["landing"]["counts"]["ready"] == 1
    assert state["landing"]["merge_blocked"] is True
    assert "working tree has unstaged changes" in state["landing"]["merge_blockers"]
    assert state["landing"]["dirty_files"] == ["README.md"]


def test_web_merge_all_rejects_dirty_project_before_launch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("# web\n\nlocal runtime state\n", encoding="utf-8")

    response = TestClient(create_app(repo)).post("/api/actions/merge-all", json={})

    assert response.status_code == 409
    assert "Merge blocked by local repository state" in response.json()["message"]
    assert "README.md" in response.json()["message"]


def test_web_artifact_content_rejects_paths_outside_project(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo, outside_artifact="/etc/passwd")

    client = TestClient(create_app(repo))
    artifacts = client.get("/api/runs/build-web/artifacts").json()["artifacts"]
    outside = next(item for item in artifacts if item["label"] == "summary")
    response = client.get(f"/api/runs/build-web/artifacts/{outside['index']}/content")
    assert response.status_code == 403
    assert response.json()["message"] == "artifact path is outside the project"


def test_web_queue_build_enqueues_without_click_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = TestClient(create_app(repo))
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

    hidden = client.get("/api/state?type=queue&query=unmatched").json()
    assert hidden["live"]["items"] == []

    matching = client.get("/api/state?type=queue&query=saved").json()
    assert matching["live"]["items"][0]["queue_task_id"] == "saved-searches"


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

    state = TestClient(create_app(repo)).get("/api/state").json()
    item = state["landing"]["items"][0]

    assert item["queue_status"] == "queued"
    assert item["diff_error"] is None
    assert item["changed_file_count"] == 0


def test_web_records_queue_events_and_exposes_operator_timeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = TestClient(create_app(repo))
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

    state = TestClient(create_app(repo)).get("/api/state").json()

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

    events = TestClient(create_app(repo)).get("/api/events").json()

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

    detail = TestClient(create_app(repo)).get("/api/runs/run-history").json()

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

    state = TestClient(create_app(repo)).get("/api/state?type=merge").json()

    assert state["history"]["items"][0]["cost_display"] == "2.0K in / 300 out"


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

    client = TestClient(create_app(repo))
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

    client = TestClient(create_app(repo))
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

    client = TestClient(create_app(repo))
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
        json.dumps({"command_id": "cmd-1", "run_id": "run-1"}) + "\n",
        encoding="utf-8",
    )

    state = TestClient(create_app(repo)).get("/api/state").json()
    labels = [issue["label"] for issue in state["runtime"]["issues"]]

    assert state["runtime"]["status"] == "attention"
    assert state["runtime"]["command_backlog"]["processing"] == 1
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

    def fake_kill(target_pid: int, sig: int) -> None:
        if sig == 0:
            return
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)

    try:
        client = TestClient(create_app(repo))
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

    def fake_kill(target_pid: int, sig: int) -> None:
        if sig == 0:
            return
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.queue.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)

    response = TestClient(create_app(repo)).post("/api/watcher/stop", json={})

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

    def fake_kill(target_pid: int, sig: int) -> None:
        if sig == 0:
            return
        signals.append((target_pid, sig))

    monkeypatch.setattr("otto.queue.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.runtime.os.kill", fake_kill)
    monkeypatch.setattr("otto.mission_control.service.os.kill", fake_kill)

    response = TestClient(create_app(repo)).post("/api/watcher/stop", json={})

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

    client = TestClient(create_app(repo))
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

    client = TestClient(create_app(repo))
    state = client.get("/api/state").json()

    assert state["watcher"]["health"]["state"] == "stopped"
    assert state["watcher"]["health"]["lock_pid"] is None
    assert state["watcher"]["health"]["blocking_pid"] is None


def test_web_reports_held_queue_lock_as_stale_runtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock = acquire_lock(repo)
    try:
        client = TestClient(create_app(repo))
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
        client = TestClient(create_app(repo))
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

    client = TestClient(create_app(repo))
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


def test_web_start_watcher_reports_started_when_state_becomes_alive(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
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

    client = TestClient(create_app(repo))
    response = client.post("/api/watcher/start", json={"concurrent": 2})

    assert response.status_code == 200
    assert response.json()["message"] == "watcher started"
    assert response.json()["supervisor"]["watcher_pid"] == pid
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "watcher.started"


def test_web_start_watcher_records_immediate_failure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    class _FailedPopen:
        pid = 12345
        returncode = 42

        def __init__(self, argv, **kwargs) -> None:
            pass

        def poll(self):
            return self.returncode

    monkeypatch.setattr("otto.mission_control.service.subprocess.Popen", _FailedPopen)

    client = TestClient(create_app(repo))
    response = client.post("/api/watcher/start", json={"concurrent": 2})

    assert response.status_code == 500
    assert "watcher exited immediately with 42" in response.json()["message"]
    event = client.get("/api/events").json()["items"][0]
    assert event["kind"] == "watcher.start.failed"
    assert event["severity"] == "error"
