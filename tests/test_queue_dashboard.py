from __future__ import annotations

import io
from pathlib import Path

import pytest
from textual.widgets import SelectionList

from otto.queue.dashboard import (
    ResumeSelectionApp,
    _print_dashboard_closed_notice,
)
from otto.queue.schema import QueueTask
from tests._helpers import init_repo


def _queue_task(task_id: str, *, branch: str, worktree: str, command: str = "build") -> QueueTask:
    return QueueTask(
        id=task_id,
        command_argv=[command, f"intent for {task_id}"],
        added_at="2026-04-21T20:00:00Z",
        branch=branch,
        worktree=worktree,
    )


def test_print_dashboard_closed_notice_for_running_tasks() -> None:
    stream = io.StringIO()

    printed = _print_dashboard_closed_notice(2, stream=stream)

    assert printed is True
    assert stream.getvalue() == (
        "Dashboard closed. Watcher continues running in foreground.\n"
        "2 tasks still running; reopen with `otto queue dashboard` while they complete.\n"
        "Press Ctrl-C to interrupt (twice for immediate stop).\n"
    )


def test_print_dashboard_closed_notice_skips_clean_exit() -> None:
    stream = io.StringIO()

    printed = _print_dashboard_closed_notice(0, stream=stream)

    assert printed is False
    assert stream.getvalue() == ""


@pytest.mark.asyncio
async def test_resume_selection_app_returns_selected_ids(tmp_path: Path):
    repo = init_repo(tmp_path)
    del repo
    tasks = [
        _queue_task("labels", branch="build/labels", worktree=".worktrees/labels"),
        _queue_task("due", branch="build/due", worktree=".worktrees/due"),
    ]
    app = ResumeSelectionApp(tasks)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("space")
        await pilot.pause()
        selection = app.query_one("#resume-list", SelectionList)
        assert selection.selected == ["labels"]
