"""Shared Mission Control session resolution helpers.

Queue-driven i2p runs execute inside per-task worktrees, so their session
artifacts often live under ``<project>/.worktrees/<task>/otto_logs/sessions``
rather than the selected project's root ``otto_logs/sessions`` directory.
Every web surface that accepts a session id should resolve through this
module so run-view, spec-review, and i2p diagnostics agree on what exists.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from otto.queue.schema import state_path


@dataclass(frozen=True)
class ResolvedSession:
    """A session directory discovered in the selected project."""

    session_id: str
    path: Path
    source: str
    worktree: Path | None = None


def validate_session_id(session_id: str) -> str:
    """Return a safe session id or raise 404 for traversal-shaped values."""

    if "/" in session_id or "\\" in session_id or session_id in {"", ".", ".."}:
        raise HTTPException(
            status_code=404,
            detail=f"session id rejected: {session_id!r}",
        )
    return session_id


def iter_session_dirs(project_dir: Path) -> list[ResolvedSession]:
    """Return all project-root and queue-worktree session dirs.

    Root project sessions win duplicate ids. Worktree roots are bounded under
    ``project_dir`` after resolution so symlink/path surprises do not escape
    the selected project.
    """

    from otto import paths as _paths
    project_root = project_dir.resolve(strict=False)
    roots: list[tuple[Path, str, Path | None]] = [
        (_paths.sessions_root(project_root), "project", None)
    ]
    worktrees_root = project_root / ".worktrees"
    if worktrees_root.exists() and worktrees_root.is_dir():
        for child in sorted(worktrees_root.iterdir(), key=lambda p: p.name):
            if child.is_dir():
                roots.append((_paths.sessions_root(child), "worktree", child))

    by_id: dict[str, ResolvedSession] = {}
    for root, source, worktree in roots:
        resolved_root = root.resolve(strict=False)
        if not _is_relative_to(resolved_root, project_root):
            continue
        if not resolved_root.exists() or not resolved_root.is_dir():
            continue
        for entry in sorted(resolved_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            resolved_entry = entry.resolve(strict=False)
            if not _is_relative_to(resolved_entry, resolved_root):
                continue
            by_id.setdefault(
                entry.name,
                ResolvedSession(
                    session_id=entry.name,
                    path=resolved_entry,
                    source=source,
                    worktree=worktree.resolve(strict=False) if worktree else None,
                ),
            )
    return list(by_id.values())


def session_dirs(project_dir: Path) -> dict[str, Path]:
    """Return ``session_id -> session_dir`` for the selected project."""

    return {resolved.session_id: resolved.path for resolved in iter_session_dirs(project_dir)}


def resolve_session_dir(project_dir: Path, session_id: str) -> Path:
    """Resolve a session id to a discovered session directory."""

    safe_id = validate_session_id(session_id)
    found = session_dirs(project_dir).get(safe_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"session not found: {session_id!r}",
        )
    return found


def resolve_spec_dir(project_dir: Path, session_id: str) -> Path:
    """Resolve a session's ``spec`` directory."""

    sd = resolve_session_dir(project_dir, session_id) / "spec"
    if not sd.exists() or not sd.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"spec dir not found for session {session_id!r}",
        )
    return sd


def queue_state_for_session(project_dir: Path, session_id: str) -> dict[str, Any] | None:
    """Return the queue task state matching ``session_id``, when present."""

    safe_id = validate_session_id(session_id)
    path = state_path(project_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, dict):
        return None
    for task_id, raw_state in tasks.items():
        if not isinstance(raw_state, dict):
            continue
        ids = _queue_session_ids(raw_state)
        if safe_id not in ids:
            continue
        out = dict(raw_state)
        out.setdefault("task_id", str(task_id))
        out.setdefault("run_id", safe_id)
        _fill_live_duration(out)
        return out
    return None


def _queue_session_ids(raw_state: dict[str, Any]) -> set[str]:
    values: Iterable[Any] = (
        raw_state.get("attempt_run_id"),
        raw_state.get("child_run_id"),
        raw_state.get("run_id"),
    )
    return {str(value).strip() for value in values if str(value or "").strip()}


def _fill_live_duration(state: dict[str, Any]) -> None:
    if state.get("duration_s") is not None or state.get("wall_s") is not None:
        return
    if state.get("finished_at"):
        return
    status = str(state.get("status") or "").strip().lower()
    if status not in {"starting", "initializing", "running"}:
        return
    started_at = _parse_utc(state.get("started_at"))
    if started_at is None:
        return
    state["duration_s"] = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())


def _parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
