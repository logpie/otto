"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import json
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
from otto.display import _truncate_at_word, console, format_cost, format_duration, rich_escape
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
            console.print("  Auto-stashed uncommitted changes", style="dim")
            return True
        return False

    return True


def preflight_checks(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> tuple[int | None, list[dict]]:
    """Run pre-flight checks before launching the orchestrator.

    Returns (error_code, pending_tasks). If error_code is not None, the caller
    should abort with that exit code. Otherwise pending_tasks is the list of
    tasks to run.

    Side effects: checks out default branch, runs baseline, resets stale tasks,
    injects deps from file-plan.
    """
    default_branch = config["default_branch"]

    # Ensure we're on the default branch
    checkout = subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if checkout.returncode != 0:
        subprocess.run(
            ["git", "stash", "--include-untracked"],
            cwd=project_dir, capture_output=True,
        )
        retry = subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True, text=True,
        )
        if retry.returncode != 0:
            console.print(f"[red]Cannot checkout {default_branch}: {retry.stderr.strip()}[/red]")
            return (2, [])

    actual = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if actual != default_branch:
        console.print(f"[red]Expected branch {default_branch}, on {actual}[/red]")
        return (2, [])

    if not check_clean_tree(project_dir):
        console.print("[red]Working tree is dirty -- fix before running otto[/red]")
        return (2, [])

    # Baseline check
    test_command = config.get("test_command")
    if test_command:
        _log_info("Running baseline check...")
        baseline_env = _subprocess_env()
        baseline_env["CI"] = "true"
        try:
            result = subprocess.run(
                test_command, shell=True, cwd=project_dir,
                capture_output=True, timeout=config["verify_timeout"],
                env=baseline_env,
            )
        except subprocess.TimeoutExpired:
            console.print("  [yellow]Warning: Baseline tests timed out (interactive runner?) -- proceeding[/yellow]")
            result = None
        if result is not None:
            if result.returncode not in (0, 5):
                console.print("  [yellow]Warning: Baseline tests failing -- recorded, proceeding[/yellow]")
            elif result.returncode == 0:
                import re as _re
                stdout_text = (result.stdout or b"").decode(errors="replace")
                match = _re.search(r"(\d+) passed", stdout_text)
                count = f" ({match.group(1)} tests)" if match else ""
                console.print(f"  [green]\u2713[/green] Baseline passing{count}", style="dim")

    # Recover stale "running" tasks
    tasks = load_tasks(tasks_file)
    for t in tasks:
        if t.get("status") == "running":
            update_task(tasks_file, t["key"], status="pending",
                        error=None, session_id=None)
            console.print(f"  [yellow]Warning: Task #{t['id']} was stuck in 'running' -- reset to pending[/yellow]")

    tasks = load_tasks(tasks_file)
    pending = [t for t in tasks if t.get("status") == "pending"]
    if not pending:
        console.print("No pending tasks", style="dim")
        return (0, [])

    # Inject dependencies from file-plan.md
    if not config.get("no_architect", False) and len(pending) >= 2:
        from otto.architect import parse_file_plan
        arch_deps = parse_file_plan(project_dir)
        if arch_deps:
            tasks = load_tasks(tasks_file)
            pending = [t for t in tasks if t.get("status") == "pending"]
            pending_by_id = {t["id"]: t for t in pending}
            injected = 0
            for dep_id, on_id in arch_deps:
                task = pending_by_id.get(dep_id)
                if task:
                    deps = list(task.get("depends_on") or [])
                    if on_id not in deps:
                        deps.append(on_id)
                        update_task(tasks_file, task["key"], depends_on=deps)
                        injected += 1
            if injected:
                console.print(f"  Injected {injected} dependencies from file-plan.md", style="dim")
            # Reload after injecting deps
            tasks = load_tasks(tasks_file)
            pending = [t for t in tasks if t.get("status") == "pending"]

    return (None, pending)


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


def _run_cleanup_git_command(
    project_dir: Path,
    cmd: list[str],
    action: str,
) -> subprocess.CompletedProcess:
    """Run a best-effort git cleanup command and warn on failure."""
    result = subprocess.run(
        cmd,
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        _log_warn(f"Cleanup failed during {action}: {details}")
    return result


def _restore_workspace_state(
    project_dir: Path,
    reset_ref: str | None = None,
    pre_existing_untracked: set[str] | None = None,
) -> None:
    """Restore tracked files and remove only Otto-created untracked files."""
    cmd = ["git", "reset", "--hard"]
    if reset_ref:
        cmd.append(reset_ref)
    _run_cleanup_git_command(project_dir, cmd, "git reset --hard")
    _remove_otto_created_untracked(project_dir, pre_existing_untracked)



def _should_show_tool(name: str, detail: str) -> bool:
    """Decide if a tool call should be shown to the user."""
    if name in ("Write", "Edit"):
        return bool(detail)
    if name == "Read":
        return any(ext in detail for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go"))
    if name == "Bash":
        cmd = detail.strip()
        first_word = cmd.split()[0] if cmd else ""
        if first_word in ("python", "python3", "node") and any(flag in cmd for flag in (" -c ", " -e ")):
            return False
        return first_word in (
            "pytest", "python", "python3", "npx", "npm", "jest",
            "make", "cargo", "go", "ruby", "dotnet", "node",
            "cat", "ls", "find", "grep", "head", "tail",
            "pnpm", "yarn", "uv", "bash", "sh", "tsc",
        )
    return True


def _build_agent_tool_event(block) -> dict[str, Any] | None:
    """Build a progress payload for a tool use block."""
    name = block.name
    inputs = block.input or {}
    raw_detail = _tool_use_summary(block)

    if name == "Glob":
        if not _should_show_tool("Read", raw_detail):
            return None
        return {"name": "Read", "detail": raw_detail[:80]}

    if name not in ("Read", "Write", "Edit", "Bash"):
        return None
    if not _should_show_tool(name, raw_detail):
        return None

    event: dict[str, Any] = {"name": name, "detail": raw_detail[:80]}
    if name == "Edit":
        old = inputs.get("old_string", "")
        new = inputs.get("new_string", "")
        if old or new:
            event["old_lines"] = old.splitlines()[:4]
            event["new_lines"] = new.splitlines()[:4]
            event["old_total"] = old.count("\n") + 1 if old else 0
            event["new_total"] = new.count("\n") + 1 if new else 0
    elif name == "Write":
        content = inputs.get("content", "")
        if content:
            event["preview_lines"] = content.splitlines()[:3]
            event["total_lines"] = content.count("\n") + 1
    return event


def _extract_qa_spec_results(report: str) -> list[dict[str, Any]]:
    """Extract authoritative pass/fail results from a QA report."""
    results: list[dict[str, Any]] = []
    result_index: dict[str, int] = {}
    current_spec_key: str | None = None

    def _record_result(key: str | None, passed: bool) -> None:
        final_key = key or f"line:{len(results)}"
        payload = {"key": final_key, "passed": passed}
        if final_key in result_index:
            results[result_index[final_key]] = payload
        else:
            result_index[final_key] = len(results)
            results.append(payload)

    for line in report.splitlines():
        text = line.strip()
        if not text:
            continue

        clean = text.replace("**", "").replace("__", "")
        has_pass = "\u2705" in text or "PASS" in text
        has_fail = "\u274c" in text or "FAIL" in text

        is_spec_header = (
            text.startswith("###")
            or text.startswith("**Spec")
            or (clean.startswith("Spec ") and clean[5:6].isdigit())
        )
        is_table_row = text.startswith("|") and (has_pass or has_fail)
        is_table_header = text.startswith("|") and (
            "Spec" in text[:15] or "Check" in text[:20] or "---" in text[:5]
        )
        is_numbered_check = (
            len(clean) > 2
            and clean[0] in ("\u2713", "\u2717", "\u2705", "\u274c")
            and clean[1:].lstrip().split(".")[0].strip().isdigit()
        )
        is_result_line = (
            text.startswith(("**PASS", "PASS", "**RESULT"))
            or (text.startswith("- \u2705") and "PASS" not in text[:5])
        )
        is_standalone_check = (
            len(clean) > 2
            and clean[0] in ("\u2713", "\u2717", "\u2705", "\u274c")
            and not is_numbered_check
            and not is_spec_header
        )

        if "VERDICT" in text or is_table_header:
            continue
        if text.startswith(("- Container", "- Header", "\u25cb Edge")):
            continue
        if "Minor Observation" in text or "not spec violation" in text.lower():
            continue

        if is_spec_header:
            spec_text = clean.lstrip("# ").strip()
            for marker in (": \u2705 PASS", ": \u274c FAIL", "\u2705", "\u274c"):
                spec_text = spec_text.replace(marker, "").rstrip(": ").strip()
            current_spec_key = f"spec:{spec_text}"
            if has_pass or has_fail:
                _record_result(current_spec_key, has_pass and not has_fail)
            continue

        if is_table_row:
            parts = [part.strip() for part in clean.split("|") if part.strip()]
            key = f"table:{parts[0]}" if parts else None
            _record_result(key, has_pass and not has_fail)
            current_spec_key = None
            continue

        if is_numbered_check:
            number = clean[1:].lstrip().split(".", 1)[0].strip()
            _record_result(f"number:{number}", clean[0] in ("\u2713", "\u2705"))
            current_spec_key = None
            continue

        if is_standalone_check:
            desc = clean[1:].strip()
            _record_result(f"check:{desc}", clean[0] in ("\u2713", "\u2705"))
            current_spec_key = None
            continue

        if is_result_line and (has_pass or has_fail):
            detail_text = clean.replace("RESULT", "").replace("PASS", "").replace("FAIL", "")
            detail_text = detail_text.replace("\u2705", "").replace("\u274c", "")
            detail_text = detail_text.lstrip(": \u2014-*()").strip()
            _record_result(current_spec_key or f"result:{detail_text or clean}", has_pass and not has_fail)
            current_spec_key = None
            continue

        if has_fail and text.startswith(("**FAIL", "FAIL")):
            detail_text = clean.replace("FAIL", "").replace("\u274c", "").lstrip(" \u2014-()").strip()
            _record_result(current_spec_key or f"fail:{detail_text or clean}", False)
            current_spec_key = None

    return results


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
        _run_cleanup_git_command(
            project_dir,
            ["git", "checkout", default_branch],
            f"git checkout {default_branch}",
        )
    _run_cleanup_git_command(
        project_dir,
        ["git", "branch", "-D", branch_name],
        f"git branch -D {branch_name}",
    )


def rebase_and_merge(project_dir: Path, task_branch: str, default_branch: str) -> bool:
    """Rebase task_branch onto default_branch then ff-only merge.

    Used by v4 orchestrator for serial merge of parallel tasks.
    Returns False on rebase conflict.
    """
    # Rebase task branch onto default
    rebase = subprocess.run(
        ["git", "rebase", default_branch, task_branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if rebase.returncode != 0:
        # Abort the failed rebase
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=project_dir, capture_output=True,
        )
        return False

    # Fast-forward merge
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, check=True,
    )
    result = subprocess.run(
        ["git", "merge", "--ff-only", task_branch],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode == 0:
        subprocess.run(
            ["git", "branch", "-d", task_branch],
            cwd=project_dir, capture_output=True,
        )
        return True
    return False


async def coding_loop(
    task_plan: Any,  # otto.planner.TaskPlan
    context: Any,    # otto.context.PipelineContext
    config: dict[str, Any],
    project_dir: Path,
    telemetry: Any,  # otto.telemetry.Telemetry
    tasks_file: Path | None = None,
) -> Any:  # otto.context.TaskResult
    """v4 coding loop — run a single task through prepare/code/verify/QA/merge.

    Passes an on_progress callback to run_task_with_qa() so phase events
    flow through the telemetry dual-write (legacy pilot_results.jsonl).
    Emits TaskMerged/TaskFailed at the actual completion time (not deferred).
    """
    from otto.context import TaskResult
    from otto.telemetry import AgentToolCall, TaskFailed, TaskMerged, TaskStarted

    task_key = task_plan.task_key

    # Load task from tasks.yaml
    tasks = load_tasks(tasks_file) if tasks_file else []
    task = next((t for t in tasks if t.get("key") == task_key), None)
    if not task:
        return TaskResult(task_key=task_key, success=False, error="task not found")

    task_id = task.get("id", 0)
    prompt = task.get("prompt", "")
    task_start = time.monotonic()

    # Log task start
    telemetry.log(TaskStarted(
        task_key=task_key, task_id=task_id,
        prompt=prompt, strategy=task_plan.strategy,
    ))

    # Print task header
    from otto.display import TaskDisplay
    console.print()
    console.print(f"  \u25cf [bold]Running[/bold]  [dim]#{task_id}  {task_key[:8]}[/dim]")
    display = TaskDisplay(console)
    display.start()

    # Bridge on_progress events to:
    # 1. Rich Live display (TaskDisplay) — inline progress visible to user
    # 2. Legacy JSONL (pilot_results.jsonl) — for otto status -w, otto show
    def _on_progress(event_type: str, data: dict) -> None:
        try:
            # Route to live display
            if event_type == "phase":
                display.update_phase(
                    name=data.get("name", ""),
                    status=data.get("status", ""),
                    time_s=data.get("time_s", 0.0),
                    error=data.get("error", ""),
                    detail=data.get("detail", ""),
                    cost=data.get("cost", 0),
                )
            elif event_type == "agent_tool":
                display.add_tool(data=data)
            elif event_type == "agent_tool_result":
                display.add_tool_result(data=data)
            elif event_type == "qa_finding":
                display.add_finding(data.get("text", ""))
            elif event_type == "qa_summary":
                display.set_qa_summary(
                    total=data.get("total", 0),
                    passed=data.get("passed", 0),
                    failed=data.get("failed", 0),
                )
        except Exception:
            pass
        try:
            # Route to legacy JSONL
            if telemetry._legacy_enabled:
                telemetry._emit_legacy_progress({
                    "tool": "progress",
                    "event": event_type,
                    "task_key": task_key,
                    **data,
                })
        except Exception:
            pass

    try:
        # Inject factual context (learnings, research) — NOT planner hints.
        # Learnings are observations from prior tasks. Research is web/doc findings.
        # These are raw environmental info, not an LLM's paraphrased advice.
        context_parts: list[str] = []
        if context.learnings:
            context_parts.append("LEARNINGS FROM PRIOR TASKS:\n" + "\n".join(
                f"- {l}" for l in context.learnings
            ))
        research = context.get_research(task_key)
        if research:
            context_parts.append(f"RESEARCH FINDINGS:\n{research}")

        factual_context = "\n\n".join(context_parts) if context_parts else None

        result = await run_task_with_qa(
            task, config, project_dir, tasks_file,
            hint=factual_context, on_progress=_on_progress,
        )

        duration = time.monotonic() - task_start
        cost = float(result.get("cost_usd", 0.0) or 0.0)
        elapsed_str = display.stop()

        if result.get("success"):
            commit_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            ).stdout.strip()

            # Print result
            console.print(f"    [green]\u2713[/green] passed  [dim]{elapsed_str}  ${cost:.2f}[/dim]")

            # Emit TaskMerged NOW (not deferred to batch loop)
            telemetry.log(TaskMerged(
                task_key=task_key, task_id=task_id,
                cost_usd=cost, duration_s=duration,
                diff_summary=result.get("diff_summary", ""),
            ))

            return TaskResult(
                task_key=task_key, success=True,
                commit_sha=commit_sha,
                cost_usd=cost, duration_s=duration,
                qa_report=result.get("qa_report", ""),
                diff_summary=result.get("diff_summary", ""),
            )

        error = str(result.get("error", "") or "")
        console.print(f"    [red]\u2717[/red] failed  [dim]{elapsed_str}  ${cost:.2f}[/dim]")
        telemetry.log(TaskFailed(
            task_key=task_key, task_id=task_id,
            error=error,
            cost_usd=cost, duration_s=duration,
        ))
        return TaskResult(
            task_key=task_key, success=False,
            cost_usd=cost, error=error,
            duration_s=duration,
            qa_report=result.get("qa_report", ""),
            diff_summary=result.get("diff_summary", ""),
        )

    except Exception as exc:
        display.stop()
        duration = time.monotonic() - task_start
        telemetry.log(TaskFailed(
            task_key=task_key, task_id=task_id,
            error=str(exc), duration_s=duration,
        ))
        return TaskResult(
            task_key=task_key, success=False,
            error=f"unexpected error: {exc}", duration_s=duration,
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
    _run_cleanup_git_command(
        project_dir,
        ["git", "checkout", default_branch],
        f"git checkout {default_branch}",
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

# Aliases from display module
_format_duration = format_duration
_format_cost = format_cost


def _log_info(msg: str) -> None:
    console.print("\u2500" * 60, style="dim")
    console.print(f"  {msg}")


def _log_task_start(task_id: int, key: str, attempt: int, max_attempts: int, prompt: str) -> None:
    console.print()
    console.print("\u2501" * 60, style="bold")
    console.print(f"[bold]  Task #{task_id}[/bold]  {rich_escape(prompt[:80])}")
    console.print(f"  attempt {attempt}/{max_attempts}  \u00b7  key {key}", style="dim")
    console.print("\u2501" * 60, style="bold")


def _log_pass(task_id: int, branch: str, duration: float | None = None, cost: float = 0.0) -> None:
    dur = f" in {_format_duration(duration)}" if duration else ""
    cost_str = f" ({_format_cost(cost)})" if cost > 0 else ""
    console.print(f"\n  [green bold]\u2713 Task #{task_id} PASSED[/green bold] [dim]\u2014 merged to {branch}{dur}{cost_str}[/dim]")


def _log_fail(task_id: int, reason: str, duration: float | None = None, cost: float = 0.0) -> None:
    dur = f" in {_format_duration(duration)}" if duration else ""
    cost_str = f" ({_format_cost(cost)})" if cost > 0 else ""
    console.print(f"\n  [red bold]\u2717 Task #{task_id} FAILED[/red bold] [dim]\u2014 {rich_escape(reason)}{dur}{cost_str}[/dim]")


def _log_warn(msg: str) -> None:
    console.print(f"  [yellow]Warning: {rich_escape(msg)}[/yellow]")


def _log_verify(tiers: list) -> None:
    """Print verification results inline with test counts."""
    import re as _re
    console.print(f"\n  {'─' * 50}", style="dim")
    for t in tiers:
        if t.skipped:
            continue
        icon = "[green]\u2713[/green]" if t.passed else "[red]\u2717[/red]"
        count_str = ""
        if t.output:
            match = _re.search(r"(\d+) passed", t.output)
            if match:
                count_str = f" [dim]({match.group(1)} tests)[/dim]"
        console.print(f"  {icon} {t.tier}{count_str}")


def _print_tool_use(block) -> None:
    """Print a tool use block like the Claude TUI."""
    name = block.name
    inputs = block.input or {}

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
        console.print(f"  [bold cyan]\u25cf {name}[/bold cyan]  [dim]{rich_escape(detail)}[/dim]")
    else:
        console.print(f"  [bold cyan]\u25cf {name}[/bold cyan]")

    # Show edit diff for Edit tool
    if name == "Edit":
        old = inputs.get("old_string", "")
        new = inputs.get("new_string", "")
        if old or new:
            for line in old.splitlines()[:3]:
                console.print(f"    - {rich_escape(line)}", style="dim red")
            if old.count("\n") > 3:
                console.print(f"    ... ({old.count(chr(10)) - 3} more lines)", style="dim")
            for line in new.splitlines()[:3]:
                console.print(f"    + {rich_escape(line)}", style="dim green")
            if new.count("\n") > 3:
                console.print(f"    ... ({new.count(chr(10)) - 3} more lines)", style="dim")

    # Show content preview for Write tool
    elif name == "Write":
        content = inputs.get("content", "")
        if content:
            lines = content.splitlines()
            for line in lines[:3]:
                console.print(f"    + {rich_escape(line)}", style="dim green")
            if len(lines) > 3:
                console.print(f"    ... ({len(lines) - 3} more lines)", style="dim")


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
        shown = lines[-5:] if len(lines) > 5 else lines
        for line in shown:
            console.print(f"    {rich_escape(line)}", style="red")
    else:
        lines = content.strip().splitlines()
        if len(lines) <= 3:
            for line in lines:
                console.print(f"    {rich_escape(line)}", style="dim")
        else:
            console.print(f"    {rich_escape(lines[0])}", style="dim")
            console.print(f"    {rich_escape(lines[1])}", style="dim")
            console.print(f"    ... ({len(lines) - 3} more lines)", style="dim")
            console.print(f"    {rich_escape(lines[-1])}", style="dim")


CODING_SYSTEM_PROMPT = """\
<role>
You are an autonomous coding agent. You implement features, fix bugs, and write tests.
Your work is verified externally — you must meet the acceptance spec exactly.
</role>

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

QA_SYSTEM_PROMPT = """\
You are an adversarial QA tester. Your job is to find bugs the test suite missed.
The code already passed verification in a clean worktree — focus on what tests DON'T cover.

For each spec item: test the HARDEST case. Report PASS or FAIL with evidence.
Use whatever approach works — read code, write test scripts, curl endpoints,
run the app, use browser testing. You decide.

Kill any servers you started (by PID, not pkill)."""


def _build_coding_prompt(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    effective_dir: Path,
    hint: str | None = None,
) -> str:
    """Build the initial coding agent prompt with spec, source context, and learnings.

    Extracted from run_task() so prepare_task() and run_task() share the same logic.
    """
    prompt = task["prompt"]
    key = task["key"]
    spec = task.get("spec")
    feedback = task.get("feedback", "")
    if hint:
        feedback = hint

    base_prompt = prompt
    if feedback:
        base_prompt = f"{prompt}\n\nIMPORTANT feedback from the user:\n{feedback}"

    # Provide file tree instead of pre-loaded file contents.
    # The agent has Read/Grep/Glob tools — let it decide what to read.
    file_tree_result = subprocess.run(
        ["git", "ls-files"],
        cwd=effective_dir, capture_output=True, text=True,
    )
    file_tree = file_tree_result.stdout.strip() if file_tree_result.returncode == 0 else ""

    # Include spec items if available — classified as verifiable or visual
    spec_section = ""
    if spec:
        from otto.tasks import spec_text, spec_is_verifiable
        spec_lines = []
        for i, item in enumerate(spec):
            text = spec_text(item)
            tag = "[verifiable]" if spec_is_verifiable(item) else "[visual]"
            spec_lines.append(f"  {i+1}. {tag} {text}")
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

PROJECT FILES:
{file_tree}
{spec_section}

Implement the feature and write tests for all [verifiable] spec items.
The spec is your contract — meet every item. Test the hardest cases.

Write notes to otto_arch/task-notes/{key}.md when done:
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
    return agent_prompt


def prepare_task(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Prepare a task for coding: create git branch, build prompt with spec/context.

    Returns a dict with:
        work_dir: str — directory for the coding agent to work in
        prompt: str — full coding prompt with spec, source context, learnings
        system_prompt: str — coding agent system prompt
        base_sha: str — SHA of the base commit for later verification

    This is the "setup" half of run_task(), extracted for the native subagent
    architecture where the pilot dispatches a coding subagent directly.
    """
    key = task["key"]
    default_branch = config["default_branch"]

    if tasks_file:
        update_task(tasks_file, key, status="running", attempts=0)

    try:
        # Create branch
        base_sha = create_task_branch(project_dir, key, default_branch, task=task)

        # Snapshot untracked files BEFORE coding — verify_task needs this to
        # distinguish agent-created files from pre-existing ones.
        pre_untracked = list(_snapshot_untracked(project_dir) or [])

        # Auto-detect test_command if spec exists but no test_command configured
        spec = task.get("spec")
        test_command = config.get("test_command")
        if spec and not test_command:
            detected = detect_test_command(project_dir)
            test_command = detected if detected else "pytest"

        # Build the coding agent prompt
        agent_prompt = _build_coding_prompt(
            task, config, project_dir, project_dir, hint=hint,
        )

        return {
            "work_dir": str(project_dir),
            "prompt": agent_prompt,
            "system_prompt": CODING_SYSTEM_PROMPT,
            "base_sha": base_sha,
            "pre_untracked": pre_untracked,
        }
    except Exception as exc:
        # Cleanup on failure — don't strand task in "running"
        if tasks_file:
            update_task(tasks_file, key, status="failed",
                        error=f"prepare failed: {exc}")
        subprocess.run(["git", "checkout", default_branch],
                       cwd=project_dir, capture_output=True)
        raise


def verify_task(
    task_key: str,
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    pre_untracked: list[str] | None = None,
    auto_merge: bool = True,
) -> dict[str, Any]:
    """Verify task implementation: build candidate, run tests, optionally merge.

    Call this AFTER the coding subagent finishes.

    Args:
        pre_untracked: untracked files snapshot from prepare_task(). Required to
            correctly distinguish agent-created files from pre-existing ones.
        auto_merge: if True (default), merge to default branch on success.
            Set to False to leave the squash commit on the task branch —
            useful when QA testing should happen before merge.

    Returns a dict with:
        passed: bool — whether verification passed
        error: str|None — failure details if not passed
        diff_summary: str — summary of changes made

    This is the "verify + merge" half of run_task(), extracted for the native
    subagent architecture.
    """
    key = task_key
    default_branch = config["default_branch"]
    test_command = config.get("test_command")
    verify_cmd = None
    timeout = config["verify_timeout"]

    # Load task to get verify_cmd, spec, and other metadata
    task = None
    if tasks_file:
        tasks = load_tasks(tasks_file)
        task = next((t for t in tasks if t.get("key") == key), None)
        if task:
            verify_cmd = task.get("verify")

    # Use pre-agent untracked snapshot from prepare_task, not a fresh one
    pre_existing_untracked = set(pre_untracked) if pre_untracked else set()

    # Ensure we're on the correct task branch (prepare_task for another task
    # may have switched branches since we last ran).
    expected_branch = f"otto/{key}"
    current_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current_branch != expected_branch:
        checkout = subprocess.run(
            ["git", "checkout", expected_branch],
            cwd=project_dir, capture_output=True,
        )
        if checkout.returncode != 0:
            return {
                "passed": False,
                "error": f"Branch {expected_branch} not found. Call prepare_task first.",
                "diff_summary": "",
            }

    base_sha = subprocess.run(
        ["git", "merge-base", default_branch, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if not base_sha:
        base_sha = subprocess.run(
            ["git", "rev-parse", f"{default_branch}"],
            cwd=project_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()

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

    if no_changes:
        has_spec = task and task.get("spec")
        if has_spec:
            # No changes on a task with spec = agent didn't do anything. Fail so pilot retries.
            return {"passed": False, "error": "No code changes detected — agent may have failed silently", "diff_summary": ""}
        # No spec + no changes = nothing to do, pass
        subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
        cleanup_branch(project_dir, key, default_branch)
        if tasks_file:
            update_task(tasks_file, key, status="passed")
        return {"passed": True, "error": None, "diff_summary": "No changes needed"}

    # Build candidate commit
    candidate_sha = build_candidate_commit(
        project_dir, base_sha, None, pre_existing_untracked,
    )

    # Re-detect test command after agent may have created the project
    if not config.get("test_command"):
        detected = detect_test_command(project_dir)
        if detected:
            test_command = detected

    # Run verification in disposable worktree
    verify_result = run_verification(
        project_dir=project_dir,
        candidate_sha=candidate_sha,
        test_command=test_command,
        verify_cmd=verify_cmd,
        timeout=timeout,
    )

    if verify_result.passed:
        # Squash all branch commits into a single commit
        try:
            subprocess.run(
                ["git", "reset", "--mixed", base_sha],
                cwd=project_dir, capture_output=True, check=True,
            )
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

            # Get task prompt for commit message
            commit_msg = f"otto: task {key}"
            if tasks_file:
                tasks = load_tasks(tasks_file)
                task = next((t for t in tasks if t.get("key") == key), None)
                if task:
                    commit_msg = f"otto: {task['prompt'][:60]} (#{task.get('id', '?')})"

            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=project_dir, capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, Exception) as e:
            stderr = getattr(e, "stderr", str(e))
            _restore_workspace_state(project_dir, pre_existing_untracked=pre_existing_untracked)
            subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
            cleanup_branch(project_dir, key, default_branch)
            if tasks_file:
                update_task(tasks_file, key, status="failed",
                            error=f"squash commit failed: {stderr}",
                            error_code="internal_error")
            return {"passed": False, "error": f"squash commit failed: {stderr}", "diff_summary": ""}

        # Build diff summary from the squash commit
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", base_sha, "HEAD"],
            cwd=project_dir, capture_output=True, text=True,
        )
        diff_summary = diff_stat.stdout.strip() if diff_stat.returncode == 0 else ""

        if not auto_merge:
            # Leave squash commit on task branch — caller will merge after QA
            return {"passed": True, "error": None, "diff_summary": diff_summary}

        # Merge to default branch
        if merge_to_default(project_dir, key, default_branch):
            if tasks_file:
                update_task(tasks_file, key, status="passed")
            return {"passed": True, "error": None, "diff_summary": diff_summary}
        else:
            if tasks_file:
                update_task(tasks_file, key, status="failed",
                            error=f"branch diverged — otto/{key} preserved",
                            error_code="merge_diverged")
            return {
                "passed": False,
                "error": f"branch diverged — otto/{key} preserved, manual rebase needed",
                "diff_summary": "",
            }

    # Verification failed — reset working tree for retry
    subprocess.run(
        ["git", "reset", "--mixed", base_sha],
        cwd=project_dir, capture_output=True,
    )
    failure_output = verify_result.failure_output or "verification failed"
    return {"passed": False, "error": failure_output, "diff_summary": ""}


def _setup_task_worktree(project_dir: Path, key: str, base_sha: str) -> Path:
    """Create an isolated git worktree for parallel task execution."""
    wt_dir = project_dir / ".worktrees" / f"otto-{key}"
    branch_name = f"otto/{key}"

    tasks_path = project_dir / "tasks.yaml"
    task = None
    if tasks_path.exists():
        task = next((t for t in load_tasks(tasks_path) if t.get("key") == key), None)

    branch_exists = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=project_dir,
        capture_output=True,
    ).returncode == 0
    if branch_exists and task and task.get("status") == "failed" and task.get("error_code") == "merge_diverged":
        raise RuntimeError(
            f"Branch {branch_name} preserved from diverge failure — "
            f"manually resolve or run 'otto reset' first"
        )

    # Clean up stale worktree if it exists
    if wt_dir.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=project_dir, capture_output=True,
        )
    # Delete stale branch if it exists and is not intentionally preserved
    if branch_exists:
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
    branch_name = f"otto/{key}"
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_dir)],
        cwd=project_dir, capture_output=True,
    )
    _run_cleanup_git_command(
        project_dir,
        ["git", "branch", "-D", branch_name],
        f"git branch -D {branch_name}",
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
            console.print(f"  [dim]{_task_tag}[/dim] {msg}")
        else:
            console.print(msg)

    if tasks_file:
        update_task(tasks_file, key, status="running", attempts=0)

    task_start = time.monotonic()
    pre_existing_untracked: set[str] | None = None
    log_dir = project_dir / "otto_logs" / key
    total_cost = 0.0
    session_id = None
    last_error = None  # verification failure output for retry feedback
    try:
        # Snapshot pre-existing untracked files so we don't sweep them into the commit
        pre_existing_untracked = _snapshot_untracked(effective_dir)

        # Create branch (skip in parallel mode — caller sets up worktree with branch)
        if parallel_mode:
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=effective_dir, capture_output=True, text=True, check=True,
            ).stdout.strip()
        else:
            base_sha = create_task_branch(project_dir, key, default_branch, task=task)

        # Auto-detect test_command if spec exists but no test_command configured
        spec = task.get("spec")
        if spec and not test_command:
            test_command = detect_test_command(effective_dir)
            if not test_command:
                test_command = "pytest"  # fallback for Python projects

        # Print task header before testgen (so testgen output is under the right task)
        if parallel_mode:
            _tprint(f"[bold]Task #{task_id}[/bold]  {rich_escape(prompt[:60])}  [dim]started[/dim]")
        else:
            console.print()
            console.print("\u2501" * 60, style="bold")
            console.print(f"[bold]  Task #{task_id}[/bold]  {rich_escape(prompt[:80])}")
            console.print(f"  key {key}", style="dim")
            console.print("\u2501" * 60, style="bold")

        # Timing for profiling
        timings: dict[str, float] = {}

        log_dir.mkdir(parents=True, exist_ok=True)

        # Optional TDD mode: generate adversarial tests before coding
        test_file_path_val = None
        tdd_commit_sha = None  # SHA after TDD tests committed (for retry reset)
        if config.get("tdd", False) and spec:
            from otto.testgen import build_blackbox_context, run_testgen_agent, validate_generated_tests
            if not parallel_mode:
                console.print(f"  TDD mode: generating adversarial tests ({len(spec)} criteria)...", style="dim")
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
                        console.print(f"  [green]\u2713[/green] [dim]TDD tests ready ({validation.failed} failing, {validation.passed} passing)[/dim]")

        for attempt in range(max_retries + 1):
            attempt_num = attempt + 1
            if not parallel_mode:
                console.print(f"\n  attempt {attempt_num}/{max_retries + 1}", style="dim")

            if tasks_file:
                update_task(tasks_file, key, attempts=attempt_num)

            # Build agent prompt — include relevant source files so agent doesn't need to explore
            if attempt == 0 or last_error is None:
                agent_prompt = _build_coding_prompt(
                    task, config, project_dir, effective_dir,
                )
                # TDD mode: tell coding agent about pre-generated test files
                if test_file_path_val and test_file_path_val.exists():
                    agent_prompt += (
                        f"\n\nACCEPTANCE TESTS: {test_file_path_val.relative_to(effective_dir)}\n"
                        f"These tests were generated from the spec before implementation.\n"
                        f"Fix bugs in them if needed (broken imports, wrong API usage).\n"
                        f"You may also write additional tests."
                    )
            else:
                agent_prompt = (
                    f"Verification failed. Here is the output:\n\n"
                    f"{last_error}\n\n"
                    f"Original task: {prompt}\n\n"
                    f"You are working in {effective_dir}. Do NOT create git commits."
                )

            # Run agent + build candidate + verify — catch infrastructure failures
            try:
                try:
                    agent_opts = ClaudeAgentOptions(
                        permission_mode="bypassPermissions",
                        cwd=str(effective_dir),
                            setting_sources=["user", "project"],
                        env=_subprocess_env(),
                        effort=config.get("effort", "high"),
                        system_prompt=CODING_SYSTEM_PROMPT,
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
                            progress_file = log_dir / "progress.txt"
                            for block in message.content:
                                if ThinkingBlock and isinstance(block, ThinkingBlock):
                                    thinking = getattr(block, "thinking", "")
                                    if thinking:
                                        agent_log_lines.append(f"[thinking] {thinking}")
                                elif TextBlock and isinstance(block, TextBlock) and block.text:
                                    if not parallel_mode:
                                        console.print(block.text)
                                    agent_log_lines.append(block.text)
                                    # Write reasoning to progress file
                                    try:
                                        with open(progress_file, "a") as pf:
                                            # Show first line of reasoning
                                            first_line = block.text.strip().split("\n")[0]
                                            if first_line:
                                                pf.write(first_line + "\n")
                                    except OSError:
                                        pass
                                elif ToolUseBlock and isinstance(block, ToolUseBlock):
                                    if not parallel_mode:
                                        _print_tool_use(block)
                                    summary_line = f"● {block.name}  {_tool_use_summary(block)}"
                                    agent_log_lines.append(summary_line)
                                    # Write tool call to progress file
                                    try:
                                        with open(progress_file, "a") as pf:
                                            pf.write(summary_line + "\n")
                                    except OSError:
                                        pass
                                elif ToolResultBlock and isinstance(block, ToolResultBlock):
                                    if not parallel_mode:
                                        _print_tool_result(block)
                                    content = block.content if isinstance(block.content, str) else str(block.content)
                                    if content.strip():
                                        prefix = "ERROR: " if block.is_error else ""
                                        agent_log_lines.append(f"  {prefix}{content[:500]}")
                                        # Write errors to progress (important for user to see)
                                        if block.is_error:
                                            try:
                                                with open(progress_file, "a") as pf:
                                                    pf.write(f"  ERROR: {content[:200]}\n")
                                            except OSError:
                                                pass

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
                new_untracked = {f for f in untracked_check.stdout.strip().splitlines() if f} - (pre_existing_untracked or set())
                no_changes = diff_check.returncode == 0 and not new_untracked

                if no_changes and not tdd_commit_sha:
                    if spec:
                        # Task has spec items but agent made no changes — suspicious.
                        # Don't auto-pass; treat as failure so pilot can retry.
                        if not parallel_mode:
                            _log_warn("Agent made no changes despite spec requirements — retrying")
                        last_error = "No file changes were detected after your session."
                        continue

                    # No spec and no changes — genuinely nothing to do
                    if not parallel_mode:
                        console.print("  No changes needed", style="dim")
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

                # Re-detect test command after agent may have created the project
                if not config.get("test_command"):
                    detected = detect_test_command(effective_dir)
                    if detected:
                        test_command = detected

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
            except OSError:
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
                    console.print()

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

            # Unwind candidate commit for retry
            subprocess.run(
                ["git", "reset", "--mixed", commit_base],
                cwd=effective_dir, capture_output=True,
            )
            last_error = verify_result.failure_output
            if not parallel_mode:
                _log_warn("Verification failed — retrying")

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
    except Exception as e:
        duration = time.monotonic() - task_start
        _log_fail(task_id, f"unexpected error: {e}", duration, cost=total_cost)
        if not parallel_mode:
            _cleanup_task_failure(
                project_dir, key, default_branch, tasks_file,
                pre_existing_untracked=pre_existing_untracked,
                error=f"unexpected error: {e}", error_code="internal_error",
                cost_usd=total_cost,
                duration_s=duration,
            )
        elif tasks_file:
            update_task(tasks_file, key,
                        status="failed", error=f"unexpected error: {e}",
                        error_code="internal_error",
                        cost_usd=total_cost,
                        duration_s=round(duration, 1))
        return False


async def run_qa_agent(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    diff_summary: str,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Run adversarial QA agent. Returns {passed, report, has_failures, cost_usd}."""
    spec = task.get("spec")
    if not spec:
        return {"passed": True, "report": "No spec items — QA skipped", "has_failures": False}

    from otto.tasks import spec_text, spec_is_verifiable

    # Build spec section for QA
    spec_lines = []
    for i, item in enumerate(spec):
        text = spec_text(item)
        kind = "verifiable" if spec_is_verifiable(item) else "visual"
        spec_lines.append(f"  {i+1}. [{kind}] {text}")
    spec_section = "\n".join(spec_lines)

    qa_prompt = f"""You are running adversarial QA on this implementation.

ACCEPTANCE SPEC:
{spec_section}

DIFF SUMMARY:
{diff_summary}

TASK: {task.get('prompt', '')}

Test the HARDEST cases first. For each spec item, try to find the ONE case that breaks it.
Report exactly what you tested, what you expected, and what happened.

If everything genuinely passes, end your report with: QA VERDICT: PASS
If any spec item fails, end your report with: QA VERDICT: FAIL

You are working in {project_dir}. Do NOT create git commits."""

    # Only give QA chrome-devtools if the spec has visual items that need browser
    has_visual_specs = any(not spec_is_verifiable(item) for item in spec)
    qa_mcp_servers = {}
    if has_visual_specs:
        user_claude_json = Path.home() / ".claude.json"
        if user_claude_json.exists():
            try:
                user_config = json.loads(user_claude_json.read_text())
                for name, srv in user_config.get("mcpServers", {}).items():
                    if name == "chrome-devtools":
                        srv = dict(srv)
                        args = list(srv.get("args", []))
                        if "--headless" not in args:
                            args.append("--headless")
                        if not any(a.startswith("--viewport") for a in args):
                            args.extend(["--viewport", "1280x720"])
                        if not any(a.startswith("--userDataDir") for a in args):
                            otto_chrome_profile = str(Path.home() / ".cache" / "otto" / "chrome-profile")
                            args.extend(["--userDataDir", otto_chrome_profile])
                        srv["args"] = args
                        qa_mcp_servers[name] = srv
            except (Exception,):
                pass

    qa_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=["project"],
        env=_subprocess_env(),
        effort=config.get("effort", "high"),
        system_prompt=QA_SYSTEM_PROMPT,
    )
    if qa_mcp_servers:
        qa_opts.mcp_servers = qa_mcp_servers
    if config.get("model"):
        qa_opts.model = config["model"]

    qa_timeout = config.get("qa_timeout", 3600)  # 1 hour circuit breaker
    report_lines: list[str] = []

    try:
        async def _run_qa():
            nonlocal report_lines
            async for message in query(prompt=qa_prompt, options=qa_opts):
                if isinstance(message, ResultMessage):
                    pass
                elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                    pass
                elif AssistantMessage and isinstance(message, AssistantMessage):
                    for block in message.content:
                        if TextBlock and isinstance(block, TextBlock) and block.text:
                            report_lines.append(block.text)
                            # Extract QA findings for live display
                            if on_progress:
                                for line in block.text.splitlines():
                                    line_s = line.strip()
                                    if not line_s:
                                        continue
                                    # Look for spec result patterns
                                    if any(marker in line_s for marker in
                                           ["PASS", "FAIL", "CONCERN",
                                            "✅", "❌", "⚠",
                                            "Spec ", "spec "]):
                                        # Clean up and emit as QA finding
                                        try:
                                            on_progress("qa_finding", {"text": line_s[:200]})
                                        except Exception:
                                            pass
                        elif ToolUseBlock and isinstance(block, ToolUseBlock):
                            # Emit QA agent tool calls so user sees activity during QA
                            if on_progress:
                                try:
                                    event = _build_agent_tool_event(block)
                                    if event:
                                        on_progress("agent_tool", event)
                                except Exception:
                                    pass

        await asyncio.wait_for(_run_qa(), timeout=qa_timeout)
        qa_completed = True
    except asyncio.TimeoutError:
        report_lines.append(f"\n[QA agent timed out after {qa_timeout}s]")
        qa_completed = False
    except Exception as e:
        report_lines.append(f"\n[QA agent error: {e}]")
        qa_completed = False

    report = "\n".join(report_lines)
    if on_progress:
        try:
            spec_results = _extract_qa_spec_results(report)
            on_progress("qa_summary", {
                "total": len(spec_results),
                "passed": sum(1 for result in spec_results if result["passed"]),
                "failed": sum(1 for result in spec_results if not result["passed"]),
            })
        except Exception:
            pass
    # Explicit FAIL in report = definitely failed
    has_explicit_fail = "QA VERDICT: FAIL" in report or "FAIL" in report.upper().split("QA VERDICT")[-1] if "QA VERDICT" in report else False
    # QA must complete AND have an explicit PASS verdict to be considered passing
    has_explicit_pass = "QA VERDICT: PASS" in report

    if has_explicit_fail:
        passed = False
    elif not qa_completed:
        passed = False  # timeout/error = inconclusive, not pass
    elif not has_explicit_pass and not report_lines:
        passed = False  # empty output = inconclusive
    else:
        passed = not has_explicit_fail

    return {"passed": passed, "report": report, "has_failures": not passed}


async def run_task_with_qa(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    hint: str | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Run full task loop: prepare -> code -> verify -> QA -> merge.

    Args:
        on_progress: Optional callback ``(event_type: str, data: dict) -> None``
            called at key execution points. Event types:
            - ``"phase"``  — phase start/end (name, status, time_s, cost, error)
            - ``"agent_tool"`` — significant tool call from coding/QA agent (name, detail)

    Returns {success, status, cost_usd, error, diff_summary, qa_report, phase_timings}.
    """
    key = task["key"]
    task_id = task["id"]
    prompt = task["prompt"]
    max_retries = task.get("max_retries", config["max_retries"])
    max_task_time = config.get("max_task_time", 3600)  # 1 hour circuit breaker
    default_branch = config["default_branch"]
    timeout = config["verify_timeout"]

    task_start = time.monotonic()
    total_cost = 0.0
    session_id = None
    last_error = None
    # Resume attempt count from persisted state — prevents the pilot from
    # circumventing max_retries by calling run_task_with_qa multiple times.
    prior_attempts = task.get("attempts", 0) or 0
    total_attempts = prior_attempts
    phase_timings: dict[str, float] = {}  # phase_name -> elapsed seconds
    # Live state for otto status -w (read from another terminal)
    _live_state_file = project_dir / "otto_logs" / "live-state.json"
    _live_phases: dict[str, dict] = {
        p: {"status": "pending", "time_s": 0.0}
        for p in ["prepare", "coding", "test", "qa", "merge"]
    }
    _live_tools: list[str] = []

    def emit(event: str, **data: Any) -> None:
        if on_progress:
            try:
                on_progress(event, data)
            except Exception:
                pass
        # Update live state file for otto status -w
        try:
            if event == "phase":
                name = data.get("name", "")
                if name in _live_phases:
                    _live_phases[name]["status"] = data.get("status", "")
                    if data.get("time_s"):
                        _live_phases[name]["time_s"] = data["time_s"]
                    if data.get("error"):
                        _live_phases[name]["error"] = data["error"][:100]
            elif event == "agent_tool":
                detail = data.get("detail", "")
                tool_name = data.get("name", "")
                _live_tools.append(f"{tool_name}  {detail}" if detail else tool_name)
                if len(_live_tools) > 20:
                    _live_tools[:] = _live_tools[-20:]
            _live_state_file.write_text(json.dumps({
                "task_key": key, "task_id": task_id,
                "prompt": prompt[:80],
                "elapsed_s": round(time.monotonic() - task_start, 1),
                "cost_usd": total_cost,
                "phases": _live_phases,
                "recent_tools": list(_live_tools),
            }))
        except Exception:
            pass

    def _result(success: bool, status: str, error: str = "",
                diff_summary: str = "", qa_report: str = "") -> dict[str, Any]:
        # Clean up live state file
        try:
            if _live_state_file.exists():
                _live_state_file.unlink()
        except OSError:
            pass
        duration = time.monotonic() - task_start
        if tasks_file:
            try:
                updates: dict[str, Any] = {"status": status, "cost_usd": total_cost}
                if duration > 0:
                    updates["duration_s"] = round(duration, 1)
                if error:
                    updates["error"] = error
                update_task(tasks_file, key, **updates)
            except Exception:
                pass
        return {
            "success": success,
            "status": status,
            "cost_usd": total_cost,
            "error": error,
            "diff_summary": diff_summary,
            "qa_report": qa_report,
            "phase_timings": phase_timings,
        }

    try:
        # Step 1: Prepare — create branch, build prompt
        emit("phase", name="prepare", status="running")
        prep_start = time.monotonic()
        prep = prepare_task(task, config, project_dir, tasks_file, hint=hint)
        base_sha = prep["base_sha"]
        pre_existing_untracked = set(prep.get("pre_untracked") or [])
        agent_prompt = prep["prompt"]

        log_dir = project_dir / "otto_logs" / key
        log_dir.mkdir(parents=True, exist_ok=True)

        verify_cmd = task.get("verify")
        test_command = config.get("test_command")
        spec = task.get("spec")

        # Auto-detect test_command if spec exists but no test_command configured
        if spec and not test_command:
            detected = detect_test_command(project_dir)
            test_command = detected if detected else "pytest"

        # Baseline test check — verify test infrastructure works BEFORE coding.
        # Only blocks on infrastructure failures (missing modules, broken config).
        # Normal test failures are allowed (bugfix projects, greenfield with no tests).
        if test_command:
            from otto.verify import run_tier1
            baseline = run_tier1(project_dir, test_command, timeout)
            if not baseline.passed and not baseline.skipped:
                output = baseline.output or ""
                # Detect infrastructure failures vs normal test failures.
                # Infra failures: the test runner itself can't start or find tests.
                infra_keywords = [
                    "Cannot find module",  # missing JS dependency/config
                    "ModuleNotFoundError",  # missing Python module
                    "command not found",  # test runner not installed
                    "No module named",  # Python import failure
                    "SyntaxError",  # broken source code
                    "error: unrecognized arguments",  # bad pytest config
                    "errors during collection",  # pytest can't collect tests
                ]
                is_infra_failure = any(kw in output for kw in infra_keywords)
                if is_infra_failure:
                    prep_elapsed = round(time.monotonic() - prep_start, 1)
                    phase_timings["prepare"] = prep_elapsed
                    emit("phase", name="prepare", status="fail", time_s=prep_elapsed,
                         error="baseline tests fail before coding — infrastructure issue")
                    err_detail = output[-500:]
                    return _result(
                        False, "failed",
                        error=f"BASELINE_FAIL: test infrastructure is broken on unmodified code. "
                              f"This is a setup issue, not a coding issue.\n{err_detail}",
                    )
                # Normal test failures (bugfix project, some tests fail) — proceed.
                # The coding agent will fix them.

        prep_elapsed = round(time.monotonic() - prep_start, 1)
        phase_timings["prepare"] = prep_elapsed
        emit("phase", name="prepare", status="done", time_s=prep_elapsed)

        # Check if this task was previously passed (retried via `otto retry --force`)
        # Only skip if ALL conditions hold:
        #   1. tasks.yaml records status="passed" (prior successful run)
        #   2. Merge commit SHA recorded from prior run
        #   3. Task fingerprint matches (prompt+spec hasn't changed since pass)
        #   4. Branch has no new commits (HEAD == base_sha, agent hasn't started)
        #   5. The merge commit is still reachable on default branch
        import hashlib
        _task_fp = hashlib.sha256(
            (prompt + str(spec or "")).encode()
        ).hexdigest()[:16]
        merged_sha = task.get("merged_sha", "")
        merged_fp = task.get("task_fingerprint", "")
        if task.get("status") == "passed" and merged_sha and merged_fp == _task_fp:
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            ).stdout.strip()
            if head_sha == base_sha:
                # Verify the merge commit is still reachable
                reachable = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", merged_sha, default_branch],
                    cwd=project_dir, capture_output=True,
                ).returncode == 0
                if reachable:
                    _log_warn("Task was previously passed — skipping (use otto reset to re-run)")
                    emit("phase", name="coding", status="done", time_s=0,
                         detail="already implemented")
                    subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
                    cleanup_branch(project_dir, key, default_branch)
                    return _result(True, "passed",
                                   diff_summary="Already implemented (from previous run)")

        # Step 2+3: Code + Verify + QA loop
        # Both coding retries AND QA-triggered retries count against max_retries.
        # Overall time budget prevents unbounded cycles.
        # prior_attempts ensures the pilot can't circumvent max_retries by
        # calling run_task_with_qa multiple times for the same task.
        remaining = max(0, max_retries + 1 - prior_attempts)
        if remaining == 0:
            return _result(False, "failed",
                           error=f"max retries already exhausted ({prior_attempts} prior attempts)")
        for attempt in range(remaining):
            attempt_num = prior_attempts + attempt + 1
            total_attempts += 1

            # Time budget check — prevent unbounded QA-retry cycles
            elapsed = time.monotonic() - task_start
            if elapsed > max_task_time and attempt > 0:
                return _result(False, "failed",
                               error=f"time budget exceeded ({int(elapsed)}s > {max_task_time}s) "
                                     f"after {total_attempts} attempts")

            if tasks_file:
                update_task(tasks_file, key, attempts=total_attempts)

            # Build prompt for retry
            if attempt > 0 and last_error is not None:
                agent_prompt = (
                    f"Verification failed. Here is the output:\n\n"
                    f"{last_error}\n\n"
                    f"Original task: {prompt}\n\n"
                    f"You are working in {project_dir}. Do NOT create git commits."
                )

            # Run coding agent
            emit("phase", name="coding", status="running", attempt=attempt_num)
            coding_start = time.monotonic()
            try:
                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(project_dir),
                    setting_sources=["user", "project"],
                    env=_subprocess_env(),
                    effort=config.get("effort", "high"),
                    system_prompt=CODING_SYSTEM_PROMPT,
                )
                if config.get("model"):
                    agent_opts.model = config["model"]
                if session_id:
                    agent_opts.resume = session_id
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
                        pass

                agent_log_lines: list[str] = []
                result_msg = None
                _last_block_name = ""
                _last_block_inputs: dict = {}
                async for message in query(prompt=agent_prompt, options=agent_opts):
                    if isinstance(message, ResultMessage):
                        result_msg = message
                    elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                        result_msg = message
                    elif AssistantMessage and isinstance(message, AssistantMessage):
                        for block in message.content:
                            # Check for ToolResultBlock in any message type
                            if ToolResultBlock and isinstance(block, ToolResultBlock):
                                content = str(getattr(block, "content", ""))
                                if content and _last_block_name == "Bash":
                                    result_line = ""
                                    for rl in reversed(content.splitlines()):
                                        ls = rl.strip()
                                        if any(kw in ls.lower() for kw in
                                               ["passed", "failed", "tests:", "test suites:"]):
                                            if any(c.isdigit() for c in ls):
                                                result_line = ls[:70]
                                                break
                                    if result_line:
                                        is_pass = "passed" in result_line.lower() and "failed" not in result_line.lower()
                                        emit("agent_tool_result", detail=result_line, passed=is_pass)
                                _last_block_name = ""
                                _last_block_inputs = {}
                                continue
                            if TextBlock and isinstance(block, TextBlock) and block.text:
                                agent_log_lines.append(block.text)
                            elif ToolUseBlock and isinstance(block, ToolUseBlock):
                                _last_block_name = block.name
                                _last_block_inputs = block.input or {}
                                agent_log_lines.append(f"● {block.name}  {_tool_use_summary(block)}")
                                event = _build_agent_tool_event(block)
                                if event:
                                    emit("agent_tool", **event)
                            # Note: ToolResultBlock handling moved to top of block loop

                # Persist agent log
                try:
                    (log_dir / f"attempt-{attempt_num}-agent.log").write_text(
                        "\n".join(agent_log_lines)
                    )
                except OSError:
                    pass

                if result_msg and getattr(result_msg, "session_id", None):
                    session_id = result_msg.session_id
                    if tasks_file:
                        update_task(tasks_file, key, session_id=session_id)

                raw_cost = getattr(result_msg, "total_cost_usd", None)
                attempt_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
                total_cost += attempt_cost

                coding_elapsed = round(time.monotonic() - coding_start, 1)
                phase_timings["coding"] = phase_timings.get("coding", 0) + coding_elapsed
                # No git diff stat here — it's unreliable during coding
                # (agent may or may not have committed). The display tracks
                # file/line counts from Write/Edit events instead.
                emit("phase", name="coding", status="done", time_s=coding_elapsed,
                     cost=attempt_cost, attempt=attempt_num)

                if result_msg and result_msg.is_error:
                    raise RuntimeError(f"Agent error: {result_msg.result or 'unknown'}")

            except Exception as e:
                coding_elapsed = round(time.monotonic() - coding_start, 1)
                emit("phase", name="coding", status="fail", time_s=coding_elapsed,
                     error=str(e)[:80], attempt=attempt_num)
                _log_warn(f"Agent error: {e}")
                _restore_workspace_state(
                    project_dir,
                    reset_ref=base_sha,
                    pre_existing_untracked=pre_existing_untracked,
                )
                last_error = str(e)
                continue

            # Check if agent made changes
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

            if no_changes:
                if spec:
                    last_error = "No file changes were detected after your session."
                    continue
                # No spec + no changes = nothing to do, pass
                subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
                cleanup_branch(project_dir, key, default_branch)
                return _result(True, "passed", diff_summary="No changes needed")

            # Build candidate commit
            candidate_sha = build_candidate_commit(
                project_dir, base_sha, None, pre_existing_untracked,
            )

            # Re-detect test command
            if not config.get("test_command"):
                detected = detect_test_command(project_dir)
                if detected:
                    test_command = detected

            # Run verification
            emit("phase", name="test", status="running")
            verify_start = time.monotonic()
            verify_result = run_verification(
                project_dir=project_dir,
                candidate_sha=candidate_sha,
                test_command=test_command,
                verify_cmd=verify_cmd,
                timeout=timeout,
            )
            verify_elapsed = round(time.monotonic() - verify_start, 1)
            phase_timings["test"] = phase_timings.get("test", 0) + verify_elapsed

            # Write verify log
            try:
                (log_dir / f"attempt-{attempt_num}-verify.log").write_text(
                    "\n".join(f"{t.tier}: {'PASS' if t.passed else 'FAIL'}\n{t.output}"
                              for t in verify_result.tiers)
                )
            except OSError:
                pass

            # Extract test summary from verify output for display
            verify_detail = ""
            for tier in verify_result.tiers:
                if tier.output:
                    for line in reversed(tier.output.splitlines()):
                        ls = line.strip()
                        # Match common test result patterns
                        if any(kw in ls.lower() for kw in
                               ["passed", "failed", "error", "tests:", "test suites:"]):
                            if any(c.isdigit() for c in ls):
                                verify_detail = ls[:70]
                                break

            if verify_result.passed:
                emit("phase", name="test", status="done", time_s=verify_elapsed,
                     detail=verify_detail)

                # Squash commits into a single commit
                try:
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=project_dir, capture_output=True, check=True,
                    )
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

                    commit_msg = f"otto: {prompt[:60]} (#{task_id})"
                    subprocess.run(
                        ["git", "commit", "-m", commit_msg],
                        cwd=project_dir, capture_output=True, text=True, check=True,
                    )
                except (subprocess.CalledProcessError, Exception) as e:
                    stderr = getattr(e, "stderr", str(e))
                    _cleanup_task_failure(
                        project_dir, key, default_branch, tasks_file,
                        pre_existing_untracked=pre_existing_untracked,
                        error=f"squash commit failed: {stderr}",
                        error_code="internal_error",
                        cost_usd=total_cost,
                        duration_s=time.monotonic() - task_start,
                    )
                    return _result(False, "failed", error=f"squash commit failed: {stderr}")

                # Build diff — full diff for QA, stat for display/telemetry
                diff_stat = subprocess.run(
                    ["git", "diff", "--stat", base_sha, "HEAD"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                diff_summary = diff_stat.stdout.strip() if diff_stat.returncode == 0 else ""
                full_diff = subprocess.run(
                    ["git", "diff", base_sha, "HEAD"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                diff_for_qa = full_diff.stdout.strip() if full_diff.returncode == 0 else diff_summary

                # Write verify.log for read_verify_output
                try:
                    (log_dir / "verify.log").write_text("PASSED")
                except OSError:
                    pass

                # Step 4: QA agent (if spec exists)
                qa_report = ""
                if spec:
                    emit("phase", name="qa", status="running")
                    qa_start = time.monotonic()
                    qa_result = await run_qa_agent(task, config, project_dir, diff_for_qa,
                                                      on_progress=on_progress)
                    qa_elapsed = round(time.monotonic() - qa_start, 1)
                    phase_timings["qa"] = phase_timings.get("qa", 0) + qa_elapsed
                    qa_report = qa_result.get("report", "")

                    # Persist QA report for otto show/logs
                    try:
                        (log_dir / "qa-report.md").write_text(qa_report or "No QA output")
                    except OSError:
                        pass

                    if not qa_result["passed"]:
                        emit("phase", name="qa", status="fail", time_s=qa_elapsed,
                             error="QA verdict: FAIL")
                        # QA failed — retry coding with QA findings
                        # Reset to base for retry
                        subprocess.run(
                            ["git", "reset", "--mixed", base_sha],
                            cwd=project_dir, capture_output=True,
                        )
                        last_error = (
                            f"QA TESTING FAILED. Fix these issues:\n\n{qa_report}\n\n"
                            f"Original task: {prompt}"
                        )
                        # Continue the retry loop (counts against max_retries)
                        continue
                    else:
                        emit("phase", name="qa", status="done", time_s=qa_elapsed)

                # Step 5: Merge to default branch
                emit("phase", name="merge", status="running")
                merge_start = time.monotonic()
                if merge_to_default(project_dir, key, default_branch):
                    merge_elapsed = round(time.monotonic() - merge_start, 1)
                    phase_timings["merge"] = merge_elapsed
                    emit("phase", name="merge", status="done", time_s=merge_elapsed)
                    # Record merge commit SHA + task fingerprint for already-merged detection on retry
                    try:
                        _merged_sha = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=project_dir, capture_output=True, text=True,
                        ).stdout.strip()
                        if tasks_file and _merged_sha:
                            update_task(tasks_file, key,
                                        merged_sha=_merged_sha,
                                        task_fingerprint=_task_fp)
                    except Exception:
                        pass
                    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                    if testgen_dir.exists():
                        shutil.rmtree(testgen_dir, ignore_errors=True)
                    return _result(True, "passed", diff_summary=diff_summary, qa_report=qa_report)
                else:
                    merge_elapsed = round(time.monotonic() - merge_start, 1)
                    emit("phase", name="merge", status="fail", time_s=merge_elapsed,
                         error="branch diverged")
                    return _result(
                        False, "failed",
                        error=f"branch diverged — otto/{key} preserved, manual rebase needed",
                        diff_summary=diff_summary,
                        qa_report=qa_report,
                    )

            # Verification failed — reset for retry
            verify_err = verify_result.failure_output or "verification failed"
            emit("phase", name="test", status="fail", time_s=verify_elapsed,
                 error=verify_err[:80], detail=verify_detail)
            subprocess.run(
                ["git", "reset", "--mixed", base_sha],
                cwd=project_dir, capture_output=True,
            )
            last_error = verify_result.failure_output

        # All retries exhausted — include the last actual error so the pilot
        # knows what went wrong (not just "max retries exhausted")
        last_err_detail = f"\nLast error:\n{last_error}" if last_error else ""
        _cleanup_task_failure(
            project_dir, key, default_branch, tasks_file,
            pre_existing_untracked=pre_existing_untracked,
            error=f"max retries exhausted{last_err_detail}", error_code="max_retries",
            cost_usd=total_cost,
            duration_s=time.monotonic() - task_start,
        )
        return _result(False, "failed",
                       error=f"max retries exhausted ({total_attempts} attempts).{last_err_detail}")

    except Exception as e:
        duration = time.monotonic() - task_start
        try:
            _cleanup_task_failure(
                project_dir, key, default_branch, tasks_file,
                error=f"unexpected error: {e}", error_code="internal_error",
                cost_usd=total_cost,
                duration_s=duration,
            )
        except Exception:
            pass
        return _result(False, "failed", error=f"unexpected error: {e}")


def _print_summary(
    results: list[tuple[dict, bool]],
    total_duration: float,
    integration_passed: bool | None = None,
    total_cost: float = 0.0,
    task_progress: dict[str, list[dict]] | None = None,
) -> None:
    """Print summary of all tasks after a run.

    Args:
        task_progress: Optional mapping of task_key -> list of progress events
            collected during the run. Used to display per-phase timing breakdowns.
    """
    passed = sum(1 for _, s in results if s)
    failed = len(results) - passed

    cost_str = f"  {_format_cost(total_cost)}" if total_cost > 0 else ""
    console.print()
    console.print(f"  [bold]Run complete[/bold]  [dim]{_format_duration(total_duration)}{cost_str}[/dim]")
    console.print()

    for task, success in results:
        icon = "[green]\u2713[/green]" if success else "[red]\u2717[/red]"
        task_key = task.get("key", "")
        task_cost = task.get("cost_usd", 0.0)
        task_duration = task.get("duration_s", 0.0)

        # Build per-phase timing string from progress events
        phase_parts: list[str] = []
        file_changes: list[str] = []
        qa_summary = ""
        if task_progress and task_key in task_progress:
            events = task_progress[task_key]
            # Prefer phase_timings from the final run_task_with_qa result
            phase_times: dict[str, float] = {}
            result_evt = None
            for evt in events:
                if evt.get("_result") and "phase_timings" in evt:
                    result_evt = evt
            if result_evt:
                phase_times = {
                    k: float(v) for k, v in result_evt["phase_timings"].items() if v
                }
            else:
                for evt in events:
                    if evt.get("event") == "phase" and evt.get("status") in ("done", "fail"):
                        pname = evt.get("name", "")
                        ptime = evt.get("time_s", 0)
                        if pname and ptime:
                            phase_times[pname] = phase_times.get(pname, 0) + float(ptime)
            for pname in ["prepare", "coding", "test", "qa", "merge"]:
                if pname in phase_times and phase_times[pname] >= 1:
                    phase_parts.append(f"{_format_duration(phase_times[pname])} {pname}")

        # Build the main status line
        dur_str = f"  [dim]{_format_duration(task_duration)}[/dim]" if task_duration else ""
        cost_part = f"  [dim]{_format_cost(task_cost)}[/dim]" if task_cost > 0 else ""
        console.print(f"  {icon} [bold]#{task['id']}[/bold]  {rich_escape(task['prompt'][:60])}{dur_str}{cost_part}")

        # Show phase timing breakdown on the next line
        if phase_parts:
            sep = " \u00b7 "
            console.print(f"       {sep.join(phase_parts)}", style="dim")

        # Show diff summary from task metadata if available
        diff_summary = task.get("diff_summary", "")
        if diff_summary:
            diff_files = []
            for line in diff_summary.splitlines():
                line = line.strip()
                if "|" in line:
                    fname = line.split("|")[0].strip()
                    diff_files.append(fname)
            if diff_files:
                files_str = "  ".join(diff_files[:5])
                if len(diff_files) > 5:
                    files_str += f"  (+{len(diff_files) - 5} more)"
                console.print(f"       {files_str}", style="dim")

    console.print()
    if failed == 0:
        console.print(f"  [green bold]{passed}/{len(results)} tasks passed[/green bold]")
    else:
        console.print(f"  [green]{passed} passed[/green]  [red]{failed} failed[/red]  [dim]of {len(results)} tasks[/dim]")

    if integration_passed is not None:
        icon = "[green]\u2713[/green]" if integration_passed else "[red]\u2717[/red]"
        label = "passed" if integration_passed else "FAILED"
        console.print(f"  {icon} Integration gate {label}")

    console.print()
