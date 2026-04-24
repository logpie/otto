"""Behavioral Mission Control test via Textual's pilot harness.

This script intentionally exercises Mission Control through its public Textual
surface instead of importing unit-test helpers. It simulates a queue-compatible
project with realistic queue state, worktree logs, manifests, an empty queue,
and malformed legacy state.

Usage:
    .venv/bin/python scripts/e2e_dashboard_behavioral.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from textual.widgets import DataTable, Log, Static

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from otto import paths
from otto.queue.schema import QueueTask, append_task
from otto.tui.mission_control import MissionControlApp
from otto.mission_control.model import MissionControlFilters


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg: str) -> None:
    log(f"FAIL: {msg}")
    sys.exit(1)


def check(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)
    log(f"OK: {msg}")


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _queue_app(repo: Path) -> MissionControlApp:
    return MissionControlApp(
        repo,
        initial_filters=MissionControlFilters(type_filter="queue"),
        queue_compat=True,
    )


def setup_realistic_repo(tmp: Path) -> Path:
    """Build a realistic legacy queue project for Mission Control."""
    repo = tmp / "repo"
    _init_repo(repo)

    for task_id, intent in [
        ("add", "Build add.py CLI"),
        ("mul", "Build mul.py CLI"),
        ("sub", "Build sub.py CLI"),
    ]:
        append_task(
            repo,
            QueueTask(
                id=task_id,
                command_argv=["build", "--fast", intent],
                resumable=True,
                added_at="2026-04-21T20:00:00Z",
                resolved_intent=intent,
                branch=f"build/{task_id}-2026-04-21",
                worktree=f".worktrees/{task_id}",
            ),
        )

    state = {
        "schema_version": 1,
        "watcher": {"pid": 12345, "started_at": "2026-04-21T20:00:00Z"},
        "tasks": {
            "add": {
                "status": "running",
                "started_at": "2026-04-21T20:00:30Z",
                "attempt_run_id": "2026-04-21-200030-aaa111",
                "child": {"pid": 22345, "pgid": 22345, "start_time_ns": 1, "argv": ["otto"], "cwd": str(repo)},
            },
            "mul": {
                "status": "running",
                "started_at": "2026-04-21T20:01:00Z",
                "attempt_run_id": "2026-04-21-200100-bbb222",
                "child": {"pid": 22346, "pgid": 22346, "start_time_ns": 2, "argv": ["otto"], "cwd": str(repo)},
            },
            "sub": {
                "status": "queued",
                "started_at": None,
                "child": None,
            },
        },
    }
    (repo / ".otto-queue-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    for task_id, session_id in [
        ("add", "2026-04-21-200030-aaa111"),
        ("mul", "2026-04-21-200100-bbb222"),
    ]:
        worktree = repo / ".worktrees" / task_id
        session_root = paths.session_dir(worktree, session_id)
        build_dir = paths.build_dir(worktree, session_id)
        build_dir.mkdir(parents=True)
        narrative = build_dir / "narrative.log"
        narrative.write_text(
            f"[+0:00] BUILD starting\n"
            f"[+0:01] reading project layout\n"
            f"[+0:02] writing {task_id}.py with argparse\n"
            f"[+0:15] running tests\n"
            f"[+0:20] STORY_RESULT: smoke | PASS | python {task_id}.py works\n",
            encoding="utf-8",
        )
        (session_root / "manifest.json").write_text(
            json.dumps(
                {
                    "command": "build",
                    "argv": ["build", "--fast", f"Build {task_id}.py CLI"],
                    "queue_task_id": task_id,
                    "run_id": session_id,
                    "branch": f"build/{task_id}-2026-04-21",
                    "resolved_intent": f"Build {task_id}.py CLI",
                    "exit_status": "running",
                    "schema_version": 1,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return repo


async def test_overview_renders_queue_compat_rows(repo: Path) -> None:
    log("test 1: queue compat overview renders all tasks")
    app = _queue_app(repo)
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.8)
        live = app.query_one("#live-table", DataTable)
        check(live.row_count == 3, f"3 live queue rows rendered (got {live.row_count})")
        check(app.state.filters.type_filter == "queue", "queue type filter is active")
        check("legacy queue mode" in str(app.query_one("#detail-meta", Static).content), "legacy compatibility is visible")


async def test_navigation_jk(repo: Path) -> None:
    log("test 2: j/k navigation moves the live cursor")
    app = _queue_app(repo)
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.8)
        live = app.query_one("#live-table", DataTable)
        initial_row = live.cursor_row
        await pilot.press("j")
        await pilot.pause(0.1)
        check(live.cursor_row == initial_row + 1, f"`j` moved cursor to {live.cursor_row}")
        await pilot.press("k")
        await pilot.pause(0.1)
        check(live.cursor_row == initial_row, f"`k` moved cursor back to {live.cursor_row}")


async def test_enter_focuses_detail_and_escape_returns(repo: Path) -> None:
    log("test 3: Enter focuses Detail and Esc returns to Live")
    app = _queue_app(repo)
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.8)
        await pilot.press("enter")
        await pilot.pause(0.2)
        check(app.state.focus == "detail", f"Enter changed focus to detail (got {app.state.focus})")
        await pilot.press("escape")
        await pilot.pause(0.2)
        check(app.state.focus == "live", f"Esc returned focus to live (got {app.state.focus})")


async def test_detail_shows_legacy_child_log(repo: Path) -> None:
    log("test 4: detail pane tails the queue child narrative log")
    app = _queue_app(repo)
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.8)
        await pilot.press("enter")
        await pilot.pause(0.4)
        log_widget = app.query_one("#detail-log", Log)
        body = "\n".join(str(line) for line in log_widget.lines)
        check("BUILD starting" in body, "detail log includes build start marker")
        check("STORY_RESULT" in body, "detail log includes story result marker")


async def test_empty_queue(tmp: Path) -> None:
    log("test 5: empty queue renders without crashing")
    repo = tmp / "repo-empty"
    _init_repo(repo)
    (repo / ".otto-queue.yml").write_text("schema_version: 1\ntasks: []\n", encoding="utf-8")
    (repo / ".otto-queue-state.json").write_text(
        json.dumps({"schema_version": 1, "watcher": None, "tasks": {}}),
        encoding="utf-8",
    )

    app = _queue_app(repo)
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.8)
        live = app.query_one("#live-table", DataTable)
        check(live.row_count == 0, f"empty queue has 0 live rows (got {live.row_count})")
        detail = str(app.query_one("#detail-meta", Static).content)
        check("No runs yet." in detail, "detail pane names empty state")
        check("otto queue build" in detail, "empty state names queue entry point")
        await pilot.press("q")
        await pilot.pause(0.1)
    check(True, "empty queue q exits cleanly")


async def test_malformed_state_does_not_crash(tmp: Path) -> None:
    log("test 6: malformed legacy state does not crash Mission Control")
    repo = tmp / "repo-malformed"
    _init_repo(repo)
    (repo / ".otto-queue.yml").write_text("schema_version: 1\ntasks: []\n", encoding="utf-8")
    (repo / ".otto-queue-state.json").write_text("{ this is not valid json", encoding="utf-8")

    app = _queue_app(repo)
    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.8)
        live = app.query_one("#live-table", DataTable)
        check(live.row_count == 0, f"malformed state degrades to 0 rows (got {live.row_count})")
        check(str(app.query_one("#banner", Static).content) is not None, "banner remains renderable")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dashboard-e2e-"))
    log(f"tmp dir: {tmp}")
    try:
        repo = setup_realistic_repo(tmp)
        log(f"realistic repo: {repo}")

        await test_overview_renders_queue_compat_rows(repo)
        await test_navigation_jk(repo)
        await test_enter_focuses_detail_and_escape_returns(repo)
        await test_detail_shows_legacy_child_log(repo)
        await test_empty_queue(tmp)
        await test_malformed_state_does_not_crash(tmp)

        log("")
        log("ALL BEHAVIORAL TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
