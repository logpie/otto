"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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

from otto.config import git_meta_dir, detect_test_command
from otto.tasks import load_tasks, update_task
from otto.testgen import generate_tests, detect_test_framework, test_file_path, run_mutation_check
from otto.verify import VerifyResult, run_tier1, run_verification, _subprocess_env




def check_clean_tree(project_dir: Path) -> bool:
    """Check that tracked files have no uncommitted changes.

    Only checks tracked files — untracked files are fine.
    Otto runtime files (tasks.yaml, .tasks.lock) are ignored.
    If the tree is dirty with non-otto changes, auto-stash them.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uno"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False

    otto_runtime = {"tasks.yaml", ".tasks.lock"}
    has_non_otto_changes = False
    for line in result.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            has_non_otto_changes = True
            break
        filename = parts[1].strip('"')
        if filename not in otto_runtime:
            has_non_otto_changes = True
            break

    if has_non_otto_changes:
        # Auto-stash non-otto changes so we can proceed
        stash = subprocess.run(
            ["git", "stash", "push", "-m", "otto: auto-stash before run"],
            cwd=project_dir, capture_output=True, text=True,
        )
        if stash.returncode == 0 and "No local changes" not in stash.stdout:
            print(f"  {_DIM}Auto-stashed uncommitted changes{_RESET}", flush=True)
            return True
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


def _prune_empty_parents(path: Path, root: Path) -> None:
    """Remove empty parent directories up to, but not including, root."""
    current = path
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _remove_path(path: Path, root: Path) -> None:
    """Remove a file/symlink/directory and prune empty parents."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            return
    _prune_empty_parents(path.parent, root)


def _remove_otto_created_untracked(
    project_dir: Path,
    pre_existing_untracked: set[str] | None,
) -> None:
    """Delete only untracked files created during the run."""
    if pre_existing_untracked is None:
        return

    current_untracked = _snapshot_untracked(project_dir)
    created_untracked = sorted(
        current_untracked - pre_existing_untracked,
        key=lambda rel: len(Path(rel).parts),
        reverse=True,
    )
    for rel_path in created_untracked:
        _remove_path(project_dir / rel_path, project_dir)


def _restore_workspace_state(
    project_dir: Path,
    reset_ref: str | None = None,
    pre_existing_untracked: set[str] | None = None,
) -> None:
    """Restore tracked files and remove only Otto-created untracked files."""
    cmd = ["git", "reset", "--hard"]
    if reset_ref:
        cmd.append(reset_ref)
    subprocess.run(cmd, cwd=project_dir, capture_output=True)
    _remove_otto_created_untracked(project_dir, pre_existing_untracked)


def _file_sha256(path: Path) -> str:
    """Return a stable hash for tamper checks."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list_worktree_changes(worktree_dir: Path) -> tuple[set[str], set[str]]:
    """Return tracked and untracked paths changed from HEAD in a worktree."""
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=worktree_dir, capture_output=True, text=True, check=True,
    )
    tracked_paths = {line for line in tracked.stdout.splitlines() if line}
    untracked_paths = _snapshot_untracked(worktree_dir)
    return tracked_paths, untracked_paths


def _copy_changed_paths_from_worktree(
    source_dir: Path,
    dest_dir: Path,
    tracked_paths: set[str],
    untracked_paths: set[str],
    pre_existing_untracked: set[str] | None = None,
) -> set[str]:
    """Copy a verified worktree diff back onto the main checkout."""
    changed_paths = tracked_paths | untracked_paths
    preserve = pre_existing_untracked or set()

    for rel_path in sorted(changed_paths):
        source = source_dir / rel_path
        dest = dest_dir / rel_path

        if rel_path in untracked_paths and rel_path in preserve:
            raise RuntimeError(
                f"refusing to overwrite pre-existing untracked file: {rel_path}"
            )

        if source.exists() or source.is_symlink():
            if dest.exists() or dest.is_symlink():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest, follow_symlinks=False)
        else:
            _remove_path(dest, dest_dir)

    return changed_paths


def _run_integration_gate_in_worktree(
    worktree_dir: Path,
    test_command: str | None,
    timeout: int,
) -> VerifyResult:
    """Run the integration gate against a mutable worktree checkout."""
    tier = run_tier1(worktree_dir, test_command, timeout)
    return VerifyResult(passed=tier.passed or tier.skipped, tiers=[tier])


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
    pre_existing_untracked: set[str] | None = None,
    error: str = "unknown",
    error_code: str = "unknown",
    cost_usd: float = 0.0,
) -> None:
    """Unified cleanup for all task failure paths: retries exhausted, interruption, exceptions."""
    _restore_workspace_state(
        project_dir,
        pre_existing_untracked=pre_existing_untracked,
    )
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
            updates: dict[str, Any] = {
                "status": "failed", "error": error, "error_code": error_code,
            }
            if cost_usd > 0:
                updates["cost_usd"] = cost_usd
            update_task(tasks_file, key, **updates)
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
    print(f"{_BOLD}  Task #{task_id}{_RESET}  {prompt[:80]}", flush=True)
    print(f"  {_DIM}attempt {attempt}/{max_attempts}  ·  key {key}{_RESET}", flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"


def _format_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def _log_pass(task_id: int, branch: str, duration: float | None = None, cost: float = 0.0) -> None:
    dur = f" in {_format_duration(duration)}" if duration else ""
    cost_str = f" ({_format_cost(cost)})" if cost > 0 else ""
    print(f"\n  {_GREEN}{_BOLD}✓ Task #{task_id} PASSED{_RESET} {_DIM}— merged to {branch}{dur}{cost_str}{_RESET}", flush=True)


def _log_fail(task_id: int, reason: str, duration: float | None = None, cost: float = 0.0) -> None:
    dur = f" in {_format_duration(duration)}" if duration else ""
    cost_str = f" ({_format_cost(cost)})" if cost > 0 else ""
    print(f"\n  {_RED}{_BOLD}✗ Task #{task_id} FAILED{_RESET} {_DIM}— {reason}{dur}{cost_str}{_RESET}", flush=True)


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


async def _diagnose_test_bug(
    rubric: list[str],
    failure_output: str,
    test_file: Path,
    project_dir: Path,
) -> str | None:
    """Diagnose whether a test failure is a test bug or an implementation bug.

    Reads the rubric, the failing test, and the failure output.
    Returns a description of the test bug if found, or None if the test is correct.
    Uses a one-shot claude -p call (cheap, fast).
    """
    try:
        test_content = test_file.read_text()
    except OSError:
        return None

    rubric_text = "\n".join(f"- {r}" for r in rubric)
    # Truncate to keep prompt reasonable
    failure_snippet = failure_output[:2000]
    test_snippet = test_content[:3000]

    prompt = (
        f"You are a QA judge. A coding agent failed to pass an adversarial test. "
        f"Determine if the TEST has a bug, or if the IMPLEMENTATION needs fixing.\n\n"
        f"SPEC (rubric):\n{rubric_text}\n\n"
        f"TEST FILE (first 3000 chars):\n{test_snippet}\n\n"
        f"FAILURE OUTPUT:\n{failure_snippet}\n\n"
        f"Analyze: does the failing test expect behavior that the spec describes? "
        f"Or does the test expect something the spec does NOT ask for (test bug)?\n\n"
        f"Answer with EXACTLY one line:\n"
        f"TEST_BUG: <description> — if the test expects behavior not in the spec\n"
        f"IMPLEMENTATION_BUG — if the test correctly tests the spec and the code is wrong"
    )

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True, text=True, timeout=30,
            start_new_session=True,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("TEST_BUG:"):
            return result.stdout.strip()[len("TEST_BUG:"):].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None
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

    # Auto-detect test_command if rubric exists but no test_command configured
    rubric = task.get("rubric")
    if rubric and not test_command:
        test_command = detect_test_command(project_dir)
        if not test_command:
            test_command = "pytest"  # fallback for Python projects

    # Print task header before testgen (so testgen output is under the right task)
    print(flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)
    print(f"{_BOLD}  Task #{task_id}{_RESET}  {prompt[:80]}", flush=True)
    print(f"  {_DIM}key {key}{_RESET}", flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)

    # Timing for profiling
    timings: dict[str, float] = {}

    # Adversarial testgen: write tests BEFORE coding agent when rubric exists
    test_file_path_val = None
    test_commit_sha = None
    test_file_sha = None

    if rubric:
        from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests

        print(f"  {_DIM}Building black-box context...{_RESET}", flush=True)
        t0 = time.monotonic()
        task_hint = prompt + "\n" + "\n".join(rubric)
        blackbox_ctx = build_blackbox_context(project_dir, task_hint=task_hint)
        timings["blackbox_context"] = time.monotonic() - t0

        print(f"  {_DIM}Testgen agent writing adversarial tests ({len(rubric)} criteria)...{_RESET}", flush=True)
        t0 = time.monotonic()
        testgen_logs: list[str] = []
        test_file_path_val, testgen_logs = await run_testgen_agent(rubric, key, blackbox_ctx, project_dir)
        timings["testgen_agent"] = time.monotonic() - t0

        if test_file_path_val:
            # Two-phase validation
            validation = validate_generated_tests(test_file_path_val, "pytest", project_dir)

            if validation.status == "collection_error":
                _log_warn(f"Generated tests have errors — regenerating once")
                test_file_path_val.unlink()
                test_file_path_val, regen_logs = await run_testgen_agent(rubric, key, blackbox_ctx, project_dir)
                testgen_logs.extend(regen_logs)
                if test_file_path_val:
                    validation = validate_generated_tests(test_file_path_val, "pytest", project_dir)
                    if validation.status == "collection_error":
                        _log_warn("Regenerated tests still broken — skipping rubric tests")
                        test_file_path_val.unlink()
                        test_file_path_val = None

            if test_file_path_val and validation.status == "all_pass":
                print(f"\n  {_YELLOW}{_BOLD}⚠⚠⚠ WARNING: All rubric tests PASS before implementation{_RESET}", flush=True)
                print(f"  {_DIM}Regenerating tests...{_RESET}", flush=True)
                test_file_path_val.unlink()
                test_file_path_val, regen_logs = await run_testgen_agent(rubric, key, blackbox_ctx, project_dir)
                testgen_logs.extend(regen_logs)
                if test_file_path_val:
                    validation = validate_generated_tests(test_file_path_val, "pytest", project_dir)
                    if validation.status == "all_pass":
                        print(f"\n  {_YELLOW}{_BOLD}⚠⚠⚠ WARNING: Tests still pass — skipping rubric tests for task #{task_id}{_RESET}", flush=True)
                        print(f"  {_DIM}Coding agent will run WITHOUT adversarial test coverage.{_RESET}", flush=True)
                        test_file_path_val.unlink()
                        test_file_path_val = None

            if test_file_path_val and validation.status == "tdd_ok":
                print(f"  {_GREEN}✓{_RESET} {_DIM}Adversarial tests ready ({validation.failed} failing, {validation.passed} regression){_RESET}", flush=True)

        # Commit test file if we have one
        if test_file_path_val:
            subprocess.run(["git", "add", str(test_file_path_val.relative_to(project_dir))],
                           cwd=project_dir, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"otto: add rubric tests for task #{task_id}"],
                           cwd=project_dir, capture_output=True)
            test_commit_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=project_dir,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            test_file_sha = subprocess.run(
                ["git", "hash-object", str(test_file_path_val)],
                capture_output=True, text=True,
            ).stdout.strip()
    else:
        # No rubric — use old concurrent testgen approach
        print(f"  {_DIM}Generating adversarial tests...{_RESET}", flush=True)
        testgen_task = asyncio.create_task(
            generate_tests(prompt, project_dir, key)
        )

    # Setup log directory
    log_dir = project_dir / "otto_logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)

    # Persist testgen agent log if we ran the testgen agent
    if rubric and testgen_logs:
        try:
            testgen_log_file = log_dir / "testgen-agent.log"
            testgen_log_file.write_text("\n".join(testgen_logs))
        except OSError:
            pass

    # Persist TDD check results
    if rubric and test_file_path_val:
        try:
            tdd_log = log_dir / "tdd-check.log"
            tdd_log.write_text(
                f"status: {validation.status}\n"
                f"passed: {validation.passed}\n"
                f"failed: {validation.failed}\n"
                f"error_output:\n{validation.error_output}\n"
            )
        except (OSError, NameError):
            pass

    session_id = None
    last_error = None  # verification failure output for retry feedback
    total_cost = 0.0  # accumulated cost across retries
    for attempt in range(max_retries + 1):
        attempt_num = attempt + 1
        print(f"\n  {_DIM}attempt {attempt_num}/{max_retries + 1}{_RESET}", flush=True)

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
                f"Do NOT write tests — acceptance tests will be generated separately.\n\n"
                f"Be EFFICIENT: read only the files you need to edit. "
                f"Do NOT explore the entire project — focus on the task."
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

        # Tell the coding agent about adversarial test file (do NOT modify it)
        if test_file_path_val:
            agent_prompt += (
                f"\n\nACCEPTANCE TESTS: {test_file_path_val.relative_to(project_dir)} contains tests you must pass. "
                f"Do NOT modify this file — these are adversarial tests you must satisfy.\n\n"
                f"IMPORTANT: Implement ONLY what the task description asks for. "
                f"If a test seems to require functionality beyond the spec (e.g., a non-standard "
                f"algorithm extension), implement the STANDARD behavior and let that test fail. "
                f"Do NOT invent non-standard features, workarounds, or hacks to pass a suspicious test. "
                f"A correct standard implementation that fails one questionable test is better than "
                f"a corrupted implementation that passes all tests."
            )

        # Run agent + build candidate + verify — catch infrastructure failures
        try:
            try:
                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(project_dir),
                    max_turns=30,  # prevent infinite loops
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

                # Extract cost from result
                raw_cost = getattr(result_msg, "total_cost_usd", None)
                attempt_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
                total_cost += attempt_cost

                # Check if agent reported an error
                if result_msg and result_msg.is_error:
                    raise RuntimeError(f"Agent error: {result_msg.result or 'unknown'}")

            except Exception as e:
                _log_warn(f"Agent error: {e}")
                # Reset workspace — preserve test commit if it exists
                reset_sha = test_commit_sha if test_commit_sha else base_sha
                _restore_workspace_state(
                    project_dir,
                    reset_ref=reset_sha,
                    pre_existing_untracked=pre_existing_untracked,
                )
                continue

            # Await testgen on first attempt (only for non-rubric path)
            testgen_file = None
            if not rubric:
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

            # Tamper check: ensure coding agent didn't modify the test file
            if test_file_sha and test_file_path_val:
                current = subprocess.run(
                    ["git", "hash-object", str(test_file_path_val)],
                    capture_output=True, text=True, cwd=project_dir,
                ).stdout.strip()
                if current != test_file_sha:
                    subprocess.run(
                        ["git", "checkout", test_commit_sha, "--",
                         str(test_file_path_val.relative_to(project_dir))],
                        cwd=project_dir, capture_output=True,
                    )
                    print(f"  {_YELLOW}⚠ Test file tampered by coding agent — restored{_RESET}", flush=True)

            # Check if agent made any changes (compare against appropriate base)
            commit_base = test_commit_sha if test_commit_sha else base_sha
            diff_check = subprocess.run(
                ["git", "diff", "--quiet", commit_base],
                cwd=project_dir, capture_output=True,
            )
            untracked_check = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=project_dir, capture_output=True, text=True,
            )
            new_untracked = {f for f in untracked_check.stdout.strip().splitlines() if f} - pre_existing_untracked
            no_changes = diff_check.returncode == 0 and not new_untracked

            if no_changes and not testgen_file and not test_file_path_val:
                # No code changes and no rubric tests — nothing to commit
                print(f"  {_DIM}No changes needed{_RESET}", flush=True)
                subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
                cleanup_branch(project_dir, key, default_branch)
                if tasks_file:
                    updates = {"status": "passed"}
                    if total_cost > 0:
                        updates["cost_usd"] = total_cost
                    update_task(tasks_file, key, **updates)
                timings["total"] = time.monotonic() - task_start
                _log_pass(task_id, default_branch, timings["total"], cost=total_cost)
                try:
                    (log_dir / "timing.log").write_text(
                        "\n".join(f"{k}: {v:.1f}s" for k, v in timings.items()) + "\n"
                    )
                except OSError:
                    pass
                return True

            # Build candidate commit
            # When rubric tests exist, they're already committed — pass test_commit_sha as base
            # and testgen_file=None (tests are already in the tree)
            candidate_sha = build_candidate_commit(
                project_dir, commit_base, testgen_file if not rubric else None,
                pre_existing_untracked,
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
            _log_fail(task_id, f"unexpected error: {e}", time.monotonic() - task_start, cost=total_cost)
            _cleanup_task_failure(
                project_dir, key, default_branch, tasks_file,
                pre_existing_untracked=pre_existing_untracked,
                error=f"unexpected error: {e}", error_code="internal_error",
                cost_usd=total_cost,
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
            # Squash all branch commits into a single commit
            # When rubric tests exist, there are 2 commits (test + candidate) to squash
            try:
                # Reset to base_sha (not commit_base) to squash everything into one commit
                subprocess.run(
                    ["git", "reset", "--mixed", base_sha],
                    cwd=project_dir, capture_output=True, check=True,
                )
                # Re-stage everything
                subprocess.run(
                    ["git", "add", "-u"],
                    cwd=project_dir, capture_output=True, check=True,
                )
                untracked_final = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                skip = pre_existing_untracked or set()
                for f in untracked_final.stdout.split("\0"):
                    if f and f not in skip:
                        subprocess.run(
                            ["git", "add", "--", f],
                            cwd=project_dir, capture_output=True,
                        )
                subprocess.run(
                    ["git", "commit", "-m",
                     f"otto: {prompt[:60]} (#{task_id})"],
                    cwd=project_dir, capture_output=True, text=True,
                    check=True,
                )
            except (subprocess.CalledProcessError, Exception) as e:
                stderr = getattr(e, "stderr", str(e))
                _log_fail(task_id, f"squash commit failed: {stderr}", time.monotonic() - task_start, cost=total_cost)
                _cleanup_task_failure(
                    project_dir, key, default_branch, tasks_file,
                    pre_existing_untracked=pre_existing_untracked,
                    error=f"squash commit failed: {stderr}", error_code="internal_error",
                    cost_usd=total_cost,
                )
                return False
            # Mutation check — validate test quality (informational only)
            if test_file_path_val:
                try:
                    caught, mut_desc = run_mutation_check(
                        project_dir, test_file_path_val, test_command or "pytest",
                    )
                    if caught:
                        print(f"  {_GREEN}✓{_RESET} {_DIM}Mutation check: tests caught the intentional break{_RESET}", flush=True)
                    else:
                        print(f"  {_YELLOW}⚠ Mutation check: tests did NOT catch an intentional break — tests may be weak{_RESET}", flush=True)
                        print(f"    {_DIM}{mut_desc}{_RESET}", flush=True)
                    # Persist mutation check result
                    try:
                        mut_log = log_dir / f"attempt-{attempt_num}-mutation.log"
                        mut_log.write_text(f"caught: {caught}\n{mut_desc}\n")
                    except OSError:
                        pass
                except Exception as e:
                    print(f"  {_DIM}Mutation check skipped: {e}{_RESET}", flush=True)

            # Merge to default — post-merge bookkeeping errors are non-destructive
            if merge_to_default(project_dir, key, default_branch):
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                if tasks_file:
                    updates = {"status": "passed"}
                    if total_cost > 0:
                        updates["cost_usd"] = total_cost
                    update_task(tasks_file, key, **updates)
                timings["total"] = time.monotonic() - task_start
                _log_pass(task_id, default_branch, timings["total"], cost=total_cost)
                try:
                    (log_dir / "timing.log").write_text(
                        "\n".join(f"{k}: {v:.1f}s" for k, v in timings.items()) + "\n"
                    )
                except OSError:
                    pass
                return True
            else:
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                if tasks_file:
                    updates: dict[str, Any] = {
                        "status": "failed",
                        "error": f"branch diverged — otto/{key} preserved, manual rebase needed",
                        "error_code": "merge_diverged",
                    }
                    if total_cost > 0:
                        updates["cost_usd"] = total_cost
                    update_task(tasks_file, key, **updates)
                _log_fail(task_id, f"branch diverged — otto/{key} preserved, manual rebase needed", time.monotonic() - task_start, cost=total_cost)
                return False
        else:
            # Verification failed — check if adversarial test has a bug
            if test_file_path_val and verify_result.failure_output:
                test_bug = await _diagnose_test_bug(
                    rubric, verify_result.failure_output,
                    test_file_path_val, project_dir,
                )
                if test_bug:
                    print(f"  {_YELLOW}⚠ Detected test bug — regenerating adversarial tests{_RESET}", flush=True)
                    print(f"    {_DIM}{test_bug}{_RESET}", flush=True)
                    # Unwind, regenerate tests, and restart the attempt loop
                    subprocess.run(
                        ["git", "reset", "--hard", base_sha],
                        cwd=project_dir, capture_output=True,
                    )
                    _remove_otto_created_untracked(project_dir, pre_existing_untracked)
                    subprocess.run(["git", "checkout", default_branch],
                                   cwd=project_dir, capture_output=True)
                    cleanup_branch(project_dir, key, default_branch)
                    # Re-create branch and regenerate tests
                    base_sha = create_task_branch(project_dir, key, default_branch, task=task)
                    if test_file_path_val.exists():
                        test_file_path_val.unlink()
                    from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests
                    blackbox_ctx = build_blackbox_context(project_dir, task_hint=task_hint)
                    test_file_path_val, testgen_logs = await run_testgen_agent(
                        rubric, key, blackbox_ctx, project_dir
                    )
                    if test_file_path_val:
                        validation = validate_generated_tests(test_file_path_val, "pytest", project_dir)
                        if validation.status == "tdd_ok":
                            subprocess.run(["git", "add", str(test_file_path_val.relative_to(project_dir))],
                                           cwd=project_dir, capture_output=True)
                            subprocess.run(["git", "commit", "-m", f"otto: regenerate rubric tests for task #{task_id}"],
                                           cwd=project_dir, capture_output=True)
                            test_commit_sha = subprocess.run(
                                ["git", "rev-parse", "HEAD"], cwd=project_dir,
                                capture_output=True, text=True, check=True,
                            ).stdout.strip()
                            commit_base = test_commit_sha
                            test_file_sha = subprocess.run(
                                ["git", "hash-object", str(test_file_path_val)],
                                capture_output=True, text=True,
                            ).stdout.strip()
                            print(f"  {_GREEN}✓{_RESET} {_DIM}Regenerated adversarial tests ({validation.failed} failing){_RESET}", flush=True)
                    continue  # restart the attempt loop with new tests

            # Unwind candidate commit for retry
            subprocess.run(
                ["git", "reset", "--mixed", commit_base],
                cwd=project_dir, capture_output=True,
            )
            last_error = verify_result.failure_output
            _log_warn(f"Verification failed — retrying")

    # All retries exhausted
    _cleanup_task_failure(
        project_dir, key, default_branch, tasks_file,
        pre_existing_untracked=pre_existing_untracked,
        error="max retries exhausted", error_code="max_retries",
        cost_usd=total_cost,
    )
    _log_fail(task_id, "all retries exhausted", time.monotonic() - task_start, cost=total_cost)
    return False


async def _run_integration_gate(
    passed_tasks: list[dict],
    config: dict,
    project_dir: Path,
) -> bool | None:
    """Generate and run cross-feature integration tests. Returns True if passed."""
    from otto.testgen import generate_integration_tests
    from otto.verify import run_integration_gate

    test_command = config.get("test_command")
    timeout = config["verify_timeout"]
    max_retries = config.get("max_retries", 3)
    pre_existing_untracked = _snapshot_untracked(project_dir)

    print(flush=True)
    _log_info("Integration gate — testing features together")
    print(f"  {_DIM}Generating cross-feature integration tests...{_RESET}", flush=True)

    # Generate integration tests
    integration_file = None
    try:
        integration_file = await generate_integration_tests(passed_tasks, project_dir)
        if integration_file:
            print(f"  {_GREEN}✓{_RESET} {_DIM}Integration tests generated{_RESET}", flush=True)
        else:
            _log_warn("Integration test generation failed — skipping gate")
            return None  # None = skipped (distinct from True=passed, False=failed)
    except Exception as e:
        _log_warn(f"Integration test generation failed: {e}")
        return None

    # Run integration gate against a disposable worktree first.
    print(f"  {_DIM}Running integration tests...{_RESET}", flush=True)
    gate_result = run_integration_gate(
        project_dir=project_dir,
        test_command=test_command,
        integration_test_file=integration_file,
        timeout=timeout,
    )
    _log_verify(gate_result.tiers)

    if gate_result.passed:
        dest = project_dir / "tests" / "otto_integration.py"
        if str(dest.relative_to(project_dir)) in pre_existing_untracked:
            _log_warn("Integration test path conflicts with a pre-existing untracked file")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(integration_file, dest)
        subprocess.run(
            ["git", "add", str(dest.relative_to(project_dir))],
            cwd=project_dir, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "otto: add cross-feature integration tests"],
            cwd=project_dir, capture_output=True, check=True,
        )
        print(f"\n  {_GREEN}{_BOLD}✓ Integration gate PASSED{_RESET}", flush=True)
        return True

    # Integration tests failed — fix them in an isolated worktree so main stays clean.
    fix_worktree = Path(tempfile.mkdtemp(prefix="otto-integration-fix-"))
    integration_rel_path = Path("tests") / "otto_integration.py"
    worktree_test_file = fix_worktree / integration_rel_path
    integration_test_sha = _file_sha256(integration_file)

    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(fix_worktree), "HEAD"],
            cwd=project_dir, capture_output=True, check=True,
        )
        worktree_test_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(integration_file, worktree_test_file)

        for attempt in range(max_retries):
            print(f"\n  {_YELLOW}⚠ Integration tests failed — fixing (attempt {attempt + 1}/{max_retries}){_RESET}", flush=True)

            fix_prompt = (
                f"Cross-feature integration tests are failing. Fix the issues.\n\n"
                f"{gate_result.failure_output}\n\n"
                f"You are working in {fix_worktree}. Do NOT create git commits. "
                f"ACCEPTANCE TESTS: {integration_rel_path} contains the integration tests you must satisfy. "
                f"Do NOT modify this file. You may edit any other project file to make all tests pass."
            )

            try:
                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(fix_worktree),
                    max_turns=20,
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

            if (
                not worktree_test_file.exists()
                or _file_sha256(worktree_test_file) != integration_test_sha
            ):
                shutil.copy2(integration_file, worktree_test_file)
                _log_warn("Integration test file tampered by coding agent — restored")

            gate_result = _run_integration_gate_in_worktree(
                fix_worktree,
                test_command,
                timeout,
            )
            _log_verify(gate_result.tiers)

            if gate_result.passed:
                try:
                    tracked_paths, untracked_paths = _list_worktree_changes(fix_worktree)
                    changed_paths = _copy_changed_paths_from_worktree(
                        fix_worktree,
                        project_dir,
                        tracked_paths,
                        untracked_paths,
                        pre_existing_untracked=pre_existing_untracked,
                    )
                    for rel_path in sorted(changed_paths):
                        subprocess.run(
                            ["git", "add", "--", rel_path],
                            cwd=project_dir, capture_output=True, check=True,
                        )
                    subprocess.run(
                        ["git", "commit", "-m", "otto: add cross-feature integration tests"],
                        cwd=project_dir, capture_output=True, check=True,
                    )
                except RuntimeError as e:
                    _log_warn(f"Integration gate copy-back conflict: {e}")
                    return False
                print(f"\n  {_GREEN}{_BOLD}✓ Integration gate PASSED{_RESET} {_DIM}(after fix){_RESET}", flush=True)
                return True
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(fix_worktree)],
            cwd=project_dir, capture_output=True,
        )
        if fix_worktree.exists():
            shutil.rmtree(fix_worktree, ignore_errors=True)

    print(f"\n  {_RED}{_BOLD}✗ Integration gate FAILED{_RESET} {_DIM}— cross-feature issues remain{_RESET}", flush=True)
    return False


def _print_summary(results: list[tuple[dict, bool]], total_duration: float, integration_passed: bool | None = None, total_cost: float = 0.0) -> None:
    """Print summary of all tasks after a run."""
    passed = sum(1 for _, s in results if s)
    failed = len(results) - passed

    cost_str = f"  {_format_cost(total_cost)}" if total_cost > 0 else ""
    print(flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)
    print(f"{_BOLD}  Run complete{_RESET}  {_DIM}{_format_duration(total_duration)}{cost_str}{_RESET}", flush=True)
    print(f"{_BOLD}{'━' * 60}{_RESET}", flush=True)

    for task, success in results:
        icon = f"{_GREEN}✓{_RESET}" if success else f"{_RED}✗{_RESET}"
        print(f"  {icon} {_BOLD}#{task['id']}{_RESET}  {task['prompt'][:80]}", flush=True)

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
    current_pre_existing_untracked: set[str] | None = None
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
            current_pre_existing_untracked = _snapshot_untracked(project_dir)
            success = await run_task(task, config, project_dir, tasks_file)
            results.append((task, success))
            current_task_key = None
            current_pre_existing_untracked = None

        # Cleanup on interruption
        if interrupted and current_task_key:
            _cleanup_task_failure(
                project_dir, current_task_key, default_branch,
                tasks_file,
                pre_existing_untracked=current_pre_existing_untracked,
                error="interrupted", error_code="interrupted",
            )
            return 1

        # Integration gate — run after 2+ tasks pass
        passed_tasks = [t for t, s in results if s]
        integration_result: bool | None = None  # None=skipped/not-run, True=passed, False=failed
        skip_integration = config.get("no_integration", False)

        if len(passed_tasks) >= 2 and not skip_integration:
            integration_result = await _run_integration_gate(
                passed_tasks, config, project_dir,
            )

        # Calculate total cost from task records
        run_total_cost = 0.0
        final_tasks = load_tasks(tasks_file)
        task_keys_in_run = {t["key"] for t, _ in results}
        for t in final_tasks:
            if t.get("key") in task_keys_in_run:
                run_total_cost += t.get("cost_usd", 0.0)

        # Print run summary
        _print_summary(results, time.monotonic() - run_start, integration_result, total_cost=run_total_cost)

        any_failed = any(not s for _, s in results)
        if integration_result is False:
            any_failed = True
        return 1 if any_failed else 0

    finally:
        # Restore auto-stashed changes if any
        stash_list = subprocess.run(
            ["git", "stash", "list"],
            cwd=project_dir, capture_output=True, text=True,
        )
        if stash_list.returncode == 0 and "otto: auto-stash before run" in stash_list.stdout:
            subprocess.run(["git", "stash", "pop"], cwd=project_dir, capture_output=True)
            print(f"  {_DIM}Restored auto-stashed changes{_RESET}", flush=True)

        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
