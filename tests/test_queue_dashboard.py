from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from textual.widgets import DataTable, Log, SelectionList, Static

import otto.cli_queue as cli_queue_module
import otto.queue.dashboard as dashboard_module
from otto.cli import main
from otto.queue.dashboard import (
    HelpModal,
    NarrativeTailer,
    OverviewScreen,
    QueueApp,
    QueueModel,
    ResumeSelectionApp,
    TaskDetailScreen,
    _print_dashboard_closed_notice,
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


def _write_queue_manifest(
    repo: Path,
    task_id: str,
    *,
    session_id: str,
    checkpoint_path: Path | None = None,
    mirror_of: str | None = None,
) -> Path:
    session_root = repo / ".worktrees" / task_id / "otto_logs" / "sessions" / session_id
    manifest_path = session_root / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_path or (session_root / "checkpoint.json")
    manifest_payload = {
        "run_id": session_id,
        "mirror_of": mirror_of or str(manifest_path.resolve()),
        "checkpoint_path": str(checkpoint_path.resolve(strict=False)),
        "cost_usd": 1.25,
        "duration_s": 5.0,
    }
    manifest_path.write_text(json.dumps(manifest_payload))

    queue_manifest = repo / "otto_logs" / "queue" / task_id / "manifest.json"
    queue_manifest.parent.mkdir(parents=True, exist_ok=True)
    queue_manifest.write_text(json.dumps(manifest_payload))
    return manifest_path.resolve()


def _write_queue_state(repo: Path, tasks: dict[str, dict]) -> None:
    now = dashboard_module._now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": {
                "pid": os.getpid(),
                "pgid": os.getpid(),
                "started_at": now,
                "heartbeat": now,
            },
            "tasks": tasks,
        },
    )


def _build_app(repo: Path, *, cancel_callback=None, read_only: bool = False) -> QueueApp:
    return QueueApp(repo, concurrent=2, cancel_callback=cancel_callback, read_only=read_only)


def _log_text(widget: Log) -> str:
    return "\n".join(str(line) for line in widget.lines)


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


def test_queue_model_infers_certify_phase_from_certify_banner(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(
        repo,
        "alpha",
        "[+0:00] — BUILD starting —\n[+0:10] — CERTIFY starting —\n[+0:11] cert in progress …\n",
        phase_dir="build",
    )
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )

    snapshot = QueueModel(repo).snapshot()

    assert snapshot.tasks[0].phase == "CERTIFY"


@pytest.mark.asyncio
async def test_overview_shows_empty_state_for_zero_tasks(tmp_path: Path):
    repo = init_repo(tmp_path)
    _write_queue_state(repo, {})

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one("#overview-table", DataTable)
        empty_state = app.screen.query_one("#empty-state", Static)

        assert table.row_count == 0
        assert table.styles.display == "none"
        assert empty_state.styles.display == "block"
        assert "No tasks queued." in str(empty_state.content)


@pytest.mark.asyncio
async def test_dashboard_shows_queue_parse_warning_and_stays_usable(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / ".otto-queue.yml").write_text("schema_version: [\n")
    _write_queue_state(repo, {})

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.screen.query_one("#overview-banner", Static)
        empty_state = app.screen.query_one("#empty-state", Static)

        assert "queue.yml" in str(banner.content)
        assert "parse error" in str(banner.content)
        assert "No tasks queued." in str(empty_state.content)


def test_overview_on_mount_tolerates_missing_table(monkeypatch):
    screen = OverviewScreen()
    monkeypatch.setattr(screen, "query", lambda selector: [])
    monkeypatch.setattr(screen, "_refresh", lambda: None)
    timers: list[tuple[float, object]] = []
    monkeypatch.setattr(screen, "set_interval", lambda interval, callback: timers.append((interval, callback)))

    screen.on_mount()

    assert timers


def test_queue_model_preserves_last_good_cache_on_malformed_queue(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    model = QueueModel(repo)

    first = model.snapshot()
    assert [task.task.id for task in first.tasks] == ["alpha"]

    (repo / ".otto-queue.yml").write_text("schema_version: 1\ntasks:\n  - id: alpha\n")
    second = model.snapshot()

    assert [task.task.id for task in second.tasks] == ["alpha"]
    assert "queue.yml" in str(model.overview_banner())


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


@pytest.mark.asyncio
async def test_detail_uses_log_and_end_resumes_follow(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    narrative_path = (
        repo
        / ".worktrees"
        / "alpha"
        / "otto_logs"
        / "sessions"
        / "session-alpha"
        / "build"
        / "narrative.log"
    )
    _write_narrative(
        repo,
        "alpha",
        "".join(f"[+0:{idx:02d}] line {idx}\n" for idx in range(40)),
    )
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )

    app = _build_app(repo)
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.4)

        assert isinstance(app.screen, TaskDetailScreen)
        log_widget = app.screen.query_one("#detail-log", Log)
        assert isinstance(log_widget, Log)
        assert log_widget.auto_scroll is True

        app.screen.action_top()
        await pilot.pause(0.1)
        assert app.screen._follow is False
        assert log_widget.auto_scroll is False
        assert log_widget.scroll_y == 0

        with narrative_path.open("a", encoding="utf-8") as handle:
            handle.write("[+0:40] after home\n")
        await pilot.pause(0.4)
        assert log_widget.scroll_y == 0

        await pilot.press("end")
        await pilot.pause(0.2)
        assert app.screen._follow is True
        assert log_widget.auto_scroll is True
        assert log_widget.scroll_y == log_widget.max_scroll_y

        with narrative_path.open("a", encoding="utf-8") as handle:
            handle.write("[+0:41] after end\n")
        await pilot.pause(0.4)
        assert log_widget.scroll_y == log_widget.max_scroll_y


@pytest.mark.asyncio
async def test_detail_follows_relocated_running_session_via_manifest_fallback(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    old_narrative = (
        repo
        / ".worktrees"
        / "alpha"
        / "otto_logs"
        / "sessions"
        / "session-alpha"
        / "build"
        / "narrative.log"
    )
    old_narrative.parent.mkdir(parents=True, exist_ok=True)
    old_narrative.write_text("[+0:00] old home\n")
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert isinstance(app.screen, TaskDetailScreen)
        log_widget = app.screen.query_one("#detail-log", Log)
        assert "old home" in _log_text(log_widget)

        relocated_session = repo / "relocated" / "otto_logs" / "sessions" / "session-alpha-relocated"
        relocated_narrative = relocated_session / "build" / "narrative.log"
        relocated_narrative.parent.mkdir(parents=True, exist_ok=True)
        relocated_narrative.write_text("[+0:01] relocated home\n")
        queue_manifest = repo / "otto_logs" / "queue" / "alpha" / "manifest.json"
        queue_manifest.parent.mkdir(parents=True, exist_ok=True)
        queue_manifest.write_text(json.dumps({
            "run_id": "session-alpha-relocated",
            "checkpoint_path": str((relocated_session / "checkpoint.json").resolve(strict=False)),
        }))
        old_narrative.unlink()
        await pilot.pause(0.6)

        info = app.screen.query_one("#detail-info", Static)
        rendered = _log_text(log_widget)
        assert "relocated home" in rendered
        assert "old home" not in rendered
        assert str(relocated_narrative) in str(info.content)


@pytest.mark.asyncio
async def test_detail_clears_when_narrative_disappears(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    narrative_path = (
        repo
        / ".worktrees"
        / "alpha"
        / "otto_logs"
        / "sessions"
        / "session-alpha"
        / "build"
        / "narrative.log"
    )
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text("[+0:00] still here\n")
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert isinstance(app.screen, TaskDetailScreen)
        log_widget = app.screen.query_one("#detail-log", Log)
        assert "still here" in _log_text(log_widget)

        narrative_path.unlink()
        await pilot.pause(0.6)

        rendered = _log_text(log_widget)
        assert "still here" not in rendered
        assert "<log file no longer available>" in rendered


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


def test_narrative_tailer_strips_terminal_escapes(tmp_path: Path):
    narrative = tmp_path / "narrative.log"
    narrative.write_text("[+0:00] \x1b[31mANSI-RED\x1b[0m visible\n")
    tailer = NarrativeTailer(lambda: narrative)

    clear, lines = tailer.poll()

    assert clear is True
    assert lines == ["[+0:00] ANSI-RED visible"]
    assert "\x1b[" not in lines[0]


def test_queue_model_strips_terminal_escapes_from_event_summary(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] \x1b[31mANSI-RED\x1b[0m visible\n")
    _write_queue_state(repo, {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}})

    snapshot = QueueModel(repo).snapshot()

    assert snapshot.tasks[0].event == "[+0:00] ANSI-RED visible"
    assert "\x1b[" not in snapshot.tasks[0].event


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


def test_resolve_manifest_path_falls_back_when_mirror_is_stale(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    queue_manifest = _write_queue_manifest(
        repo,
        "alpha",
        session_id="session-alpha",
        mirror_of=str((repo / "missing" / "manifest.json").resolve(strict=False)),
    )

    model = QueueModel(repo)
    manifest = json.loads((repo / "otto_logs" / "queue" / "alpha" / "manifest.json").read_text())

    resolved = model.resolve_manifest_path("alpha", manifest=manifest)

    assert resolved == (repo / "otto_logs" / "queue" / "alpha" / "manifest.json").resolve(strict=False)


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
async def test_overview_cancel_dedupes_notifications(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )
    seen: list[str] = []
    notices: list[tuple[str, str]] = []
    app = _build_app(repo, cancel_callback=seen.append)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs.get("severity", "information"))),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause(0.1)
        await pilot.press("c")
        await pilot.pause(0.1)

        assert seen == ["alpha"]
        assert notices[:2] == [
            ("cancel queued for alpha", "information"),
            ("cancel already sent for alpha", "information"),
        ]
        assert "alpha" in app._recent_cancel_requests

        _write_queue_state(
            repo,
            {
                "alpha": {
                    "status": "done",
                    "started_at": "2026-04-21T20:00:01Z",
                    "finished_at": "2026-04-21T20:00:05Z",
                }
            },
        )
        await pilot.pause(0.6)

        assert "alpha" not in app._recent_cancel_requests
        await pilot.press("c")
        await pilot.pause(0.1)
        assert notices[-1] == ("task is not running, cannot cancel", "warning")


@pytest.mark.asyncio
async def test_detail_cancel_dedupes_notifications(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(
        repo,
        {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}},
    )
    seen: list[str] = []
    notices: list[tuple[str, str]] = []
    app = _build_app(repo, cancel_callback=seen.append)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs.get("severity", "information"))),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("c")
        await pilot.pause(0.1)
        await pilot.press("c")
        await pilot.pause(0.1)

    assert seen == ["alpha"]
    assert notices[:2] == [
        ("cancel queued for alpha", "information"),
        ("cancel already sent for alpha", "information"),
    ]


@pytest.mark.asyncio
async def test_overview_yank_copies_selected_row_to_clipboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] BUILD starting\n[+0:05] finished alpha.\n")
    manifest_path = _write_queue_manifest(repo, "alpha", session_id="session-alpha")
    _write_queue_state(
        repo,
        {
            "alpha": {
                "status": "done",
                "started_at": "2026-04-21T20:00:00Z",
                "finished_at": "2026-04-21T20:00:05Z",
                "cost_usd": 1.25,
                "duration_s": 5.0,
            },
        },
    )

    calls: list[tuple[list[str], bytes, bool]] = []

    def _fake_run(argv: list[str], *, input: bytes, check: bool):
        calls.append((argv, input, check))

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(dashboard_module.sys, "platform", "darwin", raising=False)
    monkeypatch.setattr(dashboard_module.subprocess, "run", _fake_run)

    app = _build_app(repo)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

    assert calls == [
        (
            ["pbcopy"],
            (
                "alpha\tdone\tBUILD\tbuild/alpha\t0:05\t$1.25\t[+0:05] finished alpha.\n"
                f"session-alpha\t{manifest_path}"
            ).encode(),
            False,
        )
    ]


@pytest.mark.asyncio
async def test_detail_yank_copies_full_narrative_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    narrative_text = "[+0:00] BUILD starting\n[+0:01] line one\n[+0:02] line two\n"
    _write_narrative(repo, "alpha", narrative_text)
    _write_queue_state(
        repo,
        {
            "alpha": {
                "status": "running",
                "started_at": "2026-04-21T20:00:01Z",
            },
        },
    )

    calls: list[tuple[list[str], bytes, bool]] = []

    def _fake_run(argv: list[str], *, input: bytes, check: bool):
        calls.append((argv, input, check))

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(dashboard_module.sys, "platform", "darwin", raising=False)
    monkeypatch.setattr(dashboard_module.subprocess, "run", _fake_run)

    app = _build_app(repo)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

    assert calls == [(["pbcopy"], narrative_text.encode(), False)]


@pytest.mark.asyncio
async def test_help_overlay_appears(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(repo, {"alpha": {"status": "queued"}})

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.screen.query_one("#overview-status", Static)
        assert "q hide; reopen with `otto queue dashboard`" in str(status.content)
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)
        help_body = app.screen.query_one("#help-body", Static)
        assert "q: hide dashboard; reopen with `otto queue dashboard`" in str(help_body.content)


@pytest.mark.asyncio
async def test_read_only_viewer_footer_updates_when_watcher_stops(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] — BUILD starting —\n")
    _write_queue_state(repo, {"alpha": {"status": "running", "started_at": "2026-04-21T20:00:01Z"}})

    app = _build_app(repo, read_only=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.screen.query_one("#overview-status", Static)
        assert "watcher live" in str(status.content)
        assert "q quit" in str(status.content)

        write_state(
            repo,
            {
                "schema_version": 1,
                "watcher": None,
                "tasks": {"alpha": {"status": "interrupted"}},
            },
        )
        await pilot.pause(0.6)

        status = app.screen.query_one("#overview-status", Static)
        assert "watcher stopped" in str(status.content)
        assert "1 interrupted" in str(status.content)


@pytest.mark.asyncio
async def test_resume_selection_app_returns_selected_ids(tmp_path: Path):
    repo = init_repo(tmp_path)
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


@pytest.mark.asyncio
async def test_state_parse_error_banner_appears_and_clears(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, _queue_task("alpha", branch="build/alpha", worktree=".worktrees/alpha"))
    _write_narrative(repo, "alpha", "[+0:00] BUILD starting\n")
    (repo / ".otto-queue-state.json").write_text('{"schema_version": 1,')

    app = _build_app(repo)
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.screen.query_one("#overview-banner", Static)
        assert "state.json parse error" in str(banner.content)

        _write_queue_state(repo, {"alpha": {"status": "queued"}})
        await pilot.pause(0.6)

        banner = app.screen.query_one("#overview-banner", Static)
        assert "state.json parse error" not in str(banner.content)


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
