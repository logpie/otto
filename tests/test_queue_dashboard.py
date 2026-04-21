from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from textual.widgets import DataTable, RichLog, Static

import otto.cli_queue as cli_queue_module
from otto.cli import main
from otto.queue.dashboard import (
    HelpModal,
    NarrativeTailer,
    OverviewScreen,
    QueueApp,
    TaskDetailScreen,
)
from otto.queue.runner import Runner
from otto.queue.schema import QueueTask, append_task, write_state
from tests._helpers import init_repo


def _queue_task(task_id: str, *, branch: str, worktree: str, command: str = "build") -> QueueTask:
    return QueueTask(
        id=task_id,
        command_argv=[command, f"intent for {task_id}"],
        added_at="2026-04-21T20:00:00Z",
        branch=branch,
        worktree=worktree,
    )


def _write_narrative(repo: Path, task_id: str, text: str, *, phase_dir: str = "build") -> None:
    path = repo / ".worktrees" / task_id / "otto_logs" / "sessions" / f"session-{task_id}" / phase_dir / "narrative.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_queue_state(repo: Path, tasks: dict[str, dict]) -> None:
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": {
                "pid": 123,
                "pgid": 123,
                "started_at": "2026-04-21T20:00:00Z",
                "heartbeat": "2026-04-21T20:00:05Z",
            },
            "tasks": tasks,
        },
    )


def _build_app(repo: Path, *, cancel_callback=None) -> QueueApp:
    return QueueApp(repo, concurrent=2, cancel_callback=cancel_callback)


@pytest.mark.asyncio
async def test_overview_renders_rows_and_counts(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    append_task(repo, _queue_task("beta", branch="build/beta", worktree=".worktrees/beta"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n[+0:03] ▸ Building alpha.\n")
    _write_narrative(repo, "beta", "[+0:00] — BUILD starting —\n[+0:02] ✦ VERDICT: PASS\n")
    _write_queue_state(
        repo,
        {
            "alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"},
            "beta": {
                "status": "done",
                "started_at": "2026-04-21T20:00:00Z",
                "finished_at": "2026-04-21T20:00:10Z",
                "cost_usd": 1.25,
                "duration_s": 10.0,
            },
        },
    )

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one("#overview-table", DataTable)
        status = app.screen.query_one("#overview-status", Static)
        header = app.screen.query_one("#overview-header", Static)

        assert table.row_count == 2
        assert table.get_row_at(0)[0] == "alpha"
        assert table.get_row_at(1)[0] == "beta"
        assert "1 running" in str(status.content)
        assert "1 done" in str(status.content)
        assert "$1.25" in str(header.content)


@pytest.mark.asyncio
async def test_overview_updates_when_state_changes(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(repo, {"alpha": {"status": "queued"}})

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.screen.query_one("#overview-status", Static)
        assert "1 queued" in str(status.content)

        _write_queue_state(
            repo,
            {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
        )
        await pilot.pause(0.6)

        table = app.screen.query_one("#overview-table", DataTable)
        status = app.screen.query_one("#overview-status", Static)
        assert table.get_row_at(0)[1] == "RUNNING"
        assert "1 running" in str(status.content)


@pytest.mark.asyncio
async def test_navigation_and_detail_screen(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    append_task(repo, _queue_task("beta", branch="build/beta", worktree=".worktrees/beta"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_narrative(repo, "beta", "[+0:00] — BUILD starting —\n[+0:01] ▸ Beta details.\n")
    _write_queue_state(
        repo,
        {
            "alpha": {"status": "queued"},
            "beta": {"status": "running", "started_at": "2026-04-21T20:00:01Z"},
        },
    )

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one("#overview-table", DataTable)
        assert table.cursor_row == 0

        await pilot.press("j")
        assert table.cursor_row == 1

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TaskDetailScreen)
        header = app.screen.query_one("#detail-header", Static)
        assert "beta" in str(header.content)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, OverviewScreen)


def test_narrative_tailer_reads_new_bytes(tmp_path: Path):
    narrative = tmp_path / "narrative.log"
    narrative.write_text("[+0:00] start\n")
    tailer = NarrativeTailer(lambda: narrative)

    clear, lines = tailer.poll()
    assert clear is True
    assert lines == ["[+0:00] start"]

    with narrative.open("a") as handle:
        handle.write("[+0:01] next line\n")

    clear, lines = tailer.poll()
    assert clear is False
    assert lines == ["[+0:01] next line"]


@pytest.mark.asyncio
async def test_cancel_key_calls_callback_for_selected_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )
    seen: list[str] = []

    app = _build_app(repo, cancel_callback=seen.append)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert seen == ["alpha"]


@pytest.mark.asyncio
async def test_help_overlay_appears(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(repo, {"alpha": {"status": "queued"}})

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)


def test_queue_run_skips_dashboard_when_disabled(monkeypatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OTTO_NO_TUI", "1")
    monkeypatch.setattr(cli_queue_module, "_install_runner_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_queue_module, "_resolve_otto_bin", lambda: ["/bin/true"])
    called = {"run": 0}

    def _fake_run(self) -> int:
        called["run"] += 1
        return 0

    monkeypatch.setattr(Runner, "run", _fake_run)

    result = CliRunner().invoke(main, ["queue", "run"], catch_exceptions=False)
    assert result.exit_code == 0
    assert called == {"run": 1}
