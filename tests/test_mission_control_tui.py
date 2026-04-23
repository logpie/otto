from __future__ import annotations

import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Log, Static

from otto import paths
from otto.history import append_history_entry
from otto.queue.schema import QueueTask, append_task, write_state
from otto.runs.registry import make_run_record, update_record, write_record
from otto.tui.mission_control import MissionControlApp
from otto.tui.mission_control_actions import ActionResult
from otto.tui.mission_control_model import MissionControlFilters


def _write_live_record(repo: Path, *, run_id: str, run_type: str, status: str, primary_log: Path, extra_logs: list[str] | None = None, queue_task_id: str | None = None, pid: int | None = None) -> None:
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="queue" if run_type == "queue" else "atomic",
        run_type=run_type,
        command=run_type,
        display_name=f"{run_type}: {run_id}",
        status=status,
        cwd=repo,
        identity={"queue_task_id": queue_task_id, "merge_id": None, "parent_run_id": None},
        artifacts={
            "primary_log_path": str(primary_log),
            "extra_log_paths": list(extra_logs or []),
            "manifest_path": None,
            "summary_path": None,
            "checkpoint_path": None,
        },
        adapter_key="queue.attempt" if run_type == "queue" else f"atomic.{run_type}",
        last_event=f"{run_id} event",
    )
    record.writer.update({"pid": pid or os.getpid(), "pgid": pid or os.getpid(), "process_start_time_ns": 1, "boot_id": ""})
    write_record(repo, record)
    update_record(
        repo,
        run_id,
        heartbeat=False,
        updates={
            "timing": {
                "started_at": "2026-04-23T12:00:00Z",
                "updated_at": "2026-04-23T12:00:00Z",
                "heartbeat_at": "2026-04-23T12:00:00Z",
                "heartbeat_interval_s": 2.0,
                "heartbeat_seq": 1,
            },
            "status": status,
        },
    )


@pytest.mark.asyncio
async def test_mission_control_focus_selection_logs_and_queue_compat_filter(tmp_path: Path):
    repo = tmp_path
    build_log = paths.build_dir(repo, "build-run") / "narrative.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("build primary\n")
    build_extra = paths.session_dir(repo, "build-run") / "agent.log"
    build_extra.parent.mkdir(parents=True, exist_ok=True)
    build_extra.write_text("build secondary\n")
    queue_log = paths.build_dir(repo, "queue-run") / "narrative.log"
    queue_log.parent.mkdir(parents=True, exist_ok=True)
    queue_log.write_text("queue log\n")

    _write_live_record(repo, run_id="build-run", run_type="build", status="running", primary_log=build_log, extra_logs=[str(build_extra)], pid=999999)
    _write_live_record(repo, run_id="queue-run", run_type="queue", status="done", primary_log=queue_log, queue_task_id="queued-task")

    app = MissionControlApp(repo)
    app.model._process_probe = lambda writer: False if writer.get("pid") == 999999 else True

    async with app.run_test() as pilot:
        await pilot.pause()
        live = app.query_one("#live-table", DataTable)
        detail_meta = app.query_one("#detail-meta", Static)
        detail_actions = app.query_one("#detail-actions", Static)

        assert live.row_count == 2
        assert app.state.focus == "live"
        assert "build-run" in str(detail_meta.content)

        app.model._stale_trackers["build-run"].last_progress_monotonic -= 16
        app._refresh_state()
        assert "writer unavailable" in str(detail_actions.content)

        await pilot.press("tab")
        await pilot.pause()
        assert app.state.focus == "history"
        await pilot.press("tab")
        await pilot.pause()
        assert app.state.focus == "detail"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.state.focus == "history"

        await pilot.press("1")
        await pilot.press("down")
        await pilot.pause()
        assert app.state.selection.run_id == "queue-run"
        assert "queue-run" in str(app.query_one("#detail-meta", Static).content)

        await pilot.press("up")
        await pilot.press("enter")
        await pilot.pause()
        log_widget = app.query_one("#detail-log", Log)
        assert "build primary" in "\n".join(str(line) for line in log_widget.lines)

        await pilot.press("o")
        await pilot.pause()
        assert "build secondary" in "\n".join(str(line) for line in log_widget.lines)

    queue_app = MissionControlApp(repo, initial_filters=MissionControlFilters(type_filter="queue"), queue_compat=True)
    async with queue_app.run_test() as pilot:
        await pilot.pause()
        live = queue_app.query_one("#live-table", DataTable)
        assert live.row_count == 1
        assert queue_app.state.filters.type_filter == "queue"


@pytest.mark.asyncio
async def test_mission_control_queue_compat_renders_legacy_rows(tmp_path: Path) -> None:
    repo = tmp_path
    append_task(
        repo,
        QueueTask(
            id="legacy-task",
            command_argv=["build", "legacy task"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="legacy queue task",
            branch="build/legacy-task",
            worktree=".worktrees/legacy-task",
        ),
    )
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "legacy-task": {
                    "status": "queued",
                    "started_at": "2026-04-23T12:00:00Z",
                }
            },
        },
    )

    app = MissionControlApp(repo, initial_filters=MissionControlFilters(type_filter="queue"), queue_compat=True)

    async with app.run_test() as pilot:
        await pilot.pause()
        live = app.query_one("#live-table", DataTable)
        detail_meta = app.query_one("#detail-meta", Static)
        detail_actions = app.query_one("#detail-actions", Static)

        assert live.row_count == 1
        assert "legacy queue mode" in str(detail_meta.content)
        assert "open logs: disabled (legacy queue mode has no registry-backed log view)" in str(detail_actions.content)


@pytest.mark.asyncio
async def test_mission_control_return_to_origin_uses_current_list_pane(tmp_path: Path) -> None:
    repo = tmp_path
    build_log = paths.build_dir(repo, "build-run") / "narrative.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("build primary\n")
    _write_live_record(repo, run_id="build-run", run_type="build", status="running", primary_log=build_log)
    append_history_entry(
        repo,
        {
            "run_id": "history-run",
            "command": "build",
            "intent": "history row",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:00:00Z",
        },
    )

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state.selection.origin_pane == "live"

        await pilot.press("2")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.state.focus == "detail"

        await pilot.press("escape")
        await pilot.pause()
        assert app.state.focus == "history"


@pytest.mark.asyncio
async def test_mission_control_query_filter_modal_applies_and_cancels(tmp_path: Path) -> None:
    repo = tmp_path
    append_history_entry(
        repo,
        {
            "run_id": "keep-run",
            "command": "build",
            "intent": "keep me",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:00:00Z",
        },
    )
    append_history_entry(
        repo,
        {
            "run_id": "drop-run",
            "command": "build",
            "intent": "drop me",
            "passed": True,
            "status": "done",
            "terminal_outcome": "success",
            "timestamp": "2026-04-23T12:01:00Z",
        },
    )

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#filter-input", Input).value = "x"
        await pilot.press("escape")
        await pilot.pause()
        assert app.state.filters.query == ""

        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#filter-input", Input).value = "keep"
        app.screen.action_apply()
        await pilot.pause()

        history = app.query_one("#history-table", DataTable)
        assert app.state.filters.query == "keep"
        assert history.row_count == 1


@pytest.mark.asyncio
async def test_mission_control_keybinds_dispatch_real_actions(tmp_path: Path) -> None:
    repo = tmp_path
    build_log = paths.build_dir(repo, "build-running") / "narrative.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("running\n")
    checkpoint = paths.session_checkpoint(repo, "build-interrupted")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text("{}")
    artifact_path = paths.session_summary(repo, "build-failed")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}")
    failed_log = paths.build_dir(repo, "build-failed") / "narrative.log"
    failed_log.parent.mkdir(parents=True, exist_ok=True)
    failed_log.write_text("failed\n")
    queue_log = paths.build_dir(repo, "queue-done") / "narrative.log"
    queue_log.parent.mkdir(parents=True, exist_ok=True)
    queue_log.write_text("queue done\n")

    running = make_run_record(
        project_dir=repo,
        run_id="build-running",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build running",
        status="running",
        cwd=repo,
        artifacts={"primary_log_path": str(build_log)},
        adapter_key="atomic.build",
    )
    interrupted = make_run_record(
        project_dir=repo,
        run_id="build-interrupted",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build interrupted",
        status="interrupted",
        cwd=repo,
        artifacts={"checkpoint_path": str(checkpoint), "primary_log_path": str(build_log)},
        adapter_key="atomic.build",
    )
    failed = make_run_record(
        project_dir=repo,
        run_id="build-failed",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build failed",
        status="failed",
        cwd=repo,
        source={"argv": ["build", "retry me"]},
        artifacts={"summary_path": str(artifact_path), "primary_log_path": str(failed_log)},
        adapter_key="atomic.build",
    )
    failed.writer.update({"pid": 999999, "pgid": 999999, "process_start_time_ns": 1, "boot_id": ""})
    queue_done = make_run_record(
        project_dir=repo,
        run_id="queue-done",
        domain="queue",
        run_type="queue",
        command="queue",
        display_name="queue done",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "task-1", "merge_id": None, "parent_run_id": None},
        artifacts={"primary_log_path": str(queue_log)},
        adapter_key="queue.attempt",
    )
    for record in (running, interrupted, failed, queue_done):
        write_record(repo, record)

    app = MissionControlApp(repo)
    calls: list[tuple[str, str, str | None, list[str] | None]] = []
    merge_all_calls: list[str] = []

    def _fake_execute(record, action_kind, *, selected_artifact_path=None, selected_queue_task_ids=None):
        calls.append((record.run_id, action_kind, selected_artifact_path, selected_queue_task_ids))
        return ActionResult(ok=True, clear_banner=True)

    def _fake_merge_all():
        merge_all_calls.append("M")
        return ActionResult(ok=True, clear_banner=True)

    app._execute_detail_action = _fake_execute
    app._execute_merge_all = _fake_merge_all

    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        await pilot.press("1")
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("M")
        await pilot.pause()

    assert ("build-running", "c", None, None) in calls
    assert ("build-interrupted", "r", None, None) in calls
    assert ("build-failed", "R", None, None) in calls
    assert ("build-failed", "x", None, None) in calls
    assert ("build-failed", "e", str(artifact_path), None) in calls
    assert ("queue-done", "m", None, ["task-1"]) in calls
    assert merge_all_calls == ["M"]


@pytest.mark.asyncio
async def test_mission_control_space_selects_multiple_queue_rows_for_merge(tmp_path: Path) -> None:
    repo = tmp_path
    queue_a_log = paths.build_dir(repo, "queue-a") / "narrative.log"
    queue_a_log.parent.mkdir(parents=True, exist_ok=True)
    queue_a_log.write_text("a\n")
    queue_b_log = paths.build_dir(repo, "queue-b") / "narrative.log"
    queue_b_log.parent.mkdir(parents=True, exist_ok=True)
    queue_b_log.write_text("b\n")

    queue_a = make_run_record(
        project_dir=repo,
        run_id="queue-a",
        domain="queue",
        run_type="queue",
        command="queue",
        display_name="queue a",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "task-a", "merge_id": None, "parent_run_id": None},
        artifacts={"primary_log_path": str(queue_a_log)},
        adapter_key="queue.attempt",
    )
    queue_b = make_run_record(
        project_dir=repo,
        run_id="queue-b",
        domain="queue",
        run_type="queue",
        command="queue",
        display_name="queue b",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "task-b", "merge_id": None, "parent_run_id": None},
        artifacts={"primary_log_path": str(queue_b_log)},
        adapter_key="queue.attempt",
    )
    for record in (queue_a, queue_b):
        write_record(repo, record)

    app = MissionControlApp(repo, initial_filters=MissionControlFilters(type_filter="queue"))
    calls: list[tuple[str, str, list[str] | None]] = []

    def _fake_execute(record, action_kind, *, selected_artifact_path=None, selected_queue_task_ids=None):
        del selected_artifact_path
        calls.append((record.run_id, action_kind, selected_queue_task_ids))
        return ActionResult(ok=True, clear_banner=True)

    app._execute_detail_action = _fake_execute

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()

        status_bar = app.query_one("#status-bar", Static)
        footer = app.query_one("#footer", Static)
        assert "selected=2" in str(status_bar.content)
        assert "space select" in str(footer.content)

        await pilot.press("m")
        await pilot.pause()

    assert calls == [("queue-b", "m", ["task-a", "task-b"])]
