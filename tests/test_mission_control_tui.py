from __future__ import annotations

import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Log, Static

from otto import paths
from otto.runs.registry import make_run_record, update_record, write_record
from otto.tui.mission_control import MissionControlApp
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
