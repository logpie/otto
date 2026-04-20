"""Tests for otto/queue/runner.py — Phase 2.7-2.8.

Strategy: where possible, exercise the real Runner against a tiny shell
command (`/bin/sh -c "exit 0"`) instead of full otto subprocesses, so
tests stay fast and deterministic. PID-reuse safety + reconciliation are
tested via direct state.json edits to simulate edge cases per Codex
round 4 finding (no flaky real-PID-recycling tests).
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
    append_task,
    load_queue,
    load_state,
    write_state,
)


# ---------- acquire_lock ----------


def test_acquire_lock_succeeds_first_time(tmp_path: Path):
    fh = acquire_lock(tmp_path)
    assert fh is not None
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


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    return repo


def _make_fake_otto(tmp_path: Path, *, exit_code: int = 0, sleep: float = 0.1, write_manifest: bool = True) -> Path:
    """Write a tiny shell script that mimics otto: sleeps, optionally writes
    a manifest at the queue path, and exits with `exit_code`."""
    fake = tmp_path / "fake_otto.sh"
    manifest_block = ""
    if write_manifest:
        manifest_block = '''
TASK_ID="${OTTO_QUEUE_TASK_ID:-}"
if [ -n "$TASK_ID" ]; then
  MANIFEST_DIR="${OTTO_QUEUE_PROJECT_DIR}/otto_logs/queue/${TASK_ID}"
  mkdir -p "$MANIFEST_DIR"
  cat > "$MANIFEST_DIR/manifest.json" <<EOF
{
  "command": "build",
  "argv": ["build", "test"],
  "queue_task_id": "$TASK_ID",
  "run_id": "fake-run",
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
sleep {sleep}
{manifest_block}
exit {exit_code}
""")
    fake.chmod(0o755)
    return fake


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
    repo = _make_repo(tmp_path)
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
        # Task should be running now
        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] == "running"
        # Wait for the fake to finish (sleep 0.1)
        time.sleep(1.5)
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] == "done", \
            f"expected done, got {state['tasks']['t1']!r}"
        assert state["tasks"]["t1"]["cost_usd"] == 0.42
        assert state["tasks"]["t1"]["exit_code"] == 0
    finally:
        runner._lock_fh.close()


def test_runner_marks_failed_when_no_manifest(tmp_path: Path):
    repo = _make_repo(tmp_path)
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
        time.sleep(1.0)
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] == "failed"
        assert "no manifest" in state["tasks"]["t1"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_runner_marks_failed_on_nonzero_exit(tmp_path: Path):
    repo = _make_repo(tmp_path)
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
        time.sleep(1.0)
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["t1"]["status"] == "failed"
        assert "exit_code=1" in state["tasks"]["t1"]["failure_reason"]
    finally:
        runner._lock_fh.close()


def test_runner_respects_concurrent_cap(tmp_path: Path):
    repo = _make_repo(tmp_path)
    fake_otto = _make_fake_otto(tmp_path, exit_code=0, sleep=0.5)
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
        running = sum(1 for ts in state["tasks"].values() if ts.get("status") == "running")
        assert running == 2, f"expected 2 running, got {running}: {state['tasks']!r}"
        # Untouched tasks aren't in state.json yet — they're "queued" by absence.
        assert len(state["tasks"]) == 2, \
            f"expected only 2 tasks in state (dispatched), got {len(state['tasks'])}"
        time.sleep(2.0)
        runner._tick()
        runner._tick()
        time.sleep(2.0)
        runner._tick()
        state = load_state(repo)
        done = sum(1 for ts in state["tasks"].values() if ts.get("status") == "done")
        assert done == 3, f"expected all done, got: {state['tasks']!r}"
    finally:
        runner._lock_fh.close()


# ---------- dependencies (Phase 3) ----------


def test_runner_cascades_failure_through_after_chain(tmp_path: Path):
    """Phase 3.2 verify: A fails → B (after A) cascades failed → C (after B) cascades failed.
    Verifies transitive cascade across a 3-deep chain."""
    repo = _make_repo(tmp_path)
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
        assert state["tasks"]["a"]["status"] == "running"
        # Wait for a to die
        time.sleep(0.5)
        # Tick 2: reap a (failed); cascade-fail b (since a failed)
        runner._tick()
        state = load_state(repo)
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
    repo = _make_repo(tmp_path)
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
        assert state["tasks"]["a"]["status"] == "running"
        # b not yet dispatched → not in state.json yet
        assert "b" not in state["tasks"]
        time.sleep(1.5)
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["a"]["status"] == "done"
        # Now b should dispatch
        runner._tick()
        state = load_state(repo)
        assert state["tasks"]["b"]["status"] == "running"
    finally:
        runner._lock_fh.close()


# ---------- regression: F10 per-task timeout ----------


def test_runner_kills_task_exceeding_timeout(tmp_path: Path):
    """F10: a child that runs longer than task_timeout_s gets SIGTERM and
    transitions to status=failed with a 'timed out' reason. Without this,
    a hung agent (e.g. pkill bash bug like P3 base build) would occupy its
    concurrency slot indefinitely."""
    repo = _make_repo(tmp_path)
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
        task_timeout_s=2.0,  # 2 second timeout — well under the 60s sleep
    )
    runner = Runner(repo, cfg, otto_bin=str(fake))
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._tick()
        # Task should be running
        assert load_state(repo)["tasks"]["hang"]["status"] == "running"
        # Wait past the 2s timeout
        time.sleep(3.0)
        runner._tick()  # should observe the timeout and SIGTERM the child
        ts = load_state(repo)["tasks"]["hang"]
        # Status should have transitioned to terminating (or already past it)
        assert ts["status"] in ("terminating", "failed", "cancelled"), \
            f"expected transition from running, got {ts['status']!r}"
        assert "timed out" in (ts.get("failure_reason") or ""), \
            f"expected 'timed out' in reason, got {ts.get('failure_reason')!r}"
        # Wait for child to die + reaper to finalize
        time.sleep(3.0)
        runner._tick()
        ts2 = load_state(repo)["tasks"]["hang"]
        assert ts2["status"] == "failed", f"expected failed, got {ts2['status']!r}"
    finally:
        runner._lock_fh.close()


def test_runner_does_not_timeout_task_under_limit(tmp_path: Path):
    """A short-lived task must NOT be killed if it finishes before timeout."""
    repo = _make_repo(tmp_path)
    fake = _make_fake_otto(tmp_path, exit_code=0, sleep=0.2)
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
        time.sleep(1.5)
        runner._tick()
        ts = load_state(repo)["tasks"]["quick"]
        assert ts["status"] == "done", f"expected done, got {ts['status']!r}"
        assert "timed out" not in (ts.get("failure_reason") or "")
    finally:
        runner._lock_fh.close()


def test_runner_task_timeout_disabled_when_none(tmp_path: Path):
    """task_timeout_s=None disables the enforcement entirely (escape hatch)."""
    repo = _make_repo(tmp_path)
    fake = _make_fake_otto(tmp_path, exit_code=0, sleep=0.2)
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
        time.sleep(1.5)
        runner._tick()
        ts = load_state(repo)["tasks"]["t"]
        assert ts["status"] == "done"
    finally:
        runner._lock_fh.close()


def test_runner_config_loads_task_timeout_from_yaml():
    """`queue.task_timeout_s` in otto.yaml is honored; absent → 1800 default."""
    cfg = runner_config_from_otto_config({"queue": {"task_timeout_s": 600}})
    assert cfg.task_timeout_s == 600.0
    cfg2 = runner_config_from_otto_config({"queue": {}})
    assert cfg2.task_timeout_s == 1800.0
    cfg3 = runner_config_from_otto_config({"queue": {"task_timeout_s": None}})
    assert cfg3.task_timeout_s is None
    cfg4 = runner_config_from_otto_config({"queue": {"task_timeout_s": 0}})
    assert cfg4.task_timeout_s is None  # 0 is also "off"


# ---------- regression: F1 manifest path env-var contract ----------


def test_runner_sets_queue_project_dir_env_on_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression for F1: the runner MUST set OTTO_QUEUE_PROJECT_DIR so the
    spawned otto (whose cwd is the worktree) writes its manifest where the
    watcher (cwd = main project) will look for it. Without this, every queue
    run would be marked failed with `no manifest`."""
    repo = _make_repo(tmp_path)

    captured_env: dict[str, str] = {}

    def fake_popen(argv: list[str], *, cwd: str, env: dict[str, str], preexec_fn: Any):  # type: ignore[no-untyped-def]
        captured_env.update(env)

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
        from otto.queue.schema import load_state as _load
        runner._tick()
        # Verify both env vars are set
        assert captured_env.get("OTTO_QUEUE_TASK_ID") == "t1"
        assert captured_env.get("OTTO_QUEUE_PROJECT_DIR") == str(repo), \
            f"runner must set OTTO_QUEUE_PROJECT_DIR; got: {captured_env.get('OTTO_QUEUE_PROJECT_DIR')!r}"
        assert captured_env.get("OTTO_INTERNAL_QUEUE_RUNNER") == "1"
    finally:
        runner._lock_fh.close()


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


def test_runner_config_falls_back_to_defaults_with_empty_queue():
    cfg = runner_config_from_otto_config({"queue": {}})
    assert cfg.concurrent == 3
    assert cfg.worktree_dir == ".worktrees"
    assert cfg.on_watcher_restart == "resume"


def test_run_logs_and_continues_after_tick_exception(tmp_path: Path, caplog):
    repo = _make_repo(tmp_path)
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
        assert runner.run() == 0
    assert seen["count"] >= 2
    assert "tick failed; continuing" in caplog.text


def test_tick_logs_malformed_queue_yml_and_continues_after_fix(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    repo = _make_repo(tmp_path)
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
        assert state["tasks"]["t1"]["status"] == "running"
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


def test_run_exits_on_state_persistence_failure_after_spawn(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    repo = _make_repo(tmp_path)
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
        if calls["count"] == 3:
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


def test_reap_children_keeps_running_task_when_echild_child_is_still_alive(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    repo = _make_repo(tmp_path)
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


def test_tick_logs_same_cycle_only_once(tmp_path: Path, caplog):
    repo = _make_repo(tmp_path)
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
    repo = _make_repo(tmp_path)
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
    repo = _make_repo(tmp_path)
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


def test_spawn_uses_snapshotted_branch_when_intent_matches(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path)
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


# ---------- restart reconciliation ----------


def test_reconcile_marks_certify_failed_when_child_gone(tmp_path: Path):
    """certify is not resumable; on restart with dead child, mark failed."""
    repo = _make_repo(tmp_path)
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
    repo = _make_repo(tmp_path)
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


def test_reconcile_requeues_when_checkpoint_exists(tmp_path: Path):
    """Resumable task + checkpoint + policy=resume → re-queue (will respawn with --resume)."""
    repo = _make_repo(tmp_path)
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


def test_reconcile_keeps_terminating_task_when_child_still_alive(tmp_path: Path):
    repo = _make_repo(tmp_path)
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
    repo = _make_repo(tmp_path)
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
    repo = _make_repo(tmp_path)
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
        # task_timeout_s=None disables F10 — test asserts behavior of reconcile,
        # not of timeout enforcement (the hardcoded started_at is intentionally
        # stale to simulate watcher crash recovery).
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
