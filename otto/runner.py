"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import fcntl
import graphlib
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
from otto.display import _truncate_at_word
from otto.tasks import load_tasks, update_task
from otto.testgen import detect_test_framework, test_file_path, run_mutation_check
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
    duration_s: float = 0.0,
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
            if duration_s > 0:
                updates["duration_s"] = round(duration_s, 1)
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
    """Print verification results inline with test counts."""
    import re as _re
    print(f"\n  {_DIM}{'─' * 50}{_RESET}", flush=True)
    for t in tiers:
        if t.skipped:
            continue
        icon = f"{_GREEN}✓{_RESET}" if t.passed else f"{_RED}✗{_RESET}"
        # Extract test count from output if available
        count_str = ""
        if t.output:
            match = _re.search(r"(\d+) passed", t.output)
            if match:
                count_str = f" {_DIM}({match.group(1)} tests){_RESET}"
        print(f"  {icon} {t.tier}{count_str}", flush=True)


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
        detail = _truncate_at_word(cmd, 80)

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
        return _truncate_at_word(cmd, 120)
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


def _setup_task_worktree(project_dir: Path, key: str, base_sha: str) -> Path:
    """Create an isolated git worktree for parallel task execution."""
    wt_dir = project_dir / ".worktrees" / f"otto-{key}"
    branch_name = f"otto/{key}"
    # Clean up stale worktree if it exists
    if wt_dir.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=project_dir, capture_output=True,
        )
    # Delete stale branch if it exists
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_dir, capture_output=True,
    )
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt_dir), "-b", branch_name, base_sha],
        cwd=project_dir, capture_output=True, check=True,
    )
    return wt_dir


def _teardown_task_worktree(project_dir: Path, key: str) -> None:
    """Remove a task's git worktree and its branch."""
    wt_dir = project_dir / ".worktrees" / f"otto-{key}"
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_dir)],
        cwd=project_dir, capture_output=True,
    )
    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)


async def run_task(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    work_dir: Path | None = None,
    pre_generated_test: Path | None = None,
    sibling_test_files: list[Path] | None = None,
) -> bool:
    """Run a single task through the full loop. Returns True if passed.

    When work_dir is set (parallel mode), the task runs in an isolated worktree:
    - Branch creation and merge are handled by the caller
    - All agent/git operations use work_dir as cwd
    - tasks_file operations still use project_dir (via flock)

    When pre_generated_test is set, skips testgen and uses the provided test file.
    Used when testgen was run sequentially before parallel coding.

    sibling_test_files: repo-relative paths of other tasks' test files running
    in parallel. Excluded from verification worktrees to prevent cross-task
    contamination.
    """
    key = task["key"]
    task_id = task["id"]
    prompt = task["prompt"]
    max_retries = task.get("max_retries", config["max_retries"])
    verify_cmd = task.get("verify")
    test_command = config.get("test_command")
    default_branch = config["default_branch"]
    timeout = config["verify_timeout"]
    parallel_mode = work_dir is not None
    effective_dir = work_dir if parallel_mode else project_dir


    # In parallel mode, prefix structural messages and suppress verbose agent output.
    # Full output goes to log files (otto logs <id>).
    _task_tag = f"[#{task_id}]" if parallel_mode else ""

    def _tprint(msg: str = "", **kwargs) -> None:
        """Print with task prefix in parallel mode."""
        if _task_tag:
            print(f"  {_DIM}{_task_tag}{_RESET} {msg}", flush=True)
        else:
            print(msg, flush=True)

    # Snapshot pre-existing untracked files so we don't sweep them into the commit
    pre_existing_untracked = _snapshot_untracked(effective_dir)

    task_start = time.monotonic()

    # Create branch (skip in parallel mode — caller sets up worktree with branch)
    if parallel_mode:
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=effective_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()
    else:
        base_sha = create_task_branch(project_dir, key, default_branch, task=task)
    if tasks_file:
        update_task(tasks_file, key, status="running", attempts=0)

    # Auto-detect test_command if rubric exists but no test_command configured
    rubric = task.get("rubric")
    if rubric and not test_command:
        test_command = detect_test_command(effective_dir)
        if not test_command:
            test_command = "pytest"  # fallback for Python projects


    # Print task header before testgen (so testgen output is under the right task)
    if parallel_mode:
        _tprint(f"{_BOLD}Task #{task_id}{_RESET}  {prompt[:60]}  {_DIM}started{_RESET}")
    else:
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

    if not rubric and not parallel_mode:
        print(f"\n  {_YELLOW}{_BOLD}⚠ No rubric — skipping adversarial TDD{_RESET}", flush=True)
        if test_command:
            print(f"  {_DIM}Relying on existing tests:{_RESET} {test_command}", flush=True)
        if verify_cmd:
            print(f"  {_DIM}Relying on verify command:{_RESET} {verify_cmd}", flush=True)
        if not test_command and not verify_cmd:
            print(f"  {_YELLOW}No test command, no verify command — agent output will be merged unchecked.{_RESET}", flush=True)
        print(f"  {_DIM}Use 'otto add' without --no-rubric to enable adversarial TDD.{_RESET}\n", flush=True)

    testgen_cost = 0.0  # accumulated across testgen attempts

    if rubric and pre_generated_test:
        # Testgen was run sequentially before parallel coding — use pre-generated test
        test_file_path_val = pre_generated_test
        from otto.testgen import validate_generated_tests
        validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
        testgen_logs = []

    elif rubric:
        from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests

        if not parallel_mode:
            print(f"  {_DIM}Building black-box context...{_RESET}", flush=True)
        t0 = time.monotonic()
        task_hint = prompt + "\n" + "\n".join(rubric)
        blackbox_ctx = build_blackbox_context(effective_dir, task_hint=task_hint)
        timings["blackbox_context"] = time.monotonic() - t0

        if not parallel_mode:
            print(f"  {_DIM}Testgen agent writing adversarial tests ({len(rubric)} criteria)...{_RESET}", flush=True)
        t0 = time.monotonic()
        testgen_logs: list[str] = []
        test_file_path_val, testgen_logs, _tg_cost_i = await run_testgen_agent(rubric, key, blackbox_ctx, effective_dir, quiet=parallel_mode, task_spec=prompt)
        testgen_cost += _tg_cost_i
        timings["testgen_agent"] = time.monotonic() - t0

        if test_file_path_val:
            # Two-phase validation
            validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)

            if validation.status == "collection_error":
                _log_warn(f"Generated tests have errors — regenerating once")
                if not parallel_mode and validation.error_output:
                    # Show the actual error so it can be debugged
                    err_lines = validation.error_output.strip().splitlines()
                    for line in err_lines[-5:]:
                        print(f"    {_DIM}{line}{_RESET}", flush=True)
                test_file_path_val.unlink()
                test_file_path_val, regen_logs, _tg_cost_i = await run_testgen_agent(rubric, key, blackbox_ctx, effective_dir, quiet=parallel_mode, task_spec=prompt)
                testgen_cost += _tg_cost_i
                testgen_logs.extend(regen_logs)
                if test_file_path_val:
                    validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
                    if validation.status == "collection_error":
                        _log_warn("Regenerated tests still broken — skipping rubric tests")
                        if not parallel_mode and validation.error_output:
                            err_lines = validation.error_output.strip().splitlines()
                            for line in err_lines[-5:]:
                                print(f"    {_DIM}{line}{_RESET}", flush=True)
                        test_file_path_val.unlink()
                        test_file_path_val = None

            if test_file_path_val and validation.status == "all_pass":
                # Check if feature already exists (code from a previous run)
                # If so, keep tests as regression coverage — don't waste time regenerating
                source_context = get_relevant_file_contents(effective_dir, task_hint=prompt)
                # Simple heuristic: if source files mention key words from the task, feature likely exists
                prompt_words = {w.lower() for w in prompt.split() if len(w) >= 4}
                source_lower = source_context.lower()
                matches = sum(1 for w in prompt_words if w in source_lower)
                feature_likely_exists = matches >= 3

                if feature_likely_exists:
                    if not parallel_mode:
                        print(f"  {_DIM}All tests pass — feature likely already implemented. Keeping as regression tests.{_RESET}", flush=True)
                    # Keep the tests, they serve as regression coverage
                    validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
                else:
                    if not parallel_mode:
                        print(f"\n  {_YELLOW}{_BOLD}⚠⚠⚠ WARNING: All rubric tests PASS before implementation — tests may be too weak{_RESET}", flush=True)
                        print(f"  {_DIM}Regenerating tests...{_RESET}", flush=True)
                    test_file_path_val.unlink()
                    test_file_path_val, regen_logs, _tg_cost_i = await run_testgen_agent(rubric, key, blackbox_ctx, effective_dir, quiet=parallel_mode, task_spec=prompt)
                    testgen_cost += _tg_cost_i
                    testgen_logs.extend(regen_logs)
                    if test_file_path_val:
                        validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
                        if validation.status == "all_pass":
                            print(f"  {_DIM}Tests still pass — keeping as regression tests.{_RESET}", flush=True)

            if test_file_path_val and validation.status == "tdd_ok" and not parallel_mode:
                print(f"\n  {_DIM}{'─' * 50}{_RESET}", flush=True)
                print(f"  {_GREEN}✓{_RESET} Adversarial tests ready — {_BOLD}{validation.failed} failing{_RESET}, {_DIM}{validation.passed} regression{_RESET}", flush=True)
                print(f"  {_DIM}{'─' * 50}{_RESET}", flush=True)

        # Static quality validation — regenerate once on errors
        if test_file_path_val and rubric:
            from otto.test_validation import validate_test_quality
            quality_warnings = validate_test_quality(test_file_path_val, effective_dir)
            quality_errors = [w for w in quality_warnings if w.severity == "error"]
            if quality_errors:
                if not parallel_mode:
                    _log_warn(f"Test quality issues ({len(quality_errors)} errors) — regenerating")
                    for w in quality_errors[:5]:
                        print(f"    {w}", flush=True)
                # Build feedback from error messages for testgen agent
                feedback_lines = [w.message for w in quality_errors[:5]]
                quality_feedback = (
                    "Fix these test quality issues:\n"
                    + "\n".join(f"- {line}" for line in feedback_lines)
                )
                # Regenerate with feedback baked into prompt
                test_file_path_val.unlink(missing_ok=True)
                from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests
                task_hint = prompt + "\n" + "\n".join(rubric) + "\n\n" + quality_feedback
                regen_ctx = build_blackbox_context(effective_dir, task_hint=task_hint)
                test_file_path_val, _, _tg_cost_i = await run_testgen_agent(
                    rubric, key, regen_ctx, effective_dir, quiet=parallel_mode,
                )
                testgen_cost += _tg_cost_i
                if test_file_path_val:
                    validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
                    if validation.status == "collection_error":
                        if not parallel_mode:
                            _log_warn("Regenerated tests have collection errors — skipping")
                        test_file_path_val.unlink(missing_ok=True)
                        test_file_path_val = None
                    else:
                        # Re-validate quality (don't loop — one regen attempt only)
                        regen_warnings = validate_test_quality(test_file_path_val, effective_dir)
                        regen_errors = [w for w in regen_warnings if w.severity == "error"]
                        if regen_errors and not parallel_mode:
                            _log_warn(f"Regenerated tests still have {len(regen_errors)} quality issues — proceeding anyway")
                        elif not parallel_mode and validation.status == "tdd_ok":
                            print(f"  {_GREEN}✓{_RESET} {_DIM}Regenerated tests clean ({validation.failed} failing){_RESET}", flush=True)

        # Commit test file if we have one
        if test_file_path_val:
            subprocess.run(["git", "add", str(test_file_path_val.relative_to(effective_dir))],
                           cwd=effective_dir, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"otto: add rubric tests for task #{task_id}"],
                           cwd=effective_dir, capture_output=True)
            test_commit_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=effective_dir,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            test_file_sha = subprocess.run(
                ["git", "hash-object", str(test_file_path_val)],
                capture_output=True, text=True,
            ).stdout.strip()
    # (No fallback path — all tasks go through adversarial TDD or run with existing tests only)

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
    total_cost = testgen_cost  # start with testgen cost, add coding agent cost per attempt
    for attempt in range(max_retries + 1):
        attempt_num = attempt + 1
        if not parallel_mode:
            print(f"\n  {_DIM}attempt {attempt_num}/{max_retries + 1}{_RESET}", flush=True)

        if tasks_file:
            update_task(tasks_file, key, attempts=attempt_num)

        # Build agent prompt — include relevant source files so agent doesn't need to explore
        feedback = task.get("feedback", "")
        if attempt == 0 or last_error is None:
            base_prompt = prompt
            if feedback:
                base_prompt = f"{prompt}\n\nIMPORTANT feedback from the user:\n{feedback}"

            # Include relevant source files in the prompt
            from otto.testgen import get_relevant_file_contents
            source_context = get_relevant_file_contents(effective_dir, task_hint=prompt)

            # Include architect design context if available
            from otto.architect import load_design_context
            design_ctx = load_design_context(project_dir, role="coding")

            design_section = ""
            if design_ctx:
                design_section = (
                    f"\n\nDESIGN CONVENTIONS (follow these — other tasks depend on them):\n"
                    f"{design_ctx}\n"
                )

            # Include spec items if available
            spec_section = ""
            if rubric:
                spec_items = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(rubric))
                spec_section = f"\n\nACCEPTANCE SPEC (these are the hard requirements — meet ALL of them):\n{spec_items}\n"

            agent_prompt = (
                f"{base_prompt}\n\n"
                f"You are working in {effective_dir}. Do NOT create git commits.\n\n"
                f"RELEVANT SOURCE FILES (already read for you):\n"
                f"{source_context}"
                f"{design_section}"
                f"{spec_section}\n\n"
                f"APPROACH:\n"
                f"1. PLAN first — read the spec and current code. Can you meet ALL spec requirements\n"
                f"   with the current architecture? If not, briefly note what needs to change.\n"
                f"2. IMPLEMENT your plan.\n"
                f"3. VERIFY — run the acceptance tests yourself to check your work.\n"
                f"4. ITERATE — if tests fail, read the output and fix. Don't guess blindly.\n"
            )
        else:
            agent_prompt = (
                f"Verification failed. Fix the issue.\n\n"
                f"{last_error}\n\n"
                f"Original task: {prompt}\n\n"
                f"You are working in {effective_dir}. Do NOT create git commits.\n"
                f"Read the failing tests carefully. Is it a code bug or a test bug?\n"
                f"- Code bug: fix your implementation.\n"
                f"- Test bug (broken import, wrong stdlib usage): fix the test.\n"
                f"- Impossible constraint: explain why and implement the best feasible approach."
            )

        # Tell the coding agent about test files and permissions
        if test_file_path_val:
            agent_prompt += (
                f"\n\nACCEPTANCE TESTS: {test_file_path_val.relative_to(effective_dir)}\n"
                f"- You may FIX bugs in this file (broken imports, syntax errors, wrong stdlib usage).\n"
                f"- You may NOT weaken assertions (change thresholds, remove checks, add skip/xfail).\n"
                f"- If a test seems impossible to pass, explain why rather than hacking around it.\n\n"
                f"Other test files in tests/ are from previous tasks.\n"
                f"- If your changes intentionally break them, update their assertions.\n"
                f"- Do NOT delete tests — only update assertions.\n\n"
                f"You may also write additional tests to validate your approach."
            )

        # Warn coding agent if architect flagged this as a contract-breaking task
        if design_ctx and "CONTRACT CHANGE" in design_ctx:
            agent_prompt += (
                "\n\n⚠ This task may break existing tests by changing the API contract.\n"
                "If baseline tests fail because of your intentional changes, update their assertions."
            )

        # Run agent + build candidate + verify — catch infrastructure failures
        try:
            try:
                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(effective_dir),
                    max_turns=20,  # prevent infinite loops
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
                                if not parallel_mode:
                                    print(block.text, flush=True)
                                agent_log_lines.append(block.text)
                            elif ToolUseBlock and isinstance(block, ToolUseBlock):
                                if not parallel_mode:
                                    _print_tool_use(block)
                                agent_log_lines.append(f"● {block.name}  {_tool_use_summary(block)}")
                            elif ToolResultBlock and isinstance(block, ToolResultBlock):
                                if not parallel_mode:
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
                    effective_dir,
                    reset_ref=reset_sha,
                    pre_existing_untracked=pre_existing_untracked,
                )
                continue

            # Note: tamper detection removed. The coding agent is now trusted to
            # fix test bugs (broken imports, wrong stdlib usage) but not weaken
            # assertions. The spec items in the prompt serve as the ground truth —
            # if the agent weakens tests, re-running testgen from spec produces fresh ones.

            # Check if agent made any changes (compare against appropriate base)
            commit_base = test_commit_sha if test_commit_sha else base_sha
            diff_check = subprocess.run(
                ["git", "diff", "--quiet", commit_base],
                cwd=effective_dir, capture_output=True,
            )
            untracked_check = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=effective_dir, capture_output=True, text=True,
            )
            new_untracked = {f for f in untracked_check.stdout.strip().splitlines() if f} - pre_existing_untracked
            no_changes = diff_check.returncode == 0 and not new_untracked

            if no_changes and not test_file_path_val:
                # No code changes and no rubric tests — nothing to commit
                if not parallel_mode:
                    print(f"  {_DIM}No changes needed{_RESET}", flush=True)
                if not parallel_mode:
                    subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
                    cleanup_branch(project_dir, key, default_branch)
                timings["total"] = time.monotonic() - task_start
                if tasks_file:
                    updates = {"status": "passed", "duration_s": round(timings["total"], 1)}
                    if total_cost > 0:
                        updates["cost_usd"] = total_cost
                    update_task(tasks_file, key, **updates)
                _log_pass(task_id, default_branch, timings["total"], cost=total_cost)
                try:
                    (log_dir / "timing.log").write_text(
                        "\n".join(f"{k}: {v:.1f}s" for k, v in timings.items()) + "\n"
                    )
                except OSError:
                    pass
                return True

            # Build candidate commit
            # Rubric tests are already committed — testgen_file is always None
            # (old fallback path removed; adversarial tests committed before attempt loop)
            candidate_sha = build_candidate_commit(
                effective_dir, commit_base, None,
                pre_existing_untracked,
            )

            # Run verification in disposable worktree
            verify_result = run_verification(
                project_dir=effective_dir,
                candidate_sha=candidate_sha,
                test_command=test_command,
                verify_cmd=verify_cmd,
                timeout=timeout,
                exclude_test_files=sibling_test_files,
            )

        except Exception as e:
            # Unexpected error during agent/candidate/verify phases — safe to clean up
            _log_fail(task_id, f"unexpected error: {e}", time.monotonic() - task_start, cost=total_cost)
            if not parallel_mode:
                _cleanup_task_failure(
                    project_dir, key, default_branch, tasks_file,
                    pre_existing_untracked=pre_existing_untracked,
                    error=f"unexpected error: {e}", error_code="internal_error",
                    cost_usd=total_cost,
                    duration_s=time.monotonic() - task_start,
                )
            elif tasks_file:
                update_task(tasks_file, key,
                            status="failed", error=f"unexpected error: {e}",
                            error_code="internal_error",
                            cost_usd=total_cost,
                            duration_s=round(time.monotonic() - task_start, 1))
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

        if not parallel_mode:
            _log_verify(verify_result.tiers)

        if verify_result.passed:
            # Squash all branch commits into a single commit
            # When rubric tests exist, there are 2 commits (test + candidate) to squash
            try:
                # Reset to base_sha (not commit_base) to squash everything into one commit
                subprocess.run(
                    ["git", "reset", "--mixed", base_sha],
                    cwd=effective_dir, capture_output=True, check=True,
                )
                # Re-stage everything
                subprocess.run(
                    ["git", "add", "-u"],
                    cwd=effective_dir, capture_output=True, check=True,
                )
                untracked_final = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                    cwd=effective_dir, capture_output=True, text=True,
                )
                skip = pre_existing_untracked or set()
                for f in untracked_final.stdout.split("\0"):
                    if f and f not in skip:
                        subprocess.run(
                            ["git", "add", "--", f],
                            cwd=effective_dir, capture_output=True,
                        )
                # After git add -u and untracked staging, explicitly add the test file
                # (it was ADDED in the test commit, so after reset --mixed it's untracked
                # and gets filtered out by pre_existing_untracked)
                if test_file_path_val and test_file_path_val.exists():
                    subprocess.run(
                        ["git", "add", "--", str(test_file_path_val.relative_to(effective_dir))],
                        cwd=effective_dir, capture_output=True,
                    )
                subprocess.run(
                    ["git", "commit", "-m",
                     f"otto: {prompt[:60]} (#{task_id})"],
                    cwd=effective_dir, capture_output=True, text=True,
                    check=True,
                )
            except (subprocess.CalledProcessError, Exception) as e:
                stderr = getattr(e, "stderr", str(e))
                _log_fail(task_id, f"squash commit failed: {stderr}", time.monotonic() - task_start, cost=total_cost)
                if not parallel_mode:
                    _cleanup_task_failure(
                        project_dir, key, default_branch, tasks_file,
                        pre_existing_untracked=pre_existing_untracked,
                        error=f"squash commit failed: {stderr}", error_code="internal_error",
                        cost_usd=total_cost,
                        duration_s=time.monotonic() - task_start,
                    )
                elif tasks_file:
                    update_task(tasks_file, key,
                                status="failed", error=f"squash commit failed: {stderr}",
                                error_code="internal_error",
                                cost_usd=total_cost,
                                duration_s=round(time.monotonic() - task_start, 1))
                return False
            # Mutation check — validate test quality
            if not parallel_mode:
                print(flush=True)
            if test_file_path_val and not parallel_mode:
                try:
                    caught, mut_desc = run_mutation_check(
                        effective_dir, test_file_path_val, test_command or "pytest",
                    )
                    if caught:
                        print(f"  {_GREEN}{_BOLD}✓ Mutation check: caught{_RESET} {_DIM}— {mut_desc}{_RESET}", flush=True)
                    else:
                        print(f"  {_RED}{_BOLD}✗ Mutation check: NOT caught{_RESET} {_DIM}— tests may be weak{_RESET}", flush=True)
                        print(f"    {_DIM}{mut_desc}{_RESET}", flush=True)
                    # Persist mutation check result
                    try:
                        mut_log = log_dir / f"attempt-{attempt_num}-mutation.log"
                        mut_log.write_text(f"caught: {caught}\n{mut_desc}\n")
                    except OSError:
                        pass
                except Exception as e:
                    print(f"  {_DIM}Mutation check skipped: {e}{_RESET}", flush=True)

            # In parallel mode, caller handles merge — just return success
            if parallel_mode:
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                timings["total"] = time.monotonic() - task_start
                if tasks_file:
                    updates = {"status": "passed", "duration_s": round(timings["total"], 1)}
                    if total_cost > 0:
                        updates["cost_usd"] = total_cost
                    update_task(tasks_file, key, **updates)
                _log_pass(task_id, default_branch, timings["total"], cost=total_cost)
                try:
                    (log_dir / "timing.log").write_text(
                        "\n".join(f"{k}: {v:.1f}s" for k, v in timings.items()) + "\n"
                    )
                except OSError:
                    pass
                return True

            # Merge to default — post-merge bookkeeping errors are non-destructive
            if merge_to_default(project_dir, key, default_branch):
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                timings["total"] = time.monotonic() - task_start
                if tasks_file:
                    updates = {"status": "passed", "duration_s": round(timings["total"], 1)}
                    if total_cost > 0:
                        updates["cost_usd"] = total_cost
                    update_task(tasks_file, key, **updates)
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
                    test_file_path_val, effective_dir,
                )
                if test_bug:
                    print(f"  {_YELLOW}⚠ Detected test bug — regenerating adversarial tests{_RESET}", flush=True)
                    print(f"    {_DIM}{test_bug}{_RESET}", flush=True)
                    # Unwind, regenerate tests, and restart the attempt loop
                    subprocess.run(
                        ["git", "reset", "--hard", base_sha],
                        cwd=effective_dir, capture_output=True,
                    )
                    _remove_otto_created_untracked(effective_dir, pre_existing_untracked)
                    if not parallel_mode:
                        subprocess.run(["git", "checkout", default_branch],
                                       cwd=project_dir, capture_output=True)
                        cleanup_branch(project_dir, key, default_branch)
                        # Re-create branch and regenerate tests
                        base_sha = create_task_branch(project_dir, key, default_branch, task=task)
                    if test_file_path_val.exists():
                        test_file_path_val.unlink()
                    from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests
                    blackbox_ctx = build_blackbox_context(effective_dir, task_hint=task_hint)
                    test_file_path_val, testgen_logs, _tg_cost_i = await run_testgen_agent(
                        rubric, key, blackbox_ctx, effective_dir, quiet=parallel_mode
                    )
                    testgen_cost += _tg_cost_i
                    if test_file_path_val:
                        validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
                        if validation.status == "tdd_ok":
                            subprocess.run(["git", "add", str(test_file_path_val.relative_to(effective_dir))],
                                           cwd=effective_dir, capture_output=True)
                            subprocess.run(["git", "commit", "-m", f"otto: regenerate rubric tests for task #{task_id}"],
                                           cwd=effective_dir, capture_output=True)
                            test_commit_sha = subprocess.run(
                                ["git", "rev-parse", "HEAD"], cwd=effective_dir,
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
                cwd=effective_dir, capture_output=True,
            )
            last_error = verify_result.failure_output
            if not parallel_mode:
                _log_warn(f"Verification failed — retrying")

    # All retries exhausted
    if not parallel_mode:
        _cleanup_task_failure(
            project_dir, key, default_branch, tasks_file,
            pre_existing_untracked=pre_existing_untracked,
            error="max retries exhausted", error_code="max_retries",
            cost_usd=total_cost,
            duration_s=time.monotonic() - task_start,
        )
    elif tasks_file:
        update_task(tasks_file, key,
                    status="failed", error="max retries exhausted",
                    error_code="max_retries",
                    cost_usd=total_cost,
                    duration_s=round(time.monotonic() - task_start, 1))
    _log_fail(task_id, "all retries exhausted", time.monotonic() - task_start, cost=total_cost)
    return False


async def _review_cross_task_changes(
    passed_tasks: list[dict],
    project_dir: Path,
    config: dict,
    run_start_sha: str | None = None,
) -> bool:
    """Review all changes from independent coding agents for cross-task consistency.

    Runs a Claude agent that sees the combined diff and fixes inconsistencies
    (duplicate code, import conflicts, naming collisions). Returns True if
    review edits were made, False if no issues found or review failed.

    run_start_sha: SHA at the beginning of the current run. If provided, diffs
    only changes from this run (not old otto commits from prior runs).
    """
    # Use run_start_sha if available, otherwise find the first otto commit
    diff_base = run_start_sha
    if not diff_base:
        base_sha = subprocess.run(
            ["git", "log", "--format=%H", "--reverse", "--grep=otto:"],
            cwd=project_dir, capture_output=True, text=True,
        )
        for line in base_sha.stdout.strip().splitlines():
            if line.strip():
                parent = subprocess.run(
                    ["git", "rev-parse", f"{line.strip()}^"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                if parent.returncode == 0:
                    diff_base = parent.stdout.strip()
                break

    if not diff_base:
        return False

    diff_result = subprocess.run(
        ["git", "diff", diff_base, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if not diff_result.stdout.strip():
        return False

    # Truncate diff if too large
    combined_diff = diff_result.stdout
    if len(combined_diff) > 15000:
        combined_diff = combined_diff[:15000] + "\n... (truncated)"

    task_list = "\n".join(
        f"- Task #{t.get('id', '?')}: {t.get('prompt', '')[:80]}"
        for t in passed_tasks
    )

    prompt = f"""You are a senior engineer reviewing implementations from {len(passed_tasks)} coding agents.
Each agent worked independently on a separate feature. Your job: find and fix
inconsistencies across their combined changes.

TASKS IMPLEMENTED:
{task_list}

FULL DIFF (all task changes combined):
{combined_diff}

Look for:
- Duplicate code that should be a shared helper
- Inconsistent error handling patterns
- Import conflicts or naming collisions
- Missing edge cases one agent handled but another didn't

Fix issues directly. Do NOT create git commits.
Only fix real problems — don't refactor for style.
If everything looks consistent, just say "No cross-task issues found."
"""

    print(f"  {_DIM}Reviewing cross-task consistency...{_RESET}", flush=True)

    # Snapshot untracked files before review so we only revert otto-created ones
    pre_review_untracked = _snapshot_untracked(project_dir)

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
            max_turns=15,
        )
        if config.get("model"):
            agent_opts.model = config["model"]

        made_changes = False
        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                pass
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                pass
            elif AssistantMessage and isinstance(message, AssistantMessage):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        _print_tool_use(block)
                        if block.name in ("Edit", "Write", "Bash"):
                            made_changes = True
                    elif ToolResultBlock and isinstance(block, ToolResultBlock):
                        _print_tool_result(block)

        if made_changes:
            # Verify that review edits didn't break anything
            test_command = config.get("test_command")
            if test_command:
                check = subprocess.run(
                    test_command, shell=True, cwd=project_dir,
                    capture_output=True, timeout=config["verify_timeout"],
                    env=_subprocess_env(),
                )
                if check.returncode != 0:
                    # Review edits broke tests — revert tracked files and only
                    # remove otto-created untracked files (not user-owned ones)
                    subprocess.run(
                        ["git", "checkout", "."],
                        cwd=project_dir, capture_output=True,
                    )
                    _remove_otto_created_untracked(project_dir, pre_review_untracked)
                    print(f"  {_YELLOW}⚠ Review edits broke tests — reverted{_RESET}", flush=True)
                    return False

            # Commit review fixes — stage tracked changes + otto-created untracked files
            subprocess.run(["git", "add", "-u"], cwd=project_dir, capture_output=True)
            new_untracked = _snapshot_untracked(project_dir) - pre_review_untracked
            for f in new_untracked:
                subprocess.run(["git", "add", "--", f], cwd=project_dir, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "otto: cross-task consistency review fixes"],
                cwd=project_dir, capture_output=True,
            )
            print(f"  {_GREEN}✓{_RESET} {_DIM}Cross-task review fixes applied{_RESET}", flush=True)
            return True

    except Exception as e:
        print(f"  {_DIM}Cross-task review skipped: {e}{_RESET}", flush=True)

    return False


async def _run_integration_gate(
    passed_tasks: list[dict],
    config: dict,
    project_dir: Path,
    ripple_risks: list[tuple[int, str, str]] | None = None,
    run_start_sha: str | None = None,
) -> bool | None:
    """Generate and run cross-feature integration tests. Returns True if passed."""
    from otto.testgen import generate_integration_tests
    from otto.verify import run_integration_gate

    test_command = config.get("test_command")
    timeout = config["verify_timeout"]
    max_retries = config.get("max_retries", 3)
    pre_existing_untracked = _snapshot_untracked(project_dir)

    # Phase A: Review all changes for cross-task consistency (new in v2)
    if len(passed_tasks) >= 2:
        await _review_cross_task_changes(
            passed_tasks, project_dir, config, run_start_sha=run_start_sha,
        )

    print(flush=True)
    _log_info("Integration gate — testing features together")
    print(f"  {_DIM}Generating cross-feature integration tests...{_RESET}", flush=True)

    # Generate integration tests
    integration_file = None
    try:
        integration_file = await generate_integration_tests(
            passed_tasks, project_dir, ripple_risks=ripple_risks,
        )
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
                    max_turns=15,
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


def _record_changed_files(
    project_dir: Path,
    tasks_file: Path,
    task_key: str,
    base_sha: str,
) -> list[str]:
    """Record which files a task changed (for reconciliation)."""
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    changed = [f for f in result.stdout.strip().splitlines() if f]
    if changed:
        update_task(tasks_file, task_key, changed_files=changed)
    return changed


def _reconcile_dependencies(
    tasks_file: Path,
    project_dir: Path,
) -> list[tuple[int, str, str]]:
    """Post-run reconciliation: detect hidden dependencies and ripple risks.

    Returns list of (task_id, changed_file, affected_file) ripple risks.
    """
    from otto.testgen import _build_project_index

    tasks = load_tasks(tasks_file)
    passed = [t for t in tasks if t.get("status") == "passed" and t.get("changed_files")]
    if len(passed) < 2:
        return []

    # Build import graph from project source files
    source_result = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=project_dir, capture_output=True, text=True,
    )
    source_files = [f for f in source_result.stdout.strip().splitlines() if f]
    _, import_graph = _build_project_index(project_dir, source_files)

    # Invert: file → files that depend on it
    dependents: dict[str, set[str]] = {}
    for importer, imported_set in import_graph.items():
        for imported in imported_set:
            dependents.setdefault(imported, set()).add(importer)

    # Track which files each task changed
    task_files: dict[int, set[str]] = {}
    all_changed: dict[str, int] = {}  # file → task_id that changed it
    for t in passed:
        tid = t["id"]
        files = set(t.get("changed_files") or [])
        task_files[tid] = files
        for f in files:
            all_changed[f] = tid

    # Build dependency lookup
    task_by_id = {t["id"]: t for t in tasks}

    warnings_printed = False
    updated_deps = False
    ripple_risks: list[tuple[int, str, str]] = []

    # Level 1: File overlap between independent tasks
    for i, t1 in enumerate(passed):
        for t2 in passed[i + 1:]:
            # Check if they have a declared dependency
            t1_deps = set(t1.get("depends_on") or [])
            t2_deps = set(t2.get("depends_on") or [])
            if t2["id"] in t1_deps or t1["id"] in t2_deps:
                continue  # Already declared dependency
            overlap = task_files.get(t1["id"], set()) & task_files.get(t2["id"], set())
            if overlap:
                if not warnings_printed:
                    print(f"\n  {_YELLOW}{_BOLD}Reconciliation warnings:{_RESET}", flush=True)
                    warnings_printed = True
                print(f"  {_YELLOW}⚠ Tasks #{t1['id']} and #{t2['id']} both modified: {', '.join(sorted(overlap))}{_RESET}", flush=True)
                print(f"    {_DIM}→ Hidden dependency detected. Updating depends_on.{_RESET}", flush=True)
                # Update depends_on: later task depends on earlier
                deps = list(t2.get("depends_on") or [])
                if t1["id"] not in deps:
                    deps.append(t1["id"])
                    update_task(tasks_file, t2["key"], depends_on=deps)
                    updated_deps = True

    # Level 2: Import graph ripple analysis
    for t in passed:
        tid = t["id"]
        for changed_file in task_files.get(tid, []):
            affected_files = dependents.get(changed_file, set())
            for affected in affected_files:
                # Case A: affected file changed by another task → hidden dependency
                if affected in all_changed and all_changed[affected] != tid:
                    other_tid = all_changed[affected]
                    other_task = task_by_id.get(other_tid)
                    if other_task:
                        other_deps = set(other_task.get("depends_on") or [])
                        t_deps = set(t.get("depends_on") or [])
                        if tid not in other_deps and other_tid not in t_deps:
                            if not warnings_printed:
                                print(f"\n  {_YELLOW}{_BOLD}Reconciliation warnings:{_RESET}", flush=True)
                                warnings_printed = True
                            print(f"  {_YELLOW}⚠ Task #{tid} changed {changed_file}, "
                                  f"Task #{other_tid} changed {affected} which imports it{_RESET}", flush=True)
                            deps = list(other_task.get("depends_on") or [])
                            if tid not in deps:
                                deps.append(tid)
                                update_task(tasks_file, other_task["key"], depends_on=deps)
                                updated_deps = True

                # Case B: affected file NOT changed by any task → ripple risk
                elif affected not in all_changed:
                    ripple_risks.append((tid, changed_file, affected))

    # Print ripple risks
    if ripple_risks:
        if not warnings_printed:
            print(f"\n  {_YELLOW}{_BOLD}Reconciliation warnings:{_RESET}", flush=True)
        seen: set[str] = set()
        for tid, changed, affected in ripple_risks:
            key = f"{changed}->{affected}"
            if key not in seen:
                seen.add(key)
                print(f"  {_YELLOW}⚠ Ripple risk: Task #{tid} changed {changed}{_RESET}", flush=True)
                print(f"    {_DIM}{affected} imports {changed} but was not part of any task{_RESET}", flush=True)

    if updated_deps:
        print(f"  {_DIM}Updated depends_on in tasks.yaml for future retries{_RESET}", flush=True)

    return ripple_risks


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
            # Exit code 5 = "no tests collected" (empty test suite) — not a failure
            if result.returncode not in (0, 5):
                print(f"  {_RED}✗ Baseline tests failing — fix before running otto{_RESET}", flush=True)
                return 2

        # Recover stale "running" tasks from prior crashed runs
        tasks = load_tasks(tasks_file)
        for t in tasks:
            if t.get("status") == "running":
                update_task(tasks_file, t["key"], status="pending",
                            error=None, session_id=None)
                print(f"  {_YELLOW}⚠ Task #{t['id']} was stuck in 'running' — reset to pending{_RESET}", flush=True)

        # Auto-unblock tasks whose dependencies are now pending (retried)
        tasks = load_tasks(tasks_file)
        pending_or_passed = {t["id"] for t in tasks if t.get("status") in ("pending", "passed")}
        for t in tasks:
            if t.get("status") == "blocked":
                deps = t.get("depends_on") or []
                still_blocked = [d for d in deps if d not in pending_or_passed]
                if not still_blocked:
                    update_task(tasks_file, t["key"], status="pending",
                                error=None)
                    print(f"  {_YELLOW}⚠ Task #{t['id']} unblocked — dependencies now pending/passed{_RESET}", flush=True)

        # Process tasks with dependency ordering
        tasks = load_tasks(tasks_file)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            print(f"{_DIM}No pending tasks{_RESET}", flush=True)
            return 0

        # Architect phase — analyze codebase and produce shared conventions
        if not config.get("no_architect", False) and len(pending) >= 2:
            from otto.architect import run_architect_agent, is_stale
            arch_dir = project_dir / "otto_arch"

            should_run = not arch_dir.exists()
            if arch_dir.exists() and is_stale(project_dir):
                should_run = True
                print(f"  {_YELLOW}⚠ Architecture docs stale — refreshing{_RESET}", flush=True)
            # Always run if file-plan.md is missing (needed for dependency injection)
            if arch_dir.exists() and not (arch_dir / "file-plan.md").exists():
                should_run = True

            if should_run:
                action = "Analyzing codebase" if not arch_dir.exists() else "Refreshing"
                _log_info(f"Architect — {action}")
                try:
                    arch_path = await run_architect_agent(pending, project_dir)
                    if arch_path:
                        print(f"  {_GREEN}✓{_RESET} {_DIM}Architecture docs ready{_RESET}", flush=True)
                        # Commit conftest.py if architect generated it
                        conftest = project_dir / "tests" / "conftest.py"
                        if conftest.exists():
                            subprocess.run(
                                ["git", "add", str(conftest.relative_to(project_dir))],
                                cwd=project_dir, capture_output=True,
                            )
                            subprocess.run(
                                ["git", "commit", "-m", "otto: architect conftest.py"],
                                cwd=project_dir, capture_output=True,
                            )
                except Exception as e:
                    _log_warn(f"Architect failed: {e} — continuing without design docs")

        # Phase 1: Inject dependencies from architect's file-plan.md
        from otto.architect import parse_file_plan
        arch_deps = parse_file_plan(project_dir)
        if arch_deps:
            # Reload tasks to get current state after potential architect changes
            tasks = load_tasks(tasks_file)
            pending = [t for t in tasks if t.get("status") == "pending"]
            pending_by_id_tmp = {t["id"]: t for t in pending}
            injected = 0
            for dep_id, on_id in arch_deps:
                task = pending_by_id_tmp.get(dep_id)
                if task:
                    deps = list(task.get("depends_on") or [])
                    if on_id not in deps:
                        deps.append(on_id)
                        update_task(tasks_file, task["key"], depends_on=deps)
                        injected += 1
            if injected:
                print(f"  {_DIM}Injected {injected} dependencies from file-plan.md{_RESET}", flush=True)
                # Reload tasks after dependency injection
                tasks = load_tasks(tasks_file)
                pending = [t for t in tasks if t.get("status") == "pending"]

        # Build sets for dependency resolution
        pending_ids = {t["id"] for t in pending}
        pending_by_id = {t["id"]: t for t in pending}
        # Find tasks that failed in prior runs (dependencies that can't be satisfied)
        all_tasks_by_id = {t["id"]: t for t in tasks}
        prior_failed_ids = {
            t["id"] for t in tasks
            if t.get("status") in ("failed", "blocked")
        }

        # Build topological sorter
        ts = graphlib.TopologicalSorter()
        blocked_before_start: list[tuple[dict, int]] = []  # (task, failed_dep_id)
        for t in pending:
            deps = t.get("depends_on") or []
            # Check if any dependency failed in a prior run
            failed_deps = [d for d in deps if d in prior_failed_ids]
            if failed_deps:
                blocked_before_start.append((t, failed_deps[0]))
                continue
            # Only include pending deps (already-passed deps are satisfied)
            pending_deps = [d for d in deps if d in pending_ids]
            ts.add(t["id"], *pending_deps)

        # Mark pre-blocked tasks
        for t, failed_dep in blocked_before_start:
            dep_task = all_tasks_by_id.get(failed_dep)
            dep_label = f"#{failed_dep}" if not dep_task else f"#{failed_dep}"
            update_task(tasks_file, t["key"],
                        status="blocked",
                        error=f"dependency {dep_label} failed")
            print(f"  {_RED}✗ Task #{t['id']} blocked{_RESET} {_DIM}— dependency #{failed_dep} failed{_RESET}", flush=True)

        try:
            ts.prepare()
        except graphlib.CycleError as e:
            print(f"  {_RED}✗ Dependency cycle detected: {e}{_RESET}", flush=True)
            return 2

        max_parallel = config.get("max_parallel", 3)
        semaphore = asyncio.Semaphore(max_parallel)

        run_start = time.monotonic()
        # Capture SHA at run start for scoped cross-task review diff
        run_start_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir, capture_output=True, text=True,
        )
        _run_start_sha = run_start_sha_result.stdout.strip() if run_start_sha_result.returncode == 0 else None
        results: list[tuple[dict, bool]] = []  # (task, success)
        failed_ids: set[int] = set()

        async def _run_in_worktree(
            t: dict, base_sha: str, pre_test: Path | None = None,
            sibling_tests: list[Path] | None = None,
        ) -> tuple[dict, bool]:
            """Run a task in an isolated worktree for parallel execution."""
            async with semaphore:
                wt_dir = _setup_task_worktree(project_dir, t["key"], base_sha)
                try:
                    # Remap pre-generated test path to worktree location
                    wt_test = None
                    if pre_test and pre_test.exists():
                        rel = pre_test.relative_to(project_dir)
                        wt_test = wt_dir / rel
                    success = await run_task(
                        t, config, project_dir, tasks_file,
                        work_dir=wt_dir, pre_generated_test=wt_test,
                        sibling_test_files=sibling_tests,
                    )
                    return (t, success)
                finally:
                    _teardown_task_worktree(project_dir, t["key"])

        async def _merge_task_branch(key: str, default_branch_name: str) -> bool:
            """Merge a task branch to default with rebase retry."""
            if merge_to_default(project_dir, key, default_branch_name):
                return True
            # ff-only failed (main advanced from earlier merge) — try rebase
            branch_name = f"otto/{key}"
            rebase = subprocess.run(
                ["git", "rebase", default_branch_name, branch_name],
                cwd=project_dir, capture_output=True,
            )
            if rebase.returncode != 0:
                # Rebase conflict — abort and report
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=project_dir, capture_output=True,
                )
                return False
            return merge_to_default(project_dir, key, default_branch_name)

        while ts.is_active():
            if interrupted:
                _log_warn("Interrupted — cleaning up")
                break
            ready_ids = list(ts.get_ready())

            # Filter out blocked tasks first
            runnable: list[dict] = []
            for task_id in ready_ids:
                task = pending_by_id[task_id]
                task_deps = task.get("depends_on") or []
                blocked_by = [d for d in task_deps if d in failed_ids]
                if blocked_by:
                    update_task(tasks_file, task["key"],
                                status="blocked",
                                error=f"dependency #{blocked_by[0]} failed")
                    print(f"  {_RED}✗ Task #{task_id} blocked{_RESET} {_DIM}— dependency #{blocked_by[0]} failed{_RESET}", flush=True)
                    results.append((task, False))
                    ts.done(task_id)
                else:
                    runnable.append(task)

            if not runnable:
                continue

            if (len(runnable) == 1 or max_parallel <= 1) and not interrupted:
                # Single task or --no-parallel — run in main tree (full streaming output)
                for task in runnable:
                    if interrupted:
                        ts.done(task["id"])
                        continue
                    current_task_key = task["key"]
                    if not check_clean_tree(project_dir):
                        print(f"  {_RED}✗ Working tree is dirty — aborting{_RESET}", flush=True)
                        return 2
                    current_pre_existing_untracked = _snapshot_untracked(project_dir)
                    pre_task_sha = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=project_dir, capture_output=True, text=True,
                    ).stdout.strip()
                    success = await run_task(task, config, project_dir, tasks_file)
                    if success:
                        _record_changed_files(project_dir, tasks_file, task["key"], pre_task_sha)
                    results.append((task, success))
                    if not success:
                        failed_ids.add(task["id"])
                    current_task_key = None
                    current_pre_existing_untracked = None
                    ts.done(task["id"])
            elif not interrupted:
                # Multiple ready — run in parallel worktrees
                print(f"\n  {_CYAN}{_BOLD}⚡ Running {len(runnable)} tasks in parallel{_RESET}", flush=True)

                # Testgen phase — holistic for consistency, with per-task fallback
                pre_testgen_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project_dir, capture_output=True, text=True, check=True,
                ).stdout.strip()
                pre_tests: dict[str, Path | None] = {}

                # Identify tasks with rubrics for testgen
                runnable_with_rubrics = [t for t in runnable if t.get("rubric")]
                tasks_without_rubrics = [t for t in runnable if not t.get("rubric")]
                for t in tasks_without_rubrics:
                    pre_tests[t["key"]] = None

                if runnable_with_rubrics and not interrupted:
                    from otto.testgen import (
                        build_blackbox_context, run_holistic_testgen,
                        run_testgen_agent, validate_generated_tests,
                    )

                    # Try holistic testgen first (all tasks at once)
                    print(f"  {_DIM}Holistic testgen for {len(runnable_with_rubrics)} tasks...{_RESET}", flush=True)
                    all_hints = " ".join(
                        t["prompt"] + " " + " ".join(t.get("rubric", []))
                        for t in runnable_with_rubrics
                    )
                    ctx = build_blackbox_context(project_dir, task_hint=all_hints)
                    holistic_results = await run_holistic_testgen(
                        runnable_with_rubrics, project_dir, ctx, quiet=True,
                    )

                    # Validate holistic results, fall back to per-task for failures
                    for t in runnable_with_rubrics:
                        if interrupted:
                            break
                        key = t["key"]
                        test_path = holistic_results.get(key)

                        if test_path:
                            validation = validate_generated_tests(test_path, "pytest", project_dir)
                            if validation.status == "collection_error":
                                test_path = None  # Will fall back below

                        if not test_path:
                            # Per-task fallback
                            print(f"  {_DIM}[#{t['id']}] Holistic test failed — per-task fallback...{_RESET}", flush=True)
                            task_hint = t["prompt"] + "\n" + "\n".join(t.get("rubric", []))
                            task_ctx = build_blackbox_context(project_dir, task_hint=task_hint)
                            test_path, _, _tg_cost_i = await run_testgen_agent(
                                t["rubric"], key, task_ctx, project_dir, quiet=True,
                            )
                            testgen_cost += _tg_cost_i
                            if test_path:
                                validation = validate_generated_tests(test_path, "pytest", project_dir)
                                if validation.status == "collection_error":
                                    test_path.unlink()
                                    test_path = None

                        # Static quality validation — regenerate on errors
                        if test_path and t.get("rubric"):
                            from otto.test_validation import validate_test_quality
                            qw = validate_test_quality(test_path, project_dir)
                            qe = [w for w in qw if w.severity == "error"]
                            if qe:
                                print(f"  {_DIM}[#{t['id']}] Test quality: {len(qe)} errors — regenerating{_RESET}", flush=True)
                                for w in qe[:3]:
                                    print(f"    {_DIM}{w}{_RESET}", flush=True)
                                # Regenerate with feedback
                                feedback = "\n".join(f"- {w.message}" for w in qe[:5])
                                test_path.unlink(missing_ok=True)
                                task_hint = t["prompt"] + "\n" + "\n".join(t.get("rubric", [])) + "\n\nFix these test issues:\n" + feedback
                                task_ctx = build_blackbox_context(project_dir, task_hint=task_hint)
                                test_path, _, _tg_cost_i = await run_testgen_agent(
                                    t["rubric"], key, task_ctx, project_dir, quiet=True,
                                )
                                testgen_cost += _tg_cost_i
                                if test_path:
                                    validation = validate_generated_tests(test_path, "pytest", project_dir)
                                    if validation.status == "collection_error":
                                        test_path.unlink(missing_ok=True)
                                        test_path = None

                        pre_tests[key] = test_path

                    # Commit conftest.py if holistic testgen created it
                    conftest = project_dir / "tests" / "conftest.py"
                    if conftest.exists():
                        subprocess.run(
                            ["git", "add", str(conftest.relative_to(project_dir))],
                            cwd=project_dir, capture_output=True,
                        )
                        subprocess.run(
                            ["git", "commit", "-m", "otto: holistic testgen conftest.py"],
                            cwd=project_dir, capture_output=True,
                        )

                    # Copy test files into tests/ and commit so worktrees see them.
                    # testgen stores output under .git/otto/testgen/ which git won't add,
                    # so we copy to the proper location first.
                    for t in runnable_with_rubrics:
                        test_path = pre_tests.get(t["key"])
                        if test_path and test_path.exists():
                            dest = project_dir / "tests" / test_path.name
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(test_path), str(dest))
                            pre_tests[t["key"]] = dest  # update to repo-relative path
                            rel = str(dest.relative_to(project_dir))
                            subprocess.run(["git", "add", rel], cwd=project_dir, capture_output=True)
                            subprocess.run(
                                ["git", "commit", "-m", f"otto: pre-generate tests for #{t['id']}"],
                                cwd=project_dir, capture_output=True,
                            )

                base_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project_dir, capture_output=True, text=True, check=True,
                ).stdout.strip()

                # Build sibling test file lists — actual paths from pre-generation,
                # so exclusion works regardless of test framework.
                coros = []
                for t in runnable:
                    sibling_paths = [
                        p.relative_to(project_dir)
                        for k, p in pre_tests.items()
                        if k != t["key"] and p is not None
                    ]
                    coros.append(_run_in_worktree(
                        t, base_sha, pre_tests.get(t["key"]),
                        sibling_tests=sibling_paths or None,
                    ))

                # Reset main to before pre-generated test commits — worktrees already branched
                if base_sha != pre_testgen_sha:
                    subprocess.run(
                        ["git", "reset", "--hard", pre_testgen_sha],
                        cwd=project_dir, capture_output=True,
                    )

                parallel_results = await asyncio.gather(*coros, return_exceptions=True)

                # Sequential merge for successful tasks
                for i, result in enumerate(parallel_results):
                    t = runnable[i]
                    if isinstance(result, Exception):
                        _log_fail(t["id"], f"parallel task error: {result}")
                        results.append((t, False))
                        failed_ids.add(t["id"])
                    else:
                        _, success = result
                        if success:
                            pre_merge_sha = subprocess.run(
                                ["git", "rev-parse", "HEAD"],
                                cwd=project_dir, capture_output=True, text=True,
                            ).stdout.strip()
                            merged = await _merge_task_branch(t["key"], default_branch)
                            if not merged:
                                _log_fail(t["id"], f"merge failed after parallel run")
                                if tasks_file:
                                    update_task(tasks_file, t["key"],
                                                status="failed",
                                                error="merge conflict after parallel execution",
                                                error_code="merge_diverged")
                                results.append((t, False))
                                failed_ids.add(t["id"])
                            else:
                                _record_changed_files(project_dir, tasks_file, t["key"], pre_merge_sha)
                                results.append((t, True))
                        else:
                            results.append((t, False))
                            failed_ids.add(t["id"])
                    # Clean up worktree branch if merge didn't delete it
                    subprocess.run(
                        ["git", "branch", "-D", f"otto/{t['key']}"],
                        cwd=project_dir, capture_output=True,
                    )
                    ts.done(t["id"])

        # Cleanup on interruption
        if interrupted and current_task_key:
            _cleanup_task_failure(
                project_dir, current_task_key, default_branch,
                tasks_file,
                pre_existing_untracked=current_pre_existing_untracked,
                error="interrupted", error_code="interrupted",
            )
            return 1

        # Post-run reconciliation — detect hidden dependencies and ripple risks
        passed_tasks = [t for t, s in results if s]
        ripple_risks: list[tuple[int, str, str]] = []
        if len(passed_tasks) >= 2:
            ripple_risks = _reconcile_dependencies(tasks_file, project_dir)

            # Feed reconciliation learnings back to architect docs
            if ripple_risks:
                from otto.architect import feed_reconciliation_learnings
                warnings = [
                    f"Task #{tid} changed {changed}, {affected} imports it but was not part of any task"
                    for tid, changed, affected in ripple_risks
                ]
                feed_reconciliation_learnings(project_dir, warnings)

        # Integration gate — run after 2+ tasks pass
        integration_result: bool | None = None  # None=skipped/not-run, True=passed, False=failed
        skip_integration = config.get("no_integration", False)

        if len(passed_tasks) >= 2 and not skip_integration:
            integration_result = await _run_integration_gate(
                passed_tasks, config, project_dir,
                ripple_risks=ripple_risks,
                run_start_sha=_run_start_sha,
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
