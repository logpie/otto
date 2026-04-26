"""Tests for otto/queue/runner.py.

Uses tiny shell commands (`/bin/sh -c "exit 0"`) instead of full otto
subprocesses to keep tests fast and deterministic. PID-reuse safety +
reconciliation are tested via direct state.json edits — real-PID-recycling
tests are inherently flaky.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from otto import paths as _paths
from otto.queue.runtime import INTERRUPTED_STATUS
import otto.queue.runner as runner_module
from otto.queue.runner import (
    Runner,
    RunnerConfig,
    WatcherAlreadyRunning,
    acquire_lock,
    child_is_alive,
    kill_child_safely,
    runner_config_from_otto_config,
)
from otto.queue.schema import (
    QueueTask,
    append_command,
    append_command_ack,
    append_task,
    commands_path,
    commands_processing_path,
    load_queue,
    load_state,
    remove_task,
    write_state,
)
from tests._helpers import init_repo


def _write_resume_checkpoint(
    repo: Path,
    task: QueueTask,
    session_id: str = "2026-04-22-010203-abc123",
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    worktree = repo / (task.worktree or f".worktrees/{task.id}")
    _paths.ensure_session_scaffold(worktree, session_id)
    _paths.session_checkpoint(worktree, session_id).write_text(
        json.dumps({
            "status": "in_progress",
            "updated_at": "2026-04-22T01:02:03Z",
            **(extra or {}),
        }),
        encoding="utf-8",
    )


# ---------- acquire_lock ----------


def test_acquire_lock_succeeds_first_time(tmp_path: Path):
    fh = acquire_lock(tmp_path)
    assert fh is not None
    fh.close()


def test_failed_queue_lock_acquire_does_not_truncate_holder(tmp_path: Path):
    fh = acquire_lock(tmp_path)
    try:
        lock = runner_module.lock_path(tmp_path)
        before = lock.read_text(encoding="utf-8")

        with pytest.raises(WatcherAlreadyRunning):
            acquire_lock(tmp_path)

        assert lock.read_text(encoding="utf-8") == before
    finally:
        fh.close()


def test_acquire_lock_refuses_second_holder(tmp_path: Path):
    fh1 = acquire_lock(tmp_path)
    try:
        with pytest.raises(WatcherAlreadyRunning):
            acquire_lock(tmp_path)
    finally:
        fh1.close()


def test_acquire_lock_releasable(tmp_path: Path):
    fh1 = acquire_lock(tmp_path)
    fh1.close()
    # After close, lock can be re-acquired
    fh2 = acquire_lock(tmp_path)
    fh2.close()


def test_run_iteration_tick_failure_cleans_up_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    calls: list[str] = []
    monkeypatch.setattr(runner, "_tick", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(runner, "_kill_all_in_flight", lambda *, force=False: calls.append(f"kill:{force}"))
    monkeypatch.setattr(runner, "_clear_watcher_state", lambda: calls.append("clear"))

    _last_heartbeat, exit_code, done = runner._run_iteration(123.0)

    assert exit_code == 1
    assert done is False
    assert calls == ["kill:True", "clear"]


def test_run_iteration_heartbeat_failure_cleans_up_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(heartbeat_interval_s=0.0), otto_bin="/bin/true")
    calls: list[str] = []
    monkeypatch.setattr(runner, "_tick", lambda: None)
    monkeypatch.setattr(
        runner,
        "_update_watcher_state",
        lambda: (_ for _ in ()).throw(runner_module.StatePersistenceError("disk full")),
    )
    monkeypatch.setattr(runner, "_kill_all_in_flight", lambda *, force=False: calls.append(f"kill:{force}"))
    monkeypatch.setattr(runner, "_clear_watcher_state", lambda: calls.append("clear"))

    _last_heartbeat, exit_code, done = runner._run_iteration(0.0)

    assert exit_code == 1
    assert done is False
    assert calls == ["kill:True", "clear"]


# ---------- child_is_alive (PID-reuse safety) ----------


def test_child_is_alive_true_for_actual_process(tmp_path: Path):
    """Spawn a real subprocess; verify the validation passes for it."""
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], preexec_fn=os.setsid)
    try:
        time.sleep(0.05)  # let exec() complete so psutil sees the real argv
        import psutil
        start_time_ns = int(psutil.Process(proc.pid).create_time() * 1_000_000_000)
        child = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "start_time_ns": start_time_ns,
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "cwd": os.getcwd(),
        }
        assert child_is_alive(child) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_child_is_alive_false_for_dead_pid(tmp_path: Path):
    """Spawn + immediately wait; pid is dead afterward."""
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"], preexec_fn=os.setsid)
    proc.wait()
    child = {
        "pid": proc.pid,
        "pgid": proc.pid,
        "start_time_ns": int(time.time() * 1_000_000_000),
        "argv": ["/bin/sh", "-c", "exit 0"],
        "cwd": os.getcwd(),
    }
    assert child_is_alive(child) is False


def test_child_is_alive_false_on_start_time_mismatch(tmp_path: Path):
    """PID-reuse safety: synthetic state with wrong start_time → not our child."""
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], preexec_fn=os.setsid)
    try:
        # Provide a deliberately wrong start_time_ns (1 second off — well outside the 100ms tolerance)
        bad_start = int((time.time() - 1.0) * 1_000_000_000)
        child = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "start_time_ns": bad_start,
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "cwd": os.getcwd(),
        }
        # We still trust the PID alone if start_time mismatches — but the
        # function should detect the mismatch and return False
        assert child_is_alive(child) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_child_is_alive_false_on_cwd_mismatch(tmp_path: Path):
    """Synthetic state with wrong cwd → not our child."""
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], preexec_fn=os.setsid)
    try:
        time.sleep(0.05)
        import psutil
        start_time_ns = int(psutil.Process(proc.pid).create_time() * 1_000_000_000)
        child = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "start_time_ns": start_time_ns,
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "cwd": "/totally/different/path/does/not/exist",
        }
        assert child_is_alive(child) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_kill_child_safely_kills_alive_child(tmp_path: Path):
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], preexec_fn=os.setsid)
    try:
        import psutil
        start_time_ns = int(psutil.Process(proc.pid).create_time() * 1_000_000_000)
        child = {
            "pid": proc.pid, "pgid": proc.pid,
            "start_time_ns": start_time_ns,
            "argv": ["/bin/sh", "-c", "sleep 30"],
            "cwd": os.getcwd(),
        }
        assert kill_child_safely(child, signal.SIGTERM) is True
        # Wait for it to actually exit
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_kill_child_safely_refuses_dead_child(tmp_path: Path):
    """If the recorded child no longer matches, refuse to send the signal."""
    child = {
        "pid": 1,  # init — definitely won't match argv
        "pgid": 1,
        "start_time_ns": 0,
        "argv": ["impossible-cmd"],
        "cwd": "/tmp",
    }
    # Should refuse without sending anything to PID 1
    assert kill_child_safely(child, signal.SIGTERM) is False


# ---------- end-to-end runner: dispatch + reap ----------


def _make_fake_otto(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    sleep: float = 0.1,
    write_manifest: bool = True,
    write_ready: bool = True,
) -> Path:
    """Write a tiny shell script that mimics otto: sleeps, optionally writes
    a manifest at the queue path, and exits with `exit_code`."""
    fake = tmp_path / "fake_otto.sh"
    ready_block = ""
    if write_ready:
        ready_block = '''
TASK_ID="${OTTO_QUEUE_TASK_ID:-}"
RUN_ID="${OTTO_RUN_ID:-fake-run}"
if [ -n "$TASK_ID" ]; then
  READY_DIR="${OTTO_QUEUE_PROJECT_DIR}/otto_logs/queue/${TASK_ID}"
  mkdir -p "$READY_DIR"
  cat > "$READY_DIR/ready.json" <<EOF
{
  "schema_version": 1,
  "task_id": "$TASK_ID",
  "run_id": "$RUN_ID",
  "phase": "build",
  "ready_at": "2026-04-19T00:00:00Z",
  "pid": $$,
  "cwd": "$(pwd)",
  "session_dir": "$(pwd)/otto_logs/sessions/${RUN_ID}",
  "checkpoint_path": "$(pwd)/otto_logs/sessions/${RUN_ID}/checkpoint.json"
}
EOF
fi
'''
    manifest_block = ""
    if write_manifest:
        manifest_block = '''
TASK_ID="${OTTO_QUEUE_TASK_ID:-}"
RUN_ID="${OTTO_RUN_ID:-fake-run}"
if [ -n "$TASK_ID" ]; then
  MANIFEST_DIR="${OTTO_QUEUE_PROJECT_DIR}/otto_logs/queue/${TASK_ID}"
  mkdir -p "$MANIFEST_DIR"
  cat > "$MANIFEST_DIR/manifest.json" <<EOF
{
  "command": "build",
  "argv": ["build", "test"],
  "queue_task_id": "$TASK_ID",
  "run_id": "$RUN_ID",
  "branch": null,
  "checkpoint_path": null,
  "proof_of_work_path": null,
  "cost_usd": 0.42,
  "duration_s": 1.0,
  "started_at": "2026-04-19T00:00:00Z",
  "finished_at": "2026-04-19T00:00:01Z",
  "head_sha": null,
  "resolved_intent": "test",
  "focus": null,
  "target": null,
  "exit_status": "success",
  "schema_version": 1,
  "extra": {}
}
EOF
fi
'''
    fake.write_text(f"""#!/bin/sh
{ready_block}
sleep {sleep}
{manifest_block}
exit {exit_code}
""")
    fake.chmod(0o755)
    return fake


def _tick_until(
    runner: Runner,
    repo: Path,
    predicate,
    *,
    timeout_s: float = 1.0,
    interval_s: float = 0.02,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    state = load_state(repo)
    while time.monotonic() < deadline:
        runner._tick()
        state = load_state(repo)
        if predicate(state):
            return state
        time.sleep(interval_s)
    raise AssertionError(f"condition not met before timeout; state={state!r}")


def _tick_until_task_status(
    runner: Runner,
    repo: Path,
    task_id: str,
    statuses: set[str],
    *,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    return _tick_until(
        runner,
        repo,
        lambda state: state["tasks"].get(task_id, {}).get("status") in statuses,
        timeout_s=timeout_s,
    )


def _child_snapshot(proc: subprocess.Popen[Any], *, cwd: str, argv: list[str]) -> dict[str, Any]:
    import psutil

    return {
        "pid": proc.pid,
        "pgid": proc.pid,
        "start_time_ns": int(psutil.Process(proc.pid).create_time() * 1_000_000_000),
        "argv": argv,
        "cwd": cwd,
    }


def _write_queue_manifest(repo: Path, task_id: str, *, exit_status: str = "success") -> Path:
    manifest_path = repo / "otto_logs" / "queue" / task_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "command": "build",
        "argv": ["build", "test"],
        "queue_task_id": task_id,
        "run_id": "fake-run",
        "branch": None,
        "checkpoint_path": None,
        "proof_of_work_path": None,
        "cost_usd": 0.42,
        "duration_s": 1.0,
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": "2026-04-19T00:00:01Z",
        "head_sha": None,
        "resolved_intent": "test",
        "focus": None,
        "target": None,
        "exit_status": exit_status,
        "schema_version": 1,
        "extra": {},
    }))
    return manifest_path


def test_finalize_paused_manifest_marks_task_paused(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume"), otto_bin="/bin/true")
    _write_queue_manifest(repo, "t1", exit_status="paused")
    ts = {
        "status": "running",
        "started_at": "2026-04-19T00:00:00Z",
        "child": {"pid": 123456, "pgid": 123456},
    }

    runner._finalize_task_from_manifest(ts, "t1", exit_code=0)

    assert ts["status"] == "paused"
    assert ts["child"] is None
    assert ts["failure_reason"] == "paused; resume available"


def _spawn_orphan_child(*, cwd: Path, command: str) -> dict[str, Any]:
    script = """
import json
import os
import subprocess
import sys
import time

import psutil

cwd = sys.argv[1]
command = sys.argv[2]
child = subprocess.Popen(
    ["/bin/sh", "-c", command],
    cwd=cwd,
    preexec_fn=os.setsid,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(0.05)
print(json.dumps({
    "pid": child.pid,
    "pgid": os.getpgid(child.pid),
    "start_time_ns": int(psutil.Process(child.pid).create_time() * 1_000_000_000),
    "argv": ["/bin/sh", "-c", command],
    "cwd": cwd,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(cwd), command],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_runner_dispatches_and_reaps_a_simple_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=0.1)
    # The runner sets OTTO_QUEUE_PROJECT_DIR on spawn — fake_otto reads it.
    # Enqueue one task
    append_task(repo, QueueTask(
        id="t1", command_argv=["build", "test"],
        resolved_intent="test", added_at="2026-04-19T00:00:00Z",
        branch="build/t1-test", worktree=".worktrees/t1",
    ))
    cfg = RunnerConfig(concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5)
    runner = Runner(repo, cfg, otto_bin=str(fake_otto))
    # Run a single tick by hand to dispatch + (after waiting) reap
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        # The child process is spawned immediately; a later tick promotes it
        # from initializing to running after the child publishes readiness.
        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] in {"initializing", "running"}
        state = _tick_until_task_status(runner, repo, "t1", {"done"})
        assert state["tasks"]["t1"]["status"] == "done", \
            f"expected done, got {state['tasks']['t1']!r}"
        assert state["tasks"]["t1"]["cost_usd"] == 0.42
        assert state["tasks"]["t1"]["exit_code"] == 0
    finally:
        runner._lock_fh.close()


def test_runner_marks_failed_when_no_manifest(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=0.1, write_manifest=False)
    append_task(repo, QueueTask(
        id="t1", command_argv=["build", "test"],
        branch="build/t1-test", worktree=".worktrees/t1",
    ))
    cfg = RunnerConfig(concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5)
    runner = Runner(repo, cfg, otto_bin=str(fake_otto))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        state = _tick_until_task_status(runner, repo, "t1", {"failed"})
        assert state["tasks"]["t1"]["status"] == "failed"
        assert "no manifest" in state["tasks"]["t1"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_runner_marks_failed_on_nonzero_exit(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=1, sleep=0.1, write_manifest=False)
    append_task(repo, QueueTask(
        id="t1", command_argv=["build", "test"],
        branch="build/t1-test", worktree=".worktrees/t1",
    ))
    cfg = RunnerConfig(concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5)
    runner = Runner(repo, cfg, otto_bin=str(fake_otto))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        state = _tick_until_task_status(runner, repo, "t1", {"failed"})
        assert state["tasks"]["t1"]["status"] == "failed"
        assert "exit_code=1" in state["tasks"]["t1"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_refresh_repairs_mixed_version_child_without_attempt_run_id(tmp_path: Path):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    worktree = repo / ".worktrees" / "t1"
    _paths.ensure_session_scaffold(worktree, "child-123")
    _paths.session_checkpoint(worktree, "child-123").write_text(json.dumps({
        "run_id": "child-123",
        "status": "in_progress",
        "updated_at": "2026-04-23T00:00:00Z",
    }))

    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["t1"] = {"status": "running", "child": None, "failure_reason": None}

    runner._refresh_queue_run_records([task], state)

    record = json.loads(_paths.live_run_path(repo, "child-123").read_text())
    assert record["identity"]["child_run_id"] == "child-123"
    assert record["identity"]["expected_child_run_id"] is None
    assert record["identity"]["compatibility_warning"] == "child predates run-id"
    assert record["artifacts"]["session_dir"].endswith("/child-123")


def test_refresh_repairs_mixed_version_child_with_expected_attempt_run_id(tmp_path: Path):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    worktree = repo / ".worktrees" / "t1"
    _paths.ensure_session_scaffold(worktree, "legacy-child-123")
    _paths.session_checkpoint(worktree, "legacy-child-123").write_text(json.dumps({
        "run_id": "legacy-child-123",
        "status": "in_progress",
        "updated_at": "2026-04-23T00:00:00Z",
    }))

    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "attempt_run_id": "expected-123",
        "child": None,
        "failure_reason": None,
    }

    runner._refresh_queue_run_records([task], state)

    record = json.loads(_paths.live_run_path(repo, "expected-123").read_text())
    assert record["identity"]["child_run_id"] == "legacy-child-123"
    assert record["identity"]["expected_child_run_id"] == "expected-123"
    assert record["identity"]["compatibility_warning"] == "child predates run-id"
    assert record["artifacts"]["session_dir"].endswith("/legacy-child-123")


def test_refresh_queue_run_records_surfaces_unexpected_update_errors(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    state = {"tasks": {"t1": {"status": "running", "attempt_run_id": "run-123", "child": None, "failure_reason": None}}}

    monkeypatch.setattr(runner_module, "update_record", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        runner._refresh_queue_run_records([task], state)


def test_refresh_queue_run_records_freezes_terminal_elapsed_time(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    state = {
        "tasks": {
            "t1": {
                "status": "failed",
                "attempt_run_id": "run-123",
                "child": None,
                "failure_reason": "exit_code=2",
                "started_at": "2026-04-23T00:00:00Z",
                "finished_at": "2026-04-23T00:00:05Z",
            }
        }
    }

    runner._write_queue_run_record(task, state["tasks"]["t1"], status="running")
    changed = runner._refresh_queue_run_records([task], state)

    record = json.loads(_paths.live_run_path(repo, "run-123").read_text())
    assert changed is True
    assert state["tasks"]["t1"]["duration_s"] == 5.0
    assert record["status"] == "failed"
    assert record["timing"]["finished_at"] == "2026-04-23T00:00:05Z"
    assert record["timing"]["duration_s"] == 5.0


def test_refresh_queue_run_records_does_not_recreate_gc_deleted_terminal_attempt(tmp_path: Path) -> None:
    from otto.runs.history import append_history_snapshot

    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    state = {
        "tasks": {
            "t1": {
                "status": "done",
                "attempt_run_id": "run-123",
                "child_run_id": "run-123",
                "child": None,
                "failure_reason": None,
                "started_at": "2026-04-23T00:00:00Z",
                "finished_at": "2026-04-23T00:00:05Z",
            }
        }
    }
    append_history_snapshot(
        repo,
        {"run_id": "run-123", "status": "done", "terminal_outcome": "success"},
        strict=True,
    )

    runner._refresh_queue_run_records([task], state)

    assert not _paths.live_run_path(repo, "run-123").exists()
    assert state["tasks"]["t1"]["history_appended"] is True


def test_finalize_queue_attempt_surfaces_unexpected_finalize_errors(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    append_task(repo, task)
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    ts = {
        "status": "done",
        "attempt_run_id": "run-123",
        "child": None,
        "failure_reason": None,
        "started_at": "2026-04-23T00:00:00Z",
        "finished_at": "2026-04-23T00:00:05Z",
    }

    monkeypatch.setattr(Runner, "_history_snapshot_matches", lambda self, run_id, status: False)
    monkeypatch.setattr(runner_module, "finalize_record", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        runner._finalize_queue_attempt("t1", ts)


def test_finalize_queue_attempt_retries_history_append_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from otto.runs.history import read_history_rows

    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    ))
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    ts = {
        "status": "done",
        "attempt_run_id": "run-123",
        "child": None,
        "failure_reason": None,
        "started_at": "2026-04-23T00:00:00Z",
        "finished_at": "2026-04-23T00:00:05Z",
    }
    calls = 0
    original_append = runner._append_queue_history_snapshot

    def flaky_append(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(runner, "_append_queue_history_snapshot", flaky_append)

    runner._finalize_queue_attempt("t1", ts)
    assert ts.get("history_appended") is not True
    assert not _paths.history_jsonl(repo).exists()

    runner._repair_terminal_queue_history(load_queue(repo), {"tasks": {"t1": ts}})

    assert calls == 2
    assert ts["history_appended"] is True
    rows = read_history_rows(_paths.history_jsonl(repo))
    assert any(row.get("run_id") == "run-123" and row.get("status") == "done" for row in rows)


def test_finalize_queue_attempt_uses_persisted_task_definition_after_queue_cleanup(
    tmp_path: Path,
):
    from otto.runs.history import read_history_rows

    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
        resolved_intent="build test",
    )
    append_task(repo, task)
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    state = load_state(repo)
    ts = state["tasks"].setdefault("t1", {})
    runner._snapshot_task_definition("t1", ts)
    remove_task(repo, "t1")
    ts.update({
        "status": "removed",
        "attempt_run_id": "run-removed",
        "child": None,
        "failure_reason": "removed by user",
        "started_at": "2026-04-23T00:00:00Z",
        "finished_at": "2026-04-23T00:00:05Z",
    })

    runner._finalize_queue_attempt("t1", ts)

    assert ts["history_appended"] is True
    rows = read_history_rows(_paths.history_jsonl(repo))
    row = next(row for row in rows if row.get("run_id") == "run-removed")
    assert row["status"] == "removed"
    assert row["command"] == "build test"
    assert row["queue_task_id"] == "t1"
    assert row["intent"] == "build test"
    assert row["branch"] == "build/t1-test"
    assert row["worktree"].endswith(".worktrees/t1")
    assert row["resumable"] is True


def test_runner_respects_concurrent_cap(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=0.2)
    for i in range(3):
        append_task(repo, QueueTask(
            id=f"t{i}", command_argv=["build", str(i)],
            branch=f"build/t{i}-x", worktree=f".worktrees/t{i}",
        ))
    cfg = RunnerConfig(concurrent=2, poll_interval_s=0.1, heartbeat_interval_s=0.5)
    runner = Runner(repo, cfg, otto_bin=str(fake_otto))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        state = load_state(repo)
        in_flight = sum(1 for ts in state["tasks"].values() if ts.get("status") in {"initializing", "running"})
        assert in_flight == 2, f"expected 2 in flight, got {in_flight}: {state['tasks']!r}"
        # Untouched tasks aren't in state.json yet — they're "queued" by absence.
        assert len(state["tasks"]) == 2, \
            f"expected only 2 tasks in state (dispatched), got {len(state['tasks'])}"
        state = _tick_until(
            runner,
            repo,
            lambda current: sum(1 for ts in current["tasks"].values() if ts.get("status") == "done") == 3,
            timeout_s=2.0,
        )
        done = sum(1 for ts in state["tasks"].values() if ts.get("status") == "done")
        assert done == 3, f"expected all done, got: {state['tasks']!r}"
    finally:
        runner._lock_fh.close()


def test_runner_run_exits_when_queue_drains_with_exit_when_empty(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=0.1)
    append_task(repo, QueueTask(
        id="t1", command_argv=["build", "one"],
        branch="build/t1-one", worktree=".worktrees/t1",
    ))
    append_task(repo, QueueTask(
        id="t2", command_argv=["build", "two"],
        branch="build/t2-two", worktree=".worktrees/t2",
    ))
    cfg = RunnerConfig(
        concurrent=2,
        poll_interval_s=0.05,
        heartbeat_interval_s=10.0,
        exit_when_empty=True,
    )
    runner = Runner(repo, cfg, otto_bin=str(fake_otto))

    assert runner.run() == 0

    state = load_state(repo)
    statuses = {task_id: task["status"] for task_id, task in state["tasks"].items()}
    assert statuses == {"t1": "done", "t2": "done"}
    assert state.get("watcher") is None


# ---------- dependencies (Phase 3) ----------


def test_runner_cascades_failure_through_after_chain(tmp_path: Path):
    """Phase 3.2 verify: A fails → B (after A) cascades failed → C (after B) cascades failed.
    Verifies transitive cascade across a 3-deep chain."""
    repo = init_repo(tmp_path)
    # Fake otto that exits non-zero (no manifest written)
    failing_otto = _make_fake_otto(tmp_path, exit_code=1, sleep=0.05, write_manifest=False)
    append_task(repo, QueueTask(
        id="a", command_argv=["build", "a"],
        branch="build/a-x", worktree=".worktrees/a",
    ))
    append_task(repo, QueueTask(
        id="b", command_argv=["build", "b"],
        after=["a"],
        branch="build/b-x", worktree=".worktrees/b",
    ))
    append_task(repo, QueueTask(
        id="c", command_argv=["build", "c"],
        after=["b"],
        branch="build/c-x", worktree=".worktrees/c",
    ))
    cfg = RunnerConfig(concurrent=3, poll_interval_s=0.05, heartbeat_interval_s=10.0)
    runner = Runner(repo, cfg, otto_bin=str(failing_otto))
    runner._lock_fh = acquire_lock(repo)
    try:
        # Tick 1: dispatch a (only ready task; b/c blocked on deps)
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["a"]["status"] in {"initializing", "running"}
        # Tick until a is reaped; b cascades failed once a fails.
        state = _tick_until_task_status(runner, repo, "a", {"failed"})
        assert state["tasks"]["a"]["status"] == "failed"
        assert state["tasks"]["b"]["status"] == "failed"
        assert "dependency 'a'" in state["tasks"]["b"]["failure_reason"]
        # Tick 3: cascade-fail c (since b failed)
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["c"]["status"] == "failed"
        assert "dependency 'b'" in state["tasks"]["c"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_runner_blocks_task_with_unsatisfied_after(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, sleep=0.1)
    append_task(repo, QueueTask(
        id="a", command_argv=["build", "a"],
        branch="build/a-x", worktree=".worktrees/a",
    ))
    append_task(repo, QueueTask(
        id="b", command_argv=["build", "b"],
        after=["a"],
        branch="build/b-x", worktree=".worktrees/b",
    ))
    cfg = RunnerConfig(concurrent=2, poll_interval_s=0.1, heartbeat_interval_s=0.5)
    runner = Runner(repo, cfg, otto_bin=str(fake_otto))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["a"]["status"] in {"initializing", "running"}
        # b not yet dispatched → not in state.json yet
        assert "b" not in state["tasks"]
        state = _tick_until_task_status(runner, repo, "a", {"done"})
        assert state["tasks"]["a"]["status"] == "done"
        # Now b should dispatch
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["b"]["status"] in {"initializing", "running"}
    finally:
        runner._lock_fh.close()


# ---------- regression: per-task timeout ----------


def test_runner_kills_task_exceeding_timeout(tmp_path: Path):
    """A child that runs longer than task_timeout_s gets SIGTERM and
    transitions to status=failed with a 'timed out' reason. Without this,
    a hung agent (e.g. blocked on `wait` for a backgrounded process) would
    occupy its concurrency slot indefinitely."""
    repo = init_repo(tmp_path)
    # `trap '' TERM` ignores SIGTERM so our timeout enforcement must SIGKILL
    # eventually — but for the test we only verify SIGTERM was sent + status
    # transitions to terminating/failed within a reasonable poll cycle.
    fake = tmp_path / "slow_otto.sh"
    fake.write_text("""#!/bin/sh
sleep 60
exit 0
""")
    fake.chmod(0o755)
    append_task(repo, QueueTask(
        id="hang", command_argv=["build", "x"],
        branch="build/hang-x", worktree=".worktrees/hang",
    ))
    cfg = RunnerConfig(
        concurrent=1,
        poll_interval_s=0.1,
        heartbeat_interval_s=0.5,
        task_timeout_s=0.1,
    )
    runner = Runner(repo, cfg, otto_bin=str(fake))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        assert load_state(repo)["tasks"]["hang"]["status"] in {"initializing", "running"}
        state = load_state(repo)
        state["tasks"]["hang"]["started_at"] = "2000-01-01T00:00:00Z"
        write_state(repo, state)
        runner._tick()  # should observe the timeout and SIGTERM the child
        ts = load_state(repo)["tasks"]["hang"]
        # Status should have transitioned to terminating (or already past it)
        assert ts["status"] in ("terminating", "failed", "cancelled"), \
            f"expected transition from running, got {ts['status']!r}"
        assert "timed out" in (ts.get("failure_reason") or ""), \
            f"expected 'timed out' in reason, got {ts.get('failure_reason')!r}"
        state = _tick_until_task_status(runner, repo, "hang", {"failed"})
        ts2 = state["tasks"]["hang"]
        assert ts2["status"] == "failed", f"expected failed, got {ts2['status']!r}"
    finally:
        runner._lock_fh.close()


def test_runner_does_not_timeout_task_under_limit(tmp_path: Path):
    """A short-lived task must NOT be killed if it finishes before timeout."""
    repo = init_repo(tmp_path)
    fake = _make_fake_otto(tmp_path, exit_code=0, sleep=0.05)
    append_task(repo, QueueTask(
        id="quick", command_argv=["build", "x"],
        branch="build/q-x", worktree=".worktrees/quick",
    ))
    # Generous 30s timeout, but the fake completes in 0.2s
    cfg = RunnerConfig(
        concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5,
        task_timeout_s=30.0,
    )
    runner = Runner(repo, cfg, otto_bin=str(fake))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        state = _tick_until_task_status(runner, repo, "quick", {"done"})
        ts = state["tasks"]["quick"]
        assert ts["status"] == "done", f"expected done, got {ts['status']!r}"
        assert "timed out" not in (ts.get("failure_reason") or "")
    finally:
        runner._lock_fh.close()


def test_runner_task_timeout_disabled_when_none(tmp_path: Path):
    """task_timeout_s=None disables the enforcement entirely (escape hatch)."""
    repo = init_repo(tmp_path)
    fake = _make_fake_otto(tmp_path, exit_code=0, sleep=0.05)
    append_task(repo, QueueTask(
        id="t", command_argv=["build", "x"],
        branch="build/t-x", worktree=".worktrees/t",
    ))
    cfg = RunnerConfig(
        concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5,
        task_timeout_s=None,
    )
    runner = Runner(repo, cfg, otto_bin=str(fake))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        state = _tick_until_task_status(runner, repo, "t", {"done"})
        ts = state["tasks"]["t"]
        assert ts["status"] == "done"
    finally:
        runner._lock_fh.close()


def test_runner_config_loads_task_timeout_from_yaml():
    """`queue.task_timeout_s` in otto.yaml is honored; absent → default guard."""
    cfg = runner_config_from_otto_config({"queue": {"task_timeout_s": 600}})
    assert cfg.task_timeout_s == 600.0
    cfg2 = runner_config_from_otto_config({"queue": {}})
    assert cfg2.task_timeout_s == 4200.0
    cfg3 = runner_config_from_otto_config({"queue": {"task_timeout_s": None}})
    assert cfg3.task_timeout_s is None
    cfg4 = runner_config_from_otto_config({"queue": {"task_timeout_s": 0}})
    assert cfg4.task_timeout_s is None  # 0 is also "off"


# ---------- regression: manifest path env-var contract ----------


def test_runner_sets_queue_project_dir_env_on_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The runner MUST set OTTO_QUEUE_PROJECT_DIR so the spawned otto
    (whose cwd is the worktree) writes its manifest where the watcher
    (cwd = main project) will look for it. Without this, every queue
    run would be marked failed with `no manifest`."""
    repo = init_repo(tmp_path)

    captured_env: dict[str, str] = {}
    captured_stdin: Any = None

    def fake_popen(
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        start_new_session: bool,
        stdin: Any,
    ):  # type: ignore[no-untyped-def]
        nonlocal captured_stdin
        assert start_new_session is True
        captured_env.update(env)
        captured_stdin = stdin

        class _StubProc:
            def __init__(self) -> None:
                self.pid = os.getpid()  # any pid, we never wait on it

        return _StubProc()

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    # Stub psutil so start_time_ns capture doesn't try to read our pid
    class _StubPsutilProc:
        def create_time(self) -> float:
            return time.time()
    monkeypatch.setattr(runner_module, "psutil", type("M", (), {"Process": lambda _self=None, *a, **k: _StubPsutilProc()}), raising=False)

    append_task(repo, QueueTask(
        id="t1", command_argv=["build", "test"],
        branch="build/t1-test", worktree=".worktrees/t1",
    ))
    cfg = RunnerConfig(concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5)
    runner = Runner(repo, cfg, otto_bin="/bin/true")
    runner._lock_fh = acquire_lock(repo)
    try:
        # Manually create the worktree so add_worktree call inside _spawn succeeds.
        # Use a side-step: monkeypatch add_worktree to do nothing.
        from otto import worktree as wt_mod
        monkeypatch.setattr(wt_mod, "add_worktree", lambda **k: None)
        runner._tick()
        # Verify both env vars are set
        assert captured_env.get("OTTO_QUEUE_TASK_ID") == "t1"
        assert captured_env.get("OTTO_QUEUE_PROJECT_DIR") == str(repo), \
            f"runner must set OTTO_QUEUE_PROJECT_DIR; got: {captured_env.get('OTTO_QUEUE_PROJECT_DIR')!r}"
        assert captured_env.get("OTTO_INTERNAL_QUEUE_RUNNER") == "1"
        assert captured_stdin is runner_module.subprocess.DEVNULL
    finally:
        runner._lock_fh.close()


def test_spawn_persists_initializing_state_before_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    state: dict[str, Any] = {"tasks": {}}
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    from otto import worktree as wt_mod

    monkeypatch.setattr(wt_mod, "add_worktree", lambda **_kwargs: None)

    class _StubProc:
        pid = 12345

    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_args, **_kwargs: _StubProc())
    monkeypatch.setattr(runner, "_write_queue_run_record", lambda *_args, **_kwargs: None)

    writes: list[dict[str, Any]] = []

    def capture_write_state(project_dir: Path, next_state: dict[str, Any]) -> None:
        assert project_dir == repo
        writes.append(json.loads(json.dumps(next_state)))
        write_state(project_dir, next_state)

    monkeypatch.setattr(runner_module, "write_state", capture_write_state)

    runner._spawn(task, state)

    assert [item["tasks"]["t1"]["status"] for item in writes] == ["starting", "initializing"]
    persisted = load_state(repo)["tasks"]["t1"]
    assert persisted["status"] == "initializing"
    assert persisted["child"]["pid"] == 12345


def test_ready_marker_promotes_initializing_task_to_running(tmp_path: Path):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=1.0, write_manifest=False)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    ))
    runner = Runner(repo, RunnerConfig(concurrent=1, poll_interval_s=0.01), otto_bin=str(fake_otto))
    runner._lock_fh = acquire_lock(repo)
    child: dict[str, Any] | None = None
    try:
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] == "initializing"
        state = _tick_until_task_status(runner, repo, "t1", {"running"}, timeout_s=1.0)
        ts = state["tasks"]["t1"]
        child = ts["child"]
        assert ts["child_run_id"] == ts["attempt_run_id"]
        assert ts["ready_path"].endswith("otto_logs/queue/t1/ready.json")
        assert ts["ready_at"] == "2026-04-19T00:00:00Z"
    finally:
        if child and child_is_alive(child):
            kill_child_safely(child, signal.SIGTERM)
            try:
                os.waitpid(int(child["pid"]), 0)
            except (ChildProcessError, ProcessLookupError):
                pass
        runner._lock_fh.close()


def test_starting_queue_record_is_not_fallback_killable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = {
        "status": "starting",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": None,
    }

    runner._write_queue_run_record(task, state, status="starting")

    record = json.loads(_paths.live_run_path(repo, "2026-04-23-010203-abc123").read_text(encoding="utf-8"))
    assert record["writer"]["kind"] == "pending-child"
    assert "pgid" not in record["writer"]


def test_spawn_failure_after_starting_preserves_attempt_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    ))
    from otto import worktree as wt_mod

    monkeypatch.setattr(wt_mod, "add_worktree", lambda **_kwargs: None)

    def _popen_fails(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("exec failed")

    monkeypatch.setattr(runner_module.subprocess, "Popen", _popen_fails)
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/does-not-exist")

    runner._tick()

    task_state = load_state(repo)["tasks"]["t1"]
    assert task_state["status"] == "failed"
    assert task_state["attempt_run_id"]
    record = json.loads(_paths.live_run_path(repo, task_state["attempt_run_id"]).read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert "exec failed" in record["last_event"]


# ---------- runner_config_from_otto_config ----------


def test_runner_config_from_otto_config():
    cfg = runner_config_from_otto_config({
        "queue": {
            "concurrent": 5,
            "worktree_dir": ".my-trees",
            "on_watcher_restart": "fail",
        },
    })
    assert cfg.concurrent == 5
    assert cfg.worktree_dir == ".my-trees"
    assert cfg.on_watcher_restart == "fail"
    assert cfg.task_timeout_s == 4200.0


@pytest.mark.parametrize("bad_concurrent", [0, -2, True, "3"])
def test_runner_config_rejects_bad_concurrency(bad_concurrent: object):
    with pytest.raises(ValueError, match="queue.concurrent"):
        runner_config_from_otto_config({"queue": {"concurrent": bad_concurrent}})


@pytest.mark.parametrize("bad_timeout", [-1, False, True, "30"])
def test_runner_config_rejects_bad_task_timeout(bad_timeout: object):
    with pytest.raises(ValueError, match="queue.task_timeout_s"):
        runner_config_from_otto_config({"queue": {"task_timeout_s": bad_timeout}})


@pytest.mark.parametrize("raw_timeout", [None, 0])
def test_runner_config_allows_disabling_task_timeout(raw_timeout: object):
    cfg = runner_config_from_otto_config({"queue": {"task_timeout_s": raw_timeout}})
    assert cfg.task_timeout_s is None


def test_runner_config_falls_back_to_defaults_with_empty_queue():
    cfg = runner_config_from_otto_config({"queue": {}})
    assert cfg.concurrent == 3
    assert cfg.worktree_dir == ".worktrees"
    assert cfg.on_watcher_restart == "resume"


def test_run_logs_and_stops_after_tick_exception(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    cfg = RunnerConfig(poll_interval_s=0.01, heartbeat_interval_s=10.0)
    runner = Runner(repo, cfg, otto_bin="/bin/true")
    seen = {"count": 0}

    def flaky_tick() -> None:
        seen["count"] += 1
        if seen["count"] == 1:
            raise RuntimeError("boom")
        runner.shutdown_level = "immediate"

    runner._tick = flaky_tick  # type: ignore[method-assign]
    with caplog.at_level("ERROR", logger="otto.queue.runner"):
        assert runner.run() == 1
    assert seen["count"] == 1
    assert "tick failed; stopping runner" in caplog.text


def test_tick_logs_malformed_queue_yml_and_continues_after_fix(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=30.0)
    subprocess.run(["git", "branch", "build/t1"], cwd=repo, check=True)
    (repo / ".otto-queue.yml").write_text("schema_version: [\n")

    runner = Runner(
        repo,
        RunnerConfig(concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.5),
        otto_bin=str(fake_otto),
    )

    try:
        with caplog.at_level("ERROR", logger="otto.queue.runner"):
            runner._tick()
        assert "failed to load queue.yml: queue.yml is malformed" in caplog.text
        assert load_state(repo)["tasks"] == {}

        (repo / ".otto-queue.yml").unlink()
        append_task(repo, QueueTask(
            id="t1",
            command_argv=["build", "test"],
            branch="build/t1",
            worktree=".worktrees/t1",
        ))
        runner._tick()

        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] in {"initializing", "running"}
    finally:
        child = load_state(repo)["tasks"].get("t1", {}).get("child") or {}
        pid = child.get("pid")
        pgid = child.get("pgid")
        if isinstance(pgid, int):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
        if isinstance(pid, int):
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass


def test_tick_finishes_drain_when_commands_are_all_malformed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / ".otto-queue-commands.jsonl").write_text("{broken\n")

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner._tick()

    assert not commands_path(repo).exists()
    assert not commands_processing_path(repo).exists()
    runner._tick()
    assert not commands_processing_path(repo).exists()


def test_tick_finishes_drain_when_commands_are_already_acked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    cmd = {"schema_version": 1, "command_id": "cmd-1", "kind": "cancel", "id": "t1"}
    append_command(repo, cmd)
    append_command_ack(repo, cmd, writer_id="queue:test")

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner._tick()

    assert not commands_path(repo).exists()
    assert not commands_processing_path(repo).exists()
    runner._tick()
    assert not commands_processing_path(repo).exists()


def test_run_exits_on_state_persistence_failure_after_spawn(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    repo = init_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, sleep=30.0, write_manifest=False)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    ))
    runner = Runner(
        repo,
        RunnerConfig(poll_interval_s=0.01, heartbeat_interval_s=10.0),
        otto_bin=str(fake_otto),
    )
    real_write_state = runner_module.write_state
    real_popen = runner_module.subprocess.Popen
    calls = {"count": 0}
    child_procs: list[subprocess.Popen[str]] = []

    def flaky_write_state(project_dir: Path, state: dict[str, Any]) -> None:
        calls["count"] += 1
        if calls["count"] == 4:
            raise OSError("disk full")
        real_write_state(project_dir, state)

    def counting_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        proc = real_popen(*args, **kwargs)
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv and argv[0] == str(fake_otto):
            child_procs.append(proc)
        return proc

    monkeypatch.setattr(runner_module, "write_state", flaky_write_state)
    monkeypatch.setattr(runner_module.subprocess, "Popen", counting_popen)
    with caplog.at_level("CRITICAL", logger="otto.queue.runner"):
        assert runner.run() == 1
    assert len(child_procs) == 1
    assert "post-spawn state write failed; terminating just-spawned child to prevent duplicate" in caplog.text

    for proc in child_procs:
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)


def test_spawn_keeps_child_tracked_when_initializing_record_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    state: dict[str, Any] = {"tasks": {}}
    runner = Runner(
        repo,
        RunnerConfig(),
        otto_bin=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    child: dict[str, Any] | None = None

    try:
        def fail_initializing_record(
            task_arg: QueueTask,
            state_arg: dict[str, Any],
            *,
            status: str,
        ) -> None:
            del task_arg, state_arg
            if status == "initializing":
                raise OSError("index unavailable")

        monkeypatch.setattr(runner, "_write_queue_run_record", fail_initializing_record)

        with caplog.at_level("ERROR", logger="otto.queue.runner"):
            runner._spawn(task, state)

        child = state["tasks"]["t1"]["child"]
        assert state["tasks"]["t1"]["status"] == "initializing"
        assert child_is_alive(child)
        assert "failed to update queue run record after spawn; continuing" in caplog.text
    finally:
        if child and child_is_alive(child):
            kill_child_safely(child, signal.SIGTERM)
            try:
                os.waitpid(int(child["pid"]), 0)
            except (ChildProcessError, ProcessLookupError):
                pass


def test_reap_children_keeps_running_task_when_echild_child_is_still_alive(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "child": {"pid": 12345, "pgid": 12345},
    }

    def fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        raise ChildProcessError

    monkeypatch.setattr(os, "waitpid", fake_waitpid)
    monkeypatch.setattr(runner_module, "child_is_alive", lambda child: True)
    with caplog.at_level("INFO", logger="otto.queue.runner"):
        runner._reap_children(state)
    assert state["tasks"]["t1"]["status"] == "running"
    assert state["tasks"]["t1"]["child"] == {"pid": 12345, "pgid": 12345}
    assert "reap deferred for t1" in caplog.text


def test_reap_children_escalates_stale_terminating_child_to_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(termination_grace_s=0.0), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "terminating",
        "terminal_status": "cancelled",
        "terminating_since": "2026-04-19T00:00:00Z",
        "child": {"pid": 12345, "pgid": 12345},
    }
    sent: list[int] = []

    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (0, 0))

    def fake_kill_child_safely(child: dict[str, Any], sig: int = signal.SIGTERM) -> bool:
        sent.append(sig)
        return True

    monkeypatch.setattr(runner_module, "kill_child_safely", fake_kill_child_safely)

    runner._reap_children(state)

    assert sent == [signal.SIGKILL]
    assert state["tasks"]["t1"]["status"] == "terminating"
    assert state["tasks"]["t1"]["sigkill_sent_at"]


def test_tick_logs_same_cycle_only_once(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="a", command_argv=["build", "a"], after=["b"],
    ))
    append_task(repo, QueueTask(
        id="b", command_argv=["build", "b"], after=["a"],
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")

    with caplog.at_level("WARNING", logger="otto.queue.runner"):
        runner._tick()
        runner._tick()

    cycle_logs = [r for r in caplog.records if "dependency cycles" in r.message]
    assert len(cycle_logs) == 1


def test_cancel_running_task_stays_terminating_until_reaped(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["-c", "trap '' TERM; sleep 1"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin=["/bin/sh"])
    runner._lock_fh = acquire_lock(repo)
    try:
        state = load_state(repo)
        runner._spawn(load_queue(repo)[0], state)
        child = state["tasks"]["t1"]["child"]
        runner._apply_command({"cmd": "cancel", "id": "t1"}, state)
        assert state["tasks"]["t1"]["status"] == "terminating"
        assert state["tasks"]["t1"]["child"] == child

        for _ in range(50):
            runner._reap_children(state)
            if state["tasks"]["t1"]["status"] == "cancelled":
                break
            time.sleep(0.05)
        assert state["tasks"]["t1"]["status"] == "cancelled"
        assert state["tasks"]["t1"]["child"] is None
    finally:
        child = state["tasks"].get("t1", {}).get("child") or {}
        pid = child.get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, ProcessLookupError):
                pass
        runner._lock_fh.close()


def test_cancel_done_task_is_noop_with_warning(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["done1"] = {
        "status": "done",
        "finished_at": "2026-04-20T00:00:00Z",
        "manifest_path": "/tmp/manifest.json",
        "cost_usd": 0.42,
    }
    with caplog.at_level("WARNING", logger="otto.queue.runner"):
        runner._apply_command({"cmd": "cancel", "id": "done1"}, state)
    assert state["tasks"]["done1"]["status"] == "done"
    assert state["tasks"]["done1"]["manifest_path"] == "/tmp/manifest.json"
    assert "cancel ignored for done1 in status=done" in caplog.text


def test_unknown_command_does_not_create_ghost_task(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)

    with caplog.at_level("WARNING", logger="otto.queue.runner"):
        runner._apply_command({"cmd": "cancel", "id": "ghost"}, state, known_task_ids=set())

    assert state["tasks"] == {}
    assert "unknown task id ghost" in caplog.text


def test_cancel_queued_task_removes_queue_entry(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="queued1",
        command_argv=["build", "queued1"],
        branch="build/queued1",
        worktree=".worktrees/queued1",
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)

    runner._apply_command({"cmd": "cancel", "id": "queued1"}, state)

    assert state["tasks"]["queued1"]["status"] == "cancelled"
    assert load_queue(repo) != []
    assert runner._cleanup_removed_task_definitions(load_queue(repo), state) is True
    assert load_queue(repo) == []


def test_resume_command_requeues_interrupted_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="resume1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/resume1",
        worktree=".worktrees/resume1",
    )
    append_task(repo, task)
    _write_resume_checkpoint(repo, task)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["resume1"] = {
        "status": "interrupted",
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": "2026-04-19T00:05:00Z",
        "failure_reason": "interrupted by watcher shutdown; resume available",
    }

    runner._apply_command({"cmd": "resume", "id": "resume1"}, state)

    assert state["tasks"]["resume1"]["status"] == "queued"
    assert state["tasks"]["resume1"]["resumed_from_checkpoint"] is True
    assert state["tasks"]["resume1"]["failure_reason"] is None
    assert state["tasks"]["resume1"]["finished_at"] is None


def test_resume_command_requeues_failed_task_with_checkpoint(tmp_path: Path):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="resume1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/resume1",
        worktree=".worktrees/resume1",
    )
    append_task(repo, task)
    _write_resume_checkpoint(repo, task)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["resume1"] = {
        "status": "failed",
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": "2026-04-19T00:30:00Z",
        "failure_reason": "timed out after 1800s (limit 1800s)",
    }

    runner._apply_command({"cmd": "resume", "id": "resume1"}, state)

    assert state["tasks"]["resume1"]["status"] == "queued"
    assert state["tasks"]["resume1"]["resumed_from_checkpoint"] is True
    assert state["tasks"]["resume1"]["failure_reason"] is None
    assert state["tasks"]["resume1"]["finished_at"] is None


def test_resume_command_rejects_stale_checkpoint(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="resume1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/resume1",
        worktree=".worktrees/resume1",
    )
    append_task(repo, task)
    _write_resume_checkpoint(repo, task, extra={"git_sha": "stale-sha"})
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["resume1"] = {
        "status": "failed",
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": "2026-04-19T00:30:00Z",
        "failure_reason": "timed out after 1800s (limit 1800s)",
    }

    runner._apply_command({"cmd": "resume", "id": "resume1"}, state)

    assert state["tasks"]["resume1"]["status"] == "failed"
    assert "resumed_from_checkpoint" not in state["tasks"]["resume1"]
    assert "checkpoint is stale: git HEAD changed" in caplog.text


def test_resume_command_allows_superseding_terminal_history(tmp_path: Path):
    from otto.runs.history import append_history_snapshot, read_history_rows

    repo = init_repo(tmp_path)
    task = QueueTask(
        id="resume1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/resume1",
        worktree=".worktrees/resume1",
    )
    append_task(repo, task)
    _write_resume_checkpoint(repo, task)
    run_id = "2026-04-23-010203-abc123"
    append_history_snapshot(
        repo,
        {
            "run_id": run_id,
            "domain": "queue",
            "run_type": "queue",
            "command": "build",
            "status": INTERRUPTED_STATUS,
            "terminal_outcome": INTERRUPTED_STATUS,
            "dedupe_key": f"terminal_snapshot:{run_id}",
        },
    )
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["resume1"] = {
        "status": INTERRUPTED_STATUS,
        "attempt_run_id": run_id,
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": "2026-04-19T00:05:00Z",
        "failure_reason": "interrupted by watcher shutdown; resume available",
        "history_appended": True,
    }

    runner._apply_command({"cmd": "resume", "id": "resume1"}, state)
    assert "history_appended" not in state["tasks"]["resume1"]

    state["tasks"]["resume1"].update({
        "status": "done",
        "finished_at": runner_module.now_iso(),
        "cost_usd": 0.0,
        "duration_s": 1.0,
    })
    runner._finalize_queue_attempt("resume1", state["tasks"]["resume1"])

    rows = [
        row
        for row in read_history_rows(_paths.history_jsonl(repo))
        if row.get("dedupe_key") == f"terminal_snapshot:{run_id}"
    ]
    assert [row["status"] for row in rows] == [INTERRUPTED_STATUS, "done"]
    assert state["tasks"]["resume1"]["history_appended"] is True


def test_resume_command_ignores_non_interrupted_task(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="queued1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/queued1",
        worktree=".worktrees/queued1",
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["queued1"] = {"status": "queued"}

    with caplog.at_level("WARNING", logger="otto.queue.runner"):
        runner._apply_command({"cmd": "resume", "id": "queued1"}, state)

    assert state["tasks"]["queued1"]["status"] == "queued"
    assert "resume ignored for queued1 in status=queued" in caplog.text


def test_remove_command_updates_queue_yml_for_live_task(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="rm1",
        command_argv=["build", "rm1"],
        branch="build/rm1",
        worktree=".worktrees/rm1",
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)

    runner._apply_command({"cmd": "remove", "id": "rm1"}, state)

    assert state["tasks"]["rm1"]["status"] == "removed"
    assert load_queue(repo) != []
    assert runner._cleanup_removed_task_definitions(load_queue(repo), state) is True
    assert load_queue(repo) == []


def test_cleanup_removed_task_definition_converges_when_queue_entry_already_absent(tmp_path: Path):
    repo = init_repo(tmp_path)
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["rm1"] = {
        "status": "removed",
        "definition_removal_pending": True,
    }

    assert runner._cleanup_removed_task_definitions(load_queue(repo), state) is True
    assert "definition_removal_pending" not in state["tasks"]["rm1"]


def test_remove_done_task_is_noop_with_cleanup_warning(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="done1",
        command_argv=["build", "done1"],
        branch="build/done1",
        worktree=".worktrees/done1",
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["done1"] = {
        "status": "done",
        "finished_at": "2026-04-20T00:00:00Z",
    }

    with caplog.at_level("WARNING", logger="otto.queue.runner"):
        runner._apply_command({"cmd": "remove", "id": "done1"}, state)

    assert state["tasks"]["done1"]["status"] == "done"
    assert [task.id for task in load_queue(repo)] == ["done1"]
    assert "use cleanup" in caplog.text


def test_cleanup_command_prunes_terminal_task_from_queue(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="failed1",
        command_argv=["build", "failed1"],
        branch="build/failed1",
        worktree=".worktrees/failed1",
    ))
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    state = load_state(repo)
    state["tasks"]["failed1"] = {
        "status": "failed",
        "finished_at": "2026-04-20T00:00:00Z",
    }

    runner._apply_command({"cmd": "cleanup", "id": "failed1"}, state)

    assert state["tasks"]["failed1"]["status"] == "removed"
    assert [task.id for task in load_queue(repo)] == []


def test_immediate_shutdown_marks_running_task_interrupted(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="build1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/build1",
        worktree=".worktrees/build1",
    ))
    session_id = "2026-04-22-010203-abc123"
    _paths.ensure_session_scaffold(repo / ".worktrees" / "build1", session_id)
    _paths.session_checkpoint(repo / ".worktrees" / "build1", session_id).write_text(
        json.dumps({"status": "paused", "updated_at": "2026-04-22T01:02:03Z"})
    )
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "build1": {
                    "status": "running",
                    "started_at": "2026-04-22T01:00:00Z",
                    "child": {"pid": 999999, "pgid": 999999},
                },
            },
        },
    )

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner._kill_all_in_flight()

    updated = load_state(repo)["tasks"]["build1"]
    assert updated["status"] == "terminating"
    assert updated["terminal_status"] == "interrupted"
    assert "resume available" in updated["failure_reason"]


def test_immediate_shutdown_preserves_explicit_cancel_terminal_status(tmp_path: Path):
    repo = init_repo(tmp_path)
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "build1": {
                    "status": "terminating",
                    "started_at": "2026-04-22T01:00:00Z",
                    "child": {"pid": 999999, "pgid": 999999},
                    "terminal_status": "cancelled",
                    "failure_reason": "cancelled by user",
                },
            },
        },
    )

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner._kill_all_in_flight()

    updated = load_state(repo)["tasks"]["build1"]
    assert updated["status"] == "terminating"
    assert updated["terminal_status"] == "cancelled"
    assert updated["failure_reason"] == "cancelled by user"


def test_immediate_shutdown_force_escalates_to_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "build1": {
                    "status": "running",
                    "started_at": "2026-04-22T01:00:00Z",
                    "child": {"pid": 999999, "pgid": 999999},
                },
            },
        },
    )
    sent: list[int] = []

    def fake_kill_child_safely(child: dict[str, Any], sig: int = signal.SIGTERM) -> bool:
        sent.append(sig)
        return True

    monkeypatch.setattr(runner_module, "kill_child_safely", fake_kill_child_safely)

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner._kill_all_in_flight(force=True)

    updated = load_state(repo)["tasks"]["build1"]
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert updated["status"] == INTERRUPTED_STATUS
    assert updated["child"] is None


def test_spawn_uses_snapshotted_branch_when_intent_matches(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path)
    task1 = QueueTask(
        id="same-intent",
        command_argv=["build", "same intent"],
        resolved_intent="same intent",
        branch="build/same-intent-2026-04-20",
        worktree=".worktrees/same-intent",
    )
    task2 = QueueTask(
        id="same-intent-2",
        command_argv=["build", "same intent"],
        resolved_intent="same intent",
        branch="build/same-intent-2-2026-04-20",
        worktree=".worktrees/same-intent-2",
    )
    captured_branches: list[str] = []

    class DummyProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    def fake_add_worktree(*, project_dir: Path, worktree_path: Path, branch: str) -> None:
        captured_branches.append(branch)
        worktree_path.mkdir(parents=True, exist_ok=True)

    next_pid = {"value": 40000}

    def fake_popen(*args: Any, **kwargs: Any) -> DummyProc:
        next_pid["value"] += 1
        return DummyProc(next_pid["value"])

    monkeypatch.setattr("otto.worktree.add_worktree", fake_add_worktree)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    runner = Runner(repo, RunnerConfig(), otto_bin=["/bin/echo"])
    state = load_state(repo)
    runner._spawn(task1, state)
    runner._spawn(task2, state)

    assert captured_branches == [
        "build/same-intent-2026-04-20",
        "build/same-intent-2-2026-04-20",
    ]


def test_reconcile_on_startup_tolerates_malformed_queue_yml(tmp_path: Path, caplog):
    repo = init_repo(tmp_path)
    write_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "running1": {
                    "status": "running",
                    "child": {"pid": 999999, "pgid": 999999},
                },
            },
        },
    )
    (repo / ".otto-queue.yml").write_text("schema_version: [\n")
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume"), otto_bin="/bin/true")

    with caplog.at_level("ERROR", logger="otto.queue.runner"):
        runner._reconcile_on_startup()

    assert "failed to load queue.yml during startup reconcile" in caplog.text
    assert load_state(repo)["tasks"]["running1"]["status"] == "failed"


def test_reconcile_on_startup_finishes_empty_command_drain(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    cmd = {"schema_version": 1, "command_id": "cmd-1", "kind": "cancel", "id": "t1"}
    append_command(repo, cmd)
    append_command_ack(repo, cmd, writer_id="queue:test")

    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume"), otto_bin="/bin/true")
    runner._reconcile_on_startup()

    assert not commands_path(repo).exists()
    assert not commands_processing_path(repo).exists()


def test_queue_spec_path_uses_session_spec_dir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "test"],
        branch="build/t1-test",
        worktree=".worktrees/t1",
    )
    worktree = repo / ".worktrees" / "t1"
    spec_dir = _paths.spec_dir(worktree, "run-123")
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "spec.md"
    spec_path.write_text("# spec\n")

    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")

    assert runner._queue_spec_path(task, {"attempt_run_id": "run-123"}) == str(spec_path)


# ---------- restart reconciliation ----------


def test_reconcile_marks_certify_failed_when_child_gone(tmp_path: Path):
    """certify is not resumable; on restart with dead child, mark failed."""
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="cert1", command_argv=["certify"], resumable=False,
        branch="certify/x", worktree=".worktrees/cert1",
    ))
    # Inject a "running" state with a long-dead PID
    state = load_state(repo)
    state["tasks"]["cert1"] = {
        "status": "running",
        "started_at": "2026-04-19T00:00:00Z",
        "child": {
            "pid": 1, "pgid": 1, "start_time_ns": 0,
            "argv": ["nonexistent"], "cwd": "/tmp",
        },
    }
    write_state(repo, state)
    cfg = RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1)
    runner = Runner(repo, cfg, otto_bin="/bin/true")
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._reconcile_on_startup()
        state2 = load_state(repo)
        assert state2["tasks"]["cert1"]["status"] == "failed"
        assert "not resumable" in state2["tasks"]["cert1"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_reconcile_marks_failed_when_no_checkpoint(tmp_path: Path):
    """Resumable task but no checkpoint → marked failed."""
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="b1", command_argv=["build", "x"], resumable=True,
        branch="build/b1", worktree=".worktrees/b1",
    ))
    state = load_state(repo)
    state["tasks"]["b1"] = {
        "status": "running",
        "child": {"pid": 1, "pgid": 1, "start_time_ns": 0,
                  "argv": ["nonexistent"], "cwd": "/tmp"},
    }
    write_state(repo, state)
    cfg = RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1)
    runner = Runner(repo, cfg, otto_bin="/bin/true")
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._reconcile_on_startup()
        state2 = load_state(repo)
        assert state2["tasks"]["b1"]["status"] == "failed"
        assert "no checkpoint" in state2["tasks"]["b1"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_reconcile_requeues_starting_task_without_child(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="b1", command_argv=["build", "x"], resumable=True,
        branch="build/b1", worktree=".worktrees/b1",
    ))
    state = load_state(repo)
    state["tasks"]["b1"] = {
        "status": "starting",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": None,
    }
    write_state(repo, state)
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1), otto_bin="/bin/true")

    runner._reconcile_on_startup()

    task_state = load_state(repo)["tasks"]["b1"]
    assert task_state["status"] == "queued"
    assert task_state["attempt_run_id"] == "2026-04-23-010203-abc123"
    assert task_state["child"] is None


def test_reconcile_finalizes_dead_child_from_queue_manifest(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="b1", command_argv=["build", "x"], resumable=True,
        branch="build/b1", worktree=".worktrees/b1",
    ))
    state = load_state(repo)
    state["tasks"]["b1"] = {
        "status": "running",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 1, "pgid": 1, "start_time_ns": 0, "argv": ["dead"], "cwd": "/tmp"},
    }
    write_state(repo, state)
    _write_queue_manifest(repo, "b1", exit_status="success")
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1), otto_bin="/bin/true")

    runner._reconcile_on_startup()

    task_state = load_state(repo)["tasks"]["b1"]
    assert task_state["status"] == "done"
    assert task_state["manifest_path"]
    assert task_state["child"] is None


def test_reconcile_applies_cancel_after_terminal_manifest_finalization(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="b1", command_argv=["build", "x"], resumable=True,
        branch="build/b1", worktree=".worktrees/b1",
    ))
    state = load_state(repo)
    state["tasks"]["b1"] = {
        "status": "running",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 1, "pgid": 1, "start_time_ns": 0, "argv": ["dead"], "cwd": "/tmp"},
    }
    write_state(repo, state)
    _write_queue_manifest(repo, "b1", exit_status="success")
    append_command(repo, {"schema_version": 1, "command_id": "cmd-1", "kind": "cancel", "id": "b1"})
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1), otto_bin="/bin/true")

    runner._reconcile_on_startup()

    task_state = load_state(repo)["tasks"]["b1"]
    assert task_state["status"] == "done"
    assert task_state["failure_reason"] is None
    assert task_state["child"] is None


def test_reconcile_applies_resume_after_interrupted_terminating_finalizes(tmp_path: Path):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="b1", command_argv=["build", "x"], resumable=True,
        branch="build/b1", worktree=".worktrees/b1",
    )
    append_task(repo, task)
    _write_resume_checkpoint(repo, task)
    state = load_state(repo)
    state["tasks"]["b1"] = {
        "status": "terminating",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "started_at": "2026-04-23T00:00:00Z",
        "finished_at": None,
        "child": {"pid": 1, "pgid": 1, "start_time_ns": 0, "argv": ["dead"], "cwd": "/tmp"},
        "terminal_status": INTERRUPTED_STATUS,
        "failure_reason": "interrupted by watcher shutdown",
    }
    write_state(repo, state)
    append_command(repo, {"schema_version": 1, "command_id": "cmd-1", "kind": "resume", "id": "b1"})
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1), otto_bin="/bin/true")

    runner._reconcile_on_startup()

    task_state = load_state(repo)["tasks"]["b1"]
    assert task_state["status"] == "queued"
    assert task_state["resumed_from_checkpoint"] is True
    assert task_state["child"] is None


def test_reconcile_requeues_when_checkpoint_exists(tmp_path: Path):
    """Resumable task + checkpoint + policy=resume → re-queue (will respawn with --resume)."""
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="b2", command_argv=["build", "x"], resumable=True,
        branch="build/b2", worktree=".worktrees/b2",
    ))
    # Create a fake checkpoint at the expected path
    wt = repo / ".worktrees" / "b2"
    (wt / "otto_logs").mkdir(parents=True)
    (wt / "otto_logs" / "checkpoint.json").write_text("{}")
    state = load_state(repo)
    state["tasks"]["b2"] = {
        "status": "running",
        "child": {"pid": 1, "pgid": 1, "start_time_ns": 0,
                  "argv": ["nonexistent"], "cwd": "/tmp"},
    }
    write_state(repo, state)
    cfg = RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1)
    runner = Runner(repo, cfg, otto_bin="/bin/true")
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._reconcile_on_startup()
        state2 = load_state(repo)
        assert state2["tasks"]["b2"]["status"] == "queued"
        assert state2["tasks"]["b2"].get("resumed_from_checkpoint") is True
    finally:
        runner._lock_fh.close()


def test_reconcile_ignores_stale_paused_pointer_and_scans_for_valid_session(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="b3", command_argv=["build", "x"], resumable=True,
        branch="build/b3", worktree=".worktrees/b3",
    ))
    wt = repo / ".worktrees" / "b3"
    stale_sid = "2026-04-20-170100-aaaaaa"
    valid_sid = "2026-04-20-170200-bbbbbb"
    _paths.ensure_session_scaffold(wt, stale_sid)
    _paths.ensure_session_scaffold(wt, valid_sid)
    _paths.set_pointer(wt, _paths.PAUSED_POINTER, stale_sid)
    _paths.session_checkpoint(wt, valid_sid).write_text(json.dumps({
        "status": "paused",
        "run_id": valid_sid,
        "updated_at": "2026-04-20T17:02:00Z",
    }))

    state = load_state(repo)
    state["tasks"]["b3"] = {
        "status": "running",
        "child": {"pid": 1, "pgid": 1, "start_time_ns": 0,
                  "argv": ["nonexistent"], "cwd": "/tmp"},
    }
    write_state(repo, state)
    cfg = RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.1)
    runner = Runner(repo, cfg, otto_bin="/bin/true")
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._reconcile_on_startup()
        state2 = load_state(repo)
        assert state2["tasks"]["b3"]["status"] == "queued"
        assert state2["tasks"]["b3"].get("resumed_from_checkpoint") is True
    finally:
        runner._lock_fh.close()


def test_reconcile_keeps_terminating_task_when_child_still_alive(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    wt = repo / ".worktrees" / "t1"
    wt.mkdir(parents=True)
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "trap '' TERM; sleep 30"],
        cwd=wt,
        preexec_fn=os.setsid,
    )
    try:
        time.sleep(0.05)
        state = load_state(repo)
        child = _child_snapshot(
            proc,
            cwd=str(wt),
            argv=["/bin/sh", "-c", "trap '' TERM; sleep 30"],
        )
        state["tasks"]["t1"] = {
            "status": "terminating",
            "started_at": "2026-04-19T00:00:00Z",
            "terminating_since": runner_module.now_iso(),
            "finished_at": None,
            "child": child,
            "terminal_status": "cancelled",
            "failure_reason": "cancelled by user",
        }
        write_state(repo, state)
        runner = Runner(repo, RunnerConfig(on_watcher_restart="resume"), otto_bin="/bin/true")
        runner._lock_fh = acquire_lock(repo)
        try:
            runner._reconcile_on_startup()
            state2 = load_state(repo)
            assert state2["tasks"]["t1"]["status"] == "terminating"
            assert state2["tasks"]["t1"]["terminal_status"] == "cancelled"
            assert state2["tasks"]["t1"]["child"] == child
        finally:
            runner._lock_fh.close()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(proc.pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass


def test_reconcile_finishes_terminating_task_when_child_is_gone(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    wt = repo / ".worktrees" / "t1"
    wt.mkdir(parents=True)
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "exit 0"],
        cwd=wt,
        preexec_fn=os.setsid,
    )
    proc.wait(timeout=5)
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "terminating",
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": None,
        "child": {
            "pid": proc.pid,
            "pgid": proc.pid,
            "start_time_ns": int(time.time() * 1_000_000_000),
            "argv": ["/bin/sh", "-c", "exit 0"],
            "cwd": str(wt),
        },
        "terminal_status": "cancelled",
        "failure_reason": "cancelled by user",
    }
    write_state(repo, state)
    runner = Runner(repo, RunnerConfig(on_watcher_restart="resume"), otto_bin="/bin/true")
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._reconcile_on_startup()
        state2 = load_state(repo)
        assert state2["tasks"]["t1"]["status"] == "cancelled"
        assert state2["tasks"]["t1"]["child"] is None
        assert "terminal_status" not in state2["tasks"]["t1"]
        assert state2["tasks"]["t1"]["finished_at"] is not None
    finally:
        runner._lock_fh.close()


def test_reconcile_and_tick_preserve_running_orphaned_child_until_manifest_finalizes(tmp_path: Path):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    wt = repo / ".worktrees" / "t1"
    wt.mkdir(parents=True)
    child = _spawn_orphan_child(cwd=wt, command="trap '' TERM; sleep 30")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "child": child,
        "manifest_path": None,
        "cost_usd": None,
        "duration_s": None,
        "failure_reason": None,
    }
    write_state(repo, state)

    runner = Runner(
        repo,
        # task_timeout_s=None disables timeout enforcement — this test
        # asserts reconcile behavior (the hardcoded started_at is
        # intentionally stale to simulate watcher crash recovery).
        RunnerConfig(on_watcher_restart="resume", poll_interval_s=0.01, task_timeout_s=None),
        otto_bin="/bin/true",
    )
    runner._lock_fh = acquire_lock(repo)
    try:
        assert child_is_alive(child) is True
        with pytest.raises(ChildProcessError):
            os.waitpid(child["pid"], os.WNOHANG)

        runner._reconcile_on_startup()
        state2 = load_state(repo)
        assert state2["tasks"]["t1"]["status"] == "running"
        assert child_is_alive(state2["tasks"]["t1"]["child"]) is True

        runner._tick()
        state3 = load_state(repo)
        assert state3["tasks"]["t1"]["status"] == "running"
        assert child_is_alive(state3["tasks"]["t1"]["child"]) is True

        _write_queue_manifest(repo, "t1", exit_status="success")
        os.killpg(child["pgid"], signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and child_is_alive(child):
            time.sleep(0.05)
        assert child_is_alive(child) is False

        runner._tick()
        state4 = load_state(repo)
        assert state4["tasks"]["t1"]["status"] == "done"
        assert state4["tasks"]["t1"]["child"] is None
        assert state4["tasks"]["t1"]["manifest_path"] is not None
        assert state4["tasks"]["t1"]["cost_usd"] == 0.42
        assert state4["tasks"]["t1"]["duration_s"] == 1.0
        assert state4["tasks"]["t1"]["exit_code"] is None
    finally:
        runner._lock_fh.close()
        try:
            os.killpg(child["pgid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_reap_processes_completed_child_before_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    _write_queue_manifest(repo, "t1", exit_status="success")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "started_at": "2026-04-19T00:00:00Z",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 12345, "pgid": 12345, "start_time_ns": 1, "argv": ["done"], "cwd": str(repo)},
    }
    write_state(repo, state)
    monkeypatch.setattr(runner_module.os, "waitpid", lambda _pid, _flags: (12345, 0))
    runner = Runner(repo, RunnerConfig(task_timeout_s=1.0), otto_bin="/bin/true")

    state = load_state(repo)
    runner._reap_children(state)

    assert state["tasks"]["t1"]["status"] == "done"
    assert state["tasks"]["t1"].get("terminal_status") is None


def test_tick_reaps_completed_child_before_applying_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    _write_queue_manifest(repo, "t1", exit_status="success")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "started_at": runner_module.now_iso(),
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 12345, "pgid": 12345, "start_time_ns": 1, "argv": ["done"], "cwd": str(repo)},
    }
    write_state(repo, state)
    append_command(repo, {"schema_version": 1, "command_id": "cmd-1", "kind": "cancel", "id": "t1"})
    monkeypatch.setattr(runner_module.os, "waitpid", lambda _pid, _flags: (12345, 0))
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")

    runner._tick()

    task_state = load_state(repo)["tasks"]["t1"]
    assert task_state["status"] == "done"
    assert task_state["failure_reason"] is None


def test_cancel_rechecks_child_exit_race_before_killing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    _write_queue_manifest(repo, "t1", exit_status="success")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "started_at": runner_module.now_iso(),
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 12345, "pgid": 12345, "start_time_ns": 1, "argv": ["done"], "cwd": str(repo)},
    }
    write_state(repo, state)
    append_command(repo, {"schema_version": 1, "command_id": "cmd-1", "kind": "cancel", "id": "t1"})
    wait_results = iter([(0, 0), (12345, 0)])
    monkeypatch.setattr(runner_module.os, "waitpid", lambda _pid, _flags: next(wait_results))
    monkeypatch.setattr(
        runner_module,
        "kill_child_safely",
        lambda _child, _sig=signal.SIGTERM: (_ for _ in ()).throw(AssertionError("cancel killed exited child")),
    )
    runner = Runner(repo, RunnerConfig(concurrent=1, task_timeout_s=None), otto_bin="/bin/true")

    runner._tick()

    task_state = load_state(repo)["tasks"]["t1"]
    assert task_state["status"] == "done"
    assert task_state["failure_reason"] is None


def test_timeout_rechecks_child_exit_race_before_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    _write_queue_manifest(repo, "t1", exit_status="success")
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "started_at": "2026-04-19T00:00:00Z",
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 12345, "pgid": 12345, "start_time_ns": 1, "argv": ["done"], "cwd": str(repo)},
    }
    write_state(repo, state)
    wait_results = iter([(0, 0), (12345, 0)])
    monkeypatch.setattr(runner_module.os, "waitpid", lambda _pid, _flags: next(wait_results))
    monkeypatch.setattr(
        runner_module,
        "kill_child_safely",
        lambda _child, _sig=signal.SIGTERM: (_ for _ in ()).throw(AssertionError("timeout killed exited child")),
    )
    runner = Runner(repo, RunnerConfig(concurrent=1, task_timeout_s=1.0), otto_bin="/bin/true")

    state_for_reap = load_state(repo)
    runner._reap_children(state_for_reap)

    assert state_for_reap["tasks"]["t1"]["status"] == "done"
    assert state_for_reap["tasks"]["t1"].get("terminal_status") is None


def test_remove_running_task_keeps_definition_until_terminal_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "running",
        "started_at": runner_module.now_iso(),
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 12345, "pgid": 12345, "start_time_ns": 1, "argv": ["sleep"], "cwd": str(repo)},
    }
    write_state(repo, state)
    append_command(repo, {"schema_version": 1, "command_id": "cmd-1", "kind": "remove", "id": "t1"})
    wait_results = iter([(0, 0), (0, 0), (12345, 0)])
    monkeypatch.setattr(runner_module.os, "waitpid", lambda _pid, _flags: next(wait_results))
    monkeypatch.setattr(runner_module, "kill_child_safely", lambda _child, _sig=signal.SIGTERM: True)
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")

    runner._tick()
    assert [task.id for task in load_queue(repo)] == ["t1"]
    assert load_state(repo)["tasks"]["t1"]["status"] == "terminating"

    runner._tick()
    assert load_state(repo)["tasks"]["t1"]["status"] == "removed"
    assert load_queue(repo) == []


def test_immediate_shutdown_finalizes_queue_live_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    )
    append_task(repo, task)
    task_state = {
        "status": "running",
        "started_at": runner_module.now_iso(),
        "attempt_run_id": "2026-04-23-010203-abc123",
        "child": {"pid": 12345, "pgid": 12345, "start_time_ns": 1, "argv": ["sleep"], "cwd": str(repo)},
    }
    state = load_state(repo)
    state["tasks"]["t1"] = dict(task_state)
    write_state(repo, state)
    runner = Runner(repo, RunnerConfig(concurrent=1), otto_bin="/bin/true")
    runner._write_queue_run_record(task, task_state, status="running")
    monkeypatch.setattr(runner_module, "kill_child_safely", lambda _child, _sig=signal.SIGTERM: True)

    runner._kill_all_in_flight(force=True)

    task_state = load_state(repo)["tasks"]["t1"]
    assert task_state["status"] == INTERRUPTED_STATUS
    record = json.loads(_paths.live_run_path(repo, "2026-04-23-010203-abc123").read_text(encoding="utf-8"))
    assert record["status"] == INTERRUPTED_STATUS
    assert record["terminal_outcome"] == INTERRUPTED_STATUS


def test_tick_history_repair_waits_for_successful_state_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "done",
        "finished_at": "2026-04-23T00:00:00Z",
        "child": None,
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "failure_reason": None,
        "attempt_run_id": "run-123",
    }
    write_state(repo, state)

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")

    def _boom(_state: dict[str, Any]) -> None:
        raise runner_module.StatePersistenceError("disk full")

    monkeypatch.setattr(runner, "_write_state_or_raise", _boom)

    with pytest.raises(runner_module.StatePersistenceError, match="disk full"):
        runner._tick()

    assert not _paths.history_jsonl(repo).exists()
    persisted = load_state(repo)
    assert persisted["tasks"]["t1"].get("history_appended") is not True


def test_startup_reconcile_history_repair_waits_for_successful_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "failed",
        "finished_at": "2026-04-23T00:00:00Z",
        "child": None,
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "failure_reason": "boom",
        "attempt_run_id": "run-456",
    }
    write_state(repo, state)

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")

    def _boom(_state: dict[str, Any]) -> None:
        raise runner_module.StatePersistenceError("disk full")

    monkeypatch.setattr(runner, "_write_state_or_raise", _boom)

    with pytest.raises(runner_module.StatePersistenceError, match="disk full"):
        runner._reconcile_on_startup()

    assert not _paths.history_jsonl(repo).exists()
    persisted = load_state(repo)
    assert persisted["tasks"]["t1"].get("history_appended") is not True


def test_tick_persists_terminal_history_repair_state(tmp_path: Path):
    from otto.runs.history import read_history_rows

    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="t1",
        command_argv=["build", "x"],
        branch="build/t1",
        worktree=".worktrees/t1",
    ))
    state = load_state(repo)
    state["tasks"]["t1"] = {
        "status": "done",
        "finished_at": "2026-04-23T00:00:00Z",
        "child": None,
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "failure_reason": None,
        "attempt_run_id": "run-789",
    }
    write_state(repo, state)

    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner._tick()

    persisted = load_state(repo)
    assert persisted["tasks"]["t1"]["history_appended"] is True
    rows = read_history_rows(_paths.history_jsonl(repo))
    assert any(row.get("run_id") == "run-789" and row.get("status") == "done" for row in rows)


def test_replayed_noop_command_is_acked_and_clears_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = init_repo(tmp_path)
    task = QueueTask(
        id="resume1",
        command_argv=["build", "x"],
        resumable=True,
        branch="build/resume1",
        worktree=".worktrees/resume1",
    )
    append_task(repo, task)
    _write_resume_checkpoint(repo, task)
    state = load_state(repo)
    state["tasks"]["resume1"] = {
        "status": INTERRUPTED_STATUS,
        "started_at": "2026-04-19T00:00:00Z",
        "finished_at": "2026-04-19T00:05:00Z",
        "failure_reason": "interrupted by watcher shutdown; resume available",
    }
    write_state(repo, state)
    cmd = append_command(repo, {"cmd": "resume", "id": "resume1"})
    runner = Runner(repo, RunnerConfig(), otto_bin="/bin/true")
    runner.shutdown_level = "graceful"
    real_ack = runner_module.append_command_ack
    calls = 0

    def flaky_ack(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("ack disk full")
        return real_ack(*args, **kwargs)

    monkeypatch.setattr(runner_module, "append_command_ack", flaky_ack)

    with pytest.raises(OSError, match="ack disk full"):
        runner._tick()

    assert load_state(repo)["tasks"]["resume1"]["status"] == "queued"
    assert runner_module.queue_schema.commands_processing_path(repo).exists()

    runner._tick()

    assert calls == 2
    assert not runner_module.queue_schema.commands_processing_path(repo).exists()
    assert cmd["command_id"] in runner_module.queue_schema.load_command_ack_ids(repo)
