"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import json
import os
import re
import shutil
import subprocess
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
from otto.verify import run_verification, _subprocess_env

_CANDIDATE_ATTEMPT_RE = re.compile(r"/attempt-(\d+)$")


def _fence_untrusted_text(text: str) -> str:
    """Wrap untrusted model/input text in a code fence."""
    text = text or ""
    max_ticks = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, max_ticks + 1)
    return f"{fence}\n{text}\n{fence}"




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

    # Ensure .gitignore covers known build/dependency dirs.
    # Uses framework detection (not size heuristics) — only auto-adds
    # from a curated allowlist keyed off project manifest files.
    _FRAMEWORK_IGNORES: dict[str, list[str]] = {
        "package.json": ["node_modules/", ".next/", "dist/", "build/", "coverage/", ".turbo/"],
        "pyproject.toml": ["__pycache__/", ".venv/", "dist/", ".pytest_cache/", "*.egg-info"],
        "requirements.txt": ["__pycache__/", ".venv/", ".pytest_cache/"],
        "setup.py": ["__pycache__/", ".venv/", "dist/", "*.egg-info", "build/"],
        "Cargo.toml": ["target/"],
        "go.mod": ["vendor/"],
        "Gemfile": ["vendor/bundle/"],
    }
    gitignore = project_dir / ".gitignore"
    existing_text = gitignore.read_text() if gitignore.exists() else ""
    existing_ignores = {
        line.strip() for line in existing_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing_ignores = []
    for manifest, dirs in _FRAMEWORK_IGNORES.items():
        if (project_dir / manifest).exists():
            for d in dirs:
                if d not in existing_ignores and d.rstrip("/") not in existing_ignores:
                    missing_ignores.append(d)
    if missing_ignores:
        with open(gitignore, "a") as f:
            if existing_text and not existing_text.endswith("\n"):
                f.write("\n")
            f.write("\n".join(sorted(set(missing_ignores))) + "\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=project_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "otto: update .gitignore for build artifacts"],
            cwd=project_dir, capture_output=True,
        )
        label = ", ".join(d.rstrip("/") for d in missing_ignores[:3])
        console.print(f"  [dim]Updated .gitignore (+{label})[/dim]")

    # Ensure .git/info/exclude has otto runtime entries.
    # Written every preflight because git reset --hard can wipe it.
    from otto.config import git_meta_dir
    exclude_path = git_meta_dir(project_dir) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_exclude = exclude_path.read_text() if exclude_path.exists() else ""
    otto_excludes = ["otto_logs/", "otto_arch/", "tasks.yaml", ".tasks.lock"]
    missing_excludes = [e for e in otto_excludes if e not in existing_exclude]
    if missing_excludes:
        with open(exclude_path, "a") as f:
            f.write("\n# otto runtime files\n")
            for entry in missing_excludes:
                f.write(entry + "\n")

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

def _build_agent_tool_event(block) -> dict[str, Any] | None:
    """Build a progress payload for a tool use block."""
    def _should_emit_tool(tool_name: str, detail: str) -> bool:
        if tool_name in ("Write", "Edit"):
            return bool(detail)
        if tool_name == "Read":
            return any(ext in detail for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go"))
        if tool_name == "Bash":
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

    name = block.name
    inputs = block.input or {}
    raw_detail = _tool_use_summary(block)

    if name == "Glob":
        if not _should_emit_tool("Read", raw_detail):
            return None
        return {"name": "Read", "detail": raw_detail[:80]}

    if name not in ("Read", "Write", "Edit", "Bash"):
        return None
    if not _should_emit_tool(name, raw_detail):
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


def _should_stage_untracked(rel_path: str) -> bool:
    """Decide if an untracked file should be included in the candidate commit.

    Stages all project source files. Excludes otto runtime files and
    obvious build artifacts/caches — even if .gitignore doesn't cover them.
    """
    # Otto runtime files — never commit
    _OTTO_PATHS = ("otto_logs/", "otto_arch/", "tasks.yaml", ".tasks.lock", "otto.lock")
    if any(rel_path == p or rel_path.startswith(p) for p in _OTTO_PATHS):
        return False

    # Build artifacts and caches — never commit
    _ARTIFACT_PATTERNS = (
        "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
        ".venv/", "node_modules/", ".next/", "dist/", "build/", "coverage/",
        "target/", ".turbo/", ".egg-info",
    )
    if any(p in rel_path for p in _ARTIFACT_PATTERNS):
        return False

    # Compiled files
    if rel_path.endswith((".pyc", ".pyo", ".o", ".so", ".dylib")):
        return False

    # Everything else is candidate-eligible (source files, assets, configs)
    return True


def build_candidate_commit(
    project_dir: Path,
    base_sha: str,
    pre_existing_untracked: set[str] | None = None,
) -> str:
    """Build a candidate commit with the agent's changes."""
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
    # Stage untracked project source files — includes both agent-created
    # AND pre-existing untracked files (they may be imported by agent code).
    # Excludes otto runtime files and build artifacts via _should_stage_untracked.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    for f in untracked.stdout.split("\0"):
        if f and _should_stage_untracked(f):
            subprocess.run(
                ["git", "add", "--", f],
                cwd=project_dir, capture_output=True,
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

    Used for serial merge of parallel tasks.
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
    """v4 coding loop — run a single task through the v4.5 execution path.

    Passes an on_progress callback to run_task_v45() so phase events flow
    through the telemetry dual-write (legacy pilot_results.jsonl).
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
            elif event_type == "spec_item":
                display.add_spec_item(data.get("text", ""))
            elif event_type == "spec_items_done":
                display.flush_spec_summary()
            elif event_type == "qa_finding":
                display.add_finding(data.get("text", ""))
            elif event_type == "qa_status":
                # QA reasoning narration — show as dim status line
                text = data.get("text", "")
                if text:
                    console.print(f"      [dim]{rich_escape(text[:80])}[/dim]")
            elif event_type == "qa_item_result":
                display.add_qa_item_result(
                    text=data.get("text", ""),
                    passed=data.get("passed", True),
                    evidence=data.get("evidence", ""),
                )
            elif event_type == "qa_summary":
                display.set_qa_summary(
                    total=data.get("total", 0),
                    passed=data.get("passed", 0),
                    failed=data.get("failed", 0),
                )
            elif event_type == "attempt_boundary":
                display.add_attempt_boundary(
                    attempt=data.get("attempt", 0),
                    reason=data.get("reason", ""),
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
        # v4.5: use run_task_v45 which passes context directly
        result = await run_task_v45(
            task, config, project_dir, tasks_file,
            context=context, on_progress=_on_progress,
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
            console.print(
                f"    {time.strftime('%H:%M:%S')}  [green]\u2713[/green] passed  "
                f"[dim]{elapsed_str}  ${cost:.2f}[/dim]"
            )
            task_meta = task
            if tasks_file:
                updated_tasks = load_tasks(tasks_file)
                task_meta = next((t for t in updated_tasks if t.get("key") == task_key), task)
            diff_summary = result.get("diff_summary", "")
            file_count = sum(1 for line in diff_summary.splitlines() if "|" in line) if diff_summary else 0
            spec_count = len(task_meta.get("spec") or [])
            attempts = int(task_meta.get("attempts", 1) or 1)
            parts = []
            if file_count:
                parts.append(f"{file_count} files")
            if spec_count:
                parts.append(f"{spec_count} specs verified")
            if attempts > 1:
                parts.append(f"{attempts} attempts")
            if parts:
                console.print(f"      [dim]{' · '.join(parts)}[/dim]")

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
        console.print(
            f"    {time.strftime('%H:%M:%S')}  [red]\u2717[/red] failed  "
            f"[dim]{elapsed_str}  ${cost:.2f}[/dim]"
        )
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
            review_ref=result.get("review_ref"),
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
    console.print(f"  {msg}")


def _log_warn(msg: str) -> None:
    console.print(f"  [yellow]Warning: {rich_escape(msg)}[/yellow]")


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


# ---------------------------------------------------------------------------
# v4.5 — Bare CC coding, structured QA, risk-based tiering, candidate refs
# ---------------------------------------------------------------------------

QA_SYSTEM_PROMPT_V45 = """\
You are an adversarial QA tester. Test the implementation against
the acceptance criteria and the original task prompt.

Binding levels:
- [must] items: a failure here blocks merge. Test the hardest cases.
- [should] items: note whether followed, but do not block merge.

Also check:
- Does the implementation contradict the ORIGINAL task prompt?
- Does it break existing functionality?
- Did the agent add improvements beyond the spec? Note them positively.

Choose your testing approach based on what the task needs:
- API/CLI tasks: curl, subprocess, script-based checks
- SSR web apps: curl for content, browser for interactivity/layout
- SPA/client-side apps: browser is essential (curl sees empty div)
- Visual/UX tasks: browser + screenshot is the only way to verify

Write your verdict to the output file as JSON:
{
  "must_passed": true/false,
  "must_items": [
    {"criterion": "...", "status": "pass/fail", "evidence": "..."}
  ],
  "should_notes": [
    {"criterion": "...", "observation": "...", "screenshot": "path or null"}
  ],
  "regressions": [],
  "prompt_intent": "Implementation matches/diverges from original prompt because...",
  "extras": ["Agent added contributing factor explanations — improves UX"]
}

Kill any servers you started (by PID, not pkill)."""


def format_spec_v45(spec: list) -> str:
    """Format spec items with [must]/[should] binding for prompt injection.

    Non-verifiable (subjective) items get a ◈ marker: [must ◈], [should ◈].
    """
    from otto.tasks import spec_text, spec_binding, spec_is_verifiable
    lines = []
    for item in spec:
        text = spec_text(item)
        binding = spec_binding(item)
        marker = "" if spec_is_verifiable(item) else " \u25c8"  # ◈
        lines.append(f"  [{binding}{marker}] {text}")
    return "\n".join(lines)


def determine_qa_tier(
    task: dict[str, Any],
    spec: list,
    attempt: int,
    diff_info: dict[str, Any],
    spec_test_mapping: dict[str, str | None] | None = None,
) -> int:
    """Determine QA tier based on residual risk after verification.

    Tier 0: skip QA (all [must] items have tests, local change, first attempt)
    Tier 1: targeted QA (unmapped [must] items, cross-cutting changes)
    Tier 2: full QA with browser (visual/SPA, auth/crypto, retries)
    """
    from otto.tasks import spec_binding

    diff_files = diff_info.get("files", [])
    spec_test_mapping = spec_test_mapping or {}

    # Tier 2: high-risk domains
    HIGH_RISK_PATTERNS = ["auth", "crypto", "permission", "migration",
                          "payment", "security", "token", "session"]
    if any(pattern in f.lower() for f in diff_files for pattern in HIGH_RISK_PATTERNS):
        return 2

    # Tier 2: visual/UI specs (need browser), SPA apps, or retries
    from otto.tasks import spec_text
    has_visual = any(
        spec_binding(item) == "should"
        and any(kw in spec_text(item).lower()
                for kw in ("ui", "layout", "style", "visual", "responsive"))
        for item in spec
    )
    is_spa = any(f.endswith((".jsx", ".tsx", ".vue", ".svelte")) for f in diff_files)
    if has_visual or is_spa or attempt > 0:
        return 2

    # Tier 1: unmapped [must] items or cross-cutting changes
    unmapped = [item for item in spec
                if spec_binding(item) == "must"
                and not spec_test_mapping.get(spec_text(item))]
    if unmapped or len(diff_files) > 5:
        return 1

    # Tier 0: every [must] item has a test, local change, first attempt
    return 0


def _anchor_candidate_ref(project_dir: Path, task_key: str, attempt_num: int, commit_sha: str) -> str:
    """Anchor a verified candidate as a durable git ref.

    Returns the ref name. SHAs without refs can become dangling after reset.
    """
    ref_name = f"refs/otto/candidates/{task_key}/attempt-{attempt_num}"
    result = subprocess.run(
        ["git", "update-ref", ref_name, commit_sha],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"failed to anchor candidate ref {ref_name}: {stderr or 'git update-ref failed'}")
    return ref_name


def _find_best_candidate_ref(project_dir: Path, task_key: str) -> str | None:
    """Find the best verified candidate ref for a task.

    Returns the ref name of the most recent verified candidate, or None.
    """
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", f"refs/otto/candidates/{task_key}/"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    refs = [r.strip() for r in result.stdout.strip().splitlines() if r.strip()]
    if not refs:
        return None

    def _sort_key(ref_name: str) -> tuple[int, str]:
        match = _CANDIDATE_ATTEMPT_RE.search(ref_name)
        attempt_num = int(match.group(1)) if match else -1
        return (attempt_num, ref_name)

    return max(refs, key=_sort_key)


def _get_diff_info(project_dir: Path, base_sha: str) -> dict[str, Any]:
    """Get diff info for QA tiering."""
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )
    files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]

    full_diff = subprocess.run(
        ["git", "diff", base_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    )

    return {
        "files": files,
        "full_diff": full_diff.stdout.strip() if full_diff.returncode == 0 else "",
    }


def _parse_qa_verdict_json(report: str) -> dict[str, Any]:
    """Parse structured JSON QA verdict from agent output.

    Searches for a JSON block in the report text. Falls back to
    legacy pass/fail detection if no JSON found.
    """
    import re as _re

    # Try to find JSON block in the report
    # Look for ```json ... ``` or raw JSON object
    json_match = _re.search(r'```json\s*\n(.*?)```', report, _re.DOTALL)
    if not json_match:
        json_match = _re.search(r'(\{[^{}]*"must_passed"[^{}]*\})', report, _re.DOTALL)
    if not json_match:
        # Try to find a larger JSON block with nested objects
        json_match = _re.search(r'(\{.*"must_passed".*\})', report, _re.DOTALL)

    if json_match:
        try:
            verdict = json.loads(json_match.group(1))
            if isinstance(verdict, dict) and "must_passed" in verdict:
                return verdict
        except json.JSONDecodeError:
            pass

    # Try reading from a verdict file if the agent wrote one
    # (QA prompt says "Write your verdict to the output file as JSON")

    # Fallback: parse legacy format
    upper = report.upper()
    has_explicit_fail = "QA VERDICT: FAIL" in report or "VERDICT: FAIL" in upper
    has_explicit_pass = "QA VERDICT: PASS" in report or "VERDICT: PASS" in upper
    # Also detect natural language pass patterns
    if not has_explicit_pass and not has_explicit_fail:
        pass_patterns = ["all must", "all criteria pass", "ready to merge",
                         "all 🟢", "all pass"]
        has_explicit_pass = any(p in report.lower() for p in pass_patterns)

    return {
        "must_passed": has_explicit_pass and not has_explicit_fail,
        "must_items": [],
        "should_notes": [],
        "regressions": [],
        "prompt_intent": "",
        "extras": [],
        "_legacy_parse": True,
    }


async def run_qa_agent_v45(
    task: dict[str, Any],
    spec: list,
    config: dict[str, Any],
    project_dir: Path,
    original_prompt: str,
    diff: str,
    tier: int = 1,
    focus_items: list | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """v4.5 QA agent — structured JSON verdict, risk-based tiering.

    Returns {must_passed, verdict, raw_report, cost_usd}.
    """
    from otto.tasks import spec_text, spec_binding

    # Build spec section with binding levels
    spec_lines = []
    for i, item in enumerate(spec):
        text = spec_text(item)
        binding = spec_binding(item)
        spec_lines.append(f"  {i+1}. [{binding}] {text}")
    spec_section = "\n".join(spec_lines)

    # Build focus section for targeted QA
    focus_section = ""
    if focus_items:
        focus_texts = [spec_text(item) for item in focus_items]
        focus_section = "\n\nFocus your testing on these items that lack test coverage:\n"
        focus_section += "\n".join(f"  - {t}" for t in focus_texts)

    # Create a temp file for the verdict
    with tempfile.NamedTemporaryFile(suffix=".json", prefix="otto_qa_", delete=False) as tf:
        verdict_file = Path(tf.name)

    qa_prompt = f"""Test this implementation against the acceptance criteria and the original task prompt.

You are working in {project_dir}. All project files are in this directory. Do not search outside it.

ORIGINAL TASK PROMPT:
{original_prompt}

ACCEPTANCE CRITERIA:
{spec_section}
{focus_section}

DIFF:
{diff}

Write your JSON verdict to: {verdict_file}
"""

    # Configure MCP servers for browser testing (tier 2)
    qa_mcp_servers = {}
    if tier >= 2:
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
            except Exception:
                pass

    _qa_settings = config.get("qa_agent_settings", "project").split(",")
    qa_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_qa_settings,
        env=_subprocess_env(),
        # Keep CC's default prompt (Glob over find, etc.) + append QA instructions
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": QA_SYSTEM_PROMPT_V45},
    )
    if qa_mcp_servers:
        qa_opts.mcp_servers = qa_mcp_servers
    if config.get("model"):
        qa_opts.model = config["model"]

    qa_timeout = config.get("qa_timeout", 3600)
    report_lines: list[str] = []
    qa_cost = 0.0

    try:
        async def _run_qa():
            nonlocal report_lines, qa_cost
            async for message in query(prompt=qa_prompt, options=qa_opts):
                if isinstance(message, ResultMessage):
                    raw_cost = getattr(message, "total_cost_usd", None)
                    if isinstance(raw_cost, (int, float)):
                        qa_cost = float(raw_cost)
                elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                    raw_cost = getattr(message, "total_cost_usd", None)
                    if isinstance(raw_cost, (int, float)):
                        qa_cost = float(raw_cost)
                elif AssistantMessage and isinstance(message, AssistantMessage):
                    for block in message.content:
                        if TextBlock and isinstance(block, TextBlock) and block.text:
                            report_lines.append(block.text)
                            if on_progress:
                                for line in block.text.splitlines():
                                    line_s = line.strip()
                                    if not line_s or len(line_s) < 10:
                                        continue
                                    # PASS/FAIL/verdict lines → structured findings
                                    has_verdict = any(m in line_s for m in
                                                      ["PASS", "FAIL", "must", "should",
                                                       "✅", "❌", "✓", "✗"])
                                    if has_verdict:
                                        try:
                                            on_progress("qa_finding", {"text": line_s[:200]})
                                        except Exception:
                                            pass
                                    else:
                                        # Reasoning narration → status line
                                        try:
                                            on_progress("qa_status", {"text": line_s[:120]})
                                        except Exception:
                                            pass
                        elif ToolUseBlock and isinstance(block, ToolUseBlock):
                            if on_progress:
                                try:
                                    event = _build_agent_tool_event(block)
                                    # Also capture browser/MCP tools
                                    if not event and block.name.startswith("mcp__"):
                                        action = block.name.split("__")[-1]
                                        detail = ""
                                        inp = block.input or {}
                                        if "url" in inp:
                                            detail = inp["url"][:60]
                                        elif "selector" in inp:
                                            detail = inp["selector"][:60]
                                        event = {"name": f"Browser:{action}", "detail": detail}
                                    if event:
                                        on_progress("agent_tool", event)
                                except Exception:
                                    pass

        await asyncio.wait_for(_run_qa(), timeout=qa_timeout)
    except asyncio.TimeoutError:
        report_lines.append(f"\n[QA agent timed out after {qa_timeout}s]")
    except Exception as e:
        report_lines.append(f"\n[QA agent error: {e}]")

    raw_report = "\n".join(report_lines)

    # Try to read verdict from file first, then parse from report
    verdict = None
    if verdict_file.exists():
        try:
            verdict_text = verdict_file.read_text().strip()
            if verdict_text:
                verdict = json.loads(verdict_text)
        except (json.JSONDecodeError, OSError):
            pass
        finally:
            verdict_file.unlink(missing_ok=True)
    else:
        verdict_file.unlink(missing_ok=True)

    if not verdict or "must_passed" not in verdict:
        verdict = _parse_qa_verdict_json(raw_report)

    must_passed = verdict.get("must_passed", False)

    # Emit summary for display
    if on_progress:
        try:
            must_items = verdict.get("must_items", [])
            total = len(must_items)
            passed = sum(1 for item in must_items if item.get("status") == "pass")
            on_progress("qa_summary", {
                "total": total,
                "passed": passed,
                "failed": total - passed,
            })
        except Exception:
            pass

    return {
        "must_passed": must_passed,
        "verdict": verdict,
        "raw_report": raw_report,
        "cost_usd": qa_cost,
    }


async def run_task_v45(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    context: Any | None = None,  # PipelineContext
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """v4.5 per-task execution loop — bare CC + parallel spec gen + verify + QA.

    Key differences from run_task_with_qa():
    - Attempt 1 = bare CC (raw prompt, no custom system prompt, no spec)
    - Spec gen runs in parallel with coding, awaited before QA
    - Structured JSON QA verdict with [must]/[should] binding
    - Risk-based QA tiering (Tier 0/1/2)
    - Durable candidate refs (never discard verified code)
    - Session resume on retry

    Returns {success, status, cost_usd, error, diff_summary, qa_report,
             phase_timings, review_ref}.
    """
    from otto.spec import async_generate_spec

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
    last_error_source = None
    prior_attempts = task.get("attempts", 0) or 0
    total_attempts = prior_attempts
    # empty_retries removed — prompt now includes working dir, should not happen
    phase_timings: dict[str, float] = {}
    spec = task.get("spec")
    pre_existing_untracked: set[str] | None = None
    spec_task: asyncio.Task | None = None
    spec_started_at: float | None = None
    spec_generation_error = ""
    _result_error_code_unset = object()

    # Live state for otto status -w
    _live_state_file = project_dir / "otto_logs" / "live-state.json"
    _live_phases: dict[str, dict] = {
        p: {"status": "pending", "time_s": 0.0}
        for p in ["prepare", "spec_gen", "coding", "test", "qa", "merge"]
    }
    _live_tools: list[str] = []

    def emit(event: str, **data: Any) -> None:
        if on_progress:
            try:
                on_progress(event, data)
            except Exception:
                pass
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

    async def _cancel_spec_task() -> None:
        nonlocal spec_task
        if not spec_task:
            return
        if spec_task.done():
            spec_task = None
            return
        spec_task.cancel()
        try:
            await spec_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            spec_task = None

    async def _await_spec_task() -> None:
        nonlocal spec, spec_task, total_cost, spec_generation_error
        if not spec_task or spec:
            return
        try:
            spec_items, spec_cost, spec_error = await spec_task
        finally:
            spec_task = None

        spec_elapsed = round(time.monotonic() - spec_started_at, 1) if spec_started_at else 0.0
        total_cost += spec_cost
        if spec_items:
            spec = spec_items
            if tasks_file:
                try:
                    update_task(tasks_file, key, spec=spec)
                except Exception:
                    pass
            from otto.tasks import spec_binding as _sb_count
            _must = sum(1 for item in spec if _sb_count(item) == "must")
            _should = len(spec) - _must
            _breakdown = f"{len(spec)} items ({_must} must, {_should} should)"
            emit("phase", name="spec_gen", status="done", time_s=spec_elapsed,
                 detail=_breakdown, cost=spec_cost)
            from otto.tasks import spec_binding, spec_text
            for item in spec:
                binding = spec_binding(item)
                text = spec_text(item)
                emit("spec_item", text=f"[{binding}] {text}")
            emit("spec_items_done")
            return

        spec_generation_error = spec_error or "spec generation produced no items"
        emit("phase", name="spec_gen", status="fail", time_s=spec_elapsed,
             error=spec_generation_error[:100])

    def _result(success: bool, status: str, error: str = "",
                diff_summary: str = "", qa_report: str = "",
                review_ref: str | None = None,
                error_code: Any = _result_error_code_unset) -> dict[str, Any]:
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
                if error_code is not _result_error_code_unset:
                    updates["error_code"] = error_code
                if review_ref:
                    updates["review_ref"] = review_ref
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
            "review_ref": review_ref,
        }

    try:
        # Step 1: Prepare — create branch
        emit("phase", name="prepare", status="running")
        prep_start = time.monotonic()

        if tasks_file:
            update_task(tasks_file, key, status="running", attempts=0, review_ref=None)

        base_sha = create_task_branch(project_dir, key, default_branch, task=task)
        pre_existing_untracked = _snapshot_untracked(project_dir)

        log_dir = project_dir / "otto_logs" / key
        log_dir.mkdir(parents=True, exist_ok=True)

        verify_cmd = task.get("verify")
        test_command = config.get("test_command")

        # Auto-detect test_command
        if not test_command:
            detected = detect_test_command(project_dir)
            test_command = detected if detected else None

        # Baseline test check
        baseline_detail = ""
        if test_command:
            from otto.verify import run_tier1
            import re as _re_baseline
            baseline = run_tier1(project_dir, test_command, timeout)
            if not baseline.passed and not baseline.skipped:
                output = baseline.output or ""
                infra_keywords = [
                    "Cannot find module", "ModuleNotFoundError",
                    "command not found", "No module named",
                    "SyntaxError", "error: unrecognized arguments",
                    "errors during collection",
                ]
                if any(kw in output for kw in infra_keywords):
                    prep_elapsed = round(time.monotonic() - prep_start, 1)
                    phase_timings["prepare"] = prep_elapsed
                    emit("phase", name="prepare", status="fail", time_s=prep_elapsed,
                         error="baseline tests fail — infrastructure issue")
                    _cleanup_task_failure(
                        project_dir, key, default_branch, tasks_file,
                        pre_existing_untracked=pre_existing_untracked,
                        error=f"BASELINE_FAIL: {output[-500:]}",
                        error_code="baseline_fail",
                        cost_usd=total_cost,
                        duration_s=time.monotonic() - task_start,
                    )
                    return _result(False, "failed",
                                   error=f"BASELINE_FAIL: test infrastructure broken\n{output[-500:]}")
            # Extract test count for display
            if baseline.output:
                m = _re_baseline.search(r"(\d+) passed", baseline.output)
                if m:
                    baseline_detail = f"baseline: {m.group(1)} tests passing"

        prep_elapsed = round(time.monotonic() - prep_start, 1)
        phase_timings["prepare"] = prep_elapsed
        emit("phase", name="prepare", status="done", time_s=prep_elapsed,
             detail=baseline_detail)

        # Step 2: Coding + Verify + QA loop
        remaining = max(0, max_retries + 1 - prior_attempts)
        if remaining == 0:
            await _cancel_spec_task()
            error = f"max retries already exhausted ({prior_attempts} prior)"
            _cleanup_task_failure(
                project_dir, key, default_branch, tasks_file,
                pre_existing_untracked=pre_existing_untracked,
                error=error,
                error_code="max_retries",
                cost_usd=total_cost,
                duration_s=time.monotonic() - task_start,
            )
            return _result(False, "failed", error=error, error_code="max_retries")

        # Fire spec gen in background if no specs yet
        if not spec:
            async def _spec_with_timeout():
                try:
                    _spec_settings = config.get("spec_agent_settings", "project").split(",")
                    spec_items, spec_cost, spec_error = await asyncio.wait_for(
                        async_generate_spec(prompt, project_dir, setting_sources=_spec_settings),
                        timeout=300,  # 5 min max for spec gen
                    )
                    return spec_items, spec_cost, spec_error
                except asyncio.TimeoutError:
                    return None, 0.0, "spec generation timed out after 300s"
                except Exception as exc:
                    return None, 0.0, f"spec generation failed: {exc}"

            emit("phase", name="spec_gen", status="running")
            spec_started_at = time.monotonic()
            spec_task = asyncio.create_task(_spec_with_timeout())

        for attempt in range(remaining):
            attempt_num = prior_attempts + attempt + 1
            total_attempts += 1
            retry_reason = ""
            if attempt > 0 and last_error:
                source = last_error_source or "unknown"
                # Extract a concise reason from the error
                if source == "verify":
                    # Find "N failed" or first error line
                    import re as _re_retry
                    m = _re_retry.search(r"\d+ (?:failed|error)", last_error)
                    reason_text = m.group(0) if m else last_error.strip().splitlines()[0][:50]
                elif source == "qa":
                    # Find failed criterion or concise summary
                    reason_text = "QA found issues"
                    for eline in last_error.splitlines():
                        es = eline.strip()
                        if "FAIL" in es or "✗" in es or "CRITICAL" in es:
                            reason_text = es[:60]
                            break
                elif source == "coding":
                    reason_text = last_error.strip().splitlines()[0][:60]
                else:
                    reason_text = last_error.strip().splitlines()[0][:60]
                retry_reason = f"{source}: {reason_text}"
            emit("attempt_boundary", attempt=attempt_num, reason=retry_reason)

            # Time budget check
            elapsed = time.monotonic() - task_start
            if elapsed > max_task_time and attempt > 0:
                await _cancel_spec_task()
                error = f"time budget exceeded ({int(elapsed)}s)"
                _cleanup_task_failure(
                    project_dir, key, default_branch, tasks_file,
                    pre_existing_untracked=pre_existing_untracked,
                    error=error,
                    error_code="time_budget_exceeded",
                    cost_usd=total_cost,
                    duration_s=time.monotonic() - task_start,
                )
                return _result(False, "failed", error=error, error_code="time_budget_exceeded")

            if tasks_file:
                update_task(tasks_file, key, attempts=total_attempts)

            # Ensure specs are available before attempt 2+
            if attempt > 0 and spec_task and not spec:
                await _await_spec_task()

            # Build prompt for this attempt
            feedback = task.get("feedback", "")
            if attempt == 0 and not last_error:
                # ROUND 1: Bare CC — raw prompt + cross-task learnings + user feedback
                coding_prompt = prompt
                if feedback:
                    coding_prompt += f"\n\nIMPORTANT feedback from the user:\n{feedback}"
                if context and hasattr(context, 'observed_learnings') and context.observed_learnings:
                    coding_prompt += f"\n\nFactual observations from prior tasks:\n"
                    coding_prompt += "\n".join(
                        f"- [{l.source}] {l.text}" for l in context.observed_learnings
                    )
            else:
                # ROUND 2+: raw prompt + feedback + specs + raw errors + learnings
                coding_prompt = prompt
                if feedback:
                    coding_prompt += f"\n\nIMPORTANT feedback from the user:\n{feedback}"
                if spec:
                    coding_prompt += f"\n\nAcceptance criteria (satisfy [must], exceed where helpful):\n"
                    coding_prompt += format_spec_v45(spec)
                if last_error:
                    coding_prompt += f"\n\nPrevious attempt failed."
                    coding_prompt += f"\n  Source: {last_error_source}"
                    coding_prompt += (
                        f"\n  Raw output:\n{_fence_untrusted_text(last_error)}"
                    )
                if context and hasattr(context, 'observed_learnings') and context.observed_learnings:
                    coding_prompt += f"\n\nFactual observations from prior tasks:\n"
                    coding_prompt += "\n".join(
                        f"- [{l.source}] {l.text}" for l in context.observed_learnings
                    )

            coding_prompt += (
                f"\n\nYou are working in {project_dir}. Do not create git commits."
                f" Do not ask questions — make decisions yourself and implement."
            )

            # Run coding agent — NO custom system prompt (bare CC)
            if attempt == 0 and not last_error:
                coding_detail = "bare CC"
            else:
                reason = last_error_source or "unknown"
                coding_detail = f"attempt {attempt_num} — {reason} failed"
            emit("phase", name="coding", status="running", attempt=attempt_num,
                 detail=coding_detail)
            coding_start = time.monotonic()
            try:
                _coding_settings = config.get("coding_agent_settings", "user,project").split(",")
                agent_opts = ClaudeAgentOptions(
                    permission_mode="bypassPermissions",
                    cwd=str(project_dir),
                    setting_sources=_coding_settings,
                    env=_subprocess_env(),
                    # Use CC's default system prompt (Glob over find, etc.)
                    # None would blank it; preset keeps CC's defaults.
                    system_prompt={"type": "preset", "preset": "claude_code"},
                    # NO max_turns — agent finishes naturally
                )
                if config.get("model"):
                    agent_opts.model = config["model"]
                if session_id:
                    agent_opts.resume = session_id
                # Don't define custom subagents — the built-in Agent tool
                # is available by default. Custom definitions with vague prompts
                # ("Research APIs", "Search codebase") encourage unnecessary
                # dispatches that multiply exploration overhead.

                agent_log_lines: list[str] = []
                result_msg = None
                _last_block_name = ""
                async for message in query(prompt=coding_prompt, options=agent_opts):
                    if isinstance(message, ResultMessage):
                        result_msg = message
                    elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                        result_msg = message
                    elif AssistantMessage and isinstance(message, AssistantMessage):
                        for block in message.content:
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
                                continue
                            if TextBlock and isinstance(block, TextBlock) and block.text:
                                agent_log_lines.append(block.text)
                            elif ToolUseBlock and isinstance(block, ToolUseBlock):
                                _last_block_name = block.name
                                agent_log_lines.append(f"● {block.name}  {_tool_use_summary(block)}")
                                event = _build_agent_tool_event(block)
                                if event:
                                    emit("agent_tool", **event)

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

                if result_msg and result_msg.is_error:
                    raise RuntimeError(f"Agent error: {result_msg.result or 'unknown'}")

            except Exception as e:
                coding_elapsed = round(time.monotonic() - coding_start, 1)
                emit("phase", name="coding", status="fail", time_s=coding_elapsed,
                     error=str(e)[:80], attempt=attempt_num)
                _restore_workspace_state(
                    project_dir,
                    reset_ref=base_sha,
                    pre_existing_untracked=pre_existing_untracked,
                )
                last_error = str(e)
                last_error_source = "coding"
                continue

            # Check if agent made changes BEFORE declaring coding done
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
                # Extract agent's last reasoning for display
                last_text = ""
                for line in reversed(agent_log_lines):
                    if not line.startswith("●") and line.strip():
                        last_text = line.strip()[:120]
                        break
                reason = last_text if last_text else "no code changes"

                emit("phase", name="coding", status="done", time_s=coding_elapsed,
                     cost=attempt_cost, detail=f"no changes — {reason}")

                # Agent made no changes. Could mean "feature already exists".
                # Wait for specs, then run QA to verify properly.
                # If QA passes, the task passes. If QA fails, retry with findings.
                if spec_task and not spec:
                    emit("phase", name="spec_gen", status="running",
                         detail="awaiting specs to verify existing code...")
                    await _await_spec_task()

                qa_spec = spec
                if not qa_spec:
                    # No spec available — can't verify, treat as failure
                    emit("phase", name="coding", status="fail", time_s=0,
                         error="no changes and no spec to verify against")
                    last_error = f"No code changes produced and no spec available.\nAgent: {last_text}"
                    last_error_source = "coding"
                    continue

                # Run QA against existing (unchanged) code
                diff_info_nc = {"files": [], "full_diff": "(no changes)"}
                qa_tier_nc = max(determine_qa_tier(task, qa_spec, attempt, diff_info_nc), 1)
                emit("phase", name="qa", status="running",
                     detail=f"tier {qa_tier_nc} — verifying existing code")
                qa_start_nc = time.monotonic()

                from otto.tasks import spec_binding as _sb_nc
                focus_nc = [item for item in qa_spec if _sb_nc(item) == "must"]

                qa_result_nc = await run_qa_agent_v45(
                    task, qa_spec, config, project_dir,
                    original_prompt=prompt,
                    diff="(no code changes — agent believes feature already exists)",
                    tier=qa_tier_nc,
                    focus_items=focus_nc,
                    on_progress=on_progress,
                )
                qa_elapsed_nc = round(time.monotonic() - qa_start_nc, 1)
                total_cost += qa_result_nc.get("cost_usd", 0.0)
                qa_report_nc = qa_result_nc.get("raw_report", "")

                # Emit per-item QA results
                verdict_nc = qa_result_nc.get("verdict", {})
                for item in verdict_nc.get("must_items", []):
                    status_icon = "✓" if item.get("status") == "pass" else "✗"
                    emit("qa_item_result",
                         text=f"{status_icon} [must] {item.get('criterion', '')[:70]}",
                         passed=item.get("status") == "pass",
                         evidence=item.get("evidence", "")[:80] if item.get("status") != "pass" else "")

                try:
                    (log_dir / "qa-report.md").write_text(qa_report_nc or "No QA output")
                    if verdict_nc:
                        (log_dir / "qa-verdict.json").write_text(json.dumps(verdict_nc, indent=2))
                except OSError:
                    pass

                if qa_result_nc["must_passed"]:
                    # QA confirms feature exists and works — pass
                    emit("phase", name="qa", status="done", time_s=qa_elapsed_nc,
                         cost=qa_result_nc.get("cost_usd", 0.0))
                    emit("phase", name="merge", status="done", time_s=0,
                         detail="no changes needed")
                    subprocess.run(["git", "checkout", default_branch],
                                   cwd=project_dir, capture_output=True)
                    cleanup_branch(project_dir, key, default_branch)
                    return _result(True, "passed", qa_report=qa_report_nc,
                                   diff_summary="No changes needed — QA verified existing code")

                # QA found gaps — retry with QA findings
                failed_musts = [
                    item for item in verdict_nc.get("must_items", [])
                    if item.get("status") == "fail"
                ]
                for item in failed_musts:
                    criterion = item.get("criterion", "")[:80]
                    evidence = item.get("evidence", "")[:80]
                    emit("qa_finding", text=f"[must] ✗ {criterion}")
                    if evidence:
                        emit("qa_finding", text=f"       evidence: {evidence}")

                emit("phase", name="qa", status="fail", time_s=qa_elapsed_nc,
                     error="existing code doesn't satisfy spec")
                last_error = f"No code changes produced. QA found gaps:\n{qa_report_nc}"
                last_error_source = "qa"
                continue

            # Coding succeeded — agent produced changes
            emit("phase", name="coding", status="done", time_s=coding_elapsed,
                 cost=attempt_cost, attempt=attempt_num)

            # Build candidate commit
            candidate_sha = build_candidate_commit(
                project_dir, base_sha, pre_existing_untracked,
            )

            # Re-detect test command
            if not config.get("test_command"):
                detected = detect_test_command(project_dir)
                if detected:
                    test_command = detected

            # PRE-VERIFY: quick sanity check in the working directory.
            # Catches missing files, broken imports, etc. before paying
            # the cost of a full verify worktree cycle.
            pre_check = None
            if test_command:
                from otto.verify import run_tier1
                pre_check = run_tier1(project_dir, test_command, timeout)
                if not pre_check.passed and not pre_check.skipped:
                    emit("phase", name="coding", status="fail", time_s=coding_elapsed,
                         error="tests fail in working dir", cost=attempt_cost)
                    last_error = pre_check.output or "tests failed locally"
                    last_error_source = "verify"
                    # Reset for retry
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=project_dir, capture_output=True,
                    )
                    continue

            # VERIFY — clean worktree, deterministic
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

            verify_detail = ""
            for tier in verify_result.tiers:
                if tier.output:
                    for line in reversed(tier.output.splitlines()):
                        ls = line.strip()
                        if any(kw in ls.lower() for kw in
                               ["passed", "failed", "error", "tests:", "test suites:"]):
                            if any(c.isdigit() for c in ls):
                                verify_detail = ls[:70]
                                break

            if not verify_result.passed:
                # If pre-verify passed locally but worktree failed, this is
                # a worktree-specific issue (module mapping, missing deps).
                # Trust the local test result and proceed to QA.
                if test_command and pre_check and pre_check.passed:
                    _log_warn(
                        f"Verify worktree failed but tests pass locally — "
                        f"worktree issue, proceeding"
                    )
                    emit("phase", name="test", status="done", time_s=verify_elapsed,
                         detail=f"{verify_detail} (worktree issue bypassed)")
                    # Fall through to the verify-passed path below
                else:
                    emit("phase", name="test", status="fail", time_s=verify_elapsed,
                         error=(verify_result.failure_output or "")[:80], detail=verify_detail)
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=project_dir, capture_output=True,
                    )
                    last_error = verify_result.failure_output
                    last_error_source = "verify"
                    continue

            if verify_result.passed:
                emit("phase", name="test", status="done", time_s=verify_elapsed,
                     detail=verify_detail)

            # ANCHOR verified candidate as durable git ref
            ref_name = _anchor_candidate_ref(project_dir, key, attempt_num, candidate_sha)
            emit("phase", name="candidate", status="done", detail=ref_name)

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
                for f in untracked_final.stdout.split("\0"):
                    if f and _should_stage_untracked(f):
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

            # Build diff info for QA
            diff_info = _get_diff_info(project_dir, base_sha)
            diff_summary = subprocess.run(
                ["git", "diff", "--stat", base_sha, "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            ).stdout.strip()

            # Write verify.log
            try:
                (log_dir / "verify.log").write_text("PASSED")
            except OSError:
                pass

            # Await specs before QA (if still generating)
            if spec_task and not spec:
                emit("phase", name="spec_gen", status="running",
                     detail="awaiting specs before QA...")
                await _await_spec_task()

            # QA — risk-based tiering
            qa_report = ""
            qa_spec = spec
            qa_warning = ""
            if not qa_spec:
                fallback_detail = spec_generation_error or "structured spec unavailable"
                qa_warning = (
                    "Structured spec generation failed; running QA against the original prompt only "
                    f"({fallback_detail})."
                )
                qa_spec = [{
                    "text": "Implementation fulfills the original task prompt and avoids regressions.",
                    "binding": "must",
                }]

            qa_tier = determine_qa_tier(task, qa_spec, attempt, diff_info)
            if qa_warning:
                qa_tier = max(qa_tier, 2)

            if qa_tier >= 1:
                qa_detail = f"tier {qa_tier}"
                if qa_warning:
                    qa_detail += " — prompt-only fallback"
                emit("phase", name="qa", status="running", detail=qa_detail)
                qa_start = time.monotonic()

                from otto.tasks import spec_binding as _sb
                focus_items = [item for item in qa_spec
                               if _sb(item) == "must"]

                qa_result = await run_qa_agent_v45(
                    task, qa_spec, config, project_dir,
                    original_prompt=prompt,
                    diff=diff_info["full_diff"],
                    tier=qa_tier,
                    focus_items=focus_items,
                    on_progress=on_progress,
                )
                qa_elapsed = round(time.monotonic() - qa_start, 1)
                phase_timings["qa"] = phase_timings.get("qa", 0) + qa_elapsed
                total_cost += qa_result.get("cost_usd", 0.0)
                qa_report = qa_result.get("raw_report", "")
                if qa_warning:
                    qa_report = f"[warning] {qa_warning}\n\n{qa_report}".strip()
                verdict = qa_result.get("verdict", {})
                for item in verdict.get("must_items", []):
                    passed = item.get("status") == "pass"
                    evidence = item.get("evidence", "")[:80] if not passed else ""
                    emit(
                        "qa_item_result",
                        text=f"{'✓' if passed else '✗'} [must] {item.get('criterion', '')[:70]}",
                        passed=passed,
                        evidence=evidence,
                    )
                for item in verdict.get("should_notes", []):
                    emit(
                        "qa_item_result",
                        text=(
                            f"  [should] {item.get('criterion', '')[:70]} — "
                            f"{item.get('observation', '')[:50]}"
                        ),
                        passed=True,
                    )

                # Persist QA report
                try:
                    (log_dir / "qa-report.md").write_text(qa_report or "No QA output")
                    if verdict:
                        (log_dir / "qa-verdict.json").write_text(
                            json.dumps(verdict, indent=2)
                        )
                except OSError:
                    pass

                if not qa_result["must_passed"]:
                    failed_musts = [
                        item for item in verdict.get("must_items", [])
                        if item.get("status") == "fail"
                    ]
                    if failed_musts:
                        fail_summary = (
                            f"{len(failed_musts)}/{len(verdict.get('must_items', []))} must failed: "
                            + failed_musts[0].get("criterion", "")[:50]
                        )
                    else:
                        # Legacy parse — extract first useful line from QA report
                        first_line = ""
                        for rline in qa_report.splitlines():
                            rs = rline.strip()
                            if rs and len(rs) > 10 and not rs.startswith("["):
                                first_line = rs[:60]
                                break
                        fail_summary = first_line if first_line else "QA did not pass"
                    emit("phase", name="qa", status="fail", time_s=qa_elapsed,
                         error=f"QA: {fail_summary}"[:80])
                    # Reset for retry
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=project_dir, capture_output=True,
                    )
                    last_error = qa_report
                    last_error_source = "qa"
                    continue
                else:
                    emit("phase", name="qa", status="done", time_s=qa_elapsed,
                         cost=qa_result.get("cost_usd", 0.0))
            else:
                # Tier 0 — skip QA
                emit("phase", name="qa", status="done", time_s=0,
                     detail="tier 0 — skipped")

            # SUCCESS — merge to default branch
            emit("phase", name="merge", status="running")
            merge_start = time.monotonic()
            if merge_to_default(project_dir, key, default_branch):
                merge_elapsed = round(time.monotonic() - merge_start, 1)
                phase_timings["merge"] = merge_elapsed
                emit("phase", name="merge", status="done", time_s=merge_elapsed)
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
                    error=f"branch diverged — otto/{key} preserved",
                    diff_summary=diff_summary, qa_report=qa_report,
                    error_code="merge_diverged",
                )

        # All retries exhausted — find best verified candidate
        await _cancel_spec_task()
        best_ref = _find_best_candidate_ref(project_dir, key)
        last_err_detail = f"\nLast error:\n{last_error}" if last_error else ""
        _cleanup_task_failure(
            project_dir, key, default_branch, tasks_file,
            pre_existing_untracked=pre_existing_untracked,
            error=f"max retries exhausted{last_err_detail}", error_code="max_retries",
            cost_usd=total_cost,
            duration_s=time.monotonic() - task_start,
        )
        return _result(
            False, "failed",
            error=f"max retries exhausted ({total_attempts} attempts).{last_err_detail}",
            review_ref=best_ref,
        )

    except Exception as e:
        duration = time.monotonic() - task_start
        await _cancel_spec_task()
        try:
            _cleanup_task_failure(
                project_dir, key, default_branch, tasks_file,
                pre_existing_untracked=pre_existing_untracked,
                error=f"unexpected error: {e}", error_code="internal_error",
                cost_usd=total_cost,
                duration_s=duration,
            )
        except Exception:
            pass
        return _result(False, "failed", error=f"unexpected error: {e}")


async def run_task_with_qa(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    hint: str | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Backward-compatible wrapper for the v3 pilot."""
    if hint:
        task = dict(task)
        task["feedback"] = hint
    return await run_task_v45(
        task, config, project_dir, tasks_file,
        on_progress=on_progress,
    )


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
            # Prefer phase_timings from the final task result event
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
