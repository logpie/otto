from __future__ import annotations

import json
import time
from pathlib import Path

from otto import paths
from otto.queue.runner import Runner, RunnerConfig, acquire_lock
from otto.queue.schema import QueueTask, append_task, load_queue, load_state, write_state
from tests._helpers import init_repo

from .conftest import git


def test_queue_run_cli_dispatches_real_worktree_child_and_drains_queue(
    tmp_path: Path,
    fake_subprocess_otto,
    cli_in_repo,
) -> None:
    repo = init_repo(tmp_path)
    fake_otto = fake_subprocess_otto(exit_code=0, sleep_s=0.05, write_manifest=True)

    enqueue = cli_in_repo(repo, ["queue", "build", "queue integration task"])
    assert enqueue.exit_code == 0, enqueue.output

    run = cli_in_repo(
        repo,
        ["queue", "run", "--no-dashboard", "--exit-when-empty", "--concurrent", "1"],
        env={"OTTO_BIN": str(fake_otto), "OTTO_NO_TUI": "1"},
    )

    task = load_queue(repo)[0]
    state = load_state(repo)
    manifest_path = repo / "otto_logs" / "queue" / task.id / "manifest.json"

    assert run.exit_code == 0, run.output
    assert state["tasks"][task.id]["status"] == "done"
    assert state["tasks"][task.id]["exit_code"] == 0
    assert state["tasks"][task.id]["cost_usd"] == 0.42
    assert (repo / task.worktree).exists()
    assert manifest_path.exists()


def test_runner_requeues_paused_task_and_spawns_resume_argv(
    tmp_path: Path,
    fake_subprocess_otto,
) -> None:
    repo = init_repo(tmp_path)
    capture_path = tmp_path / "spawn-capture.json"
    fake_otto = fake_subprocess_otto(
        exit_code=0,
        sleep_s=0.05,
        write_manifest=True,
        capture_path=capture_path,
    )

    task = QueueTask(
        id="resume-task",
        command_argv=["build", "resume task"],
        resolved_intent="resume task",
        branch="build/resume-task-2026-04-22",
        worktree=".worktrees/resume-task",
    )
    append_task(repo, task)

    worktree = repo / ".worktrees" / "resume-task"
    git(repo, "worktree", "add", "-b", task.branch, str(worktree))

    session_id = "2026-04-22-010203-abcdef"
    paths.ensure_session_scaffold(worktree, session_id)
    paths.set_pointer(worktree, paths.PAUSED_POINTER, session_id)
    paths.session_checkpoint(worktree, session_id).write_text(json.dumps({
        "status": "paused",
        "run_id": session_id,
        "updated_at": "2026-04-22T01:02:03Z",
    }))

    state = load_state(repo)
    state["tasks"][task.id] = {
        "status": "running",
        "child": {
            "pid": 1,
            "pgid": 1,
            "start_time_ns": 0,
            "argv": ["gone"],
            "cwd": "/tmp",
        },
    }
    write_state(repo, state)

    runner = Runner(
        repo,
        RunnerConfig(concurrent=1, poll_interval_s=0.05, heartbeat_interval_s=0.1),
        otto_bin=str(fake_otto),
    )
    runner._lock_fh = acquire_lock(repo)
    try:
        runner._reconcile_on_startup()
        reconciled = load_state(repo)
        assert reconciled["tasks"][task.id]["status"] == "queued"
        assert reconciled["tasks"][task.id]["resumed_from_checkpoint"] is True

        runner._tick()
        time.sleep(0.25)
        runner._tick()
    finally:
        runner._lock_fh.close()

    capture = json.loads(capture_path.read_text())
    final_state = load_state(repo)

    assert capture["argv"][-1] == "--resume"
    assert capture["cwd"] == str(worktree)
    assert final_state["tasks"][task.id]["status"] == "done"
