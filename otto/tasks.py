"""Otto task management — CRUD on tasks.yaml with file locking."""

import fcntl
import os
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml


def generate_key(existing_keys: set[str]) -> str:
    """Generate a unique 12-char hex key."""
    while True:
        key = uuid.uuid4().hex[:12]
        if key not in existing_keys:
            return key


def load_tasks(tasks_path: Path) -> list[dict[str, Any]]:
    """Load tasks from tasks.yaml. Returns empty list if file doesn't exist."""
    if not tasks_path.exists():
        return []
    data = yaml.safe_load(tasks_path.read_text())
    if data is None or "tasks" not in data:
        return []
    return data["tasks"]


def save_tasks(tasks_path: Path, tasks: list[dict[str, Any]]) -> None:
    """Atomically write tasks to tasks.yaml."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(tasks_path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            yaml.dump({"tasks": tasks}, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, tasks_path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def _locked_rw(tasks_path: Path, mutator):
    """Read-modify-write tasks.yaml under flock."""
    # Canonicalize to prevent symlink/alternate-path aliasing of the lock
    canonical = tasks_path.resolve()
    lock_path = canonical.parent / ".tasks.lock"
    lock_path.touch()
    with open(lock_path, "r") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = load_tasks(canonical)
        result = mutator(tasks)
        save_tasks(canonical, tasks)
        return result


def add_task(
    tasks_path: Path,
    prompt: str,
    verify: str | None = None,
    max_retries: int | None = None,
    rubric: list[str] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """Add a new task to tasks.yaml. Thread-safe via flock."""
    def _add(tasks):
        existing_keys = {t["key"] for t in tasks if "key" in t}
        max_id = max((t.get("id", 0) for t in tasks), default=0)
        task: dict[str, Any] = {
            "id": max_id + 1,
            "key": generate_key(existing_keys),
            "prompt": prompt,
            "status": "pending",
        }
        if verify is not None:
            task["verify"] = verify
        if max_retries is not None:
            task["max_retries"] = max_retries
        if rubric is not None:
            task["rubric"] = rubric
        if context is not None:
            task["context"] = context
        tasks.append(task)
        return task

    return _locked_rw(tasks_path, _add)


def add_tasks(
    tasks_path: Path,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add multiple tasks atomically. Thread-safe via flock."""
    results: list[dict[str, Any]] = []

    def _add_batch(tasks):
        existing_keys = {t["key"] for t in tasks if "key" in t}
        max_id = max((t.get("id", 0) for t in tasks), default=0)
        for item in batch:
            max_id += 1
            task: dict[str, Any] = {
                "id": max_id,
                "key": generate_key(existing_keys),
                "prompt": item["prompt"],
                "status": "pending",
            }
            existing_keys.add(task["key"])
            for field in ("verify", "max_retries", "rubric", "context"):
                if field in item and item[field] is not None:
                    task[field] = item[field]
            tasks.append(task)
            results.append(task)

    _locked_rw(tasks_path, _add_batch)
    return results


def update_task(tasks_path: Path, key: str, **updates) -> dict[str, Any]:
    """Update a task by key. Thread-safe via flock. Raises KeyError if not found."""
    result = {}

    def _update(tasks):
        for task in tasks:
            if task.get("key") == key:
                task.update(updates)
                result.update(task)
                return
        raise KeyError(f"Task with key '{key}' not found")

    _locked_rw(tasks_path, _update)
    return result


def reset_all_tasks(tasks_path: Path) -> int:
    """Reset all tasks to pending. Thread-safe via flock. Returns count reset."""
    def _reset(tasks):
        for t in tasks:
            t["status"] = "pending"
            t.pop("attempts", None)
            t.pop("session_id", None)
            t.pop("error", None)
            t.pop("error_code", None)
        return len(tasks)

    return _locked_rw(tasks_path, _reset)
