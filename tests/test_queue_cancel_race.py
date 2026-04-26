"""Regression coverage for W2-IMPORTANT-3 — cancellation must beat watcher
pickup even when the cancel command lands *between* the tick's first
command-drain and the dispatch step.

Background
==========
The watcher's main tick was structured as:
    1. begin_command_drain  (move .otto-queue-commands.jsonl → .processing)
    2. apply commands       (cancel/remove/etc.)
    3. dispatch             (pick `queued` tasks → spawn children)

If a cancel POST landed in step 1.5 (after the drain, before dispatch)
the cancel went to the *new* commands.jsonl. The current tick's dispatch
ran without seeing it, started the task, and the cancel wasn't applied
until the *next* tick. By then the SIGTERM hit a running child instead
of a queued task, and the task may have already done meaningful work.

The fix is a second drain pass right before dispatch. This test
simulates the race by injecting a cancel command between command
application and dispatch, and asserts the dispatched task never spawns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from otto.queue.runner import Runner, RunnerConfig
from otto.queue.schema import (
    QueueTask,
    append_command,
    append_task,
    load_state,
)
from tests._helpers import init_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enqueue(repo: Path, task_id: str = "racey") -> QueueTask:
    task = QueueTask(
        id=task_id,
        command_argv=["build", task_id],
        branch=f"build/{task_id}",
        worktree=f".worktrees/{task_id}",
        added_at="2026-04-23T12:00:00Z",
        resolved_intent=f"build the {task_id} feature",
    )
    append_task(repo, task)
    return task


def _runner(repo: Path) -> Runner:
    # Use /bin/true so spawn never has real side effects (the test asserts
    # that spawn isn't called, but belt-and-braces).
    return Runner(repo, RunnerConfig(concurrent=2), otto_bin="/bin/true")


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------


def test_late_cancel_arriving_mid_tick_blocks_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a cancel command after first-drain but before dispatch.

    Without the second-drain fix, the dispatch loop would spawn the
    queued task. With the fix, the late cancel is observed before
    dispatch, the task moves to status="cancelled", and `_dispatch_new`
    skips it.
    """
    repo = init_repo(tmp_path)
    _enqueue(repo, "racey")

    runner = _runner(repo)

    spawn_calls: list[str] = []

    def fake_spawn(task: QueueTask, state: dict[str, Any]) -> None:
        spawn_calls.append(task.id)
        # Mimic the real spawn's bookkeeping just enough so subsequent
        # state assertions are sensible.
        state["tasks"][task.id] = {
            **state["tasks"].get(task.id, {}),
            "status": "starting",
        }

    monkeypatch.setattr(runner, "_spawn", fake_spawn)

    # Inject the late cancel as a side-effect of `_dispatch_new` being
    # called: append the command between drain (already happened in
    # _tick) and the dispatch's task scan. We do it by patching
    # _dispatch_new with a wrapper that posts the cancel *before*
    # delegating to the real dispatch — but the real dispatch will only
    # see the cancel if the second-drain fix re-pulled it.
    real_dispatch = runner._dispatch_new

    def wrapped_dispatch(
        tasks: list[QueueTask],
        state: dict[str, Any],
        cycle_ids: set[str],
        **kwargs: Any,
    ) -> None:
        # Before this point the tick has already drained commands once.
        # Append a cancel now to simulate the race window — the second
        # drain inside _dispatch_new should pick it up.
        append_command(repo, {"cmd": "cancel", "id": "racey"})
        real_dispatch(tasks, state, cycle_ids, **kwargs)

    monkeypatch.setattr(runner, "_dispatch_new", wrapped_dispatch)

    # Run one tick. With the fix, second-drain catches the cancel and
    # the task is cancelled before dispatch sees `queued`.
    runner._tick()

    # Verify spawn was NOT called.
    assert spawn_calls == [], (
        f"queued task was spawned despite a cancel arriving mid-tick: {spawn_calls!r}"
    )

    # Verify task ended up cancelled (or removed/snapshotted) — definitely
    # not in a starting/running state.
    state = load_state(repo)
    ts = state["tasks"].get("racey", {})
    status = ts.get("status")
    assert status == "cancelled", (
        f"expected status='cancelled' after late cancel, got {status!r}"
    )


def test_normal_dispatch_still_runs_when_no_late_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: a normal tick (no late cancel) still spawns
    the queued task. Confirms the second-drain fix doesn't accidentally
    block legitimate dispatch."""
    repo = init_repo(tmp_path)
    _enqueue(repo, "happy")

    runner = _runner(repo)

    spawn_calls: list[str] = []

    def fake_spawn(task: QueueTask, state: dict[str, Any]) -> None:
        spawn_calls.append(task.id)
        state["tasks"][task.id] = {
            **state["tasks"].get(task.id, {}),
            "status": "starting",
        }

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    runner._tick()

    assert spawn_calls == ["happy"], (
        f"queued task should have been dispatched, got {spawn_calls!r}"
    )


def test_late_cancel_on_one_of_two_queued_only_blocks_targeted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When two tasks are queued and a late cancel targets only one,
    the other still dispatches. Verifies the fix is surgical, not a
    blanket dispatch suppression."""
    repo = init_repo(tmp_path)
    _enqueue(repo, "kept")
    _enqueue(repo, "killed")

    runner = _runner(repo)
    spawn_calls: list[str] = []

    def fake_spawn(task: QueueTask, state: dict[str, Any]) -> None:
        spawn_calls.append(task.id)
        state["tasks"][task.id] = {
            **state["tasks"].get(task.id, {}),
            "status": "starting",
        }

    monkeypatch.setattr(runner, "_spawn", fake_spawn)

    real_dispatch = runner._dispatch_new

    def wrapped_dispatch(
        tasks: list[QueueTask],
        state: dict[str, Any],
        cycle_ids: set[str],
        **kwargs: Any,
    ) -> None:
        append_command(repo, {"cmd": "cancel", "id": "killed"})
        real_dispatch(tasks, state, cycle_ids, **kwargs)

    monkeypatch.setattr(runner, "_dispatch_new", wrapped_dispatch)

    runner._tick()

    assert "kept" in spawn_calls, f"kept task should have spawned, got {spawn_calls!r}"
    assert "killed" not in spawn_calls, (
        f"killed task should have been blocked by late cancel, got {spawn_calls!r}"
    )
    state = load_state(repo)
    assert state["tasks"]["killed"]["status"] == "cancelled"
