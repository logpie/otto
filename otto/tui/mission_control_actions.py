"""Shared Mission Control action primitives and execution helpers."""

from __future__ import annotations

import itertools
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from otto import paths
from otto.queue.schema import QueueTask, load_queue
from otto.runs.registry import (
    append_jsonl_row,
    load_command_ack_ids,
    load_live_record,
    read_jsonl_rows,
    utc_now_iso,
    writer_identity_matches_live_process,
)
from otto.runs.schema import RunRecord, is_terminal_status

_COMMAND_COUNTER = itertools.count(1)


@dataclass(slots=True)
class ActionState:
    key: str
    label: str
    enabled: bool
    reason: str | None
    preview: str


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str | None = None
    severity: str = "information"
    modal_title: str | None = None
    modal_message: str | None = None
    refresh: bool = False
    clear_banner: bool = False


def make_action(
    key: str,
    label: str,
    *,
    enabled: bool,
    reason: str | None,
    preview: str,
) -> ActionState:
    return ActionState(key=key, label=label, enabled=enabled, reason=reason, preview=preview)


def execute_action(
    record: RunRecord,
    action_kind: str,
    project_dir: Path,
    *,
    selected_artifact_path: str | None = None,
    selected_queue_task_ids: list[str] | None = None,
    post_result: Callable[[ActionResult], None] | None = None,
) -> ActionResult:
    project_dir = Path(project_dir)
    if action_kind == "c":
        return _execute_cancel(record, project_dir)
    if action_kind == "r":
        return _execute_resume(record, project_dir, post_result=post_result)
    if action_kind == "R":
        return _execute_retry(record, project_dir, post_result=post_result)
    if action_kind == "x":
        return _execute_remove_or_cleanup(record, project_dir, post_result=post_result)
    if action_kind == "m":
        return _execute_merge_selected(
            record,
            project_dir,
            selected_queue_task_ids=selected_queue_task_ids,
            post_result=post_result,
        )
    if action_kind == "e":
        return _execute_open_editor(
            record,
            project_dir,
            selected_artifact_path=selected_artifact_path,
            post_result=post_result,
        )
    return ActionResult(
        ok=False,
        message="action unavailable",
        severity="warning",
    )


def execute_merge_all(project_dir: Path) -> ActionResult:
    return _launch_process(
        _otto_cli_argv("merge", "--all"),
        cwd=Path(project_dir),
        description="merge all",
    )


def _execute_cancel(record: RunRecord, project_dir: Path) -> ActionResult:
    try:
        request_path, processing_path, ack_path, args = _cancel_paths_and_args(record, project_dir)
    except ValueError as exc:
        return _warning_result("Cancel unavailable", str(exc))
    unavailable = _cancel_unavailable_result(
        record,
        project_dir,
        request_path=request_path,
        processing_path=processing_path,
        ack_path=ack_path,
    )
    if unavailable is not None:
        return unavailable
    command_id = _next_command_id()
    envelope = {
        "schema_version": 1,
        "command_id": command_id,
        "run_id": record.run_id,
        "domain": record.domain,
        "kind": "cancel",
        "requested_at": utc_now_iso(),
        "requested_by": {
            "source": "tui",
            "pid": os.getpid(),
        },
        "args": args,
    }
    append_jsonl_row(request_path, envelope)
    heartbeat_s = max(float(record.timing.get("heartbeat_interval_s") or 2.0), 0.1)
    deadline = time.monotonic() + heartbeat_s
    while time.monotonic() < deadline:
        if command_id in load_command_ack_ids(ack_path):
            return ActionResult(ok=True, refresh=True, clear_banner=True)
        time.sleep(0.05)
    sent_sigterm, fallback_message = _send_sigterm_fallback(record)
    if sent_sigterm:
        return ActionResult(
            ok=False,
            message=f"cancel unacked; sent SIGTERM to pgid {record.writer.get('pgid')}",
            severity="warning",
            refresh=True,
        )
    if fallback_message:
        return ActionResult(
            ok=False,
            message=fallback_message,
            severity="warning",
            refresh=True,
        )
    return ActionResult(
        ok=False,
        message="cancel request is still pending with no fallback process group",
        severity="warning",
        refresh=True,
    )


def _execute_resume(
    record: RunRecord,
    project_dir: Path,
    *,
    post_result: Callable[[ActionResult], None] | None,
) -> ActionResult:
    if record.domain == "queue":
        task_id = _queue_task_id(record)
        if not task_id:
            return _error_result("Resume failed", "queue task id missing")
        return _launch_process(
            _otto_cli_argv("queue", "resume", task_id),
            cwd=project_dir,
            description=f"resume {task_id}",
            post_result=post_result,
        )
    if record.domain == "merge":
        return _error_result("Resume unavailable", "merge --resume is deferred")
    if record.run_type == "certify":
        return _error_result("Resume unavailable", "standalone certify has no resume path")
    cwd = _record_cwd(record)
    if cwd is None:
        return _error_result("Resume failed", "cwd missing")
    if record.run_type == "improve":
        argv = record.source.get("argv")
        subcommand = _improve_subcommand(argv)
        if subcommand is None:
            return _error_result("Resume failed", "improve subcommand unavailable")
        return _launch_process(
            _otto_cli_argv("improve", subcommand, "--resume"),
            cwd=cwd,
            description=f"resume improve {subcommand}",
            post_result=post_result,
        )
    return _launch_process(
        _otto_cli_argv(record.run_type, "--resume"),
        cwd=cwd,
        description=f"resume {record.run_type}",
        post_result=post_result,
    )


def _execute_retry(
    record: RunRecord,
    project_dir: Path,
    *,
    post_result: Callable[[ActionResult], None] | None,
) -> ActionResult:
    if record.domain == "queue":
        queue_task_id = _queue_task_id(record)
        if not queue_task_id:
            return _error_result("Requeue failed", "queue task id missing")
        try:
            task = _load_queue_task(project_dir, queue_task_id)
        except ValueError as exc:
            return _error_result("Requeue failed", str(exc))
        argv = _reconstruct_queue_command(task, existing_task_ids={t.id for t in load_queue(project_dir)})
        if argv is None:
            return _warning_result(
                "Requeue failed",
                f"task {queue_task_id} already exists — pick a new id or remove the existing first",
            )
        return _launch_process(
            argv,
            cwd=project_dir,
            description=f"requeue {queue_task_id}",
            post_result=post_result,
        )

    argv = record.source.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        return _error_result("Retry failed", "original argv unavailable")
    cwd = _record_cwd(record)
    if cwd is None:
        return _error_result("Retry failed", "cwd missing")
    return _launch_process(
        _otto_cli_argv(*argv),
        cwd=cwd,
        description=f"retry {record.run_type}",
        post_result=post_result,
    )


def _execute_remove_or_cleanup(
    record: RunRecord,
    project_dir: Path,
    *,
    post_result: Callable[[ActionResult], None] | None,
) -> ActionResult:
    if record.domain == "queue":
        task_id = _queue_task_id(record)
        if not task_id:
            return _error_result("Queue action failed", "queue task id missing")
        if record.status == "queued":
            return _launch_process(
                _otto_cli_argv("queue", "rm", task_id),
                cwd=project_dir,
                description=f"remove {task_id}",
                post_result=post_result,
            )
        return _launch_process(
            _otto_cli_argv("queue", "cleanup", task_id),
            cwd=project_dir,
            description=f"cleanup {task_id}",
            post_result=post_result,
        )

    return _launch_process(
        _otto_cli_argv("cleanup", record.run_id),
        cwd=project_dir,
        description=f"cleanup {record.run_id}",
        post_result=post_result,
    )


def _execute_merge_selected(
    record: RunRecord,
    project_dir: Path,
    *,
    selected_queue_task_ids: list[str] | None,
    post_result: Callable[[ActionResult], None] | None,
) -> ActionResult:
    task_ids = [task_id for task_id in (selected_queue_task_ids or []) if task_id]
    if not task_ids:
        task_id = _queue_task_id(record)
        if task_id:
            task_ids = [task_id]
    if not task_ids:
        return _error_result("Merge failed", "queue task id missing")
    return _launch_process(
        _otto_cli_argv("merge", *task_ids),
        cwd=project_dir,
        description=f"merge {' '.join(task_ids)}",
        post_result=post_result,
    )


def _execute_open_editor(
    record: RunRecord,
    project_dir: Path,
    *,
    selected_artifact_path: str | None,
    post_result: Callable[[ActionResult], None] | None,
) -> ActionResult:
    if not selected_artifact_path:
        return _error_result("Editor launch failed", "no selectable artifact")
    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        return _error_result("Editor launch failed", "$EDITOR is not set")
    editor_argv = shlex.split(editor)
    if not editor_argv:
        return _error_result("Editor launch failed", "$EDITOR is empty")
    del record
    return _launch_process(
        [*editor_argv, selected_artifact_path],
        cwd=project_dir,
        description=f"open {selected_artifact_path}",
        post_result=post_result,
    )


def _cancel_paths_and_args(record: RunRecord, project_dir: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    if record.domain == "queue":
        task_id = _queue_task_id(record)
        if not task_id:
            raise ValueError("queue task id unknown")
        return (
            paths.queue_commands_path(project_dir),
            paths.queue_commands_processing_path(project_dir),
            paths.queue_command_acks_path(project_dir),
            {"task_id": task_id},
        )
    if record.domain == "merge":
        return (
            paths.merge_command_requests(project_dir),
            paths.merge_command_requests_processing(project_dir),
            paths.merge_command_acks(project_dir),
            {},
        )
    return (
        paths.session_command_requests(project_dir, record.run_id),
        paths.session_command_requests_processing(project_dir, record.run_id),
        paths.session_command_acks(project_dir, record.run_id),
        {},
    )


def _load_queue_task(project_dir: Path, task_id: str) -> QueueTask:
    for task in load_queue(project_dir):
        if task.id == task_id:
            return task
    raise ValueError(f"queue task definition missing for {task_id}")


def _reconstruct_queue_command(task: QueueTask, *, existing_task_ids: set[str]) -> list[str] | None:
    command_argv = list(task.command_argv)
    if not command_argv:
        raise ValueError("queue task argv missing")
    command = command_argv[0]
    args: list[str] = ["queue", command]
    passthrough: list[str]
    if command == "build":
        if len(command_argv) < 2:
            raise ValueError("queue build intent missing")
        args.append(command_argv[1])
        passthrough = command_argv[2:]
    elif command == "improve":
        if len(command_argv) < 2:
            raise ValueError("queue improve subcommand missing")
        args.append(command_argv[1])
        index = 2
        if len(command_argv) > index and not command_argv[index].startswith("-"):
            args.append(command_argv[index])
            index += 1
        passthrough = command_argv[index:]
    elif command == "certify":
        index = 1
        if len(command_argv) > index and not command_argv[index].startswith("-"):
            args.append(command_argv[index])
            index += 1
        passthrough = command_argv[index:]
    else:
        raise ValueError(f"unsupported queue command {command!r}")

    for after_id in task.after:
        args.extend(["--after", after_id])
    if task.id in existing_task_ids:
        return None
    args.extend(["--as", task.id])
    if passthrough:
        args.append("--")
        args.extend(passthrough)
    return _otto_cli_argv(*args)


def _launch_process(
    argv: list[str],
    *,
    cwd: Path,
    description: str,
    post_result: Callable[[ActionResult], None] | None = None,
) -> ActionResult:
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return _error_result(f"{description} failed", str(exc))

    for _ in range(5):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    if proc.poll() is None:
        _watch_process_completion(proc, description=description, post_result=post_result)
        return ActionResult(ok=True, message=f"{description} launched", refresh=True)

    stdout, stderr = proc.communicate()
    if proc.returncode == 0:
        return ActionResult(ok=True, message=f"{description} finished", refresh=True)
    detail = (stderr or stdout or f"process exited with {proc.returncode}").strip()
    return _error_result(f"{description} failed", detail)


def _watch_process_completion(
    proc: subprocess.Popen[str],
    *,
    description: str,
    post_result: Callable[[ActionResult], None] | None,
) -> None:
    def _drain() -> None:
        try:
            stdout, stderr = proc.communicate()
        except Exception:
            return
        if proc.returncode == 0 or post_result is None:
            return
        detail = (stderr or stdout or f"process exited with {proc.returncode}").strip()
        post_result(_error_result(f"{description} failed", detail))

    thread = threading.Thread(target=_drain, name=f"mission-control-drain-{proc.pid}", daemon=True)
    thread.start()


def _send_sigterm_fallback(record: RunRecord) -> tuple[bool, str | None]:
    pgid = record.writer.get("pgid")
    if not isinstance(pgid, int) or pgid <= 0:
        return False, None
    if not writer_identity_matches_live_process(record.writer):
        return False, "writer no longer alive — cancel acknowledged via stale state"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError, PermissionError):
        return False, None
    return True, None


def _cancel_unavailable_result(
    record: RunRecord,
    project_dir: Path,
    *,
    request_path: Path,
    processing_path: Path,
    ack_path: Path,
) -> ActionResult | None:
    try:
        live_record = load_live_record(project_dir, record.run_id)
    except FileNotFoundError:
        return _warning_result("Cancel unavailable", "live run record no longer exists")
    if is_terminal_status(live_record.status):
        return _warning_result("Cancel unavailable", f"run already terminal ({live_record.status})")
    acked = load_command_ack_ids(ack_path)
    for path in (request_path, processing_path):
        for row in read_jsonl_rows(path):
            if str(row.get("run_id") or "") != record.run_id:
                continue
            if str(row.get("kind") or "") != "cancel":
                continue
            command_id = str(row.get("command_id") or "")
            if command_id and command_id not in acked:
                return _warning_result("Cancel unavailable", "cancel already pending")
    return None


def _otto_cli_argv(*args: str) -> list[str]:
    candidate = Path(sys.executable).resolve().parent / "otto"
    if candidate.exists():
        return [str(candidate), *args]
    return [sys.executable, "-m", "otto.cli", *args]


def _record_cwd(record: RunRecord) -> Path | None:
    raw = str(record.cwd or "").strip()
    if not raw:
        return None
    return Path(raw)


def _queue_task_id(record: RunRecord) -> str:
    return str(record.identity.get("queue_task_id") or "").strip()


def _improve_subcommand(argv: Any) -> str | None:
    if not isinstance(argv, list) or len(argv) < 2:
        return None
    if argv[0] != "improve":
        return None
    subcommand = argv[1]
    if not isinstance(subcommand, str) or not subcommand:
        return None
    return subcommand


def _next_command_id() -> str:
    return f"cmd-{utc_now_iso()}-{os.getpid()}-{next(_COMMAND_COUNTER)}"


def _error_result(title: str, message: str) -> ActionResult:
    return ActionResult(
        ok=False,
        severity="error",
        modal_title=title,
        modal_message=message,
        message=message,
    )


def _warning_result(title: str, message: str) -> ActionResult:
    return ActionResult(
        ok=False,
        severity="warning",
        modal_title=title,
        modal_message=message,
        message=message,
        refresh=True,
    )
