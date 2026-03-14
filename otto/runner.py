"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import (
        AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
    )
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    AssistantMessage = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]
    ToolResultBlock = None  # type: ignore[assignment,misc]

from otto.config import git_meta_dir
from otto.tasks import load_tasks, update_task
from otto.testgen import generate_tests, detect_test_framework, test_file_path
from otto.verify import run_verification, _subprocess_env




def check_clean_tree(project_dir: Path) -> bool:
    """Check that tracked files have no uncommitted changes.

    Only checks tracked files — untracked files are fine (the agent may need
    to coexist with user files like import configs, scratch notes, etc.).
    Otto runtime files (tasks.yaml, .tasks.lock) are also ignored since otto
    modifies them during runs.
    """
    # -uno: suppress untracked files entirely
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uno"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    otto_runtime = {"tasks.yaml", ".tasks.lock"}
    for line in result.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            return False
        filename = parts[1].strip('"')
        if filename not in otto_runtime:
            return False
    return True


def _snapshot_untracked(project_dir: Path) -> set[str]:
    """Return the set of currently untracked files (excluding ignored).

    Used before agent runs so build_candidate_commit can distinguish
    pre-existing untracked files from agent-created ones.
    """
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    return {f for f in result.stdout.split("\0") if f}


def create_task_branch(
    project_dir: Path, key: str, default_branch: str,
    task: dict[str, Any] | None = None,
) -> str:
    """Create otto/<key> branch. Returns base SHA.

    If branch exists and was preserved from a diverge failure, raises RuntimeError.
    Otherwise deletes stale branch and recreates.
    """
    branch_name = f"otto/{key}"

    # Ensure we're on the default branch before branching
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current != default_branch:
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True, check=True,
        )

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
    pre_existing_untracked: set[str] | None = None,
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
    # Stage agent-created untracked files (excluding ignored and pre-existing)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    skip = pre_existing_untracked or set()
    for f in untracked.stdout.split("\0"):
        if f and f not in skip:
            subprocess.run(
                ["git", "add", "--", f],
                cwd=project_dir, capture_output=True,
            )

    # Copy testgen file into project if available
    if testgen_file and testgen_file.exists():
        framework = detect_test_framework(project_dir) or "pytest"
        # Use test_file_path to get the directory, but preserve the original filename
        # to avoid double-suffix issues (e.g. .test.test.js)
        placeholder_path = test_file_path(framework, "placeholder")
        dest_dir = project_dir / placeholder_path.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        rel_path = placeholder_path.parent / testgen_file.name
        dest = project_dir / rel_path
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


def _cleanup_task_failure(
    project_dir: Path,
    key: str,
    default_branch: str,
    tasks_file: Path | None,
    error: str = "unknown",
    error_code: str = "unknown",
) -> None:
    """Unified cleanup for all task failure paths: retries exhausted, interruption, exceptions."""
    subprocess.run(["git", "reset", "--hard"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True,
    )
    cleanup_branch(project_dir, key, default_branch)
    # Clean testgen artifacts
    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    if testgen_dir.exists():
        shutil.rmtree(testgen_dir, ignore_errors=True)
    if tasks_file:
        try:
            update_task(
                tasks_file, key,
                status="failed", error=error, error_code=error_code,
            )
        except Exception:
            pass


# ANSI color codes
_DIM = "\033[2m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _log_info(msg: str) -> None:
    print(f"{_DIM}{'─' * 60}{_RESET}", flush=True)
    print(f"  {msg}", flush=True)


def _log_task_start(task_id: int, key: str, attempt: int, max_attempts: int, prompt: str) -> None:
    print(flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)
    print(f"{_BOLD}  Task #{task_id}{_RESET}  {prompt[:50]}", flush=True)
    print(f"  {_DIM}attempt {attempt}/{max_attempts}  ·  key {key}{_RESET}", flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"


def _log_pass(task_id: int, branch: str, duration: float | None = None) -> None:
    dur = f" in {_format_duration(duration)}" if duration else ""
    print(f"\n  {_GREEN}{_BOLD}✓ Task #{task_id} PASSED{_RESET} {_DIM}— merged to {branch}{dur}{_RESET}", flush=True)


def _log_fail(task_id: int, reason: str, duration: float | None = None) -> None:
    dur = f" in {_format_duration(duration)}" if duration else ""
    print(f"\n  {_RED}{_BOLD}✗ Task #{task_id} FAILED{_RESET} {_DIM}— {reason}{dur}{_RESET}", flush=True)


def _log_warn(msg: str) -> None:
    print(f"  {_YELLOW}⚠ {msg}{_RESET}", flush=True)


def _log_verify(tiers: list) -> None:
    """Print verification results inline."""
    for t in tiers:
        if t.skipped:
            continue
        icon = f"{_GREEN}✓{_RESET}" if t.passed else f"{_RED}✗{_RESET}"
        print(f"  {icon} {t.tier}", flush=True)


_GREEN_DIM = "\033[32;2m"
_RED_DIM = "\033[31;2m"


def _print_tool_use(block) -> None:
    """Print a tool use block like the Claude TUI."""
    name = block.name
    inputs = block.input or {}

    # Format like: ● Read  bookmarks/store.py
    label = f"{_CYAN}{_BOLD}● {name}{_RESET}"

    # Show key argument inline based on tool type
    detail = ""
    if name in ("Read", "Glob", "Grep"):
        detail = inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
    elif name in ("Edit", "Write"):
        detail = inputs.get("file_path") or ""
    elif name == "Bash":
        cmd = inputs.get("command") or ""
        detail = cmd[:80] + ("..." if len(cmd) > 80 else "")

    if detail:
        print(f"  {label}  {_DIM}{detail}{_RESET}", flush=True)
    else:
        print(f"  {label}", flush=True)

    # Show edit diff for Edit tool
    if name == "Edit":
        old = inputs.get("old_string", "")
        new = inputs.get("new_string", "")
        if old or new:
            for line in old.splitlines()[:3]:
                print(f"    {_RED_DIM}- {line}{_RESET}", flush=True)
            if old.count("\n") > 3:
                print(f"    {_DIM}  ... ({old.count(chr(10)) - 3} more lines){_RESET}", flush=True)
            for line in new.splitlines()[:3]:
                print(f"    {_GREEN_DIM}+ {line}{_RESET}", flush=True)
            if new.count("\n") > 3:
                print(f"    {_DIM}  ... ({new.count(chr(10)) - 3} more lines){_RESET}", flush=True)

    # Show content preview for Write tool
    elif name == "Write":
        content = inputs.get("content", "")
        if content:
            lines = content.splitlines()
            for line in lines[:3]:
                print(f"    {_GREEN_DIM}+ {line}{_RESET}", flush=True)
            if len(lines) > 3:
                print(f"    {_DIM}  ... ({len(lines) - 3} more lines){_RESET}", flush=True)


def _tool_use_summary(block) -> str:
    """Return a one-line summary of a tool use for logging."""
    inputs = block.input or {}
    name = block.name
    if name in ("Read", "Glob", "Grep"):
        return inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
    elif name in ("Edit", "Write"):
        return inputs.get("file_path") or ""
    elif name == "Bash":
        cmd = inputs.get("command") or ""
        return cmd[:120]
    return ""


def _print_tool_result(block) -> None:
    """Print tool result — truncated output for success, full for errors."""
    content = block.content if isinstance(block.content, str) else str(block.content)
    if not content.strip():
        return
    if block.is_error:
        lines = content.strip().splitlines()
        # Show last few lines of error (most useful part)
        shown = lines[-5:] if len(lines) > 5 else lines
        for line in shown:
            print(f"    {_RED}{line}{_RESET}", flush=True)
    else:
        lines = content.strip().splitlines()
        if len(lines) <= 3:
            for line in lines:
                print(f"    {_DIM}{line}{_RESET}", flush=True)
        else:
            # Show first 2 + last line with count
            print(f"    {_DIM}{lines[0]}{_RESET}", flush=True)
            print(f"    {_DIM}{lines[1]}{_RESET}", flush=True)
            print(f"    {_DIM}... ({len(lines) - 3} more lines){_RESET}", flush=True)
            print(f"    {_DIM}{lines[-1]}{_RESET}", flush=True)


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

    # Snapshot pre-existing untracked files so we don't sweep them into the commit
    pre_existing_untracked = _snapshot_untracked(project_dir)

    task_start = time.monotonic()

    # Create branch
    base_sha = create_task_branch(project_dir, key, default_branch, task=task)
    if tasks_file:
        update_task(tasks_file, key, status="running", attempts=0)

    # Start testgen concurrently
    rubric = task.get("rubric")
    if rubric:
        print(f"  {_DIM}Generating rubric tests ({len(rubric)} criteria)...{_RESET}", flush=True)
        from otto.testgen import generate_tests_from_rubric
        testgen_task = asyncio.create_task(
            asyncio.to_thread(generate_tests_from_rubric, rubric, prompt, project_dir, key)
        )
    else:
        print(f"  {_DIM}Generating adversarial tests...{_RESET}", flush=True)
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
        _log_task_start(task_id, key, attempt_num, max_retries + 1, prompt)

        if tasks_file:
            update_task(tasks_file, key, attempts=attempt_num)

        # Build agent prompt — include user feedback and/or verification errors
        feedback = task.get("feedback", "")
        if attempt == 0 or last_error is None:
            base_prompt = prompt
            if feedback:
                base_prompt = f"{prompt}\n\nIMPORTANT feedback from the user:\n{feedback}"
            agent_prompt = (
                f"{base_prompt}\n\n"
                f"You are working in {project_dir}. Do NOT create git commits. "
                f"Do NOT write tests — acceptance tests will be generated separately."
            )
        else:
            agent_prompt = (
                f"Verification failed. Fix the issue.\n\n"
                f"{last_error}\n\n"
                f"Original task: {prompt}\n\n"
                f"You are working in {project_dir}. Do NOT create git commits. "
                f"Do NOT write tests — acceptance tests will be generated separately. "
                f"You may edit any file in the project to make all tests pass."
            )

        # Run agent + build candidate + verify — catch infrastructure failures
        try:
            try:
                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(project_dir),
                )
                if config.get("model"):
                    agent_opts.model = config["model"]
                if session_id:
                    agent_opts.resume = session_id

                # query() is async iterator — stream messages, keep last ResultMessage
                agent_log_lines: list[str] = []
                result_msg = None
                async for message in query(prompt=agent_prompt, options=agent_opts):
                    if isinstance(message, ResultMessage):
                        result_msg = message
                    elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                        # Duck-type check for ResultMessage (mocks, stub)
                        result_msg = message
                    elif AssistantMessage and isinstance(message, AssistantMessage):
                        for block in message.content:
                            if TextBlock and isinstance(block, TextBlock) and block.text:
                                print(block.text, flush=True)
                                agent_log_lines.append(block.text)
                            elif ToolUseBlock and isinstance(block, ToolUseBlock):
                                _print_tool_use(block)
                                agent_log_lines.append(f"● {block.name}  {_tool_use_summary(block)}")
                            elif ToolResultBlock and isinstance(block, ToolResultBlock):
                                _print_tool_result(block)
                                content = block.content if isinstance(block.content, str) else str(block.content)
                                if content.strip():
                                    prefix = "ERROR: " if block.is_error else ""
                                    agent_log_lines.append(f"  {prefix}{content[:500]}")

                # Persist agent log
                try:
                    agent_log = log_dir / f"attempt-{attempt_num}-agent.log"
                    agent_log.write_text("\n".join(agent_log_lines))
                except OSError:
                    pass

                # Extract session_id for resume
                if result_msg and getattr(result_msg, "session_id", None):
                    session_id = result_msg.session_id
                    if tasks_file:
                        update_task(tasks_file, key, session_id=session_id)

                # Check if agent reported an error
                if result_msg and result_msg.is_error:
                    raise RuntimeError(f"Agent error: {result_msg.result or 'unknown'}")

            except Exception as e:
                _log_warn(f"Agent error: {e}")
                # Reset workspace to base state before retrying
                subprocess.run(
                    ["git", "reset", "--hard", base_sha],
                    cwd=project_dir, capture_output=True,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=project_dir, capture_output=True,
                )
                continue

            # Await testgen on first attempt
            testgen_file = None
            if attempt == 0:
                try:
                    testgen_file = await asyncio.wait_for(testgen_task, timeout=120)
                    if testgen_file:
                        print(f"  {_GREEN}✓{_RESET} {_DIM}Rubric tests ready{_RESET}", flush=True)
                    else:
                        print(f"  {_DIM}No rubric tests generated (tier 2 skipped){_RESET}", flush=True)
                except (asyncio.TimeoutError, Exception) as e:
                    _log_warn(f"Testgen failed or timed out: {e}")
            else:
                if testgen_task.done():
                    testgen_file = testgen_task.result()

            # Check if agent made any changes
            diff_check = subprocess.run(
                ["git", "diff", "--quiet", base_sha],
                cwd=project_dir, capture_output=True,
            )
            untracked_check = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=project_dir, capture_output=True, text=True,
            )
            new_untracked = {f for f in untracked_check.stdout.strip().splitlines() if f} - pre_existing_untracked
            no_changes = diff_check.returncode == 0 and not new_untracked

            if no_changes and not testgen_file:
                # No code changes and no rubric tests — nothing to commit
                print(f"  {_DIM}No changes needed{_RESET}", flush=True)
                subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
                cleanup_branch(project_dir, key, default_branch)
                if tasks_file:
                    update_task(tasks_file, key, status="passed")
                _log_pass(task_id, default_branch, time.monotonic() - task_start)
                return True

            # Build candidate commit
            candidate_sha = build_candidate_commit(
                project_dir, base_sha, testgen_file, pre_existing_untracked
            )

            # Run verification in disposable worktree
            verify_result = run_verification(
                project_dir=project_dir,
                candidate_sha=candidate_sha,
                test_command=test_command,
                verify_cmd=verify_cmd,
                timeout=timeout,
            )

        except Exception as e:
            # Unexpected error during agent/candidate/verify phases — safe to clean up
            _log_fail(task_id, f"unexpected error: {e}", time.monotonic() - task_start)
            _cleanup_task_failure(
                project_dir, key, default_branch, tasks_file,
                error=f"unexpected error: {e}", error_code="internal_error",
            )
            return False

        # Write verification log (non-critical, best-effort)
        try:
            verify_log = log_dir / f"attempt-{attempt_num}-verify.log"
            verify_log.write_text(
                "\n".join(f"{t.tier}: {'PASS' if t.passed else 'FAIL'}\n{t.output}"
                          for t in verify_result.tiers)
            )
        except OSError as e:
            pass  # best-effort log write

        _log_verify(verify_result.tiers)

        if verify_result.passed:
            # Amend commit message (pre-merge, so cleanup is still safe)
            try:
                amend_result = subprocess.run(
                    ["git", "commit", "--amend", "-m",
                     f"otto: {prompt[:60]} (#{task_id})"],
                    cwd=project_dir, capture_output=True, text=True,
                    check=True,
                )
            except (subprocess.CalledProcessError, Exception) as e:
                stderr = getattr(e, "stderr", str(e))
                _log_fail(task_id, f"commit amend failed: {stderr}", time.monotonic() - task_start)
                _cleanup_task_failure(
                    project_dir, key, default_branch, tasks_file,
                    error=f"commit amend failed: {stderr}", error_code="internal_error",
                )
                return False
            # Merge to default — post-merge bookkeeping errors are non-destructive
            if merge_to_default(project_dir, key, default_branch):
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                if tasks_file:
                    update_task(tasks_file, key, status="passed")
                _log_pass(task_id, default_branch, time.monotonic() - task_start)
                return True
            else:
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                if tasks_file:
                    update_task(
                        tasks_file, key, status="failed",
                        error=f"branch diverged — otto/{key} preserved, manual rebase needed",
                        error_code="merge_diverged",
                    )
                _log_fail(task_id, f"branch diverged — otto/{key} preserved, manual rebase needed", time.monotonic() - task_start)
                return False
        else:
            # Verification failed — unwind candidate commit for retry
            subprocess.run(
                ["git", "reset", "--mixed", "HEAD~1"],
                cwd=project_dir, capture_output=True,
            )
            last_error = verify_result.failure_output
            _log_warn(f"Verification failed — retrying")

    # All retries exhausted
    _cleanup_task_failure(
        project_dir, key, default_branch, tasks_file,
        error="max retries exhausted", error_code="max_retries",
    )
    _log_fail(task_id, "all retries exhausted", time.monotonic() - task_start)
    return False


async def _run_integration_gate(
    passed_tasks: list[dict],
    config: dict,
    project_dir: Path,
) -> bool:
    """Generate and run cross-feature integration tests. Returns True if passed."""
    from otto.testgen import generate_integration_tests
    from otto.verify import run_integration_gate

    default_branch = config["default_branch"]
    test_command = config.get("test_command")
    timeout = config["verify_timeout"]
    max_retries = config.get("max_retries", 3)

    print(flush=True)
    _log_info("Integration gate — testing features together")
    print(f"  {_DIM}Generating cross-feature integration tests...{_RESET}", flush=True)

    # Generate integration tests
    integration_file = None
    try:
        integration_file = generate_integration_tests(passed_tasks, project_dir)
        if integration_file:
            print(f"  {_GREEN}✓{_RESET} {_DIM}Integration tests generated{_RESET}", flush=True)
        else:
            _log_warn("Integration test generation failed — skipping gate")
            return True
    except Exception as e:
        _log_warn(f"Integration test generation failed: {e}")
        return True

    # Commit integration test file to main
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    dest = tests_dir / "otto_integration.py"
    shutil.copy2(integration_file, dest)
    subprocess.run(["git", "add", str(dest)], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "otto: add cross-feature integration tests"],
        cwd=project_dir, capture_output=True,
    )

    # Run integration gate (full test suite + integration tests in clean worktree)
    print(f"  {_DIM}Running integration tests...{_RESET}", flush=True)
    gate_result = run_integration_gate(
        project_dir=project_dir,
        test_command=test_command,
        integration_test_file=integration_file,
        timeout=timeout,
    )
    _log_verify(gate_result.tiers)

    if gate_result.passed:
        print(f"\n  {_GREEN}{_BOLD}✓ Integration gate PASSED{_RESET}", flush=True)
        return True

    # Integration tests failed — spawn agent to fix
    for attempt in range(max_retries):
        print(f"\n  {_YELLOW}⚠ Integration tests failed — fixing (attempt {attempt + 1}/{max_retries}){_RESET}", flush=True)

        fix_prompt = (
            f"Cross-feature integration tests are failing. Fix the issues.\n\n"
            f"{gate_result.failure_output}\n\n"
            f"You are working in {project_dir}. Do NOT create git commits. "
            f"You may edit any file in the project to make all tests pass."
        )

        try:
            agent_opts = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                cwd=str(project_dir),
            )
            if config.get("model"):
                agent_opts.model = config["model"]

            async for message in query(prompt=fix_prompt, options=agent_opts):
                if AssistantMessage and isinstance(message, AssistantMessage):
                    for block in message.content:
                        if TextBlock and isinstance(block, TextBlock) and block.text:
                            print(block.text, flush=True)
                        elif ToolUseBlock and isinstance(block, ToolUseBlock):
                            _print_tool_use(block)
                        elif ToolResultBlock and isinstance(block, ToolResultBlock):
                            _print_tool_result(block)
        except Exception as e:
            _log_warn(f"Agent error: {e}")
            continue

        # Commit the fix
        subprocess.run(["git", "add", "-u"], cwd=project_dir, capture_output=True)
        # Stage new untracked files (agent might create new ones)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=project_dir, capture_output=True, text=True,
        )
        for f in untracked.stdout.split("\0"):
            if f:
                subprocess.run(["git", "add", "--", f], cwd=project_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "otto: fix cross-feature integration issues", "--allow-empty"],
            cwd=project_dir, capture_output=True,
        )

        # Re-verify
        gate_result = run_integration_gate(
            project_dir=project_dir,
            test_command=test_command,
            integration_test_file=None,  # already committed
            timeout=timeout,
        )
        _log_verify(gate_result.tiers)

        if gate_result.passed:
            print(f"\n  {_GREEN}{_BOLD}✓ Integration gate PASSED{_RESET} {_DIM}(after fix){_RESET}", flush=True)
            return True

    print(f"\n  {_RED}{_BOLD}✗ Integration gate FAILED{_RESET} {_DIM}— cross-feature issues remain{_RESET}", flush=True)
    return False


def _print_summary(results: list[tuple[dict, bool]], total_duration: float, integration_passed: bool | None = None) -> None:
    """Print summary of all tasks after a run."""
    passed = sum(1 for _, s in results if s)
    failed = len(results) - passed

    print(flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)
    print(f"{_BOLD}  Run complete{_RESET}  {_DIM}{_format_duration(total_duration)}{_RESET}", flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)

    for task, success in results:
        icon = f"{_GREEN}✓{_RESET}" if success else f"{_RED}✗{_RESET}"
        print(f"  {icon} {_BOLD}#{task['id']}{_RESET}  {task['prompt'][:50]}", flush=True)

    print(flush=True)
    if failed == 0:
        print(f"  {_GREEN}{_BOLD}{passed}/{len(results)} tasks passed{_RESET}", flush=True)
    else:
        print(f"  {_GREEN}{passed} passed{_RESET}  {_RED}{failed} failed{_RESET}  {_DIM}of {len(results)} tasks{_RESET}", flush=True)

    if integration_passed is not None:
        icon = f"{_GREEN}✓{_RESET}" if integration_passed else f"{_RED}✗{_RESET}"
        label = "passed" if integration_passed else "FAILED"
        print(f"  {icon} Integration gate {label}", flush=True)

    print(flush=True)


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
        print(f"{_RED}Another otto process is running{_RESET}", flush=True)
        return 2

    # Signal handling — first Ctrl+C sets flag, second forces exit
    current_task_key = None
    interrupted = False

    def _signal_handler(signum, frame):
        nonlocal interrupted
        if interrupted:
            # Second signal — force exit
            print(f"\n{_RED}Force exit{_RESET}", flush=True)
            sys.exit(1)
        interrupted = True
        print(f"\n{_YELLOW}⚠ Interrupted — finishing current task then stopping (Ctrl+C again to force){_RESET}", flush=True)

    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    try:
        # Baseline check
        test_command = config.get("test_command")
        if test_command:
            _log_info("Running baseline check...")
            result = subprocess.run(
                test_command, shell=True, cwd=project_dir,
                capture_output=True, timeout=config["verify_timeout"],
                env=_subprocess_env(),
            )
            if result.returncode != 0:
                print(f"  {_RED}✗ Baseline tests failing — fix before running otto{_RESET}", flush=True)
                return 2

        # Process tasks
        tasks = load_tasks(tasks_file)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            print(f"{_DIM}No pending tasks{_RESET}", flush=True)
            return 0

        run_start = time.monotonic()
        results: list[tuple[dict, bool]] = []  # (task, success)
        for task in pending:
            if interrupted:
                _log_warn("Interrupted — cleaning up")
                break
            current_task_key = task["key"]
            if not check_clean_tree(project_dir):
                print(f"  {_RED}✗ Working tree is dirty — aborting{_RESET}", flush=True)
                return 2
            success = await run_task(task, config, project_dir, tasks_file)
            results.append((task, success))
            current_task_key = None

        # Cleanup on interruption
        if interrupted and current_task_key:
            _cleanup_task_failure(
                project_dir, current_task_key, default_branch,
                tasks_file, error="interrupted", error_code="interrupted",
            )
            return 1

        # Integration gate — run after 2+ tasks pass
        passed_tasks = [t for t, s in results if s]
        integration_passed = True
        skip_integration = config.get("no_integration", False)

        if len(passed_tasks) >= 2 and not skip_integration:
            integration_passed = await _run_integration_gate(
                passed_tasks, config, project_dir,
            )

        # Print run summary
        _print_summary(results, time.monotonic() - run_start, integration_passed if len(passed_tasks) >= 2 and not skip_integration else None)

        any_failed = any(not s for _, s in results) or not integration_passed
        return 1 if any_failed else 0

    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
