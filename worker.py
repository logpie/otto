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

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

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


# -- Task runner with session resume ------------------------------------------

async def run_task(
    task: dict,
    project_dir: Path,
    max_retries: int = 3,
    tasks_file: Path | None = None,
    logs_dir: Path | None = None,
) -> dict:
    """Run a single task with verify-fix loop and session resume.

    If tasks_file is provided, persists state after each attempt so the
    UI shows live progress (attempts, cost, session_id) via SSE.

    If logs_dir is provided, writes per-task log to logs_dir/{task_id}.log
    so the dashboard Log button shows task-specific output.

    NOTE on cost: ResultMessage.total_cost_usd is per-query (not cumulative
    across resumed sessions), so we sum across attempts.
    """
    session_id = None
    verify_error = ""
    total_cost = 0.0

    # Per-task log file for the dashboard Log button
    task_log_handler = None
    if logs_dir:
        task_log_path = logs_dir / f"{task['id']}.log"
        task_log_handler = logging.FileHandler(task_log_path, mode="a")
        task_log_handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(task_log_handler)
        # Write attempt separator so all attempts are visible in one log
        logger.info(f"{'='*60}")
        logger.info(f"TASK {task['id']} — START")
        logger.info(f"{'='*60}")

    try:
        return await _run_task_inner(
            task, project_dir, max_retries, tasks_file,
            session_id, verify_error, total_cost,
        )
    finally:
        if task_log_handler:
            logger.removeHandler(task_log_handler)
            task_log_handler.close()


async def _run_task_inner(
    task, project_dir, max_retries, tasks_file,
    session_id, verify_error, total_cost,
):
    for attempt in range(1, max_retries + 1):
        logger.info(f"Task {task['id']}: attempt {attempt}/{max_retries}")

        # Persist attempt count immediately so UI shows live progress
        if tasks_file:
            update_task(tasks_file, task["id"], attempts=attempt)

        prompt = build_prompt(task, attempt, verify_error)

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            max_turns=50,
            max_budget_usd=5.0,
            cwd=str(project_dir),
            setting_sources=["project"],
        )
        if session_id and attempt > 1:
            options.resume = session_id

        result_msg = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result_msg = message
        except Exception as e:
            logger.error(f"  SDK error: {e}")
            verify_error = f"Claude Code process error: {e}"
            if tasks_file:
                update_task(tasks_file, task["id"],
                            last_error=str(e), cost_usd=total_cost)
            continue

        if result_msg:
            session_id = result_msg.session_id
            if result_msg.total_cost_usd:
                total_cost += result_msg.total_cost_usd
            logger.info(
                f"  Claude finished: {result_msg.subtype}, "
                f"cost=${result_msg.total_cost_usd or 0:.4f}"
            )
            # Persist cost and session_id after each attempt
            if tasks_file:
                update_task(tasks_file, task["id"],
                            cost_usd=total_cost, session_id=session_id)
        else:
            logger.warning("  No ResultMessage received from SDK")

        # Run verification
        verify_cmd = task.get("verify_cmd") or None
        verify_timeout = task.get("verify_timeout", 120)
        passed, output = run_verify(project_dir, verify_cmd, timeout=verify_timeout)

        if passed:
            logger.info("  Verification PASSED")
            return {
                "status": "completed",
                "attempts": attempt,
                "cost_usd": total_cost,
                "session_id": session_id,
            }

        verify_error = output
        logger.info(f"  Verification FAILED: {output[:200]}")
        if tasks_file:
            update_task(tasks_file, task["id"], last_error=output[:500])

    return {
        "status": "failed",
        "attempts": max_retries,
        "cost_usd": total_cost,
        "session_id": session_id,
    }


# -- Main loop ----------------------------------------------------------------

async def worker_loop(
    tasks_file: Path,
    project_dir: Path,
    worker_name: str = "main",
    poll_interval: int = 30,
    max_tasks: int = 0,
    max_retries: int = 3,
    logs_dir: Path | None = None,
) -> None:
    """Main worker loop: pick tasks, execute, verify, repeat."""
    task_count = 0

    logger.info(f"Worker '{worker_name}' starting")
    logger.info(f"  Tasks file: {tasks_file}")
    logger.info(f"  Project dir: {project_dir}")
    logger.info(f"  Max retries: {max_retries}")

    while True:
        task = pick_task(tasks_file, worker_name)
        if not task:
            logger.info(f"No pending tasks. Waiting {poll_interval}s...")
            await asyncio.sleep(poll_interval)
            continue

        task_count += 1
        logger.info(f"Task #{task_count} - ID: {task['id']}")
        logger.info(f"  Prompt: {task['prompt'][:100]}")

        # Per-task max_retries overrides worker default; sanitize for bad stored values
        try:
            effective_retries = max(1, min(int(task.get("max_retries", max_retries)), 10))
        except (ValueError, TypeError):
            effective_retries = max_retries
        result = await run_task(
            task, project_dir, effective_retries,
            tasks_file=tasks_file, logs_dir=logs_dir,
        )

        update_task(
            tasks_file,
            task["id"],
            status=result["status"],
            attempts=result["attempts"],
            cost_usd=result.get("cost_usd", 0),
            session_id=result.get("session_id"),
        )

        logger.info(
            f"Task {task['id']}: {result['status']} "
            f"(attempt {result['attempts']}/{max_retries}, "
            f"${result.get('cost_usd', 0):.4f})"
        )

        if max_tasks > 0 and task_count >= max_tasks:
            logger.info(f"Reached max tasks ({max_tasks}). Exiting.")
            break

    logger.info("Worker loop finished.")


# -- CLI entry point ----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CC Autonomous Worker v2")
    parser.add_argument("--tasks-file", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--worker-name", default="main")
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file))

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # logs_dir defaults to same directory as tasks_file / "logs"
    logs_dir = args.tasks_file.parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    asyncio.run(
        worker_loop(
            tasks_file=args.tasks_file,
            project_dir=args.project_dir,
            worker_name=args.worker_name,
            poll_interval=args.poll_interval,
            max_tasks=args.max_tasks,
            max_retries=args.max_retries,
            logs_dir=logs_dir,
        )
    )


if __name__ == "__main__":
    main()
