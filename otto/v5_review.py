"""v5 review affordance — pause-for-review at root decomposition.

When `--review-first-decomp` is set, the watcher (v5_runner) holds dispatch
of root's emitted children until the user approves them via:
  - the CLI: `otto review approve|edit|replace|cancel`
  - the MC API (Phase 3 UI): POST to /api/v5/<session_id>/review with action

Review state lives in the v5_pending entries' `review_state` field:
  - "approved" (default in autopilot) → watcher dispatches immediately.
  - "pending_review" → watcher holds; CLI/MC must transition to "approved".
  - "cancelled" → watcher skips, marks task as merge_blocked.

This module provides the helper functions for the CLI and MC to mutate
review state safely.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

from otto.queue.subtask import v5_pending_path
from otto.observability import iso_timestamp

logger = logging.getLogger("otto.v5_review")

ReviewAction = Literal["approve", "edit", "replace", "cancel"]


@contextlib.contextmanager
def _locked_pending(project_dir: Path):
    """fcntl-locked read+write of v5_pending.jsonl."""
    path = v5_pending_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _write_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def list_pending_review(
    project_dir: Path,
    *,
    parent_task_id: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks in pending_review state, optionally filtered by parent."""
    path = v5_pending_path(project_dir)
    entries = _read_entries(path)
    out = [e for e in entries if e.get("review_state") == "pending_review"]
    if parent_task_id is not None:
        out = [e for e in out if e.get("parent_task_id") == parent_task_id]
    return out


def mark_pending_review(
    project_dir: Path,
    *,
    parent_task_id: str,
) -> int:
    """Transition all approved children of ``parent_task_id`` to pending_review.

    Used at root-level when ``--review-first-decomp`` is on: after the root
    Lead emits children, they're written with review_state=approved (default);
    we flip them to pending_review so the watcher holds dispatch.

    Returns the number of entries updated.
    """
    with _locked_pending(project_dir) as path:
        entries = _read_entries(path)
        count = 0
        for e in entries:
            if (
                e.get("parent_task_id") == parent_task_id
                and e.get("review_state") in ("approved", None)
            ):
                e["review_state"] = "pending_review"
                count += 1
        _write_entries(path, entries)
    return count


def approve(
    project_dir: Path,
    *,
    task_ids: list[str] | None = None,
    parent_task_id: str | None = None,
) -> int:
    """Transition pending_review tasks to approved.

    If ``task_ids`` is given, approve those specifically. Otherwise approve
    all pending under ``parent_task_id``. Returns count approved.
    """
    with _locked_pending(project_dir) as path:
        entries = _read_entries(path)
        count = 0
        target_ids: set[str] | None = set(task_ids) if task_ids else None
        for e in entries:
            if e.get("review_state") != "pending_review":
                continue
            if target_ids is not None and e.get("task_id") not in target_ids:
                continue
            if parent_task_id is not None and e.get("parent_task_id") != parent_task_id:
                continue
            e["review_state"] = "approved"
            e["approved_at"] = iso_timestamp()
            count += 1
        _write_entries(path, entries)
    return count


def cancel(
    project_dir: Path,
    *,
    task_ids: list[str],
) -> int:
    """Mark specific pending tasks as cancelled (won't be dispatched)."""
    with _locked_pending(project_dir) as path:
        entries = _read_entries(path)
        target_ids = set(task_ids)
        count = 0
        for e in entries:
            if e.get("task_id") in target_ids and e.get("review_state") == "pending_review":
                e["review_state"] = "cancelled"
                e["cancelled_at"] = iso_timestamp()
                count += 1
        _write_entries(path, entries)
    return count


def edit(
    project_dir: Path,
    *,
    task_id: str,
    new_intent: str,
) -> bool:
    """Replace a pending task's intent. Returns True if updated."""
    new_intent = (new_intent or "").strip()
    if not new_intent:
        raise ValueError("edit: new_intent must be non-empty")
    with _locked_pending(project_dir) as path:
        entries = _read_entries(path)
        for e in entries:
            if e.get("task_id") == task_id and e.get("review_state") == "pending_review":
                e["intent"] = new_intent
                e["edited_at"] = iso_timestamp()
                _write_entries(path, entries)
                return True
    return False


def replace(
    project_dir: Path,
    *,
    parent_task_id: str,
    new_intents: list[str],
) -> tuple[list[str], list[str]]:
    """Cancel all pending children of ``parent_task_id``, append new ones.

    Returns (cancelled_ids, new_ids).
    """
    from otto.queue.subtask import enqueue_subtask
    from otto.queue.task_graph import record_task

    cancelled_ids: list[str] = []
    new_ids: list[str] = []

    with _locked_pending(project_dir) as path:
        entries = _read_entries(path)
        # Cancel existing pending children of this parent.
        for e in entries:
            if (
                e.get("parent_task_id") == parent_task_id
                and e.get("review_state") == "pending_review"
            ):
                e["review_state"] = "cancelled"
                cancelled_ids.append(e.get("task_id", ""))
        _write_entries(path, entries)

    # Append new tasks (these go to v5_pending via enqueue_subtask).
    for intent in new_intents:
        intent = intent.strip()
        if not intent:
            continue
        new_id = enqueue_subtask(
            project_dir=project_dir,
            parent_task_id=parent_task_id,
            parent_session_dir=project_dir,  # caller may not know the parent's session_dir
            intent=intent,
        )
        record_task(
            project_dir,
            task_id=new_id,
            intent=intent,
            parent_task_id=parent_task_id,
        )
        new_ids.append(new_id)

    return cancelled_ids, new_ids
