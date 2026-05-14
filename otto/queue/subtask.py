"""Agent-emitted subtask plumbing.

When a v5 Lead calls ``mcp__otto__submit_subtask``, this module is invoked.
It generates a task_id, persists the new task to the project-level "v5
pending tasks" file, and returns the id so the Lead can chain depends_on
references.

In Phase 1, the watcher does not yet pick up agent-emitted subtasks; the
file exists for Phase 2 to consume. For testing in Phase 1, the
``drain_pending`` helper lets test fixtures process them synchronously.

In Phase 2, this module is extended so the watcher reads
``v5_pending.jsonl`` and spawns ``otto run --task-id <child>`` subprocesses
with the parent's integration branch as ``--integration-branch``.

File layout per project:
    otto_logs/cross-sessions/v5_pending.jsonl    -- append-only queue of
                                                    agent-emitted subtasks
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from otto.paths import cross_sessions_dir

V5_PENDING_FILENAME = "v5_pending.jsonl"


def v5_pending_path(project_dir: Path) -> Path:
    """Return the canonical v5 pending-tasks file path for a project."""
    return cross_sessions_dir(project_dir) / V5_PENDING_FILENAME


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _generate_task_id(intent: str) -> str:
    """Generate a v5 task id. Format: ``v5-<short-uuid>``.

    Distinct from today's queue task ids (slug-based) so the watcher in
    Phase 2 can route them differently if needed.
    """
    # Short uuid is sufficient — task_graph dedupes by (parent, intent_hash).
    return f"v5-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def _locked_append(path: Path):
    """Append-only fcntl-locked write to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def enqueue_subtask(
    *,
    project_dir: Path,
    parent_task_id: str,
    parent_session_dir: Path,
    intent: str,
    depends_on: list[str] | None = None,
    owned_paths: list[str] | None = None,
    action_ids: list[str] | None = None,
    parent_integration_branch: str | None = None,
) -> str:
    """Persist an agent-emitted subtask and return its task_id.

    Atomic: fcntl-locked append to ``v5_pending.jsonl``. The watcher (Phase 2)
    reads this file to find tasks ready to spawn.
    """
    intent = (intent or "").strip()
    if not intent:
        raise ValueError("enqueue_subtask: 'intent' is required and must be non-empty.")

    task_id = _generate_task_id(intent)
    if parent_integration_branch:
        integration_branch = parent_integration_branch
    else:
        from otto.v5_branching import integration_branch_name as _integ_name
        integration_branch = _integ_name(parent_task_id)
    entry: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(parent_session_dir),
        "intent": intent,
        "depends_on": list(depends_on or []),
        "owned_paths": list(owned_paths or []),
        "action_ids": list(action_ids or []),
        "integration_branch": integration_branch,
        "review_state": "approved",  # autopilot default; Phase 3 may set "pending_review"
        "enqueued_at": _now_iso(),
    }

    path = v5_pending_path(project_dir)
    with _locked_append(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return task_id


def read_pending(project_dir: Path) -> list[dict[str, Any]]:
    """Read all pending v5 subtasks. Phase 2 watcher consumes this."""
    path = v5_pending_path(project_dir)
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


_TERMINAL_VERDICTS = frozenset({
    "pass", "partial", "unverified", "merge_blocked", "catastrophic",
})
_NON_RUNNABLE_VERDICTS = _TERMINAL_VERDICTS | {"pending_children"}


def _globally_completed_task_ids(project_dir: Path) -> set[str]:
    """Tasks whose verdict in task_graph.json is terminal (not pending).

    The runner's local ``completed`` set in _process_children is per-call,
    so recursive invocations don't see siblings completed in earlier passes —
    leading to re-dispatch loops. The task graph is the global source of
    truth for completion; reading from it stops the thrash.
    """
    from otto.queue.task_graph import read_graph

    try:
        graph = read_graph(project_dir)
    except Exception:  # noqa: BLE001 — best-effort
        return set()
    done: set[str] = set()
    for tid, t in (graph.get("tasks") or {}).items():
        verdict = t.get("verdict")
        if isinstance(verdict, str) and verdict in _TERMINAL_VERDICTS:
            done.add(tid)
    return done


def _globally_non_runnable_task_ids(project_dir: Path) -> set[str]:
    """Tasks whose graph verdict means they should not be dispatched again.

    ``pending_children`` is not terminal for dependency satisfaction, but it is
    terminal for the planning Lead's own dispatch: that Lead already emitted
    children and must not be picked up again by a sibling scheduler loop.
    """
    from otto.queue.task_graph import read_graph

    try:
        graph = read_graph(project_dir)
    except Exception:  # noqa: BLE001 — best-effort
        return set()
    done: set[str] = set()
    for tid, t in (graph.get("tasks") or {}).items():
        verdict = t.get("verdict")
        if isinstance(verdict, str) and verdict in _NON_RUNNABLE_VERDICTS:
            done.add(tid)
    return done


def take_ready(
    project_dir: Path,
    *,
    completed_task_ids: set[str],
    in_flight_task_ids: set[str],
) -> list[dict[str, Any]]:
    """Return subtasks whose ``depends_on`` are all done and not yet running.

    Combines the caller's local completed set with the global task_graph view
    so already-resolved tasks don't get re-dispatched across recursive
    _process_children invocations.
    """
    # Union local + global completion. Local catches in-flight transitions
    # the graph hasn't observed yet; global catches across recursive calls.
    globally_done = _globally_completed_task_ids(project_dir)
    completed_all = set(completed_task_ids) | globally_done
    non_runnable = (
        set(completed_task_ids)
        | globally_done
        | _globally_non_runnable_task_ids(project_dir)
    )

    pending = read_pending(project_dir)
    ready: list[dict[str, Any]] = []
    for entry in pending:
        tid = entry.get("task_id")
        if not tid or tid in non_runnable or tid in in_flight_task_ids:
            continue
        if entry.get("review_state") not in ("approved", None):
            continue
        deps = entry.get("depends_on") or []
        if all(d in completed_all for d in deps):
            ready.append(entry)
    return ready
