from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from otto import paths
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
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
    assert row["cost_display"] != "…"
    assert row["last_event"] == "heartbeat stalled and writer identity is gone"

    detail = client.get("/api/runs/stale-web").json()
    assert detail["display_status"] == "stale"
    actions = {action["key"]: action for action in detail["legal_actions"]}
    assert actions["c"]["enabled"] is False
    assert actions["x"]["enabled"] is True


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
    assert by_id["ready-task"]["run_id"] == "run-ready"
    assert by_id["ready-task"]["stories_passed"] == 2
    assert by_id["merged-task"]["landing_state"] == "merged"
    assert by_id["merged-task"]["merge_id"] == "merge-merged"


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

    response = TestClient(create_app(repo)).post("/api/runs/queue-done/actions/merge", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "merge failed" in response.json()["message"]
    assert calls[0][-3:] == ["merge", "--fast", "hello-web"]


def test_web_state_includes_watcher_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    client = TestClient(create_app(repo))
    state = client.get("/api/state").json()
    assert state["watcher"]["alive"] is False
    assert state["watcher"]["counts"]["queued"] == 0


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
    argv = calls[0]["argv"]
    assert "queue" in argv
    assert "run" in argv
    assert "--no-dashboard" in argv
    assert "--exit-when-empty" in argv
    assert calls[0]["kwargs"]["cwd"] == str(repo.resolve())
