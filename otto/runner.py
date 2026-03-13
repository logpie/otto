"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import fcntl
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query

from otto.config import git_meta_dir, load_config
from otto.tasks import load_tasks, update_task
from otto.testgen import generate_tests, detect_test_framework, test_file_path
from otto.verify import run_verification

logger = logging.getLogger("otto.runner")


def check_clean_tree(project_dir: Path) -> bool:
    """Check if the working tree is clean (no uncommitted changes)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and not result.stdout.strip()


def create_task_branch(
    project_dir: Path, key: str, default_branch: str,
    task: dict[str, Any] | None = None,
) -> str:
    """Create otto/<key> branch. Returns base SHA.

    If branch exists and was preserved from a diverge failure, raises RuntimeError.
    Otherwise deletes stale branch and recreates.
    """
    branch_name = f"otto/{key}"

    # Check if branch exists
    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=project_dir,
        capture_output=True,
    )
    if check.returncode == 0:
        # Check if this was preserved from a diverge failure (structured error_code)
        if task and task.get("status") == "failed" and task.get("error_code") == "merge_diverged":
            raise RuntimeError(
                f"Branch otto/{key} preserved from diverge failure — "
                f"manually resolve or run 'otto reset' first"
            )
        # Delete stale branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=project_dir,
            capture_output=True,
        )

    # Record base SHA
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Create and checkout new branch
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    return base_sha


def build_candidate_commit(
    project_dir: Path,
    base_sha: str,
    testgen_file: Path | None,
) -> str:
    """Build a candidate commit with agent changes + generated test."""
    # If agent made commits, squash them
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    if head != base_sha:
        # Agent made commits — squash
        subprocess.run(
            ["git", "reset", "--mixed", base_sha],
            cwd=project_dir, capture_output=True, check=True,
        )

    # Stage all agent changes explicitly (never git add -A per spec)
    # Stage modified/deleted tracked files
    subprocess.run(
        ["git", "add", "-u"],
        cwd=project_dir, capture_output=True, check=True,
    )
    # Stage untracked files (excluding ignored via .git/info/exclude)
    # Use -z for null-terminated output to handle filenames with special chars
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    for f in untracked.stdout.split("\0"):
        if f:
            subprocess.run(
                ["git", "add", "--", f],
                cwd=project_dir, capture_output=True,
            )

    # Copy testgen file into project if available
    if testgen_file and testgen_file.exists():
        framework = detect_test_framework(project_dir) or "pytest"
        rel_path = test_file_path(framework, testgen_file.stem.replace("otto_verify_", ""))
        dest = project_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(testgen_file, dest)
        subprocess.run(
            ["git", "add", str(rel_path)],
            cwd=project_dir, capture_output=True, check=True,
        )

    # Create candidate commit
    subprocess.run(
        ["git", "commit", "-m", "otto: candidate commit", "--allow-empty"],
        cwd=project_dir, capture_output=True, check=True,
    )

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()


def merge_to_default(project_dir: Path, key: str, default_branch: str) -> bool:
    """Fast-forward merge task branch to default branch. Returns True on success."""
    branch_name = f"otto/{key}"
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, check=True,
    )
    result = subprocess.run(
        ["git", "merge", "--ff-only", branch_name],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode == 0:
        # Delete merged branch
        subprocess.run(
            ["git", "branch", "-d", branch_name],
            cwd=project_dir, capture_output=True,
        )
        return True
    # Merge failed (branch diverged) — stay on default branch, preserve task branch
    return False


def cleanup_branch(project_dir: Path, key: str, default_branch: str = "main") -> None:
    """Delete a task branch. Checks out default_branch if on the task branch."""
    branch_name = f"otto/{key}"
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current == branch_name:
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True,
        )
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_dir, capture_output=True,
    )


async def run_task(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
) -> bool:
    """Run a single task through the full loop. Returns True if passed."""
    key = task["key"]
    task_id = task["id"]
    prompt = task["prompt"]
    max_retries = task.get("max_retries", config["max_retries"])
    verify_cmd = task.get("verify")
    test_command = config.get("test_command")
    default_branch = config["default_branch"]
    timeout = config["verify_timeout"]

    # Create branch
    base_sha = create_task_branch(project_dir, key, default_branch, task=task)
    if tasks_file:
        update_task(tasks_file, key, status="running", attempts=0)

    # Start testgen concurrently
    testgen_task = asyncio.create_task(
        asyncio.to_thread(generate_tests, prompt, project_dir, key)
    )

    # Setup log directory
    log_dir = project_dir / "otto_logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)

    session_id = None
    last_error = None  # verification failure output for retry feedback
    for attempt in range(max_retries + 1):
        attempt_num = attempt + 1
        logger.info(f"Task #{task_id} ({key}) — attempt {attempt_num}/{max_retries + 1}")

        if tasks_file:
            update_task(tasks_file, key, attempts=attempt_num)

        # Build agent prompt — on retries, use verification failure feedback
        if attempt == 0 or last_error is None:
            agent_prompt = (
                f"{prompt}\n\nYou are working in {project_dir}. Do NOT create git commits."
            )
        else:
            agent_prompt = (
                f"Verification failed. Fix the issue.\n\n"
                f"{last_error}\n\n"
                f"Original task: {prompt}\n\n"
                f"You are working in {project_dir}. Do NOT create git commits."
            )

        try:
            options = ClaudeAgentOptions(
                prompt=agent_prompt,
                options={
                    "dangerously_skip_permissions": True,
                    "cwd": str(project_dir),
                    "model": config["model"],
                },
            )
            if session_id:
                options.options["resume"] = session_id

            result = query(options)

            # Extract session_id for resume
            if hasattr(result, "session_id"):
                session_id = result.session_id
                if tasks_file:
                    update_task(tasks_file, key, session_id=session_id)

        except Exception as e:
            logger.error(f"Agent error: {e}")
            continue

        # Await testgen on first attempt
        testgen_file = None
        if attempt == 0:
            try:
                testgen_file = await asyncio.wait_for(testgen_task, timeout=120)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Testgen failed or timed out: {e}")
        else:
            if testgen_task.done():
                testgen_file = testgen_task.result()

        # Build candidate commit
        candidate_sha = build_candidate_commit(project_dir, base_sha, testgen_file)

        # Run verification in disposable worktree
        verify_result = run_verification(
            project_dir=project_dir,
            candidate_sha=candidate_sha,
            test_command=test_command,
            testgen_file=testgen_file,
            verify_cmd=verify_cmd,
            timeout=timeout,
        )

        # Write verification log
        verify_log = log_dir / f"attempt-{attempt_num}-verify.log"
        verify_log.write_text(
            "\n".join(f"{t.tier}: {'PASS' if t.passed else 'FAIL'}\n{t.output}"
                      for t in verify_result.tiers)
        )

        if verify_result.passed:
            # Amend commit message
            subprocess.run(
                ["git", "commit", "--amend", "-m",
                 f"otto: {prompt[:60]} (#{task_id})"],
                cwd=project_dir, capture_output=True,
            )
            # Merge to default
            if merge_to_default(project_dir, key, default_branch):
                # Clean testgen artifacts (test file is now in the repo)
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                if tasks_file:
                    update_task(tasks_file, key, status="passed")
                logger.info(f"Task #{task_id} PASSED — merged to {default_branch}")
                return True
            else:
                # Clean testgen artifacts (test file is in the preserved branch)
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                if tasks_file:
                    update_task(
                        tasks_file, key, status="failed",
                        error=f"branch diverged — otto/{key} preserved, manual rebase needed",
                        error_code="merge_diverged",
                    )
                logger.error(f"Task #{task_id} — merge failed (branch diverged)")
                return False
        else:
            # Verification failed — unwind candidate commit for retry
            subprocess.run(
                ["git", "reset", "--mixed", "HEAD~1"],
                cwd=project_dir, capture_output=True,
            )
            last_error = verify_result.failure_output
            logger.warning(
                f"Task #{task_id} attempt {attempt_num} — verification failed"
            )

    # All retries exhausted
    subprocess.run(["git", "reset", "--hard"], cwd=project_dir, capture_output=True)
    # Remove untracked agent artifacts (respects .git/info/exclude so runtime
    # files like tasks.yaml, otto_logs/ are preserved)
    subprocess.run(["git", "clean", "-fd"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True,
    )
    cleanup_branch(project_dir, key, default_branch)
    # Clean up testgen artifacts for this task
    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    if testgen_dir.exists():
        shutil.rmtree(testgen_dir, ignore_errors=True)
    if tasks_file:
        update_task(
            tasks_file, key, status="failed",
            error="max retries exhausted", error_code="max_retries",
        )
    logger.error(f"Task #{task_id} FAILED — all retries exhausted")
    return False


async def run_all(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """Run all pending tasks. Returns exit code (0=all passed, 1=any failed).

    Lock order: otto.lock (process) → .tasks.lock (CRUD).
    All commands that acquire both must follow this order to prevent deadlocks.
    """
    default_branch = config["default_branch"]

    # Acquire process lock — use canonical git metadata dir (shared across linked worktrees)
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another otto process is running")
        return 2

    # Signal handling — set flag, cleanup in main loop (async-safe)
    current_task_key = None
    interrupted = False

    def _signal_handler(signum, frame):
        nonlocal interrupted
        interrupted = True

    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    try:
        # Baseline check
        test_command = config.get("test_command")
        if test_command:
            logger.info("Running baseline check...")
            result = subprocess.run(
                test_command, shell=True, cwd=project_dir,
                capture_output=True, timeout=config["verify_timeout"],
            )
            if result.returncode != 0:
                logger.error("Baseline tests failing — fix before running otto")
                return 2

        # Process tasks
        tasks = load_tasks(tasks_file)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            logger.info("No pending tasks")
            return 0

        any_failed = False
        for task in pending:
            if interrupted:
                logger.warning("Interrupted — cleaning up")
                break
            current_task_key = task["key"]
            if not check_clean_tree(project_dir):
                logger.error("Working tree is dirty — aborting")
                return 2
            success = await run_task(task, config, project_dir, tasks_file)
            if not success:
                any_failed = True
            current_task_key = None

        # Cleanup on interruption
        if interrupted and current_task_key:
            subprocess.run(["git", "reset", "--hard"], cwd=project_dir, capture_output=True)
            subprocess.run(
                ["git", "checkout", default_branch],
                cwd=project_dir, capture_output=True,
            )
            cleanup_branch(project_dir, current_task_key, default_branch)
            if tasks_file:
                try:
                    update_task(
                        tasks_file, current_task_key,
                        status="failed", error="interrupted",
                        error_code="interrupted",
                    )
                except Exception:
                    pass
            return 1

        return 1 if any_failed else 0

    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
