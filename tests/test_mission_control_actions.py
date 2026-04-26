from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import otto.mission_control.actions as mission_control_actions
from otto import paths
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
from otto.runs.registry import make_run_record, read_jsonl_rows, write_record
from otto.mission_control.adapters import adapter_for_key
from otto.mission_control.actions import (
    ActionResult,
    execute_action,
    execute_merge_abort,
    execute_merge_all,
    execute_merge_recover,
    execute_queue_cleanup,
)


class _FakePopen:
    calls: list[dict[str, object]] = []

    def __init__(
        self,
        argv,
        *,
        cwd,
        stdout,
        stderr,
        text,
        start_new_session=False,
    ) -> None:
        assert start_new_session is True
        type(self).calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "stdout": stdout,
                "stderr": stderr,
                "text": text,
                "start_new_session": start_new_session,
            }
        )
        self.returncode = 0
        self.pid = 4242

    def poll(self):
        return self.returncode

    def communicate(self):
        return ("", "")

    def wait(self):
        return self.returncode


class _FakeLongRunningPopen:
    def __init__(
        self,
        argv,
        *,
        cwd,
        stdout,
        stderr,
        text,
        start_new_session=False,
    ) -> None:
        assert start_new_session is True
        del argv, cwd, stdout, stderr, text
        self.returncode = None
        self.pid = 4343

    def poll(self):
        return self.returncode

    def wait(self):
        time.sleep(0.05)
        self.returncode = 1
        return self.returncode

    def communicate(self):
        self.wait()
        return ("late failure", "")


def _record(
    repo: Path,
    *,
    run_id: str,
    domain: str,
    run_type: str,
    status: str,
    adapter_key: str,
    queue_task_id: str | None = None,
    merge_id: str | None = None,
    argv: list[str] | None = None,
) -> object:
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain=domain,
        run_type=run_type,
        command=run_type,
        display_name=f"{run_type}: {run_id}",
        status=status,
        cwd=repo,
        identity={"queue_task_id": queue_task_id, "merge_id": merge_id, "parent_run_id": None},
        source={"argv": list(argv or []), "resumable": True},
        adapter_key=adapter_key,
    )
    record.timing["heartbeat_interval_s"] = 0.2
    record.writer["pgid"] = 321
    write_record(repo, record)
    return record


def _append_ack_later(path: Path, *, command_id: str, run_id: str) -> None:
    def _writer() -> None:
        time.sleep(0.03)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "command_id": command_id,
                        "run_id": run_id,
                        "acked_at": "2026-04-23T12:00:00Z",
                        "writer_id": "writer",
                        "outcome": "applied",
                        "state_version": 1,
                        "note": None,
                    }
                )
            )
            handle.write("\n")

    thread = threading.Thread(target=_writer, daemon=True)
    thread.start()
    return thread


def test_cancel_appends_envelope_and_clears_banner_for_queue_atomic_and_merge(tmp_path: Path) -> None:
    cases = [
        (
            _record(tmp_path, run_id="queue-run", domain="queue", run_type="queue", status="running", adapter_key="queue.attempt", queue_task_id="task-1"),
            paths.queue_commands_path(tmp_path),
            paths.queue_command_acks_path(tmp_path),
            {"task_id": "task-1"},
        ),
        (
            _record(tmp_path, run_id="atomic-run", domain="atomic", run_type="build", status="running", adapter_key="atomic.build"),
            paths.session_command_requests(tmp_path, "atomic-run"),
            paths.session_command_acks(tmp_path, "atomic-run"),
            {},
        ),
        (
            _record(tmp_path, run_id="merge-run", domain="merge", run_type="merge", status="running", adapter_key="merge.run", merge_id="merge-run"),
            paths.merge_command_requests(tmp_path),
            paths.merge_command_acks(tmp_path),
            {},
        ),
    ]

    for record, request_path, ack_path, expected_args in cases:
        result_holder: list[ActionResult] = []

        def _run() -> None:
            rows = []
            deadline = time.monotonic() + 1.0
            while not rows and time.monotonic() < deadline:
                rows = read_jsonl_rows(request_path)
                if not rows:
                    time.sleep(0.01)
            assert rows
            _append_ack_later(ack_path, command_id=rows[-1]["command_id"], run_id=record.run_id)

        waiter = threading.Thread(target=_run, daemon=True)
        waiter.start()
        result = execute_action(record, "c", tmp_path)
        result_holder.append(result)
        waiter.join(timeout=1.0)

        rows = read_jsonl_rows(request_path)
        assert rows[-1]["schema_version"] == 1
        assert rows[-1]["run_id"] == record.run_id
        assert rows[-1]["domain"] == record.domain
        assert rows[-1]["kind"] == "cancel"
        assert rows[-1]["requested_by"]["source"] == "tui"
        assert rows[-1]["requested_by"]["pid"] == os.getpid()
        assert rows[-1]["args"] == expected_args
        assert result_holder[-1].clear_banner is True


def test_cancel_falls_back_to_sigterm_after_one_heartbeat(tmp_path: Path, monkeypatch) -> None:
    record = _record(
        tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        status="running",
        adapter_key="atomic.build",
    )
    record.timing["heartbeat_interval_s"] = 0.05
    clock = {"now": 0.0}
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr("otto.mission_control.actions.load_command_ack_ids", lambda path: set())
    monkeypatch.setattr("otto.mission_control.actions.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("otto.mission_control.actions.time.sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr("otto.mission_control.actions.writer_identity_matches_live_process", lambda writer: True)
    monkeypatch.setattr("otto.mission_control.actions.os.killpg", lambda pgid, sig: sent.append((pgid, sig)))

    result = execute_action(record, "c", tmp_path)

    assert sent == [(321, 15)]
    assert result.ok is False
    assert "cancel unacked" in str(result.message)


def test_cancel_waits_at_least_four_seconds_before_fallback(tmp_path: Path, monkeypatch) -> None:
    record = _record(
        tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        status="running",
        adapter_key="atomic.build",
    )
    record.timing["heartbeat_interval_s"] = 0.05
    clock = {"now": 0.0}
    fallback_at: list[float] = []

    monkeypatch.setattr("otto.mission_control.actions.load_command_ack_ids", lambda path: set())
    monkeypatch.setattr("otto.mission_control.actions.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("otto.mission_control.actions.time.sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(
        "otto.mission_control.actions._send_sigterm_fallback",
        lambda record: (fallback_at.append(clock["now"]) or False, None),
    )

    result = execute_action(record, "c", tmp_path)

    assert fallback_at and fallback_at[0] >= 4.0
    assert result.message == "cancel request is still pending with no fallback process group"


def test_cancel_skips_sigterm_for_stale_writer_identity(tmp_path: Path, monkeypatch) -> None:
    record = _record(
        tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        status="running",
        adapter_key="atomic.build",
    )
    record.timing["heartbeat_interval_s"] = 0.05
    clock = {"now": 0.0}
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr("otto.mission_control.actions.load_command_ack_ids", lambda path: set())
    monkeypatch.setattr("otto.mission_control.actions.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("otto.mission_control.actions.time.sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr("otto.mission_control.actions.writer_identity_matches_live_process", lambda writer: False)
    monkeypatch.setattr("otto.mission_control.actions.os.killpg", lambda pgid, sig: sent.append((pgid, sig)))

    result = execute_action(record, "c", tmp_path)

    assert sent == []
    assert result.message == "writer no longer alive — cancel acknowledged via stale state"


def test_cancel_rejects_terminalized_live_record_before_append(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        status="running",
        adapter_key="atomic.build",
    )
    write_record(tmp_path, {**record.to_dict(), "status": "done"})

    result = execute_action(record, "c", tmp_path)

    assert result.modal_title == "Cancel unavailable"
    assert result.message == "run already terminal (done)"
    assert read_jsonl_rows(paths.session_command_requests(tmp_path, "atomic-run")) == []


def test_cancel_rejects_duplicate_pending_cancel_before_append(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        status="running",
        adapter_key="atomic.build",
    )
    append_jsonl_row = {
        "schema_version": 1,
        "command_id": "cmd-existing",
        "run_id": "atomic-run",
        "domain": "atomic",
        "kind": "cancel",
        "requested_at": "2026-04-23T12:00:00Z",
        "requested_by": {"source": "tui", "pid": os.getpid()},
        "args": {},
    }
    paths.session_command_requests(tmp_path, "atomic-run").parent.mkdir(parents=True, exist_ok=True)
    paths.session_command_requests(tmp_path, "atomic-run").write_text(json.dumps(append_jsonl_row) + "\n", encoding="utf-8")

    result = execute_action(record, "c", tmp_path)

    rows = read_jsonl_rows(paths.session_command_requests(tmp_path, "atomic-run"))
    assert len(rows) == 1
    assert result.message == "cancel already pending"


def test_resume_queue_calls_queue_resume_subprocess(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="interrupted",
        adapter_key="queue.attempt",
        queue_task_id="task-1",
    )

    execute_action(record, "r", tmp_path)

    argv = _FakePopen.calls[-1]["argv"]
    assert argv[-3:] == ["queue", "resume", "task-1"]


def test_resume_build_uses_record_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="build-run",
        domain="atomic",
        run_type="build",
        status="interrupted",
        adapter_key="atomic.build",
        argv=["build", "ship it", "--fast"],
    )

    execute_action(record, "r", tmp_path)

    assert _FakePopen.calls[-1]["argv"][-2:] == ["build", "--resume"]
    assert _FakePopen.calls[-1]["cwd"] == str(tmp_path)


def test_retry_uses_stored_source_argv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="build-run",
        domain="atomic",
        run_type="build",
        status="failed",
        adapter_key="atomic.build",
        argv=["build", "ship it", "--fast"],
    )

    execute_action(record, "R", tmp_path)

    assert _FakePopen.calls[-1]["argv"][-3:] == ["build", "ship it", "--fast"]


def test_requeue_reconstructs_queue_cli_from_stored_task_definition(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    append_task(
        tmp_path,
        QueueTask(
            id="task-1",
            command_argv=["build", "ship it", "--fast"],
            after=["base-task"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="ship it",
        ),
    )
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="done",
        adapter_key="queue.attempt",
        queue_task_id="task-1",
    )

    execute_action(record, "R", tmp_path)

    argv = _FakePopen.calls[-1]["argv"]
    assert argv[-9:] == ["queue", "build", "ship it", "--after", "base-task", "--as", "task-1-2", "--", "--fast"]


def test_requeue_dedups_past_existing_retry_ids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    append_task(
        tmp_path,
        QueueTask(
            id="task-1",
            command_argv=["build", "ship it"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="ship it",
        ),
    )
    append_task(
        tmp_path,
        QueueTask(
            id="task-1-2",
            command_argv=["build", "ship it"],
            added_at="2026-04-23T12:01:00Z",
            resolved_intent="ship it",
        ),
    )
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="done",
        adapter_key="queue.attempt",
        queue_task_id="task-1",
    )

    execute_action(record, "R", tmp_path)

    argv = _FakePopen.calls[-1]["argv"]
    assert argv[-5:] == ["queue", "build", "ship it", "--as", "task-1-3"]


def test_remove_queued_task_calls_queue_rm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="queued",
        adapter_key="queue.attempt",
        queue_task_id="task-1",
    )

    execute_action(record, "x", tmp_path)

    assert _FakePopen.calls[-1]["argv"][-3:] == ["queue", "rm", "task-1"]


def test_remove_abandoned_legacy_queue_task_calls_queue_rm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="running",
        adapter_key="queue.attempt",
        queue_task_id="task-1",
    )
    record.identity["compatibility_warning"] = "legacy queue mode"

    execute_action(record, "x", tmp_path)

    assert _FakePopen.calls[-1]["argv"][-3:] == ["queue", "rm", "task-1"]


def test_cleanup_terminal_atomic_run_calls_cleanup_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="atomic-run",
        domain="atomic",
        run_type="build",
        status="failed",
        adapter_key="atomic.build",
    )

    execute_action(record, "x", tmp_path)

    assert _FakePopen.calls[-1]["argv"][-2:] == ["cleanup", "atomic-run"]


def test_merge_selected_and_all_shell_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="done",
        adapter_key="queue.attempt",
        queue_task_id="task-1",
    )

    execute_action(record, "m", tmp_path, selected_queue_task_ids=["task-1", "task-2"])
    execute_merge_all(tmp_path)

    assert _FakePopen.calls[0]["argv"][-5:] == ["merge", "--fast", "--no-certify", "task-1", "task-2"]
    assert _FakePopen.calls[1]["argv"][-5:] == ["merge", "--fast", "--transactional", "--no-certify", "--all"]
    assert _FakePopen.calls[0]["start_new_session"] is True
    assert _FakePopen.calls[1]["start_new_session"] is True


def test_merge_abort_requires_in_progress_merge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mission_control_actions.git_ops, "merge_in_progress", lambda project_dir: False)

    result = execute_merge_abort(tmp_path)

    assert result.ok is False
    assert result.severity == "warning"
    assert "No in-progress" in (result.message or "")


def test_merge_abort_aborts_git_merge(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mission_control_actions.git_ops, "merge_in_progress", lambda project_dir: True)
    monkeypatch.setattr(
        mission_control_actions.git_ops,
        "merge_abort",
        lambda project_dir: calls.append(str(project_dir)) or mission_control_actions.git_ops.GitResult(0, "", ""),
    )

    result = execute_merge_abort(tmp_path)

    assert result.ok is True
    assert result.refresh is True
    assert calls == [str(tmp_path)]


def test_merge_recover_aborts_then_launches_agentic_merge(tmp_path: Path, monkeypatch) -> None:
    abort_calls: list[str] = []
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    monkeypatch.setattr(mission_control_actions.git_ops, "merge_in_progress", lambda project_dir: True)
    monkeypatch.setattr(
        mission_control_actions.git_ops,
        "merge_abort",
        lambda project_dir: abort_calls.append(str(project_dir)) or mission_control_actions.git_ops.GitResult(0, "", ""),
    )
    _FakePopen.calls.clear()

    result = execute_merge_recover(tmp_path)

    assert result.ok is True
    assert abort_calls == [str(tmp_path)]
    assert _FakePopen.calls[-1]["argv"][-3:] == ["merge", "--no-certify", "--all"]
    assert _FakePopen.calls[-1]["cwd"] == str(tmp_path)


def test_queue_cleanup_shells_out_for_superseded_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()

    result = execute_queue_cleanup(tmp_path, ["old-task", ""])

    assert result.ok is True
    assert _FakePopen.calls[-1]["argv"][-3:] == ["queue", "cleanup", "old-task"]


def test_otto_cli_argv_prefers_entrypoint_next_to_python(monkeypatch, tmp_path: Path) -> None:
    fake_python = tmp_path / "bin" / "python"
    fake_otto = tmp_path / "bin" / "otto"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("")
    fake_otto.write_text("")
    monkeypatch.setattr(mission_control_actions.sys, "executable", str(fake_python))

    assert mission_control_actions._otto_cli_argv("merge", "--all") == [str(fake_otto), "merge", "--all"]


def test_otto_cli_argv_prefers_entrypoint_next_to_unresolved_python_symlink(monkeypatch, tmp_path: Path) -> None:
    real_bin = tmp_path / "real-bin"
    venv_bin = tmp_path / "venv" / "bin"
    real_python = real_bin / "python"
    fake_python = venv_bin / "python"
    fake_otto = venv_bin / "otto"
    real_bin.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    real_python.write_text("")
    fake_otto.write_text("")
    fake_python.symlink_to(real_python)
    monkeypatch.setattr(mission_control_actions.sys, "executable", str(fake_python))

    assert mission_control_actions._otto_cli_argv("merge", "--all") == [str(fake_otto), "merge", "--all"]


def test_otto_cli_argv_falls_back_to_python_module(monkeypatch, tmp_path: Path) -> None:
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("")
    monkeypatch.setattr(mission_control_actions.sys, "executable", str(fake_python))

    assert mission_control_actions._otto_cli_argv("merge", "--all") == [
        str(fake_python),
        "-m",
        "otto.cli",
        "merge",
        "--all",
    ]


def test_queue_cancel_without_task_id_fails_fast(tmp_path: Path) -> None:
    record = _record(
        tmp_path,
        run_id="queue-run",
        domain="queue",
        run_type="queue",
        status="running",
        adapter_key="queue.attempt",
        queue_task_id=None,
    )

    result = execute_action(record, "c", tmp_path)

    assert result.modal_title == "Cancel unavailable"
    assert result.message == "queue task id unknown"


def test_legacy_queue_cancel_uses_queue_state_without_live_record(tmp_path: Path, monkeypatch) -> None:
    append_task(
        tmp_path,
        QueueTask(
            id="legacy-task",
            command_argv=["build", "legacy task"],
            added_at="2026-04-23T12:00:00Z",
            resolved_intent="legacy queue task",
            branch="build/legacy-task",
            worktree=".worktrees/legacy-task",
        ),
    )
    write_queue_state(
        tmp_path,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "legacy-task": {
                    "status": "running",
                    "started_at": "2026-04-23T12:00:00Z",
                }
            },
        },
    )
    monkeypatch.setattr(
        "otto.mission_control.actions.load_live_record",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy cancel should not load live record")),
    )
    adapter = adapter_for_key("queue.attempt")
    record = adapter.legacy_records(
        tmp_path,
        datetime(2026, 4, 23, 12, 1, tzinfo=timezone.utc),
        [],
    )[0]

    def _run() -> None:
        rows = []
        deadline = time.monotonic() + 1.0
        request_path = paths.queue_commands_path(tmp_path)
        while not rows and time.monotonic() < deadline:
            rows = read_jsonl_rows(request_path)
            if not rows:
                time.sleep(0.01)
        assert rows
        _append_ack_later(
            paths.queue_command_acks_path(tmp_path),
            command_id=rows[-1]["command_id"],
            run_id=record.run_id,
        )

    waiter = threading.Thread(target=_run, daemon=True)
    waiter.start()
    result = execute_action(record, "c", tmp_path)
    waiter.join(timeout=1.0)

    rows = read_jsonl_rows(paths.queue_commands_path(tmp_path))
    assert rows[-1]["run_id"] == "queue-compat:legacy-task"
    assert rows[-1]["args"] == {"task_id": "legacy-task"}
    assert result.clear_banner is True


def test_open_file_uses_editor_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDITOR", "vim -f")
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakePopen)
    _FakePopen.calls.clear()
    record = _record(
        tmp_path,
        run_id="build-run",
        domain="atomic",
        run_type="build",
        status="failed",
        adapter_key="atomic.build",
    )
    artifact_path = tmp_path / "summary.json"
    artifact_path.write_text("{}")

    execute_action(record, "e", tmp_path, selected_artifact_path=str(artifact_path))

    assert _FakePopen.calls[-1]["argv"] == ["vim", "-f", str(artifact_path)]


def test_open_file_requires_editor_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EDITOR", raising=False)
    record = _record(
        tmp_path,
        run_id="build-run",
        domain="atomic",
        run_type="build",
        status="failed",
        adapter_key="atomic.build",
    )

    result = execute_action(record, "e", tmp_path, selected_artifact_path=str(tmp_path / "summary.json"))

    assert result.ok is False
    assert result.modal_title == "Editor launch failed"
    assert result.modal_message == "EDITOR is not set"


def test_long_running_subprocess_reports_late_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _FakeLongRunningPopen)
    observed: list[ActionResult] = []
    ready = threading.Event()
    record = _record(
        tmp_path,
        run_id="build-run",
        domain="atomic",
        run_type="build",
        status="failed",
        adapter_key="atomic.build",
        argv=["build", "ship it"],
    )

    result = execute_action(
        record,
        "R",
        tmp_path,
        post_result=lambda delayed: (observed.append(delayed), ready.set()),
    )

    assert result.ok is True
    assert result.message == "retry build launched"
    assert ready.wait(timeout=1.0) is True
    assert observed[0].modal_title == "retry build failed"
    assert observed[0].modal_message == "late failure"


def test_disabled_action_reason_surfaces_without_execution(tmp_path: Path, monkeypatch) -> None:
    called = False

    def _unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess should not run")

    monkeypatch.setattr("otto.mission_control.actions.subprocess.Popen", _unexpected)
    record = _record(
        tmp_path,
        run_id="certify-run",
        domain="atomic",
        run_type="certify",
        status="interrupted",
        adapter_key="atomic.certify",
    )

    actions = {action.key: action for action in adapter_for_key("atomic.certify").legal_actions(record, None)}

    assert actions["r"].enabled is False
    assert actions["r"].reason == "standalone certify has no resume path"
    assert called is False
