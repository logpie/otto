"""Local Mission Control watcher supervisor metadata."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import paths

SCHEMA_VERSION = 1


def supervisor_path(project_dir: Path) -> Path:
    return paths.logs_dir(project_dir) / "web" / "watcher-supervisor.json"


def record_watcher_launch(
    project_dir: Path,
    *,
    watcher_pid: int,
    argv: list[str],
    log_path: Path,
    concurrent: int,
    exit_when_empty: bool,
) -> dict[str, Any]:
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "launched_at": _utc_now(),
        "launcher_pid": os.getpid(),
        "watcher_pid": watcher_pid,
        "argv": argv,
        "log_path": str(log_path.resolve(strict=False)),
        "project_dir": str(Path(project_dir).resolve(strict=False)),
        "concurrent": concurrent,
        "exit_when_empty": bool(exit_when_empty),
        "stop_requested_at": None,
        "stop_requested_by_pid": None,
        "stop_target_pid": None,
        "stop_reason": None,
    }
    _write_metadata(project_dir, metadata)
    return metadata


def record_watcher_stop(project_dir: Path, *, target_pid: int, reason: str) -> dict[str, Any]:
    metadata, _error = read_supervisor(project_dir)
    if metadata is None:
        metadata = {"schema_version": SCHEMA_VERSION, "launched_at": None}
    metadata.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "stop_requested_at": _utc_now(),
            "stop_requested_by_pid": os.getpid(),
            "stop_target_pid": target_pid,
            "stop_reason": reason,
        }
    )
    _write_metadata(project_dir, metadata)
    return metadata


def read_supervisor(project_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = supervisor_path(project_dir)
    if not path.exists():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "supervisor metadata is not a JSON object"
    return value, None


def _write_metadata(project_dir: Path, metadata: dict[str, Any]) -> None:
    path = supervisor_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
