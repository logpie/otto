"""Durable history append primitive for terminal run snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from otto import paths
from otto.runs.registry import utc_now_iso

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows best effort
    fcntl = None


def append_history_snapshot(
    project_dir: Path,
    row: dict[str, Any],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Append one v2 terminal snapshot with flock + fsync."""
    payload = dict(row)
    run_id = str(payload.get("run_id") or "").strip()
    if strict and not run_id:
        raise ValueError("history snapshot requires run_id")
    payload["schema_version"] = 2
    payload["history_kind"] = str(payload.get("history_kind") or "terminal_snapshot")
    payload["timestamp"] = str(payload.get("timestamp") or payload.get("finished_at") or utc_now_iso())
    payload["dedupe_key"] = str(
        payload.get("dedupe_key")
        or (
            f"terminal_snapshot:{run_id}"
            if payload["history_kind"] == "terminal_snapshot"
            else f"{payload['history_kind']}:{run_id}:{payload.get('event_seq', 0)}"
        )
    )
    history_path = paths.history_jsonl(project_dir)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), sort_keys=False) + "\n"
    try:
        with history_path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        if strict:
            raise
    return payload


def read_history_rows(path: Path) -> list[dict[str, Any]]:
    """Read tolerant JSONL rows, skipping malformed lines."""
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows
