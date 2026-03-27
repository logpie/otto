"""Otto task management — CRUD on tasks.yaml with file locking."""

import fcntl
import graphlib
import os
import tempfile
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

VALID_TASK_STATES = {
    "pending",
    "running",
    "verified",
    "merged",
    "merge_pending",
    "passed",
    "failed",
    "blocked",
    "merge_failed",
}


def _validate_task_status(status: Any) -> None:
    """Raise when Otto task status is not a recognized pipeline state."""
    if status not in VALID_TASK_STATES:
        raise ValueError(f"Invalid task status: {status!r}")


def spec_text(item) -> str:
    """Extract text from a spec item (either plain string or dict with 'text' key)."""
    if isinstance(item, dict):
        return item.get("text", "")
    return str(item)


def spec_is_verifiable(item) -> bool:
    """Check if a spec item is verifiable (default True for plain strings)."""
    if isinstance(item, dict):
        return item.get("verifiable", True)
    return True


def spec_binding(item) -> str:
    """Get the binding level of a spec item ('must' or 'should')."""
    if isinstance(item, dict):
        return item.get("binding", "must")
    return "must"  # plain strings default to must


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
    spec: list | None = None,
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
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if verify is not None:
            task["verify"] = verify
        if max_retries is not None:
            task["max_retries"] = max_retries
        if spec is not None:
            task["spec"] = spec
        tasks.append(task)
        return task

    return _locked_rw(tasks_path, _add)


def add_tasks(
    tasks_path: Path,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add multiple tasks atomically. Thread-safe via flock.

    Batch items may include 'depends_on' as 0-based indices into the batch.
    These are translated to task IDs after ID assignment, validated for
    range, self-refs, and cycles.
    """
    results: list[dict[str, Any]] = []

    def _add_batch(tasks):
        existing_keys = {t["key"] for t in tasks if "key" in t}
        max_id = max((t.get("id", 0) for t in tasks), default=0)

        # First pass: assign IDs and keys
        now = datetime.now(timezone.utc).isoformat()
        new_tasks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in batch:
            max_id += 1
            task: dict[str, Any] = {
                "id": max_id,
                "key": generate_key(existing_keys),
                "prompt": item["prompt"],
                "status": "pending",
                "created_at": now,
            }
            existing_keys.add(task["key"])
            for field in ("verify", "max_retries", "spec"):
                if field in item and item[field] is not None:
                    task[field] = item[field]
            new_tasks.append((item, task))

        # Second pass: translate depends_on indices to task IDs
        for i, (item, task) in enumerate(new_tasks):
            dep_indices = item.get("depends_on")
            if not dep_indices:
                continue
            for idx in dep_indices:
                if idx == i:
                    raise ValueError(f"Task {i} depends on itself")
                if idx < 0 or idx >= len(batch):
                    raise ValueError(
                        f"Task {i} depends_on index {idx} out of range [0, {len(batch) - 1}]"
                    )
            task["depends_on"] = [new_tasks[idx][1]["id"] for idx in dep_indices]

        # Cycle validation
        ts = graphlib.TopologicalSorter()
        for _, task in new_tasks:
            deps = task.get("depends_on") or []
            ts.add(task["id"], *deps)
        try:
            ts.prepare()
        except graphlib.CycleError as e:
            raise ValueError(f"Dependency cycle detected: {e}") from e

        for _, task in new_tasks:
            tasks.append(task)
            results.append(task)

    _locked_rw(tasks_path, _add_batch)
    return results


def update_task(tasks_path: Path, key: str, **updates) -> dict[str, Any]:
    """Update a task by key. Thread-safe via flock. Raises KeyError if not found."""
    result = {}
    if "status" in updates and updates["status"] is not None:
        _validate_task_status(updates["status"])

    def _update(tasks):
        for task in tasks:
            if task.get("key") == key:
                for k, v in updates.items():
                    if v is None:
                        task.pop(k, None)
                    else:
                        task[k] = v
                result.update(task)
                return
        raise KeyError(f"Task with key '{key}' not found")

    _locked_rw(tasks_path, _update)
    return result


def delete_task(tasks_path: Path, task_id: int) -> dict[str, Any]:
    """Delete a task by ID. Thread-safe via flock. Raises KeyError if not found."""
    removed: dict[str, Any] = {}

    def _delete(tasks):
        for i, task in enumerate(tasks):
            if task.get("id") == task_id:
                removed.update(task)
                tasks.pop(i)
                return
        raise KeyError(f"Task #{task_id} not found")

    _locked_rw(tasks_path, _delete)
    return removed


def reset_all_tasks(tasks_path: Path) -> int:
    """Reset all tasks to pending. Thread-safe via flock. Returns count reset."""
    def _reset(tasks):
        for t in tasks:
            t["status"] = "pending"
            t.pop("attempts", None)
            t.pop("session_id", None)
            t.pop("error", None)
            t.pop("error_code", None)
            t.pop("changed_files", None)
            t.pop("completed_at", None)
        return len(tasks)

    return _locked_rw(tasks_path, _reset)
