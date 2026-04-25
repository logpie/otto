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


def build_terminal_snapshot(
    *,
    run_id: str,
    domain: str,
    run_type: str,
    command: str,
    intent_meta: dict[str, Any],
    status: str,
    terminal_outcome: str | None,
    timing: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from otto.history import normalize_command_label

    resolved_run_id = str(run_id).strip()
    if not resolved_run_id:
        raise ValueError("terminal snapshot requires run_id")
    timing = dict(timing or {})
    metrics = dict(metrics or {})
    git = dict(git or {})
    source = dict(source or {})
    identity = dict(identity or {})
    normalized_artifacts = _normalize_artifacts(artifacts)
    finished_at = _string_or_none(timing.get("finished_at"))
    snapshot = {
        "run_id": resolved_run_id,
        "build_id": resolved_run_id,
        "domain": str(domain or "").strip(),
        "run_type": str(run_type or "").strip(),
        "command": normalize_command_label(command),
        "intent": _string_or_none(intent_meta.get("summary")) or "",
        "intent_path": _string_or_none(intent_meta.get("intent_path")),
        "spec_path": _string_or_none(intent_meta.get("spec_path")),
        "passed": str(status or "").strip() == "done",
        "status": str(status or "").strip(),
        "terminal_outcome": _string_or_none(terminal_outcome),
        "started_at": _string_or_none(timing.get("started_at")),
        "finished_at": finished_at,
        "timestamp": _string_or_none(timing.get("timestamp")) or finished_at or utc_now_iso(),
        "branch": _string_or_none(git.get("branch")),
        "target_branch": _string_or_none(git.get("target_branch")),
        "head_sha": _string_or_none(git.get("head_sha")),
        "worktree": _string_or_none(git.get("worktree")),
        "resumable": bool(source.get("resumable", False)),
        "session_dir": normalized_artifacts["session_dir"],
        "manifest_path": normalized_artifacts["manifest_path"],
        "summary_path": normalized_artifacts["summary_path"],
        "checkpoint_path": normalized_artifacts["checkpoint_path"],
        "primary_log_path": normalized_artifacts["primary_log_path"],
        "extra_log_paths": list(normalized_artifacts["extra_log_paths"]),
        "artifacts": normalized_artifacts,
        "cost_usd": _float_or_none(metrics.get("cost_usd")),
        "duration_s": _float_or_none(timing.get("duration_s")),
    }
    queue_task_id = _string_or_none(identity.get("queue_task_id"))
    if queue_task_id:
        snapshot["queue_task_id"] = queue_task_id
    merge_id = _string_or_none(identity.get("merge_id"))
    if merge_id:
        snapshot["merge_id"] = merge_id
    for key, value in dict(extra_fields or {}).items():
        snapshot[key] = value
    return snapshot


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


def load_project_history_rows(project_dir: Path, *, limit_hint: int | None = None) -> list[dict[str, Any]]:
    """Merge v2, legacy, and archived history rows into one deduped timeline."""
    sources = [
        paths.history_jsonl(project_dir),
        paths.legacy_run_history_jsonl(project_dir),
        *(archive / paths.LEGACY_RUN_HISTORY for archive in paths.archived_pre_restructure_dirs(project_dir)),
    ]
    if limit_hint is None or limit_hint <= 0:
        loaded_sources = [
            _LoadedHistorySource(
                path=source,
                source_index=source_index,
                rows=read_history_rows(source),
                exhausted=True,
                fallback_ts=_source_fallback_ts(source),
            )
            for source_index, source in enumerate(sources)
            if source.exists()
        ]
    else:
        loaded_sources = _load_bounded_history_sources(sources, limit_hint=max(limit_hint, 1))
    selected = _dedupe_history_entries(_flatten_history_entries(loaded_sources))
    selected.sort(key=lambda item: item[0])
    return [entry for _, _, _, entry in selected]


def _normalize_artifacts(artifacts: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(artifacts or {})
    extra_log_paths = data.get("extra_log_paths")
    if not isinstance(extra_log_paths, list):
        extra_log_paths = []
    return {
        "session_dir": _string_or_none(data.get("session_dir")),
        "manifest_path": _present_or_none(data.get("manifest_path")),
        "checkpoint_path": _present_or_none(data.get("checkpoint_path")),
        "summary_path": _present_or_none(data.get("summary_path")),
        "primary_log_path": _present_or_none(data.get("primary_log_path")),
        "extra_log_paths": [
            resolved
            for path in extra_log_paths
            if (resolved := _present_or_none(path))
        ],
    }


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _present_or_none(path: Any) -> str | None:
    text = _string_or_none(path)
    if not text:
        return None
    try:
        return text if Path(text).expanduser().exists() else None
    except OSError:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class _LoadedHistorySource:
    def __init__(
        self,
        *,
        path: Path,
        source_index: int,
        rows: list[dict[str, Any]],
        exhausted: bool,
        fallback_ts: float,
    ) -> None:
        self.path = path
        self.source_index = source_index
        self.rows = rows
        self.exhausted = exhausted
        self.fallback_ts = fallback_ts


def _load_bounded_history_sources(sources: list[Path], *, limit_hint: int) -> list[_LoadedHistorySource]:
    loaded_sources = [
        (
            _load_history_source(source, source_index=source_index, limit=limit_hint)
            if source.exists()
            else _LoadedHistorySource(
                path=source,
                source_index=source_index,
                rows=[],
                exhausted=True,
                fallback_ts=0.0,
            )
        )
        for source_index, source in enumerate(sources)
    ]
    if not any(source.rows or not source.exhausted for source in loaded_sources):
        return []

    while True:
        selected = _dedupe_history_entries(_flatten_history_entries(loaded_sources))
        pending_expansions: set[int] = set()
        if len(selected) < limit_hint:
            pending_expansions.update(
                index for index, source in enumerate(loaded_sources) if not source.exhausted
            )
        for item in selected:
            _, source_index, _, entry = item
            for higher_index in range(source_index):
                higher_source = loaded_sources[higher_index]
                if higher_source.exhausted:
                    continue
                if not _source_rows_might_suppress(higher_source.rows, entry):
                    pending_expansions.add(higher_index)
        if not pending_expansions:
            return loaded_sources
        for source_index in pending_expansions:
            source = loaded_sources[source_index]
            next_limit = max(len(source.rows) * 2, limit_hint)
            loaded_sources[source_index] = _load_history_source(
                source.path,
                source_index=source.source_index,
                limit=next_limit,
            )


def _load_history_source(path: Path, *, source_index: int, limit: int) -> _LoadedHistorySource:
    rows, exhausted = _tail_history_rows(path, limit=limit)
    return _LoadedHistorySource(
        path=path,
        source_index=source_index,
        rows=rows,
        exhausted=exhausted,
        fallback_ts=_source_fallback_ts(path),
    )


def _tail_history_rows(path: Path, *, limit: int) -> tuple[list[dict[str, Any]], bool]:
    if limit <= 0:
        return [], False
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            cursor = handle.tell()
            buffered = b""
            rows_rev: list[dict[str, Any]] = []
            hit_limit = False
            while cursor > 0 and len(rows_rev) < limit:
                read_size = min(8192, cursor)
                cursor -= read_size
                handle.seek(cursor)
                chunk = handle.read(read_size)
                buffered = chunk + buffered
                parts = buffered.splitlines()
                if cursor > 0:
                    buffered = parts[0]
                    complete_lines = parts[1:]
                else:
                    buffered = b""
                    complete_lines = parts
                for raw_line in reversed(complete_lines):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows_rev.append(value)
                        if len(rows_rev) >= limit:
                            hit_limit = True
                            break
            return list(reversed(rows_rev)), cursor == 0 and not hit_limit
    except OSError:
        return [], True


def _flatten_history_entries(
    sources: list[_LoadedHistorySource],
) -> list[tuple[tuple[float, int, int], int, int, dict[str, Any]]]:
    entries: list[tuple[tuple[float, int, int], int, int, dict[str, Any]]] = []
    for source in sources:
        for line_index, entry in enumerate(source.rows):
            entries.append((
                _history_sort_key(
                    entry,
                    fallback_ts=source.fallback_ts,
                    source_index=source.source_index,
                    line_index=line_index,
                ),
                source.source_index,
                line_index,
                entry,
            ))
    return entries


def _source_rows_might_suppress(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> bool:
    candidate_run_id = _history_run_id(candidate)
    candidate_command = str(candidate.get("command") or "").strip()
    candidate_normalized_command = _normalize_command_label(candidate_command) if candidate_command else ""
    candidate_dedupe_key = str(candidate.get("dedupe_key") or "").strip()
    for row in rows:
        dedupe_key = str(row.get("dedupe_key") or "").strip()
        if candidate_dedupe_key and dedupe_key == candidate_dedupe_key:
            return True
        run_id = _history_run_id(row)
        if not candidate_run_id or run_id != candidate_run_id:
            continue
        command = str(row.get("command") or "").strip()
        normalized_command = _normalize_command_label(command) if command else ""
        if candidate_normalized_command == normalized_command:
            return True
        if candidate_normalized_command and not normalized_command:
            return True
        if normalized_command and not candidate_normalized_command:
            return True
    return False


def _source_fallback_ts(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


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
