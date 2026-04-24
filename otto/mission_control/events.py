"""Durable Mission Control operator events."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import paths

EVENT_SCHEMA_VERSION = 1
DEFAULT_EVENT_LIMIT = 80
MAX_EVENT_LIMIT = 500
MAX_EVENT_TAIL_BYTES = 512_000


def events_path(project_dir: Path) -> Path:
    return paths.logs_dir(project_dir) / "mission-control" / "events.jsonl"


def append_event(
    project_dir: Path,
    *,
    kind: str,
    message: str,
    severity: str = "info",
    run_id: str | None = None,
    task_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one operator event and return the persisted row.

    The event log is intentionally local and append-only. It gives the web
    portal a durable timeline across browser refreshes and process restarts
    without coupling UI state to transient toasts.
    """

    path = events_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": f"{time.time_ns()}-{os.getpid()}",
        "created_at": _utc_now(),
        "kind": _clean(kind) or "mission.event",
        "severity": _severity(severity),
        "message": _clean(message) or "Mission Control event",
        "run_id": _clean(run_id),
        "task_id": _clean(task_id),
        "actor": {"source": "web", "pid": os.getpid()},
        "details": details if isinstance(details, dict) else {},
    }
    line = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return row


def events_status(project_dir: Path, *, limit: int = DEFAULT_EVENT_LIMIT) -> dict[str, Any]:
    path = events_path(project_dir)
    limit = _bounded_limit(limit)
    items, total_count, malformed_count, truncated = _read_rows(path, limit=limit)
    return {
        "path": str(path.resolve(strict=False)),
        "items": items,
        "total_count": total_count,
        "malformed_count": malformed_count,
        "limit": limit,
        "truncated": truncated,
    }


def _read_rows(path: Path, *, limit: int) -> tuple[list[dict[str, Any]], int, int, bool]:
    if not path.exists():
        return [], 0, 0, False
    rows: list[dict[str, Any]] = []
    total_count = 0
    malformed_count = 0
    try:
        lines, truncated = _read_tail_lines(path)
    except OSError:
        return [], 0, 1, False
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed_count += 1
            continue
        if not isinstance(value, dict):
            malformed_count += 1
            continue
        try:
            rows.append(_normalize_row(value))
        except (TypeError, ValueError):
            malformed_count += 1
            continue
        total_count += 1
    return rows[-limit:][::-1], total_count, malformed_count, truncated


def _read_tail_lines(path: Path) -> tuple[list[str], bool]:
    size = path.stat().st_size
    if size <= MAX_EVENT_TAIL_BYTES:
        return path.read_text(encoding="utf-8").splitlines(), False
    start = max(0, size - MAX_EVENT_TAIL_BYTES)
    with path.open("rb") as handle:
        drop_partial = False
        if start > 0:
            handle.seek(start - 1)
            drop_partial = handle.read(1) != b"\n"
        handle.seek(start)
        data = handle.read(MAX_EVENT_TAIL_BYTES)
    lines = data.decode("utf-8", errors="replace").splitlines()
    if drop_partial and lines:
        lines = lines[1:]
    return lines, True


def _normalize_row(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _int_or_default(value.get("schema_version"), EVENT_SCHEMA_VERSION),
        "event_id": str(value.get("event_id") or ""),
        "created_at": str(value.get("created_at") or ""),
        "kind": str(value.get("kind") or "mission.event"),
        "severity": _severity(str(value.get("severity") or "info")),
        "message": str(value.get("message") or "Mission Control event"),
        "run_id": _clean(value.get("run_id")),
        "task_id": _clean(value.get("task_id")),
        "actor": value.get("actor") if isinstance(value.get("actor"), dict) else {},
        "details": value.get("details") if isinstance(value.get("details"), dict) else {},
    }


def _bounded_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_EVENT_LIMIT
    return max(1, min(MAX_EVENT_LIMIT, limit))


def _severity(value: str) -> str:
    text = str(value or "").strip().lower()
    if text == "information":
        return "info"
    if text in {"error", "warning", "info", "success"}:
        return text
    return "info"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
