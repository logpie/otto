from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from otto.queue.schema import QueueTask, append_task
from otto.mission_control.model import MissionControlModel
from tests._helpers import init_repo


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PATH"] = str(repo_root / ".venv" / "bin") + os.pathsep + env.get("PATH", "")
    env["CLAUDECODE"] = ""
    env["CI"] = "true"
    return env


def _publisher_script() -> str:
    return textwrap.dedent(
        """
        import json
        import sys
        import time
        from pathlib import Path

        from otto import paths
        from otto.merge.state import MergeState, write_state
        from otto.runs.registry import RunPublisher, make_run_record

        project_dir = Path(sys.argv[1])
        domain = sys.argv[2]
        run_type = sys.argv[3]
        run_id = sys.argv[4]
        final_sleep = float(sys.argv[5])
        finalize = sys.argv[6] == "1"
        primary_phase = "build"
        session_dir = paths.session_dir(project_dir, run_id)
        log_path = session_dir / primary_phase / "narrative.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{run_id} start\\n", encoding="utf-8")
        session_dir.mkdir(parents=True, exist_ok=True)
        if domain == "merge":
            write_state(
                project_dir,
                MergeState(
                    merge_id=run_id,
                    started_at="2026-04-23T12:00:00Z",
                    target="main",
                    branches_in_order=["feature/a", "feature/b"],
                ),
            )
            primary_log = paths.merge_dir(project_dir) / "merge.log"
            primary_log.parent.mkdir(parents=True, exist_ok=True)
            primary_log.write_text("merge start\\n", encoding="utf-8")
            artifacts = {
                "session_dir": str(paths.merge_dir(project_dir) / run_id),
                "manifest_path": None,
                "checkpoint_path": None,
                "summary_path": None,
                "primary_log_path": str(primary_log),
                "extra_log_paths": [],
            }
            adapter_key = "merge.run"
        else:
            artifacts = {
                "session_dir": str(session_dir),
                "manifest_path": str(session_dir / "manifest.json"),
                "checkpoint_path": None,
                "summary_path": str(session_dir / "summary.json"),
                "primary_log_path": str(log_path),
                "extra_log_paths": [],
            }
            adapter_key = f"{domain}.{run_type}"
        record = make_run_record(
            project_dir=project_dir,
            run_id=run_id,
            domain=domain,
            run_type=run_type,
            command=run_type,
            display_name=f"{run_type}: {run_id}",
            status="running",
            cwd=project_dir,
            artifacts=artifacts,
            adapter_key=adapter_key,
            last_event="starting",
        )
        record.timing["heartbeat_interval_s"] = 0.2
        with RunPublisher(project_dir, record, heartbeat_interval_s=0.2) as publisher:
            print("READY", flush=True)
            time.sleep(final_sleep)
            if finalize:
                publisher.finalize(status="done", terminal_outcome="success", updates={"last_event": "done"})
        """
    )


def _make_fake_otto(tmp_path: Path, *, sleep: float = 2.5) -> Path:
    fake = tmp_path / "fake_otto.sh"
    fake.write_text(
        f"""#!/bin/sh
TASK_ID="${{OTTO_QUEUE_TASK_ID:-}}"
RUN_ID="${{OTTO_RUN_ID:-queue-run}}"
PROJECT_DIR="${{OTTO_QUEUE_PROJECT_DIR:-$PWD}}"
WORKTREE="$PROJECT_DIR/.worktrees/$TASK_ID"
SESSION_DIR="$WORKTREE/otto_logs/sessions/$RUN_ID"
mkdir -p "$SESSION_DIR/build" "$PROJECT_DIR/otto_logs/queue/$TASK_ID"
printf '%s\\n' "$TASK_ID queue log" > "$SESSION_DIR/build/narrative.log"
cat > "$PROJECT_DIR/otto_logs/queue/$TASK_ID/manifest.json" <<EOF
{{
  "command": "build",
  "argv": ["build", "{sleep}"],
  "queue_task_id": "$TASK_ID",
  "run_id": "$RUN_ID",
  "branch": "build/$TASK_ID",
  "checkpoint_path": null,
  "proof_of_work_path": null,
  "cost_usd": 0.42,
  "duration_s": 1.0,
  "started_at": "2026-04-23T12:00:00Z",
  "finished_at": "2026-04-23T12:00:01Z",
  "head_sha": null,
  "resolved_intent": "$TASK_ID",
  "focus": null,
  "target": null,
  "exit_status": "success",
  "schema_version": 1,
  "extra": {{}}
}}
EOF
sleep {sleep}
"""
    )
    fake.chmod(0o755)
    return fake


def _wait_ready(proc: subprocess.Popen[str], *, timeout_s: float = 5.0) -> None:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=0.2)
            raise AssertionError(
                f"publisher exited before READY: rc={proc.returncode}, "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if not ready:
            continue
        line = proc.stdout.readline().strip()
        assert line == "READY"
        return
    raise AssertionError("timed out waiting for READY")


def test_mission_control_multiprocess_registry_integration(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    env = _subprocess_env()

    fake_otto = _make_fake_otto(tmp_path)
    append_task(
        repo,
        QueueTask(
            id="queued-task",
            command_argv=["build", "queued task"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="queued task",
            branch="build/queued-task",
            worktree=".worktrees/queued-task",
        ),
    )

    watcher_env = dict(env)
    watcher_env["OTTO_BIN"] = str(fake_otto)
    watcher = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import os
                import sys
                from pathlib import Path

                from otto.queue.runner import Runner, RunnerConfig

                project_dir = Path(sys.argv[1])
                runner = Runner(
                    project_dir,
                    RunnerConfig(concurrent=1, poll_interval_s=0.1, heartbeat_interval_s=0.2),
                    otto_bin=os.environ["OTTO_BIN"],
                )
                raise SystemExit(runner.run())
                """
            ),
            str(repo),
        ],
        cwd=repo,
        env=watcher_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    build_proc = subprocess.Popen(
        [sys.executable, "-c", _publisher_script(), str(repo), "atomic", "build", "build-run", "0.8", "1"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    merge_proc = subprocess.Popen(
        [sys.executable, "-c", _publisher_script(), str(repo), "merge", "merge", "merge-run", "2.0", "1"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stale_proc = subprocess.Popen(
        [sys.executable, "-c", _publisher_script(), str(repo), "atomic", "build", "stale-run", "0.5", "0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_ready(build_proc)
        _wait_ready(merge_proc)
        _wait_ready(stale_proc)

        model = MissionControlModel(repo)
        state = model.initial_state()
        deadline = time.time() + 4.0
        seen = set()
        while time.time() < deadline:
            state = model.refresh(state)
            seen = {item.record.run_id for item in state.live_runs.items}
            if {"build-run", "merge-run", "stale-run"} <= seen and any(item.record.run_type == "queue" for item in state.live_runs.items):
                break
            time.sleep(0.1)
        assert {"build-run", "merge-run", "stale-run"} <= seen
        assert any(item.record.run_type == "queue" for item in state.live_runs.items)

        deadline = time.time() + 4.0
        by_id = {}
        while time.time() < deadline:
            state = model.refresh(state)
            by_id = {item.record.run_id: item for item in state.live_runs.items}
            if by_id.get("build-run") and by_id["build-run"].record.status == "done":
                break
            time.sleep(0.1)
        assert by_id["build-run"].record.status == "done"
        assert by_id["merge-run"].record.status in {"running", "done"}
        assert any(item.record.run_type == "queue" for item in state.live_runs.items)

        stale_proc.terminate()
        stale_proc.wait(timeout=5)
        state = model.refresh(state)
        model._stale_trackers["stale-run"].last_progress_monotonic -= 16.0
        state = model.refresh(state)
        by_id = {item.record.run_id: item for item in state.live_runs.items}
        assert by_id["stale-run"].overlay is not None
        assert by_id["stale-run"].overlay.label == "STALE"
    finally:
        for proc in (watcher, build_proc, merge_proc, stale_proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
