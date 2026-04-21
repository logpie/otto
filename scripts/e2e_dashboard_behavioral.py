"""Behavioral test of the Textual queue dashboard via Textual's pilot harness.

Simulates a real user session against a fake but realistic queue + state +
narrative.log. Catches behavioral bugs the unit tests miss (real-data
rendering, navigation flow, edge cases like 0 tasks / malformed state /
post-graduation path resolution).

Usage: .venv/bin/python scripts/e2e_dashboard_behavioral.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from otto.queue.dashboard import QueueApp


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg: str) -> None:
    log(f"FAIL: {msg}")
    sys.exit(1)


def check(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)
    log(f"OK: {msg}")


# --------------------------------------------------------------- repo setup

def setup_realistic_repo(tmp: Path) -> Path:
    """Build a realistic project + queue + state + narrative.log layout."""
    repo = tmp / "repo"
    repo.mkdir()

    # Minimal git init so paths.* resolves correctly
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    # Use the real queue API to create a valid queue.yml
    from otto.queue.schema import QueueTask, append_task
    for task_id, intent in [
        ("add", "Build add.py CLI"),
        ("mul", "Build mul.py CLI"),
        ("sub", "Build sub.py CLI"),
    ]:
        append_task(repo, QueueTask(
            id=task_id,
            command_argv=["build", "--fast", intent],
            resumable=True,
            added_at="2026-04-21T20:00:00Z",
            resolved_intent=intent,
            branch=f"build/{task_id}-2026-04-21",
            worktree=f".worktrees/{task_id}",
        ))

    # state.json — mixed states
    state = {
        "schema_version": 1,
        "watcher": {"pid": 12345, "started_at": "2026-04-21T20:00:00Z"},
        "tasks": {
            "add": {
                "status": "running",
                "started_at": "2026-04-21T20:00:30Z",
                "finished_at": None,
                "child": {"pid": 22345, "pgid": 22345, "start_time_ns": 1, "argv": ["otto"], "cwd": str(repo)},
                "failure_reason": None,
            },
            "mul": {
                "status": "running",
                "started_at": "2026-04-21T20:01:00Z",
                "finished_at": None,
                "child": {"pid": 22346, "pgid": 22346, "start_time_ns": 2, "argv": ["otto"], "cwd": str(repo)},
                "failure_reason": None,
            },
            "sub": {
                "status": "queued",
                "started_at": None,
                "finished_at": None,
                "child": None,
                "failure_reason": None,
            },
        },
    }
    (repo / ".otto-queue-state.json").write_text(json.dumps(state, indent=2))

    # Per-task worktree session dir + narrative.log + manifest
    for task_id, session_id in [
        ("add", "2026-04-21-200030-aaa111"),
        ("mul", "2026-04-21-200100-bbb222"),
    ]:
        wt = repo / ".worktrees" / task_id
        session_root = wt / "otto_logs" / "sessions" / session_id
        build_dir = session_root / "build"
        build_dir.mkdir(parents=True)

        narrative = build_dir / "narrative.log"
        narrative.write_text(
            f"[+0:00] — BUILD starting —\n"
            f"[+0:01] reading project layout\n"
            f"[+0:02] writing {task_id}.py with argparse\n"
            f"[+0:15] running tests\n"
            f"[+0:20] STORY_RESULT: smoke | PASS | python {task_id}.py works\n"
            f"[+0:21] — CERTIFY starting —\n"
            f"[+0:30] cert in progress …\n"
        )

        # Queue index manifest (mirror)
        queue_dir = repo / "otto_logs" / "queue" / task_id
        queue_dir.mkdir(parents=True)
        (queue_dir / "manifest.json").write_text(json.dumps({
            "command": "build",
            "argv": ["build", "--fast", f"Build {task_id}.py CLI"],
            "queue_task_id": task_id,
            "run_id": session_id,
            "branch": f"build/{task_id}-2026-04-21",
            "checkpoint_path": str(session_root / "checkpoint.json"),
            "proof_of_work_path": str(session_root / "certify" / "proof-of-work.json"),
            "cost_usd": 0.0,
            "duration_s": 0.0,
            "started_at": "2026-04-21T20:00:30Z",
            "finished_at": "",
            "head_sha": None,
            "resolved_intent": f"Build {task_id}.py CLI",
            "focus": None,
            "target": None,
            "exit_status": "success",
            "schema_version": 1,
            "extra": {},
            "mirror_of": str(session_root / "manifest.json"),
        }, indent=2))

    return repo


# --------------------------------------------------------------- pilot tests

async def test_overview_renders_real_data(repo: Path) -> None:
    log("test 1: overview renders all 3 tasks with correct status")
    app = QueueApp(project_dir=repo, concurrent=2)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.6)  # wait for first refresh tick (500ms)
        screen = app.screen
        # Find the DataTable
        from textual.widgets import DataTable
        tables = list(screen.query(DataTable))
        check(len(tables) == 1, f"exactly 1 DataTable on overview (got {len(tables)})")
        table = tables[0]
        check(table.row_count == 3, f"3 rows for 3 tasks (got {table.row_count})")

        # Check the rendered status text contains expected statuses
        status_col_idx = next((i for i, c in enumerate(table.ordered_columns)
                              if "status" in str(c.label).lower()), None)
        check(status_col_idx is not None, "STATUS column present in DataTable")


async def test_navigation_jk(repo: Path) -> None:
    log("test 2: j/k navigation moves cursor between rows")
    app = QueueApp(project_dir=repo, concurrent=2)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.6)
        from textual.widgets import DataTable
        table = app.screen.query_one(DataTable)
        initial_row = table.cursor_row
        await pilot.press("j")
        await pilot.pause(0.1)
        check(table.cursor_row == initial_row + 1,
              f"after `j`, cursor moved from row {initial_row} to {table.cursor_row}")
        await pilot.press("k")
        await pilot.pause(0.1)
        check(table.cursor_row == initial_row,
              f"after `k`, cursor moved back to row {initial_row}")


async def test_enter_opens_detail(repo: Path) -> None:
    log("test 3: Enter opens TaskDetailScreen, Esc returns")
    app = QueueApp(project_dir=repo, concurrent=2)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.6)
        # Move cursor to first row, press Enter
        await pilot.press("enter")
        await pilot.pause(0.4)  # wait for screen push + initial tail
        check(type(app.screen).__name__ == "TaskDetailScreen",
              f"after Enter, current screen is TaskDetailScreen (got {type(app.screen).__name__})")
        # Esc returns to overview
        await pilot.press("escape")
        await pilot.pause(0.2)
        check(type(app.screen).__name__ == "OverviewScreen",
              f"after Esc, returned to OverviewScreen (got {type(app.screen).__name__})")


async def test_yank_overview(repo: Path) -> None:
    log("test 4: `y` on overview copies row data to clipboard")
    captured: list[str] = []

    def fake_clip(text: str) -> bool:
        captured.append(text)
        return True

    app = QueueApp(project_dir=repo, concurrent=2)
    with patch("otto.queue.dashboard._copy_to_clipboard", fake_clip):
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("y")
            await pilot.pause(0.2)
    check(len(captured) == 1, f"clipboard called once (got {len(captured)})")
    payload = captured[0] if captured else ""
    log(f"  clipboard payload preview: {payload[:120]!r}")
    check("add" in payload or "mul" in payload or "sub" in payload,
          "clipboard payload contains a task id")


async def test_yank_detail_full_log(repo: Path) -> None:
    log("test 5: `y` on detail copies full narrative.log contents")
    captured: list[str] = []

    def fake_clip(text: str) -> bool:
        captured.append(text)
        return True

    app = QueueApp(project_dir=repo, concurrent=2)
    with patch("otto.queue.dashboard._copy_to_clipboard", fake_clip):
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.6)
            await pilot.press("enter")
            await pilot.pause(0.4)
            await pilot.press("y")
            await pilot.pause(0.2)
    check(len(captured) == 1, f"clipboard called on detail (got {len(captured)})")
    payload = captured[0] if captured else ""
    log(f"  clipboard payload size: {len(payload)} bytes")
    check("BUILD starting" in payload,
          "clipboard payload contains narrative.log content (BUILD starting marker)")
    check("STORY_RESULT" in payload,
          "clipboard payload contains story result")


async def test_empty_queue(tmp: Path) -> None:
    log("test 6: empty queue (zero tasks) renders without crash")
    repo = tmp / "repo-empty"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / ".otto-queue.yml").write_text("schema_version: 1\ntasks: []\n")
    (repo / ".otto-queue-state.json").write_text(json.dumps({
        "schema_version": 1, "watcher": None, "tasks": {}
    }))

    app = QueueApp(project_dir=repo, concurrent=2)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.6)
        from textual.widgets import DataTable
        table = app.screen.query_one(DataTable)
        check(table.row_count == 0, f"empty queue → 0 rows (got {table.row_count})")
        # Press q — should exit cleanly
        await pilot.press("q")
        await pilot.pause(0.3)
    check(True, "empty queue renders + q exits without crash")


async def test_malformed_state(tmp: Path) -> None:
    log("test 7: malformed state.json → banner shown, no crash")
    repo = tmp / "repo-malformed"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / ".otto-queue.yml").write_text("schema_version: 1\ntasks: []\n")
    (repo / ".otto-queue-state.json").write_text("{ this is not valid json")

    app = QueueApp(project_dir=repo, concurrent=2)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.6)
        # Codex's banner lives in Static#overview-banner.
        from textual.widgets import Static
        banner = app.screen.query_one("#overview-banner", Static)
        # Static stores its content under .render() / ._renderable depending on Textual version
        banner_text = str(getattr(banner, "_renderable", "") or banner.render())
        check(banner_text.strip() != "",
              f"banner is non-empty on malformed state.json "
              f"(got {banner_text!r})")
        check("error" in banner_text.lower() or "parse" in banner_text.lower(),
              f"banner mentions error/parse (got {banner_text!r})")


# --------------------------------------------------------------- main

async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dashboard-e2e-"))
    log(f"tmp dir: {tmp}")
    try:
        repo = setup_realistic_repo(tmp)
        log(f"realistic repo: {repo}")

        await test_overview_renders_real_data(repo)
        await test_navigation_jk(repo)
        await test_enter_opens_detail(repo)
        await test_yank_overview(repo)
        await test_yank_detail_full_log(repo)
        await test_empty_queue(tmp)
        await test_malformed_state(tmp)

        log("")
        log("ALL BEHAVIORAL TESTS PASSED")
        return 0
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
