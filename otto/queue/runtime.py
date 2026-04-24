"""Shared queue runtime helpers.

Keeps watcher liveness and per-task resume discovery in one place so the
watcher, CLI, and read-only dashboard agree on what "active" and
"resumable" mean.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from otto import paths
from otto.queue.schema import QueueTask

INTERRUPTED_STATUS = "interrupted"
IN_FLIGHT_STATUSES = {"starting", "running", "terminating"}

_QUEUE_RUNNER_CHILD = False


def set_queue_runner_child(enabled: bool) -> None:
    """Record that this process was launched as a queue child.

    ``otto.cli.main`` strips ``OTTO_INTERNAL_QUEUE_RUNNER`` before command
    execution so nested subprocesses do not inherit the venv bypass. Runtime
    code still needs to know the original launch mode after that strip.
    """
    global _QUEUE_RUNNER_CHILD
    _QUEUE_RUNNER_CHILD = enabled


def is_queue_runner_child() -> bool:
    """Return True when the current Otto process was launched by the queue."""
    return _QUEUE_RUNNER_CHILD or os.environ.get("OTTO_INTERNAL_QUEUE_RUNNER") == "1"


def watcher_alive(state: dict, *, max_age_s: float = 10.0) -> bool:
    """Return True iff state.json's watcher heartbeat is fresh and live."""
    watcher = state.get("watcher")
    if not isinstance(watcher, dict):
        return False
    pid = watcher.get("pid")
    heartbeat = watcher.get("heartbeat")
    started_at = watcher.get("started_at")
    if not isinstance(pid, int) or not heartbeat or not started_at:
        return False
    try:
        when = datetime.strptime(heartbeat, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - when).total_seconds()
        if age >= max_age_s:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    except Exception:
        return False


def task_display_status(task_state: dict | None) -> str:
    """Return the user-facing status for a task state entry."""
    if not isinstance(task_state, dict):
        return "queued"
    status = str(task_state.get("status") or "queued")
    if status == "terminating" and task_state.get("terminal_status") == INTERRUPTED_STATUS:
        return INTERRUPTED_STATUS
    return status


def worktree_path_for_task(project_dir: Path, task: QueueTask) -> Path | None:
    if not task.worktree:
        return None
    return project_dir / task.worktree


def checkpoint_path_for_task(project_dir: Path, task: QueueTask) -> Path | None:
    """Return the best active checkpoint path for a queued task, if any."""
    worktree_dir = worktree_path_for_task(project_dir, task)
    if worktree_dir is None:
        return None

    paused_session = paths.resolve_pointer(worktree_dir, paths.PAUSED_POINTER)
    if paused_session is not None:
        checkpoint_path = paused_session / "checkpoint.json"
        if checkpoint_path.exists():
            return checkpoint_path

    sessions_root = paths.sessions_root(worktree_dir)
    if sessions_root.exists():
        best: tuple[float, Path] | None = None
        for checkpoint_path in sessions_root.glob("*/checkpoint.json"):
            if not checkpoint_path.exists():
                continue
            try:
                data = json.loads(checkpoint_path.read_text())
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                data = {}
            if data.get("status") not in {"in_progress", "paused"}:
                continue
            updated_at = data.get("updated_at")
            timestamp = 0.0
            if isinstance(updated_at, str) and updated_at:
                try:
                    timestamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    timestamp = 0.0
            if timestamp == 0.0:
                try:
                    timestamp = checkpoint_path.stat().st_mtime
                except OSError:
                    timestamp = 0.0
            candidate = (timestamp, checkpoint_path)
            if best is None or candidate[0] >= best[0]:
                best = candidate
        if best is not None:
            return best[1]

    legacy_checkpoint = paths.legacy_checkpoint(worktree_dir)
    if legacy_checkpoint.exists():
        return legacy_checkpoint
    return None


def task_resume_available(project_dir: Path, task: QueueTask) -> bool:
    if not task.resumable:
        return False
    return checkpoint_path_for_task(project_dir, task) is not None
