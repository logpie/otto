"""Queue file schemas + atomic I/O (Phase 2.1).

Three files, three writers:
- queue.yml   — CLI appends task definitions and may remove queued tasks offline; watcher reads only
- state.json  — watcher sole writer; CLI reads via load_state()
- commands.jsonl — CLI appends mutation requests; watcher consumes

Atomic writes via tempfile + os.rename. Concurrent CLI writes to queue.yml
and commands.jsonl serialised via fcntl.flock(LOCK_EX). The watcher never
takes a write lock — it's the only writer of state.json, and queue.yml is
re-read on mtime change (no lock needed for reads).

See plan-parallel.md §3.4 + Phase 2.1 verify criteria.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from otto import paths as otto_paths
from otto.runs.registry import append_jsonl_row, read_jsonl_rows, utc_now_iso

QUEUE_FILE = ".otto-queue.yml"
STATE_FILE = ".otto-queue-state.json"
COMMANDS_FILE = ".otto-queue-commands.jsonl"
LOCK_FILE = ".otto-queue.lock"

QUEUE_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1


# ---------- task definitions (queue.yml) ----------

@dataclass
class QueueTask:
    """One entry in .otto-queue.yml. Definition only; status lives in state.json.

    All fields here are immutable after enqueue. The watcher never modifies
    queue.yml.
    """

    id: str                          # slug from intent (or --as), permanent for queue.yml lifetime
    command_argv: list[str]          # full argv, e.g. ["build", "add csv"] or ["improve", "bugs", "--rounds", "3"]
    after: list[str] = field(default_factory=list)  # task ids this depends on
    resumable: bool = True            # false for certify (no --resume path); true for build/improve
    added_at: str = ""                # ISO 8601
    # Snapshot at enqueue time (immutable):
    resolved_intent: str | None = None
    focus: str | None = None
    target: str | None = None
    spec_file_path: str | None = None
    branch: str | None = None
    worktree: str | None = None       # relative path under project_dir
    # User-provided extras — opaque, preserved on round-trip
    notes: str | None = None


# ---------- queue.yml r/w ----------

def queue_path(project_dir: Path) -> Path:
    return project_dir / QUEUE_FILE


def state_path(project_dir: Path) -> Path:
    return otto_paths.queue_state_path(project_dir)


def commands_path(project_dir: Path) -> Path:
    return otto_paths.queue_commands_path(project_dir)


def commands_processing_path(project_dir: Path) -> Path:
    return otto_paths.queue_commands_processing_path(project_dir)


def command_acks_path(project_dir: Path) -> Path:
    return otto_paths.queue_command_acks_path(project_dir)


def commands_lock_path(project_dir: Path) -> Path:
    return project_dir / ".otto-queue-commands.jsonl.lock"


def lock_path(project_dir: Path) -> Path:
    return project_dir / LOCK_FILE


def load_queue(project_dir: Path) -> list[QueueTask]:
    """Load all task definitions from queue.yml. Empty if file missing."""
    path = queue_path(project_dir)
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"queue.yml is malformed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected mapping, got {type(raw).__name__}")
    if raw.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version mismatch (got {raw.get('schema_version')!r}, "
            f"expected {QUEUE_SCHEMA_VERSION}). Please migrate manually."
        )
    tasks_raw = raw.get("tasks") or []
    if not isinstance(tasks_raw, list):
        raise ValueError(f"{path}: 'tasks' must be a list, got {type(tasks_raw).__name__}")
    out: list[QueueTask] = []
    for entry in tasks_raw:
        if not isinstance(entry, dict):
            # Skip malformed entries with a clear error — refuse to silently lose tasks
            raise ValueError(f"{path}: task entry must be a mapping, got {entry!r}")
        out.append(_task_from_dict(entry))
    return out


def _task_from_dict(d: dict[str, Any]) -> QueueTask:
    """Build a QueueTask, filling defaults for missing fields."""
    # Validate required fields up front for clearer errors
    if "id" not in d:
        raise ValueError(f"task missing required field 'id': {d!r}")
    if "command_argv" not in d:
        raise ValueError(f"task missing required field 'command_argv': {d!r}")
    argv = d["command_argv"]
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError(f"task {d['id']!r}: command_argv must be list[str], got {argv!r}")
    return QueueTask(
        id=str(d["id"]),
        command_argv=list(argv),
        after=list(d.get("after") or []),
        resumable=bool(d.get("resumable", True)),
        added_at=str(d.get("added_at") or ""),
        resolved_intent=d.get("resolved_intent"),
        focus=d.get("focus"),
        target=d.get("target"),
        spec_file_path=d.get("spec_file_path"),
        branch=d.get("branch"),
        worktree=d.get("worktree"),
        notes=d.get("notes"),
    )


def append_task(project_dir: Path, task: QueueTask) -> None:
    """Append one task to queue.yml. Atomic: lock → read → append → rename.

    Existing entries are NEVER modified or removed by this function.
    """
    path = queue_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".yml.lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            current = load_queue(project_dir)
            # Reject id collision: caller is responsible for ensuring unique
            # ids via slugify_intent's dedup, but defense-in-depth here.
            if any(t.id == task.id for t in current):
                raise ValueError(f"task id {task.id!r} already exists in queue")
            current.append(task)
            _write_queue(path, current)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def remove_task(project_dir: Path, task_id: str) -> bool:
    """Remove one task from queue.yml under the queue lock.

    Returns True if the task existed and was removed, False otherwise.
    """
    path = queue_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".yml.lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            current = load_queue(project_dir)
            kept = [task for task in current if task.id != task_id]
            if len(kept) == len(current):
                return False
            _write_queue(path, kept)
            return True
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def reorder_tasks(project_dir: Path, prioritized_ids: list[str]) -> None:
    """Move selected task ids to the front of queue.yml, preserving order."""
    if not prioritized_ids:
        return
    path = queue_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".yml.lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            current = load_queue(project_dir)
            wanted = set(prioritized_ids)
            ordered = [task for task in current if task.id in wanted]
            ordered.sort(key=lambda task: prioritized_ids.index(task.id))
            remainder = [task for task in current if task.id not in wanted]
            _write_queue(path, [*ordered, *remainder])
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _write_queue(path: Path, tasks: list[QueueTask]) -> None:
    """Atomic write of queue.yml."""
    payload = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "tasks": [_task_to_dict(t) for t in tasks],
    }
    _atomic_write_text(path, yaml.dump(payload, default_flow_style=False, sort_keys=False))


def _task_to_dict(task: QueueTask) -> dict[str, Any]:
    """Drop None-valued optional fields for cleaner YAML output."""
    d = asdict(task)
    return {k: v for k, v in d.items() if v is not None and v != []}


# ---------- state.json r/w ----------

def load_state(project_dir: Path) -> dict[str, Any]:
    """Load state.json. Returns minimal default dict if missing.

    Returns the raw dict so readers aren't coupled to a runtime dataclass
    schema. Expected shape:
    ``{"schema_version": 1, "watcher": {...} | None, "tasks": {"id": {...}}}``.
    """
    path = state_path(project_dir)
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected object, got {type(data).__name__}")
    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version mismatch (got {data.get('schema_version')!r}, "
            f"expected {STATE_SCHEMA_VERSION})."
        )
    return data


def write_state(project_dir: Path, state: dict[str, Any]) -> None:
    """Atomic write of state.json. **Watcher sole writer** — CLI must NOT call.

    The dict shape is ``{"schema_version", "watcher", "tasks": {"id": {...}}}``.
    """
    path = state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        state = {**state, "schema_version": STATE_SCHEMA_VERSION}
    _atomic_write_text(path, json.dumps(state, indent=2, sort_keys=False))


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "watcher": None,
        "tasks": {},
    }


# ---------- commands.jsonl (CLI → watcher message log) ----------

def append_command(project_dir: Path, cmd: dict[str, Any]) -> None:
    """Append one command line to commands.jsonl. CLI calls this; watcher consumes.

    Atomic via flock — multiple concurrent CLI calls don't interleave.
    """
    path = commands_path(project_dir)
    lock_target = commands_lock_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(cmd, sort_keys=False) + "\n"
    with open(lock_target, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def drain_commands(project_dir: Path) -> list[dict[str, Any]]:
    """Read all commands and atomically truncate the file. **Watcher only.**

    Atomic: rename to .processing → read → unlink. If the watcher crashes
    between rename and read, the .processing file persists and is picked
    up on next call.
    """
    path = commands_path(project_dir)
    proc_path = path.with_suffix(".jsonl.processing")
    lock_target = commands_lock_path(project_dir)
    with open(lock_target, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            # Use rename-then-read; if .processing exists from a prior crash, drain it first.
            if proc_path.exists():
                if path.exists():
                    with open(proc_path, "a") as pf:
                        pf.write(path.read_text())
                        pf.flush()
                        os.fsync(pf.fileno())
                    path.unlink()
            else:
                if not path.exists():
                    return []
                path.rename(proc_path)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    out: list[dict[str, Any]] = []
    for line in proc_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            if isinstance(cmd, dict):
                out.append(cmd)
        except json.JSONDecodeError:
            # Skip malformed lines — log via caller; refuse to crash the watcher
            continue
    proc_path.unlink()
    return out


def load_command_ack_ids(project_dir: Path) -> set[str]:
    return {
        str(row.get("command_id") or "")
        for row in read_jsonl_rows(command_acks_path(project_dir))
        if row.get("command_id")
    }


def append_command_ack(
    project_dir: Path,
    cmd: dict[str, Any],
    *,
    writer_id: str,
    outcome: str = "applied",
    state_version: int | None = None,
    note: str | None = None,
) -> dict[str, Any] | None:
    command_id = str(cmd.get("command_id") or "").strip()
    if not command_id:
        return None
    row = {
        "schema_version": 1,
        "command_id": command_id,
        "run_id": cmd.get("run_id"),
        "acked_at": utc_now_iso(),
        "writer_id": writer_id,
        "outcome": outcome,
        "state_version": state_version,
        "note": note,
    }
    return append_jsonl_row(command_acks_path(project_dir), row)


def begin_command_drain(project_dir: Path) -> list[dict[str, Any]]:
    """Drain request logs into `.processing` and return unacked commands."""
    path = commands_path(project_dir)
    proc_path = commands_processing_path(project_dir)
    lock_target = commands_lock_path(project_dir)
    with open(lock_target, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            if proc_path.exists():
                if path.exists():
                    with open(proc_path, "a") as pf:
                        pf.write(path.read_text())
                        pf.flush()
                        os.fsync(pf.fileno())
                    path.unlink()
            elif path.exists():
                path.rename(proc_path)
            else:
                return []
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    acked = load_command_ack_ids(project_dir)
    out: list[dict[str, Any]] = []
    for line in proc_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(cmd, dict):
            continue
        command_id = str(cmd.get("command_id") or "")
        if command_id and command_id in acked:
            continue
        out.append(cmd)
    return out


def finish_command_drain(project_dir: Path) -> None:
    commands_processing_path(project_dir).unlink(missing_ok=True)


# ---------- atomic write helper ----------

def _atomic_write_text(path: Path, content: str) -> None:
    """Write content atomically via tempfile + rename in the same dir.

    Ensures that readers either see the old contents or the new ones,
    never a partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
