"""Durable history append/read helpers for terminal run snapshots."""

from __future__ import annotations

import json
import os
from datetime import datetime
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


def load_project_history_rows(project_dir: Path) -> list[dict[str, Any]]:
    """Merge v2, legacy, and archived history rows into one deduped timeline."""
    sources = [
        paths.history_jsonl(project_dir),
        paths.logs_dir(project_dir) / paths.LEGACY_RUN_HISTORY,
        *(archive / paths.LEGACY_RUN_HISTORY for archive in paths.archived_pre_restructure_dirs(project_dir)),
    ]
    entries: list[tuple[tuple[float, int, int], int, int, dict[str, Any]]] = []
    for source_index, source in enumerate(sources):
        if not source.exists():
            continue
        try:
            fallback_ts = source.stat().st_mtime
        except OSError:
            fallback_ts = 0.0
        for line_index, entry in enumerate(read_history_rows(source)):
            entries.append((
                _history_sort_key(
                    entry,
                    fallback_ts=fallback_ts,
                    source_index=source_index,
                    line_index=line_index,
                ),
                source_index,
                line_index,
                entry,
            ))
    selected = _dedupe_history_entries(entries)
    selected.sort(key=lambda item: item[0])
    return [entry for _, _, _, entry in selected]


def _dedupe_history_entries(
    entries: list[tuple[tuple[float, int, int], int, int, dict[str, Any]]],
) -> list[tuple[tuple[float, int, int], int, int, dict[str, Any]]]:
    selected: list[tuple[tuple[float, int, int], int, int, dict[str, Any]]] = []
    selected_keys: set[tuple[str, str]] = set()
    selected_no_command_run_ids: set[str] = set()
    selected_command_run_ids: set[str] = set()

    def preference(
        item: tuple[tuple[float, int, int], int, int, dict[str, Any]],
    ) -> tuple[int, int, float, int]:
        sort_key, source_index, line_index, entry = item
        is_snapshot = (
            entry.get("schema_version") == 2
            and entry.get("history_kind") == "terminal_snapshot"
        )
        return (1 if is_snapshot else 0, -source_index, sort_key[0], line_index)

    for item in sorted(entries, key=preference, reverse=True):
        _, _, _, entry = item
        run_id = _history_run_id(entry)
        raw_command = str(entry.get("command") or "").strip()
        command = _normalize_command_label(raw_command) if raw_command else ""
        dedupe_key = str(entry.get("dedupe_key") or "").strip()
        key = ("dedupe", dedupe_key) if dedupe_key else ("run-command", f"{run_id}:{command}")
        if key in selected_keys:
            continue
        if run_id and not command and run_id in selected_command_run_ids:
            continue
        if run_id and command and run_id in selected_no_command_run_ids:
            continue
        selected.append(item)
        selected_keys.add(key)
        if run_id and command:
            selected_command_run_ids.add(run_id)
        elif run_id:
            selected_no_command_run_ids.add(run_id)
    return selected


def _history_sort_key(
    entry: dict[str, Any],
    *,
    fallback_ts: float,
    source_index: int,
    line_index: int,
) -> tuple[float, int, int]:
    ts = entry.get("timestamp") or entry.get("started_at") or entry.get("updated_at")
    if isinstance(ts, str) and ts:
        try:
            return (
                datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(),
                source_index,
                line_index,
            )
        except ValueError:
            pass
    return (fallback_ts, source_index, line_index)


def _history_run_id(entry: dict[str, Any]) -> str:
    return str(
        entry.get("run_id")
        or entry.get("session_id")
        or entry.get("build_id")
        or ""
    ).strip()


def _normalize_command_label(command: str | None) -> str:
    raw = str(command or "").strip()
    if not raw:
        return "build"
    if raw.startswith("improve."):
        return f"improve {raw.split('.', 1)[1]}".strip()
    return raw.replace(".", " ")
