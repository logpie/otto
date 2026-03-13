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
from contextlib import suppress
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
from claude_agent_sdk.types import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from verify_utils import _detect_verification

logger = logging.getLogger("worker")


# -- Task queue helpers -------------------------------------------------------

def _locked_task_rw(tasks_file: Path, mutator):
    """Read-modify-write tasks.json under a separate lockfile.

    Uses a dedicated .lock file so that flock operates on a stable inode
    (not the data file which gets replaced). This intentionally uses the
    sibling filename tasks.lock, and both worker and manager MUST use
    that same convention to avoid lost writes.

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


def _parse_task_time(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return time.mktime(time.strptime(timestamp, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _reset_task_for_requeue(task: dict) -> None:
    task["status"] = "pending"
    task["worker"] = None
    task["started_at"] = None
    task["finished_at"] = None
    task["heartbeat_at"] = None
    task["attempts"] = 0
    task["cost_usd"] = 0.0
    task["session_id"] = None
    task.pop("last_error", None)


def pick_task(tasks_file: Path, worker_name: str = "main") -> dict | None:
    """Atomically pick the first pending task and mark it in_progress."""
    picked = [None]

    def _pick(tasks):
        for task in tasks:
            if task["status"] == "pending":
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                task["status"] = "in_progress"
                task["started_at"] = now
                task["heartbeat_at"] = now
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


HEARTBEAT_INTERVAL = 60
# stale_timeout must be well above heartbeat interval to avoid premature requeue
MIN_STALE_TIMEOUT = HEARTBEAT_INTERVAL * 5  # 300s


async def _heartbeat_task(tasks_file: Path, task_id: str, interval: int = HEARTBEAT_INTERVAL) -> None:
    while True:
        await asyncio.sleep(interval)
        update_task(
            tasks_file,
            task_id,
            heartbeat_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )


def requeue_stale_tasks(tasks_file: Path, stale_timeout: int = 1800) -> int:
    """Reset abandoned in_progress tasks back to pending."""
    cutoff = time.time() - stale_timeout
    requeued = 0

    def _requeue(tasks):
        nonlocal requeued
        for task in tasks:
            if task.get("status") != "in_progress":
                continue
            heartbeat_at = _parse_task_time(task.get("heartbeat_at"))
            if heartbeat_at is None:
                heartbeat_at = _parse_task_time(task.get("started_at"))
            if heartbeat_at is None or heartbeat_at >= cutoff:
                continue
            _reset_task_for_requeue(task)
            requeued += 1

    _locked_task_rw(tasks_file, _requeue)
    return requeued


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
        detection = _detect_verification(project_dir)
        if detection["type"] == "none":
            return True, detection["detail"]
        verify_cmd = detection["cmd"]

    try:
        result = subprocess.run(
            verify_cmd,
            shell=True,
            executable="/bin/bash",
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


# -- NL spec → verification script -------------------------------------------

def generate_verify_script(
    project_dir: Path,
    verify_prompt: str,
    task_prompt: str,
    timeout: int = 120,
) -> str | None:
    """One-time call: translate an NL verification spec into a shell command.

    Spawns `claude -p` to inspect the project and produce a deterministic
    verification command. Returns the command string, or None on failure.
    """
    gen_prompt = (
        "You are a QA engineer writing a BEHAVIORAL verification script. "
        "Given a task and verification goal, write a bash script that tests the "
        "actual behavior — NOT by grepping source code for strings.\n\n"
        "Good verifications:\n"
        "- Build the app and check it compiles\n"
        "- Start a server, curl an endpoint, check the response\n"
        "- Run the program and check its output\n"
        "- Time an operation and check it meets a threshold\n"
        "- Run existing tests if they cover the feature\n\n"
        "Bad verifications (DO NOT DO):\n"
        "- grep for function names in source files\n"
        "- Check if certain strings exist in code\n"
        "- Inspect file contents instead of running code\n\n"
        "The script must:\n"
        "- Exit 0 on success, non-zero on failure\n"
        "- Print a clear message on failure\n"
        "- Clean up after itself (kill servers, remove temp files)\n"
        "- Complete within 60 seconds\n"
        "- Be self-contained bash that runs from the project root\n\n"
        f"TASK: {task_prompt}\n\n"
        f"VERIFICATION GOAL: {verify_prompt}\n\n"
        "Look at the project to understand how to build/run it, then output ONLY "
        "the bash script. No explanation, no markdown fences, no commentary."
    )

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["claude", "-p", gen_prompt, "--max-turns", "5"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_dir),
            env=env,
        )
        cmd = result.stdout.strip()
        if not cmd or result.returncode != 0:
            logger.warning(f"  Failed to generate verify script: {result.stderr[:200]}")
            return None
        # Extract code from markdown fences if present (model may add commentary)
        import re
        fence_match = re.search(r"```(?:\w*)\n(.*?)```", cmd, re.DOTALL)
        if fence_match:
            cmd = fence_match.group(1).strip()
        elif "```" in cmd:
            # Fallback: strip all fence lines
            lines = cmd.splitlines()
            lines = [l for l in lines if not l.startswith("```")]
            cmd = "\n".join(lines).strip()
        logger.info(f"  Generated verify command: {cmd[:200]}")
        return cmd
    except FileNotFoundError:
        logger.warning("  claude CLI not found — cannot generate verify script")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("  Timed out generating verify script")
        return None
    except Exception as e:
        logger.warning(f"  Error generating verify script: {e}")
        return None


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
    # If no verify_prompt was given, derive it from the task prompt itself.
    verify_prompt = task.get("verify_prompt") or task.get("verify", "") or ""
    if not verify_prompt:
        verify_prompt = task["prompt"]
        task["verify_prompt"] = verify_prompt
        if tasks_file:
            update_task(tasks_file, task["id"], verify_prompt=verify_prompt)
        logger.info(f"  No verify_prompt set — using task prompt as verification goal")

    # Start verify script generation concurrently with the first attempt.
    # This is a separate claude -p process so it can't be gamed by the agent.
    verify_gen_task = None
    if not task.get("verify_cmd"):
        logger.info(f"  Starting concurrent verify generation: {verify_prompt[:100]}")
        verify_gen_task = asyncio.create_task(
            asyncio.to_thread(
                generate_verify_script,
                project_dir,
                verify_prompt,
                task["prompt"],
            )
        )

    for attempt in range(1, max_retries + 1):
        logger.info(f"Task {task['id']}: attempt {attempt}/{max_retries}")

        # Persist attempt count immediately so UI shows live progress
        heartbeat = None
        if tasks_file:
            update_task(
                tasks_file,
                task["id"],
                attempts=attempt,
                heartbeat_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            heartbeat = asyncio.create_task(_heartbeat_task(tasks_file, task["id"]))

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
            try:
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        result_msg = message
                    elif isinstance(message, AssistantMessage):
                        if message.error:
                            logger.warning(f"  [assistant:error] {message.error}")
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                logger.info(f"  [text] {block.text[:300]}")
                            elif isinstance(block, ToolUseBlock):
                                inp = str(block.input)[:200]
                                logger.info(f"  [tool:call] {block.name}: {inp}")
                            elif isinstance(block, ToolResultBlock):
                                err = " (ERROR)" if block.is_error else ""
                                content = str(block.content)[:200] if block.content else ""
                                logger.info(f"  [tool:result]{err} {content}")
                    elif isinstance(message, UserMessage):
                        # Tool results come back as UserMessage
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                err = " (ERROR)" if block.is_error else ""
                                content = str(block.content)[:200] if block.content else ""
                                logger.info(f"  [tool:result]{err} {content}")
                    elif isinstance(message, SystemMessage):
                        logger.info(f"  [system] {message.subtype}: {str(message.data)[:200]}")
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
                    f"turns={result_msg.num_turns}, "
                    f"cost=${result_msg.total_cost_usd or 0:.4f}, "
                    f"duration={result_msg.duration_ms / 1000:.0f}s, "
                    f"session={result_msg.session_id}"
                )
                # Persist cost and session_id after each attempt
                if tasks_file:
                    update_task(tasks_file, task["id"],
                                cost_usd=total_cost, session_id=session_id)
                if result_msg.subtype != "success":
                    verify_error = (
                        f"Agent session ended with subtype '{result_msg.subtype}'"
                    )
                    logger.warning(f"  Skipping verification: {verify_error}")
                    if tasks_file:
                        update_task(tasks_file, task["id"], last_error=verify_error[:500])
                    continue
            else:
                verify_error = "No ResultMessage received from SDK"
                logger.warning(f"  {verify_error}")
                if tasks_file:
                    update_task(tasks_file, task["id"], last_error=verify_error[:500])
                continue

            # Collect concurrently-generated verify script if ready
            if verify_gen_task and not task.get("verify_cmd"):
                try:
                    generated_cmd = await verify_gen_task
                    if generated_cmd:
                        task["verify_cmd"] = generated_cmd
                        if tasks_file:
                            update_task(tasks_file, task["id"], verify_cmd=generated_cmd)
                        logger.info(f"  Verify script ready: {generated_cmd[:150]}")
                    else:
                        logger.info("  Verify generation returned nothing — using auto-detect")
                except Exception as e:
                    logger.warning(f"  Verify generation failed: {e}")
                verify_gen_task = None  # Don't await again on retry

            # Run verification in a thread so the heartbeat task can
            # continue updating heartbeat_at during long verify commands.
            verify_cmd = task.get("verify_cmd") or None
            verify_timeout = task.get("verify_timeout", 120)
            passed, output = await asyncio.to_thread(
                run_verify, project_dir, verify_cmd, timeout=verify_timeout
            )

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
        finally:
            if heartbeat:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

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
    stale_timeout: int = 1800,
    logs_dir: Path | None = None,
) -> None:
    """Main worker loop: pick tasks, execute, verify, repeat."""
    if stale_timeout < MIN_STALE_TIMEOUT:
        logger.warning(
            f"stale_timeout={stale_timeout}s is below minimum {MIN_STALE_TIMEOUT}s "
            f"(heartbeat interval is {HEARTBEAT_INTERVAL}s). Clamping."
        )
        stale_timeout = MIN_STALE_TIMEOUT

    task_count = 0

    logger.info(f"Worker '{worker_name}' starting")
    logger.info(f"  Tasks file: {tasks_file}")
    logger.info(f"  Project dir: {project_dir}")
    logger.info(f"  Max retries: {max_retries}")
    logger.info(f"  Stale timeout: {stale_timeout}s")

    requeued = requeue_stale_tasks(tasks_file, stale_timeout=stale_timeout)
    if requeued:
        logger.warning(f"Requeued {requeued} stale task(s) on startup")

    while True:
        requeued = requeue_stale_tasks(tasks_file, stale_timeout=stale_timeout)
        if requeued:
            logger.warning(f"Requeued {requeued} stale task(s)")
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
    parser.add_argument("--stale-timeout", type=int, default=1800)
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
            stale_timeout=args.stale_timeout,
            logs_dir=logs_dir,
        )
    )


if __name__ == "__main__":
    main()
