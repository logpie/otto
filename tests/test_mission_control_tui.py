from __future__ import annotations

import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Log, Static

from otto import paths
from otto.history import append_history_entry
from otto.queue.schema import QueueTask, append_task, write_state
from otto.runs.registry import make_run_record, update_record, write_record
from otto.theme import MISSION_CONTROL_THEME
from otto.tui.mission_control import HelpModal, MissionControlApp, SearchableLog
from otto.tui.mission_control_actions import ActionResult
from otto.tui.mission_control_model import MissionControlFilters

pytestmark = pytest.mark.tui


class _EditorPopen:
    calls: list[list[str]] = []

    def __init__(self, argv, *, cwd, stdout, stderr, text) -> None:
        del cwd, stdout, stderr, text
        type(self).calls.append(list(argv))
        self.returncode = 0
        self.pid = 5150

    def poll(self):
        return self.returncode

    def communicate(self):
        return ("", "")


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
async def test_mission_control_yank_copies_untruncated_metadata_and_detail_log(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path
    capture_path = tmp_path / "clipboard.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pbcopy = bindir / "pbcopy"
    pbcopy.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\ncat > {capture_path}\n")
    pbcopy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    primary_log = paths.build_dir(repo, "queue-copy-run") / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("BUILD starting\nSTORY_RESULT: copied PASS\n")
    queue_manifest = repo / "otto_logs" / "queue" / "copy-task" / "manifest.json"
    queue_manifest.parent.mkdir(parents=True, exist_ok=True)
    queue_manifest.write_text("{}\n")

    record = make_run_record(
        project_dir=repo,
        run_id="queue-copy-run",
        domain="queue",
        run_type="queue",
        command="queue build",
        display_name="queue copy",
        status="running",
        cwd=repo,
        identity={"queue_task_id": "copy-task"},
        source={"argv": ["otto", "queue", "build", "copy"]},
        git={"branch": "build/copy-task-2026-04-23"},
        intent={"summary": "copy the full selected row"},
        artifacts={
            "primary_log_path": str(primary_log),
            "manifest_path": str(queue_manifest),
        },
        adapter_key="queue.attempt",
        last_event="copy row",
    )
    write_record(repo, record)

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        payload = capture_path.read_text()
        assert "copy-task" in payload
        assert "build/copy-task-2026-04-23" in payload
        assert str(queue_manifest) in payload
        assert "/otto_logs/" in payload
        assert "..." not in payload

        capture_path.unlink()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

    assert "STORY_RESULT: copied PASS" in capture_path.read_text()


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
async def test_mission_control_help_modal_opens_and_closes(tmp_path: Path) -> None:
    repo = tmp_path
    build_log = paths.build_dir(repo, "build-run") / "narrative.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("build primary\n")
    _write_live_record(repo, run_id="build-run", run_type="build", status="running", primary_log=build_log)

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        assert isinstance(app.screen, HelpModal)
        assert "Status Codes" in str(app.screen.query_one("#help-body", Static).content)
        assert "Compatibility Notes" in str(app.screen.query_one("#help-body", Static).content)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_mission_control_empty_state_names_entry_points(tmp_path: Path) -> None:
    app = MissionControlApp(tmp_path, initial_filters=MissionControlFilters(type_filter="queue"), queue_compat=True)

    async with app.run_test() as pilot:
        await pilot.pause()

        detail = str(app.query_one("#detail-meta", Static).content)
        assert "No runs yet." in detail
        assert "otto build" in detail
        assert "otto improve bugs" in detail
        assert "otto certify" in detail
        assert "otto queue build" in detail


@pytest.mark.asyncio
async def test_mission_control_log_search_keybind_tracks_matches(tmp_path: Path) -> None:
    repo = tmp_path
    build_log = paths.build_dir(repo, "build-run") / "narrative.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("alpha start\nbeta alpha\ngamma\nALPHA end\n")
    _write_live_record(repo, run_id="build-run", run_type="build", status="running", primary_log=build_log)

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("ctrl+f")
        await pilot.pause()

        app.screen.query_one("#filter-input", Input).value = "alpha"
        app.screen.action_apply()
        await pilot.pause()

        log_widget = app.query_one("#detail-log", SearchableLog)
        assert app._log_search_query == "alpha"
        assert app._log_search_match_total == 3
        assert app._log_search_match_index == 0
        assert log_widget.current_match == (0, 0, 5)

        await pilot.press("n")
        await pilot.pause()
        assert app._log_search_match_index == 1
        assert log_widget.current_match == (1, 5, 10)

        await pilot.press("N")
        await pilot.pause()
        assert app._log_search_match_index == 0
        assert log_widget.current_match == (0, 0, 5)


@pytest.mark.asyncio
async def test_mission_control_status_cells_use_theme_colors(tmp_path: Path) -> None:
    repo = tmp_path
    running_log = paths.build_dir(repo, "build-running") / "narrative.log"
    running_log.parent.mkdir(parents=True, exist_ok=True)
    running_log.write_text("running\n")
    failed_log = paths.build_dir(repo, "build-failed") / "narrative.log"
    failed_log.parent.mkdir(parents=True, exist_ok=True)
    failed_log.write_text("failed\n")

    _write_live_record(repo, run_id="build-running", run_type="build", status="running", primary_log=running_log)
    _write_live_record(repo, run_id="build-failed", run_type="build", status="failed", primary_log=failed_log)

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        live = app.query_one("#live-table", DataTable)
        running_row = live.get_row_at(0)
        failed_row = live.get_row_at(1)

        assert str(running_row[0]) == "RUNNING"
        assert running_row[0].style == MISSION_CONTROL_THEME.running
        assert str(failed_row[0]) == "FAILED"
        assert failed_row[0].style == MISSION_CONTROL_THEME.failed


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


def test_mission_control_merge_all_forwards_delayed_result(tmp_path: Path, monkeypatch) -> None:
    app = MissionControlApp(tmp_path)
    observed: list[ActionResult] = []
    monkeypatch.setattr(app, "call_from_thread", lambda callback, result: callback(result))
    monkeypatch.setattr(app, "_handle_action_result", lambda result: observed.append(result))

    def _fake_execute_merge_all(project_dir, *, post_result=None):
        assert project_dir == tmp_path
        assert post_result is not None
        post_result(
            ActionResult(
                ok=False,
                severity="error",
                modal_title="merge all failed",
                modal_message="late failure",
                message="late failure",
            )
        )
        return ActionResult(ok=True, message="merge all launched", refresh=True)

    monkeypatch.setattr("otto.tui.mission_control.execute_merge_all", _fake_execute_merge_all)

    result = app._execute_merge_all()

    assert result.message == "merge all launched"
    assert observed[0].modal_title == "merge all failed"
    assert observed[0].modal_message == "late failure"


@pytest.mark.asyncio
async def test_mission_control_history_filters_and_paging_keybinds(tmp_path: Path) -> None:
    repo = tmp_path
    for index in range(60):
        append_history_entry(
            repo,
            {
                "run_id": f"build-{index:02d}",
                "command": "build",
                "intent": f"build row {index}",
                "passed": True,
                "status": "done",
                "terminal_outcome": "success",
                "timestamp": f"2026-04-23T12:{index % 60:02d}:00Z",
            },
        )
    for index in range(5):
        append_history_entry(
            repo,
            {
                "run_id": f"queue-{index:02d}",
                "domain": "queue",
                "run_type": "queue",
                "command": "queue",
                "intent": f"queue row {index}",
                "passed": False,
                "status": "failed",
                "terminal_outcome": "failure",
                "timestamp": f"2026-04-23T13:{index:02d}:00Z",
            },
        )
    for index in range(3):
        append_history_entry(
            repo,
            {
                "run_id": f"improve-{index:02d}",
                "command": "improve bugs",
                "intent": f"interrupted row {index}",
                "passed": False,
                "status": "interrupted",
                "terminal_outcome": "interrupted",
                "timestamp": f"2026-04-23T14:{index:02d}:00Z",
            },
        )
    for index in range(2):
        append_history_entry(
            repo,
            {
                "run_id": f"merge-{index:02d}",
                "domain": "merge",
                "run_type": "merge",
                "command": "merge",
                "intent": f"cancelled row {index}",
                "passed": False,
                "status": "cancelled",
                "terminal_outcome": "cancelled",
                "timestamp": f"2026-04-23T15:{index:02d}:00Z",
            },
        )

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state.history_page.total_rows == 70
        assert app.state.history_page.page == 0

        await pilot.press("pagedown")
        await pilot.pause()
        assert app.state.history_page.page == 0

        await pilot.press("2")
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert app.state.history_page.page == 1

        await pilot.press("pageup")
        await pilot.pause()
        assert app.state.history_page.page == 0

        await pilot.press("]")
        await pilot.pause()
        assert app.state.history_page.page == 1

        await pilot.press("[")
        await pilot.pause()
        assert app.state.history_page.page == 0

        await pilot.press("f")
        await pilot.pause()
        assert app.state.filters.outcome_filter == "success"
        assert app.state.history_page.total_rows == 60

        await pilot.press("f")
        await pilot.pause()
        assert app.state.filters.outcome_filter == "failed"
        assert app.state.history_page.total_rows == 5

        await pilot.press("f")
        await pilot.pause()
        assert app.state.filters.outcome_filter == "interrupted"
        assert app.state.history_page.total_rows == 3

        await pilot.press("f")
        await pilot.pause()
        assert app.state.filters.outcome_filter == "cancelled"
        assert app.state.history_page.total_rows == 2

        await pilot.press("f")
        await pilot.pause()
        assert app.state.filters.outcome_filter == "removed"
        assert app.state.history_page.total_rows == 0

        await pilot.press("f")
        await pilot.pause()
        assert app.state.filters.outcome_filter == "all"
        assert app.state.history_page.total_rows == 70

        await pilot.press("t")
        await pilot.pause()
        assert app.state.filters.type_filter == "build"
        assert app.state.history_page.total_rows == 60


@pytest.mark.asyncio
async def test_mission_control_detail_artifact_selection_launches_editor_for_selected_row(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path
    summary_path = paths.session_summary(repo, "build-failed")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("{}")
    primary_log = paths.build_dir(repo, "build-failed") / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("failed\n")

    record = make_run_record(
        project_dir=repo,
        run_id="build-failed",
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build failed",
        status="failed",
        cwd=repo,
        artifacts={"summary_path": str(summary_path), "primary_log_path": str(primary_log)},
        adapter_key="atomic.build",
    )
    write_record(repo, record)

    monkeypatch.setenv("EDITOR", "vim -f")
    monkeypatch.setattr("otto.tui.mission_control_actions.subprocess.Popen", _EditorPopen)
    _EditorPopen.calls.clear()

    app = MissionControlApp(repo)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()

        artifacts = app.query_one("#detail-artifacts", DataTable)
        assert artifacts.row_count == 2
        assert app.state.selection.artifact_index == 0

        await pilot.press("down")
        await pilot.pause()
        assert app.state.selection.artifact_index == 1

        await pilot.press("e")
        await pilot.pause()

    assert _EditorPopen.calls[-1] == ["vim", "-f", str(primary_log)]
