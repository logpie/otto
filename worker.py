"""
CC Autonomous Worker v2 — Python worker using claude-agent-sdk.

Replaces ralph-loop.sh with:
- Session resume on retries (agent keeps context)
- Orchestrator-owned verification (agent never touches test harness)
- Cost tracking per task

Usage:
    python worker.py --tasks-file tasks.json --project-dir /path/to/project [options]
"""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("worker")


# -- Task queue helpers -------------------------------------------------------

def _locked_task_rw(tasks_file: Path, mutator):
    """Read-modify-write tasks.json under a separate lockfile.

    Uses a dedicated .lock file so that flock operates on a stable inode
    (not the data file which gets replaced). Both worker and manager
    MUST use this same pattern to avoid lost writes.

    Args:
        tasks_file: Path to tasks.json
        mutator: callable(tasks: list[dict]) -> Any. Mutates tasks in place.
                 Return value is passed through.
    """
    lock_path = tasks_file.with_suffix(".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = json.loads(tasks_file.read_text()) if tasks_file.exists() else []
        result = mutator(tasks)
        # Atomic write: temp file + os.replace
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(tasks_file.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(tasks, f, indent=2)
            os.replace(tmp_path, str(tasks_file))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return result


def pick_task(tasks_file: Path, worker_name: str = "main") -> dict | None:
    """Atomically pick the first pending task and mark it in_progress."""
    picked = [None]

    def _pick(tasks):
        for task in tasks:
            if task["status"] == "pending":
                task["status"] = "in_progress"
                task["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                task["worker"] = worker_name
                task.setdefault("attempts", 0)
                picked[0] = dict(task)  # snapshot
                return
    _locked_task_rw(tasks_file, _pick)
    return picked[0]


def update_task(tasks_file: Path, task_id: str, **updates) -> None:
    """Atomically update a task's fields."""
    def _update(tasks):
        for task in tasks:
            if task["id"] == task_id:
                task.update(updates)
                if updates.get("status") in ("completed", "failed"):
                    task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                break
    _locked_task_rw(tasks_file, _update)
