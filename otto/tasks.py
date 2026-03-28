"""Otto task management — CRUD on tasks.yaml with file locking."""

from __future__ import annotations

import fcntl
import graphlib
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
    "conflict",
    "merge_failed",
}

_PLANNER_DERIVED_STATUSES = {"conflict", "blocked"}
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
    return "must"


def generate_key(existing_keys: set[str]) -> str:
    """Generate a unique 12-char hex key."""
    while True:
        key = uuid.uuid4().hex[:12]
        if key not in existing_keys:
            return key


def planner_input_fingerprint(task: dict[str, Any]) -> str:
    """Hash planner source-of-truth inputs only."""
    payload = {
        "prompt": str(task.get("prompt", "") or ""),
        "depends_on": list(task.get("depends_on") or []),
        "feedback": str(task.get("feedback", "") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


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
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(tasks_path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as handle:
            yaml.dump({"tasks": tasks}, handle, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, tasks_path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def _locked_rw(tasks_path: Path, mutator: Callable[[list[dict[str, Any]]], Any]) -> Any:
    """Read-modify-write tasks.yaml under flock."""
    canonical = tasks_path.resolve()
    lock_path = canonical.parent / ".tasks.lock"
    lock_path.touch()
    with open(lock_path, "r") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = load_tasks(canonical)
        result = mutator(tasks)
        save_tasks(canonical, tasks)
        return result


def _clear_planner_fields(task: dict[str, Any], *, reset_status: bool) -> None:
    task.pop("planner_conflicts", None)
    task.pop("blocked_by", None)
    task.pop("blocked_reason", None)
    if reset_status and task.get("status") in _PLANNER_DERIVED_STATUSES:
        task["status"] = "pending"
        task.pop("error", None)
        task.pop("error_code", None)
        task.pop("completed_at", None)


def _normalize_planner_conflict_entry(
    task_by_key: dict[str, dict[str, Any]],
    entry: Any,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    keys = [str(key) for key in entry.get("tasks") or [] if str(key)]
    if len(keys) < 2:
        return None
    fingerprints = entry.get("fingerprints")
    if not isinstance(fingerprints, dict):
        return None
    for key in keys:
        task = task_by_key.get(key)
        if not task:
            return None
        if str(fingerprints.get(key, "")) != planner_input_fingerprint(task):
            return None
    return {
        "tasks": keys,
        "description": str(entry.get("description", "") or ""),
        "suggestion": str(entry.get("suggestion", "") or ""),
        "fingerprints": {key: str(fingerprints.get(key, "")) for key in keys},
    }


def _recompute_planner_state(tasks: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    task_by_key = {
        str(task.get("key", "") or ""): task
        for task in tasks
        if task.get("key")
    }

    for task in tasks:
        current_fingerprint = planner_input_fingerprint(task)
        if task.get("planner_fingerprint") != current_fingerprint:
            task["planner_fingerprint"] = current_fingerprint
            _clear_planner_fields(task, reset_status=True)

    reverse_deps: dict[str, set[str]] = {}
    id_to_key = {
        int(task["id"]): str(task["key"])
        for task in tasks
        if isinstance(task.get("id"), int) and task.get("key")
    }
    for task in tasks:
        task_key = str(task.get("key", "") or "")
        if not task_key:
            continue
        for dep_id in task.get("depends_on") or []:
            dep_key = id_to_key.get(dep_id)
            if dep_key:
                reverse_deps.setdefault(dep_key, set()).add(task_key)

    direct_conflicts: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        task_key = str(task.get("key", "") or "")
        if not task_key:
            continue
        valid_entries: list[dict[str, Any]] = []
        for entry in task.get("planner_conflicts") or []:
            normalized = _normalize_planner_conflict_entry(task_by_key, entry)
            if normalized and task_key in normalized["tasks"]:
                valid_entries.append(normalized)
        if valid_entries:
            task["planner_conflicts"] = valid_entries
            direct_conflicts[task_key] = valid_entries
        else:
            task.pop("planner_conflicts", None)

    blocked_by: dict[str, set[str]] = {}
    for root_key in direct_conflicts:
        queue = [root_key]
        visited = {root_key}
        while queue:
            parent = queue.pop(0)
            for child_key in reverse_deps.get(parent, set()):
                if child_key in direct_conflicts:
                    continue
                blocked_by.setdefault(child_key, set()).add(root_key)
                if child_key not in visited:
                    visited.add(child_key)
                    queue.append(child_key)

    for task in tasks:
        task_key = str(task.get("key", "") or "")
        if not task_key:
            continue

        if task_key in direct_conflicts and task.get("status") in {"pending", "conflict"}:
            first_conflict = direct_conflicts[task_key][0]
            description = first_conflict.get("description") or "Planner flagged this task as contradictory."
            task["status"] = "conflict"
            task["error"] = description
            task["error_code"] = "planner_conflict"
            task["completed_at"] = task.get("completed_at") or now
            task.pop("blocked_by", None)
            task.pop("blocked_reason", None)
            continue

        if task_key in blocked_by and task.get("status") in {"pending", "blocked"}:
            root_keys = sorted(blocked_by[task_key])
            root_ids = ", ".join(
                f"#{task_by_key[root_key].get('id', '?')}"
                for root_key in root_keys
                if root_key in task_by_key
            )
            reason = f"Blocked by conflicting task(s): {root_ids}" if root_ids else "Blocked by conflicting dependency."
            task["status"] = "blocked"
            task["blocked_by"] = root_keys
            task["blocked_reason"] = reason
            task["error"] = reason
            task["error_code"] = "planner_blocked"
            task["completed_at"] = task.get("completed_at") or now
            continue

        task.pop("blocked_by", None)
        task.pop("blocked_reason", None)
        if task.get("status") in _PLANNER_DERIVED_STATUSES:
            task["status"] = "pending"
            task.pop("error", None)
            task.pop("error_code", None)
            task.pop("completed_at", None)


def mutate_and_recompute(
    tasks_path: Path,
    mutator: Callable[[list[dict[str, Any]]], Any],
) -> Any:
    """Mutate tasks and refresh planner-derived state in one locked transaction."""

    def _mutate(tasks: list[dict[str, Any]]) -> Any:
        result = mutator(tasks)
        _recompute_planner_state(tasks)
        return result

    return _locked_rw(tasks_path, _mutate)


def refresh_planner_state(tasks_path: Path) -> list[dict[str, Any]]:
    """Refresh planner cache fields after manual edits."""
    mutate_and_recompute(tasks_path, lambda tasks: None)
    return load_tasks(tasks_path)


def add_task(
    tasks_path: Path,
    prompt: str,
    verify: str | None = None,
    max_retries: int | None = None,
    spec: list | None = None,
) -> dict[str, Any]:
    """Add a new task to tasks.yaml. Thread-safe via flock."""
    created: dict[str, Any] = {}

    def _add(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        existing_keys = {task["key"] for task in tasks if "key" in task}
        max_id = max((task.get("id", 0) for task in tasks), default=0)
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
        created.update(task)
        return task

    mutate_and_recompute(tasks_path, _add)
    return next(task for task in load_tasks(tasks_path) if task.get("key") == created.get("key"))


def add_tasks(
    tasks_path: Path,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add multiple tasks atomically. Thread-safe via flock."""
    created_keys: list[str] = []

    def _add_batch(tasks: list[dict[str, Any]]) -> None:
        existing_keys = {task["key"] for task in tasks if "key" in task}
        max_id = max((task.get("id", 0) for task in tasks), default=0)
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

        for idx, (item, task) in enumerate(new_tasks):
            dep_indices = item.get("depends_on")
            if not dep_indices:
                continue
            for dep_idx in dep_indices:
                if dep_idx == idx:
                    raise ValueError(f"Task {idx} depends on itself")
                if dep_idx < 0 or dep_idx >= len(batch):
                    raise ValueError(
                        f"Task {idx} depends_on index {dep_idx} out of range [0, {len(batch) - 1}]"
                    )
            task["depends_on"] = [new_tasks[dep_idx][1]["id"] for dep_idx in dep_indices]

        sorter = graphlib.TopologicalSorter()
        for _, task in new_tasks:
            sorter.add(task["id"], *(task.get("depends_on") or []))
        try:
            sorter.prepare()
        except graphlib.CycleError as exc:
            raise ValueError(f"Dependency cycle detected: {exc}") from exc

        for _, task in new_tasks:
            tasks.append(task)
            created_keys.append(task["key"])

    mutate_and_recompute(tasks_path, _add_batch)
    created = {task["key"]: task for task in load_tasks(tasks_path)}
    return [created[key] for key in created_keys]


def update_task(tasks_path: Path, key: str, **updates) -> dict[str, Any]:
    """Update a task by key. Thread-safe via flock. Raises KeyError if not found."""
    if "status" in updates and updates["status"] is not None:
        _validate_task_status(updates["status"])

    def _update(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        for task in tasks:
            if task.get("key") == key:
                for field, value in updates.items():
                    if value is None:
                        task.pop(field, None)
                    else:
                        task[field] = value
                return dict(task)
        raise KeyError(f"Task with key '{key}' not found")

    return mutate_and_recompute(tasks_path, _update)


def delete_task(tasks_path: Path, task_id: int) -> dict[str, Any]:
    """Delete a task by ID. Thread-safe via flock. Raises KeyError if not found."""
    removed: dict[str, Any] = {}

    def _delete(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        for idx, task in enumerate(tasks):
            if task.get("id") == task_id:
                removed.update(task)
                tasks.pop(idx)
                return dict(removed)
        raise KeyError(f"Task #{task_id} not found")

    mutate_and_recompute(tasks_path, _delete)
    return removed


def reset_all_tasks(tasks_path: Path) -> int:
    """Reset all tasks to pending. Thread-safe via flock. Returns count reset."""

    def _reset(tasks: list[dict[str, Any]]) -> int:
        for task in tasks:
            task["status"] = "pending"
            task.pop("attempts", None)
            task.pop("session_id", None)
            task.pop("error", None)
            task.pop("error_code", None)
            task.pop("changed_files", None)
            task.pop("completed_at", None)
            task.pop("planner_conflicts", None)
            task.pop("blocked_by", None)
            task.pop("blocked_reason", None)
        return len(tasks)

    return mutate_and_recompute(tasks_path, _reset)
