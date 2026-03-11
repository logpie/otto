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


# -- Prompt builder -----------------------------------------------------------

# NOTE: The old ralph-loop.sh included "VERIFICATION AUTHORING RULES" telling the
# agent to write/update verify.sh. Those are intentionally dropped in v2 because
# verification is now orchestrator-owned — the agent never touches the test harness.
_AUTONOMY_RULES = """\
RULES:
- NEVER ask questions or present options. Make the best decision and do it.
- If something fails, fix it yourself. Try alternative approaches.
- If an API is down or unreachable, switch to a working alternative.
- After making changes, run the tests/app yourself to verify it works.
- Do NOT stop until you have verified your changes work end-to-end.
- Then commit with a descriptive message.
- Do NOT add result caching or memoization to pass speed tests. Optimize the actual code path."""


def build_prompt(task: dict, attempt: int = 1, verify_error: str = "") -> str:
    """Build the prompt for Claude, including retry context if applicable."""
    # Support both old 'verify' field and new 'verify_prompt' field
    verify_goal = task.get("verify_prompt") or task.get("verify", "")
    verify_section = ""
    if verify_goal:
        verify_section = f"\nVERIFICATION GOAL:\n{verify_goal}\n"

    if attempt == 1:
        return (
            f"You are running autonomously with NO human in the loop.\n\n"
            f"{_AUTONOMY_RULES}\n"
            f"{verify_section}\n"
            f"TASK: {task['prompt']}"
        )
    else:
        return (
            f"You are running autonomously with NO human in the loop. "
            f"This is attempt {attempt}.\n\n"
            f"Your previous attempt FAILED verification. "
            f"Here is the error output:\n\n"
            f"--- VERIFICATION ERROR ---\n"
            f"{verify_error}\n"
            f"--- END ERROR ---\n\n"
            f"{_AUTONOMY_RULES}\n"
            f"{verify_section}\n"
            f"ORIGINAL TASK: {task['prompt']}"
        )


# -- Verification runner ------------------------------------------------------

def run_verify(
    project_dir: Path,
    verify_cmd: str | None = None,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Run verification command. Returns (passed, output).

    Priority:
    1. Explicit verify_cmd if provided
    2. Project verify.sh if it exists (backward compat with v1)
    3. Auto-detect test files (pytest, then unittest)
    4. Pass by default if nothing configured

    NOTE: The orchestrator runs verify.sh, but the agent does NOT write it.
    Existing verify.sh scripts from v1 are still honored.
    """
    if not verify_cmd:
        # Check for existing verify.sh (backward compat)
        verify_script = project_dir / "verify.sh"
        if verify_script.exists() and os.access(verify_script, os.X_OK):
            verify_cmd = "bash verify.sh"
        else:
            # Auto-detect test files (including one level of subdirectories)
            test_files = (
                list(project_dir.glob("test_*.py"))
                + list(project_dir.glob("*_test.py"))
                + list(project_dir.glob("*/test_*.py"))
                + list(project_dir.glob("*/*_test.py"))
            )
            if test_files:
                verify_cmd = f"{sys.executable} -m pytest -x --tb=short"
            else:
                return True, "No verification configured"

    try:
        result = subprocess.run(
            verify_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_dir),
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Verification timed out after {timeout}s"
    except Exception as e:
        return False, f"Verification error: {e}"
