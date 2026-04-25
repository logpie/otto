"""Runtime ownership and recovery diagnostics for Mission Control."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import paths
from otto.mission_control.supervisor import read_supervisor
from otto.mission_control.supervisor import supervisor_path
from otto.queue.runtime import watcher_alive
from otto.queue.schema import (
    commands_path,
    commands_processing_path,
    load_queue,
    load_state as load_queue_state,
    lock_path as queue_lock_path,
    queue_path,
    state_path as queue_state_path,
)


def runtime_status(
    project_dir: Path,
    *,
    watcher: dict[str, Any],
    landing: dict[str, Any],
) -> dict[str, Any]:
    queue_file = _file_status(queue_path(project_dir))
    state_file = _file_status(queue_state_path(project_dir))
    commands_file = _jsonl_file_status(commands_path(project_dir))
    processing_file = _jsonl_file_status(commands_processing_path(project_dir))
    issues: list[dict[str, Any]] = []

    queue_tasks: int | None = None
    try:
        queue_tasks = len(load_queue(project_dir))
    except Exception as exc:
        queue_file["error"] = str(exc)
        issues.append(
            _runtime_issue(
                "error",
                "Queue file unreadable",
                str(exc),
                "Fix `.otto-queue.yml` before starting the watcher or merging queued work.",
            )
        )

    state_tasks: int | None = None
    try:
        state = load_queue_state(project_dir)
        tasks = state.get("tasks") if isinstance(state, dict) else None
        state_tasks = len(tasks) if isinstance(tasks, dict) else 0
    except Exception as exc:
        state_file["error"] = str(exc)
        issues.append(
            _runtime_issue(
                "error",
                "Queue state unreadable",
                str(exc),
                "Inspect `.otto-queue-state.json`; restore valid JSON or move the broken file aside.",
            )
        )

    health = watcher.get("health") if isinstance(watcher.get("health"), dict) else {}
    watcher_state = str(health.get("state") or "stopped")
    supervisor = _supervisor_status(project_dir, health, watcher_state)
    counts = watcher.get("counts") if isinstance(watcher.get("counts"), dict) else {}
    queued_count = int(counts.get("queued") or 0)
    attention_count = sum(int(counts.get(status) or 0) for status in ("failed", "interrupted", "stale"))
    pending_commands = int(commands_file.get("line_count") or 0)
    processing_commands = int(processing_file.get("line_count") or 0)
    malformed_commands = int(commands_file.get("malformed_count") or 0) + int(processing_file.get("malformed_count") or 0)
    generated_at = datetime.now(tz=timezone.utc)
    command_items = [
        *_command_backlog_items(commands_processing_path(project_dir), state="processing", now=generated_at),
        *_command_backlog_items(commands_path(project_dir), state="pending", now=generated_at),
    ][:8]

    if watcher_state == "stale":
        issues.append(
            _runtime_issue(
                "warning",
                "Watcher runtime is stale",
                str(health.get("next_action") or "A watcher or queue lock is blocking new dispatch."),
                "Use Stop watcher, then Start watcher when ready.",
            )
        )
    if queued_count and watcher_state != "running":
        issues.append(
            _runtime_issue(
                "warning",
                "Queued work is paused",
                f"{queued_count} queued task{'' if queued_count == 1 else 's'} will not start while the watcher is {watcher_state}.",
                "Start the watcher when queued work should run.",
            )
        )
    if processing_commands and watcher_state != "running":
        issues.append(
            _runtime_issue(
                "warning",
                "Command drain is unfinished",
                f"{processing_commands} command{'' if processing_commands == 1 else 's'} remain in `.processing`.",
                "Start the watcher so it can finish acknowledging queued actions.",
            )
        )
    elif pending_commands and watcher_state != "running":
        issues.append(
            _runtime_issue(
                "info",
                "Commands are waiting",
                f"{pending_commands} command{'' if pending_commands == 1 else 's'} are waiting for the watcher.",
                "Start the watcher to apply queued actions.",
            )
        )
    if malformed_commands:
        issues.append(
            _runtime_issue(
                "error",
                "Command log has malformed rows",
                f"{malformed_commands} JSONL row{'' if malformed_commands == 1 else 's'} could not be parsed.",
                "Inspect `.otto-queue-commands*.jsonl` before trusting pending actions.",
            )
        )
    if attention_count:
        issues.append(
            _runtime_issue(
                "warning",
                "Tasks need attention",
                f"{attention_count} task{'' if attention_count == 1 else 's'} failed, stalled, or were interrupted.",
                "Open the affected run and use the review packet next action.",
            )
        )

    if bool(landing.get("merge_blocked")):
        blockers = landing.get("merge_blockers") if isinstance(landing.get("merge_blockers"), list) else []
        detail = "; ".join(str(item) for item in blockers[:3]) or "Local repository state blocks merge."
        issues.append(
            _runtime_issue(
                "warning",
                "Merge is blocked",
                detail,
                "Commit, stash, or revert local project changes before merging ready work.",
            )
        )

    severity_rank = {"error": 3, "warning": 2, "info": 1}
    issues.sort(key=lambda item: severity_rank.get(str(item.get("severity")), 0), reverse=True)
    return {
        "status": "attention" if issues else "healthy",
        "generated_at": _utc_iso(generated_at),
        "queue_tasks": queue_tasks,
        "state_tasks": state_tasks,
        "command_backlog": {
            "pending": pending_commands,
            "processing": processing_commands,
            "malformed": malformed_commands,
            "items": command_items,
        },
        "files": {
            "queue": queue_file,
            "state": state_file,
            "commands": commands_file,
            "processing": processing_file,
        },
        "supervisor": supervisor,
        "issues": issues,
    }


def watcher_health(project_dir: Path, state: dict[str, Any], *, probe_lock: bool = True) -> dict[str, Any]:
    watcher = state.get("watcher") if isinstance(state, dict) else None
    watcher = watcher if isinstance(watcher, dict) else {}
    watcher_pid = _int_or_none(watcher.get("pid"))
    watcher_process_alive = _pid_alive(watcher_pid)
    lock_pid = _queue_lock_holder_pid(project_dir) if probe_lock else None
    lock_process_alive = _pid_alive(lock_pid)
    heartbeat_age_s = _heartbeat_age_s(watcher.get("heartbeat"))
    running = watcher_alive(state)
    blocking_pid = watcher_pid if running else lock_pid if lock_process_alive else None
    if running:
        health_state = "running"
        next_action = "Stop watcher to pause queue dispatch."
    elif blocking_pid:
        health_state = "stale"
        next_action = "Stop the stale watcher before starting another one."
    else:
        health_state = "stopped"
        next_action = "Start watcher when queued tasks should run."
    return {
        "state": health_state,
        "blocking_pid": blocking_pid,
        "watcher_pid": watcher_pid,
        "watcher_process_alive": watcher_process_alive,
        "lock_pid": lock_pid,
        "lock_process_alive": lock_process_alive,
        "heartbeat": watcher.get("heartbeat") if isinstance(watcher.get("heartbeat"), str) else None,
        "heartbeat_age_s": heartbeat_age_s,
        "started_at": watcher.get("started_at") if isinstance(watcher.get("started_at"), str) else None,
        "log_path": str((paths.logs_dir(project_dir) / "web" / "watcher.log").resolve(strict=False)),
        "next_action": next_action,
    }


def _runtime_issue(severity: str, label: str, detail: str, next_action: str) -> dict[str, str]:
    return {
        "severity": severity,
        "label": label,
        "detail": detail,
        "next_action": next_action,
    }


def _supervisor_status(project_dir: Path, health: dict[str, Any], watcher_state: str) -> dict[str, Any]:
    log_path = Path(str(health.get("log_path") or paths.logs_dir(project_dir) / "web" / "watcher.log"))
    blocking_pid = _int_or_none(health.get("blocking_pid"))
    metadata, metadata_error = read_supervisor(project_dir)
    supervised_pid = _int_or_none(metadata.get("watcher_pid") if metadata else None)
    return {
        "mode": "local-single-user",
        "path": str(supervisor_path(project_dir).resolve(strict=False)),
        "metadata": metadata,
        "metadata_error": metadata_error,
        "supervised_pid": supervised_pid,
        "matches_blocking_pid": bool(blocking_pid and supervised_pid == blocking_pid),
        "can_start": watcher_state == "stopped",
        "can_stop": bool(blocking_pid and (health.get("lock_pid") == blocking_pid or supervised_pid == blocking_pid)),
        "start_blocked_reason": None if watcher_state == "stopped" else str(health.get("next_action") or ""),
        "stop_target_pid": blocking_pid,
        "watcher_log_path": str(log_path.resolve(strict=False)),
        "web_log_exists": log_path.exists(),
        "queue_lock_holder_pid": _int_or_none(health.get("lock_pid")),
    }


def _file_status(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": str(path.resolve(strict=False)),
        "exists": path.exists(),
        "size_bytes": None,
        "mtime": None,
        "error": None,
    }
    try:
        stat = path.stat()
    except OSError:
        return out
    out["size_bytes"] = stat.st_size
    out["mtime"] = _utc_iso(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
    return out


def _jsonl_file_status(path: Path) -> dict[str, Any]:
    out = _file_status(path)
    out["line_count"] = 0
    out["malformed_count"] = 0
    if not out["exists"]:
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        out["error"] = str(exc)
        return out
    for line in lines:
        if not line.strip():
            continue
        out["line_count"] += 1
        try:
            json.loads(line)
        except json.JSONDecodeError:
            out["malformed_count"] += 1
    return out


def _command_backlog_items(path: Path, *, state: str, now: datetime) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        requested_at = _string_or_none(row.get("requested_at") or row.get("created_at") or row.get("queued_at"))
        parsed_requested_at = _parse_utc(requested_at)
        args = row.get("args")
        args = args if isinstance(args, dict) else {}
        items.append(
            {
                "state": state,
                "command_id": _string_or_none(row.get("command_id")),
                "kind": _string_or_none(row.get("kind") or row.get("action") or row.get("command")),
                "run_id": _string_or_none(row.get("run_id")),
                "task_id": _string_or_none(row.get("task_id") or row.get("queue_task_id") or args.get("task_id") or args.get("id")),
                "requested_at": requested_at,
                "age_s": max(0.0, (now - parsed_requested_at).total_seconds()) if parsed_requested_at else None,
            }
        )
    return items


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _queue_lock_holder_pid(project_dir: Path) -> int | None:
    """Return the queue-lock PID only when the flock is actually held."""
    path = queue_lock_path(project_dir)
    try:
        with path.open("r", encoding="utf-8") as handle:
            text = handle.read().strip()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return _int_or_none(text)
                return None
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return None
    except OSError:
        return None


def _heartbeat_age_s(value: Any) -> float | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(tz=timezone.utc) - parsed).total_seconds())


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None
