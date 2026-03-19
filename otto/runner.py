"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import os
import shutil
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

# Optional imports (may not exist in older SDK versions)
try:
    from claude_agent_sdk.types import AgentDefinition
except (ImportError, AttributeError):
    AgentDefinition = None  # type: ignore[assignment,misc]
try:
    from claude_agent_sdk.types import ThinkingBlock
except (ImportError, AttributeError):
    ThinkingBlock = None  # type: ignore[assignment,misc]

from otto.config import git_meta_dir, detect_test_command
from otto.display import _truncate_at_word
from otto.tasks import load_tasks, update_task
from otto.testgen import detect_test_framework, test_file_path
from otto.verify import VerifyResult, run_verification, _subprocess_env




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
) -> bool:
    """Run a single task through the full loop. Returns True if passed.

    When work_dir is set (parallel mode), the task runs in an isolated worktree:
    - Branch creation and merge are handled by the caller
    - All agent/git operations use work_dir as cwd
    - tasks_file operations still use project_dir (via flock)
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

    # Auto-detect test_command if spec exists but no test_command configured
    spec = task.get("spec")
    if spec and not test_command:
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

    # Setup log directory
    log_dir = project_dir / "otto_logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)

    # Optional TDD mode: generate adversarial tests before coding
    test_file_path_val = None
    tdd_commit_sha = None  # SHA after TDD tests committed (for retry reset)
    if config.get("tdd", False) and spec:
        from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests
        if not parallel_mode:
            print(f"  {_DIM}TDD mode: generating adversarial tests ({len(spec)} criteria)...{_RESET}", flush=True)
        task_hint = prompt + "\n" + "\n".join(spec)
        blackbox_ctx = build_blackbox_context(effective_dir, task_hint=task_hint)
        test_file_path_val, _, _tg_cost = await run_testgen_agent(
            spec, key, blackbox_ctx, effective_dir, quiet=parallel_mode, task_spec=prompt,
        )
        if test_file_path_val:
            validation = validate_generated_tests(test_file_path_val, "pytest", effective_dir)
            if validation.status in ("collection_error", "no_tests"):
                if not parallel_mode:
                    _log_warn(f"Generated tests unusable ({validation.status}) — skipping TDD tests")
                test_file_path_val.unlink()
                test_file_path_val = None
            elif validation.status == "all_pass":
                if not parallel_mode:
                    _log_warn("All TDD tests pass before implementation — tests may be too weak, skipping")
                test_file_path_val.unlink()
                test_file_path_val = None
            else:
                # Commit test file
                subprocess.run(["git", "add", str(test_file_path_val.relative_to(effective_dir))],
                               cwd=effective_dir, capture_output=True)
                subprocess.run(["git", "commit", "-m", f"otto: TDD tests for task #{task_id}"],
                               cwd=effective_dir, capture_output=True)
                tdd_commit_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=effective_dir,
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                if not parallel_mode:
                    print(f"  {_GREEN}✓{_RESET} {_DIM}TDD tests ready ({validation.failed} failing, {validation.passed} passing){_RESET}", flush=True)

    session_id = None
    last_error = None  # verification failure output for retry feedback
    total_cost = 0.0
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

            # Include spec items if available — classified as verifiable or visual
            spec_section = ""
            if spec:
                from otto.tasks import spec_text, spec_is_verifiable, spec_test_hint
                spec_lines = []
                for i, item in enumerate(spec):
                    text = spec_text(item)
                    if spec_is_verifiable(item):
                        hint = spec_test_hint(item)
                        hint_str = f"\n     Test hint: {hint}" if hint else ""
                        spec_lines.append(f"  {i+1}. [verifiable] {text}{hint_str}")
                    else:
                        spec_lines.append(f"  {i+1}. [visual] {text}")
                spec_section = f"\n\nACCEPTANCE SPEC (meet ALL of them):\n" + "\n".join(spec_lines) + "\n"

            # Persistent memory
            learnings_file = project_dir / "otto_arch" / "learnings.md"
            learnings_section = ""
            if learnings_file.exists():
                learnings_section = f"\nLEARNINGS (from previous tasks):\n{learnings_file.read_text()}\n"

            task_notes_path = project_dir / "otto_arch" / "task-notes" / f"{key}.md"
            task_notes_section = ""
            if task_notes_path.exists():
                task_notes_section = f"\nTASK NOTES (from previous attempts):\n{task_notes_path.read_text()}\n"

            agent_prompt = f"""{base_prompt}

You are working in {effective_dir}. Do NOT create git commits.

RELEVANT SOURCE FILES (already read for you):
{source_context}
{design_section}
{spec_section}

APPROACH:
1. PLAN — read the spec and codebase. Can current architecture meet ALL requirements?
   If not, note what needs to change and design an approach that CAN meet them.
2. IMPLEMENT your plan.
3. WRITE TESTS that verify each spec item — test the hardest case, not just the happy path.
4. RUN TESTS and fix failures. Iterate until all pass.
5. VERIFY — re-read each spec item. For each, ask: does my implementation handle the
   hardest case? If a spec says "<300ms", does it work for cold fetches, not just cache hits?
6. Write notes to otto_arch/task-notes/{key}.md:
   - What approach you took and why
   - What you learned about the codebase
   - Any gotchas for future tasks
   - Any spec items that couldn't be fully met and why

If your first approach doesn't meet a hard constraint, don't give up —
rethink the architecture. Try at least 3 different approaches before
concluding anything is infeasible.

{learnings_section}
{task_notes_section}
"""
            # TDD mode: tell coding agent about pre-generated test files
            if test_file_path_val and test_file_path_val.exists():
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

        # Run agent + build candidate + verify — catch infrastructure failures
        try:
            try:
                _coding_system_prompt = """\
<role>
You are an autonomous coding agent. You implement features, fix bugs, and write tests.
Your work is verified externally — you must meet the acceptance spec exactly.
</role>

<spec_rules>
SPEC COMPLIANCE — your highest priority:
- The acceptance spec is the contract. Meet EVERY item, not just the easy ones.
- A spec item like "<300ms latency" means ALL requests, not just cached/warm ones.
  If you only optimize the fast path, you haven't met the spec.
- When you think a constraint is met, test the HARDEST case, not the easiest.
  If "<300ms" works for cache hits but not cold fetches, it's not met.
- If a constraint is genuinely impossible (e.g., network latency to external API),
  implement the best feasible approach AND write a note explaining what was tried
  and why the hard limit can't be met. Do not silently declare success.

SPEC-DODGING (never do this):
- Meeting a performance constraint by only measuring the fast path
- Meeting a feature constraint by stubbing/mocking instead of implementing
- Meeting a constraint by removing the thing that was hard to optimize
- Declaring a constraint met when only some cases pass

<example type="violation">
Spec: "e2e latency <300ms"
BAD: Add caching, measure cache hits at <1ms, declare spec met.
WHY: Cold fetches still take 500ms+. Only the easy case was solved.
BETTER: Cache + prefetch + parallel requests + stale-while-revalidate.
         If still >300ms on cold fetch, explain what was tried in task notes.
</example>
</spec_rules>

<autonomy>
- You are running AUTONOMOUSLY. Do NOT ask questions or wait for input.
- Make decisions yourself. If unsure, pick the best option and document why.
- Create a .gitignore if the project needs one (node_modules/, __pycache__/, etc.)
</autonomy>

<completion_check>
Before you finish, verify against the spec:
1. Re-read each spec item.
2. For each [verifiable] item: name the specific test that proves it.
   If no test exists for a verifiable item, write one now.
3. For each [verifiable] item: test the HARDEST case, not the easiest.
4. For [visual] items: implement your best judgment, no test required.
5. If any verifiable item can't be met after trying 3+ different approaches,
   document what was tried in task notes. Do not silently skip it.
</completion_check>"""

                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(effective_dir),
                    max_turns=config.get("max_turns", 200),
                    setting_sources=["user", "project"],
                    env=_subprocess_env(),
                    effort=config.get("effort", "high"),
                    system_prompt=_coding_system_prompt,
                )
                if config.get("model"):
                    agent_opts.model = config["model"]
                if session_id:
                    agent_opts.resume = session_id
                # Subagents for parallelized work within a task
                if AgentDefinition:
                    try:
                        agent_opts.agents = {
                            "researcher": AgentDefinition(
                                description="Research APIs, read docs, investigate approaches",
                                prompt="You are a research assistant. Investigate the topic thoroughly and report findings.",
                                model="haiku",
                            ),
                            "explorer": AgentDefinition(
                                description="Search codebase for patterns, find relevant files",
                                prompt="You are a codebase explorer. Search for relevant code patterns, find files, and report what you find.",
                                model="haiku",
                            ),
                        }
                    except (TypeError, AttributeError, ValueError):
                        pass  # SDK version doesn't support subagents — skip

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
                            if ThinkingBlock and isinstance(block, ThinkingBlock):
                                thinking = getattr(block, "thinking", "")
                                if thinking:
                                    agent_log_lines.append(f"[thinking] {thinking}")
                            elif TextBlock and isinstance(block, TextBlock) and block.text:
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
                # Reset workspace — preserve TDD commit if it exists
                reset_sha = tdd_commit_sha if tdd_commit_sha else base_sha
                _restore_workspace_state(
                    effective_dir,
                    reset_ref=reset_sha,
                    pre_existing_untracked=pre_existing_untracked,
                )
                continue

            # Check if agent made any changes
            commit_base = tdd_commit_sha if tdd_commit_sha else base_sha
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

            if no_changes and not tdd_commit_sha:
                if spec:
                    # Task has spec items but agent made no changes — suspicious.
                    # Don't auto-pass; treat as failure so pilot can retry.
                    if not parallel_mode:
                        _log_warn("Agent made no changes despite spec requirements — retrying")
                    last_error = "Agent produced no code changes. The spec requirements have not been implemented."
                    continue

                # No spec and no changes — genuinely nothing to do
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
            # Spec tests are already committed — testgen_file is always None
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
            # When spec tests exist, there are 2 commits (test + candidate) to squash
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
            if not parallel_mode:
                print(flush=True)

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
