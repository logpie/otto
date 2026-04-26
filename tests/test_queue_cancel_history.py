"""Regression coverage for W2-CRITICAL-1: cancelled queue tasks must
not vanish from /api/state.

Background
==========
Before the fix, cancelling a *queued* (never-started) queue task did:
    1. Mark the task `cancelled` in queue state.
    2. Snapshot + remove the task definition.
    3. Skip the history append (no attempt_run_id) → no row anywhere.
    4. Result: the row vanished from /api/state — no live, no history,
       just an orphan manifest entry on disk.

The atomic-domain cancel path (W12a) wrote a terminal live record and
the history surfaced correctly. These tests pin both paths so the queue
path can never silently drop the row again.

Each test uses ``MissionControlService`` directly so we don't need to
spawn the watcher / web backend / browser.
"""

from __future__ import annotations

from pathlib import Path


from otto import paths
from otto.mission_control.service import MissionControlService
from otto.queue.runner import Runner, RunnerConfig
from otto.queue.schema import QueueTask, append_task, load_state
from otto.runs.history import append_history_snapshot, build_terminal_snapshot, read_history_rows
from otto.runs.registry import finalize_record, make_run_record, write_record

from tests._helpers import init_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enqueue_task(repo: Path, task_id: str = "queued1") -> QueueTask:
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


def _cancel_via_runner(repo: Path, task_id: str) -> None:
    """Apply the queue cancel command end-to-end through the runner.

    Mirrors what the watcher's main loop does in production: drain the
    JSONL command file, apply the command (sets status → cancelled),
    persist state, then run the maintenance pass that finalizes the
    terminal queue history snapshot before removing the task definition.
    """
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    runner._apply_command({"cmd": "cancel", "id": task_id}, state)
    # Persist the cancel transition first — the maintenance pass reads
    # the persisted state via load_queue/load_state internals.
    from otto.queue.schema import write_state
    write_state(repo, state)
    # Run the same maintenance steps the watcher tick runs (lines 529-531
    # in queue/runner.py).
    from otto.queue.schema import load_queue
    tasks = load_queue(repo)
    runner._repair_terminal_queue_history(tasks, state)
    runner._cleanup_removed_task_definitions(tasks, state)
    write_state(repo, state)


def _api_state_history_items(repo: Path) -> list[dict]:
    service = MissionControlService(repo)
    return service.state()["history"]["items"]


def _api_state_live_items(repo: Path) -> list[dict]:
    service = MissionControlService(repo)
    return service.state()["live"]["items"]


# ---------------------------------------------------------------------------
# Queue-domain cancel — the bug
# ---------------------------------------------------------------------------


def test_queue_cancel_appears_in_history(tmp_path: Path) -> None:
    """A queue-domain cancel must surface in /api/state history.

    Before the fix this list was empty for cancelled queued tasks
    because no history snapshot was ever appended.
    """
    repo = init_repo(tmp_path)
    _enqueue_task(repo, "queued1")
    _cancel_via_runner(repo, "queued1")

    items = _api_state_history_items(repo)
    cancelled = [
        row
        for row in items
        if row.get("queue_task_id") == "queued1" or "queued1" in (row.get("run_id") or "")
    ]
    assert cancelled, (
        "expected a history row for cancelled queue task 'queued1' but got "
        f"{[row.get('run_id') for row in items]}"
    )
    assert len(cancelled) == 1, f"expected exactly one history row, got {cancelled}"
    row = cancelled[0]
    assert row["status"] == "cancelled", row
    assert row["terminal_outcome"] == "cancelled", row
    assert row["domain"] == "queue", row
    assert row["queue_task_id"] == "queued1", row


def test_queue_cancel_history_dedupes_across_repeat_finalize(tmp_path: Path) -> None:
    """Repeat maintenance ticks must not append duplicate cancel rows.

    Before the dedupe key was anchored to a stable synthetic run_id, every
    watcher tick after the cancel could write another row.
    """
    repo = init_repo(tmp_path)
    _enqueue_task(repo, "queued1")
    _cancel_via_runner(repo, "queued1")
    # Run a second "tick" worth of maintenance to confirm dedupe.
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    from otto.queue.schema import load_queue, write_state
    tasks = load_queue(repo)
    runner._repair_terminal_queue_history(tasks, state)
    runner._cleanup_removed_task_definitions(tasks, state)
    write_state(repo, state)

    items = _api_state_history_items(repo)
    cancelled = [
        row
        for row in items
        if row.get("queue_task_id") == "queued1"
    ]
    assert len(cancelled) == 1, cancelled


def test_queue_cancel_writes_terminal_history_snapshot_on_disk(tmp_path: Path) -> None:
    """Verify the underlying history.jsonl actually has the snapshot row.

    The /api/state contract reads from the same file via
    load_project_history_rows, but pinning the disk format is a smaller,
    faster check that catches schema regressions independently of the
    higher-level model layer.
    """
    repo = init_repo(tmp_path)
    _enqueue_task(repo, "queued1")
    _cancel_via_runner(repo, "queued1")

    rows = read_history_rows(paths.history_jsonl(repo))
    assert rows, "expected at least one history snapshot to be written"
    cancel_rows = [
        row
        for row in rows
        if row.get("queue_task_id") == "queued1" and row.get("status") == "cancelled"
    ]
    assert cancel_rows, f"no cancelled snapshot for queued1 in {rows}"
    snapshot = cancel_rows[-1]
    assert snapshot["schema_version"] == 2, snapshot
    assert snapshot["history_kind"] == "terminal_snapshot", snapshot
    assert snapshot["terminal_outcome"] == "cancelled", snapshot
    assert snapshot["domain"] == "queue", snapshot
    # dedupe_key is stable so requeue+cancel of the same id with same
    # added_at would dedupe; this is the desired behaviour.
    assert snapshot["dedupe_key"].startswith("terminal_snapshot:queue-cancel:queued1"), snapshot


# ---------------------------------------------------------------------------
# Atomic-domain cancel — the working baseline (regression-pin)
# ---------------------------------------------------------------------------


def test_atomic_cancel_appears_in_history(tmp_path: Path) -> None:
    """Atomic-domain cancel works correctly today (W12a) — pin it.

    If a future refactor accidentally broke atomic cancel along with
    the queue fix, this test catches it. No `definition_removal_pending`
    bookkeeping is needed: atomic runs write their own live record and
    finalize via the standard registry path.
    """
    repo = init_repo(tmp_path)
    run_id = "build-atomic-1"
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name=f"build: {run_id}",
        status="running",
        cwd=repo,
        identity={"queue_task_id": None, "merge_id": None, "parent_run_id": None},
        adapter_key="atomic.build",
        last_event="started",
        intent={"summary": "an atomic build under cancel test"},
    )
    write_record(repo, record)
    finalize_record(
        repo,
        run_id,
        status="cancelled",
        terminal_outcome="cancelled",
    )
    # Append the terminal history snapshot the way the atomic pipeline
    # would on shutdown.
    append_history_snapshot(
        repo,
        build_terminal_snapshot(
            run_id=run_id,
            domain="atomic",
            run_type="build",
            command="build",
            intent_meta={"summary": "an atomic build under cancel test"},
            status="cancelled",
            terminal_outcome="cancelled",
            timing={"started_at": "2026-04-23T12:00:00Z", "finished_at": "2026-04-23T12:01:00Z"},
        ),
        strict=True,
    )

    items = _api_state_history_items(repo)
    cancelled = [row for row in items if row.get("run_id") == run_id]
    assert cancelled, [row.get("run_id") for row in items]
    assert cancelled[0]["status"] == "cancelled"
    assert cancelled[0]["terminal_outcome"] == "cancelled"
    assert cancelled[0]["domain"] == "atomic"


# ---------------------------------------------------------------------------
# Mid-cancel intermediate state — task is gone from queue but fully terminal
# ---------------------------------------------------------------------------


def test_queue_cancel_task_definition_is_removed_after_history_lands(tmp_path: Path) -> None:
    """Cleanup must wait until the terminal history snapshot is written.

    Before the fix, cleanup removed the task definition unconditionally
    when there was no attempt_run_id, racing the (skipped) history
    append. The strengthened cleanup guard now waits for
    `history_appended` regardless of whether a real run id exists.
    """
    repo = init_repo(tmp_path)
    _enqueue_task(repo, "queued1")
    _cancel_via_runner(repo, "queued1")

    # Definition should be removed (queue is empty) AND history present.
    from otto.queue.schema import load_queue
    assert load_queue(repo) == [], "task definition should be cleaned up after history lands"

    rows = read_history_rows(paths.history_jsonl(repo))
    assert any(
        row.get("queue_task_id") == "queued1" and row.get("status") == "cancelled"
        for row in rows
    ), "history must contain the cancelled snapshot before cleanup runs"
