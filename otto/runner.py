"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from otto.agent import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    _subprocess_env,
    merge_usage,
    normalize_usage,
    query,
    tool_use_summary as _tool_use_summary,
)

from otto.config import agent_provider, git_meta_dir, detect_test_command
from otto.context import QAMode
from otto.display import _truncate_at_word, console, format_cost, format_duration, rich_escape
from otto.git_ops import (
    check_clean_tree,
    build_candidate_commit,
    _should_stage_untracked,
    _anchor_candidate_ref,
    _find_best_candidate_ref,
    _get_diff_info,
    _cleanup_task_failure,
    _restore_workspace_state,
    _snapshot_untracked,
)
from otto.observability import write_json_file
from otto.qa import (
    format_spec_v45,
    determine_qa_tier,
    run_qa,
)
from otto.tasks import load_tasks, update_task
from otto.testing import run_test_suite, _subprocess_env
from otto.claim_verify import verify_claims, format_claim_findings
from otto.retry_excerpt import build_retry_excerpt


def _display_cost_text(cost: float, *, available: bool) -> str:
    """Render cost for terminal output."""
    if not available:
        return "cost unavailable"
    return f"${cost:.2f}"


def _usage_text(token_usage: dict[str, int]) -> str:
    """Render a concise token-usage summary."""
    if not token_usage:
        return ""
    parts = []
    if token_usage.get("input_tokens"):
        parts.append(f"in {token_usage['input_tokens']}")
    if token_usage.get("cached_input_tokens"):
        parts.append(f"cache {token_usage['cached_input_tokens']}")
    if token_usage.get("output_tokens"):
        parts.append(f"out {token_usage['output_tokens']}")
    if token_usage.get("total_tokens"):
        parts.append(f"total {token_usage['total_tokens']}")
    return "tokens " + ", ".join(parts) if parts else ""


def _suggest_claude_md(project_dir: Path) -> None:
    """Suggest CLAUDE.md content based on project structure."""
    hints = []
    test_helper_pattern = re.compile(r"createMock|mock.*factory|fixture", re.IGNORECASE)

    # Detect project type
    has_pkg = (project_dir / "package.json").exists()
    has_py = any((project_dir / f).exists() for f in
                 ("pyproject.toml", "requirements.txt", "setup.py"))
    has_tests = any((project_dir / d).exists() for d in
                    ("__tests__", "tests", "test"))

    if has_pkg:
        # Check for common patterns
        try:
            import json as _json
            pkg = _json.loads((project_dir / "package.json").read_text())
            if "tailwindcss" in str(pkg.get("dependencies", {})) + str(pkg.get("devDependencies", {})):
                hints.append("Uses Tailwind CSS — match existing class patterns")
            if "next" in pkg.get("dependencies", {}):
                hints.append("Next.js project — components in src/components/")
            if "react" in pkg.get("dependencies", {}):
                hints.append("React project — follow existing component patterns")
        except Exception:
            pass

    if has_tests:
        # Check for shared test helpers (lightweight — cap files and read size)
        for test_dir in ("__tests__", "tests", "test"):
            d = project_dir / test_dir
            if d.exists():
                found_test_helper = False
                checked = 0
                for path in d.rglob("*"):
                    if not path.is_file() or path.stat().st_size > 50_000:
                        continue
                    checked += 1
                    if checked > 20:  # only scan first 20 files
                        break
                    try:
                        content = path.read_text()[:5000]  # first 5KB only
                    except (OSError, UnicodeDecodeError):
                        continue
                    if test_helper_pattern.search(content):
                        found_test_helper = True
                        break
                if found_test_helper:
                    hints.append("Has test helpers — reuse existing mock/fixture factories")
                break

    # Print suggestion
    if hints:
        console.print(
            f"  [yellow]Tip:[/yellow] No CLAUDE.md found. Detected patterns:"
        )
        for h in hints:
            console.print(f"    [dim]- {h}[/dim]")
        console.print(
            f"  [dim]Create CLAUDE.md with project conventions — the coding agent will follow them.[/dim]"
        )
    else:
        console.print(
            "  [yellow]Tip:[/yellow] No CLAUDE.md found. Create one with project conventions"
            " — the coding agent will follow them."
        )


def _fence_untrusted_text(text: str) -> str:
    """Wrap untrusted model/input text in a code fence."""
    text = text or ""
    max_ticks = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, max_ticks + 1)
    return f"{fence}\n{text}\n{fence}"


def preflight_checks(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> tuple[int | None, list[dict]]:
    """Run pre-flight checks before launching the orchestrator.

    Returns (error_code, pending_tasks). If error_code is not None, the caller
    should abort with that exit code. Otherwise pending_tasks is the list of
    tasks to run.

    Preflight is read-only until validation passes: it never stashes user work,
    switches branches, or creates commits. If the repo is not ready, it fails
    with a clear message.
    """
    default_branch = config["default_branch"]

    # ── Step 1: Validate repo state (read-only — no mutations) ────────
    actual = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if actual != default_branch:
        console.print(
            f"[red]Otto requires branch '{default_branch}' but repo is on '{actual}'.\n"
            f"  Run: git checkout {default_branch}[/red]"
        )
        return (2, [])

    if not check_clean_tree(project_dir):
        console.print("[red]Working tree has uncommitted changes — commit or stash before running otto[/red]")
        return (2, [])

    # ── Step 2: Non-destructive setup (no commits, no branch changes) ─
    # Suggest CLAUDE.md if missing — coding agents read it for project conventions
    if not (project_dir / "CLAUDE.md").exists():
        _suggest_claude_md(project_dir)

    # Ensure .git/info/exclude covers framework build dirs + otto runtime files.
    # Uses .git/info/exclude (local-only, never committed) instead of .gitignore
    # to avoid creating unrelated commits on the default branch.
    _FRAMEWORK_IGNORES: dict[str, list[str]] = {
        "package.json": ["node_modules/", ".next/", "dist/", "build/", "coverage/", ".turbo/"],
        "pyproject.toml": ["__pycache__/", ".venv/", "dist/", ".pytest_cache/", "*.egg-info"],
        "requirements.txt": ["__pycache__/", ".venv/", ".pytest_cache/"],
        "setup.py": ["__pycache__/", ".venv/", "dist/", "*.egg-info", "build/"],
        "Cargo.toml": ["target/"],
        "go.mod": ["vendor/"],
        "Gemfile": ["vendor/bundle/"],
    }
    from otto.config import git_meta_dir
    exclude_path = git_meta_dir(project_dir) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_exclude = exclude_path.read_text() if exclude_path.exists() else ""

    # Collect framework-specific ignores
    framework_excludes: list[str] = []
    for manifest, dirs in _FRAMEWORK_IGNORES.items():
        if (project_dir / manifest).exists():
            for d in dirs:
                if d not in existing_exclude and d.rstrip("/") not in existing_exclude:
                    framework_excludes.append(d)

    # Greenfield heuristics before manifests exist.
    if any(project_dir.rglob("*.py")):
        for entry in ("__pycache__/", ".pytest_cache/", ".venv/"):
            if entry not in existing_exclude and entry.rstrip("/") not in existing_exclude:
                framework_excludes.append(entry)
    if any(project_dir.rglob("*.js")) or any(project_dir.rglob("*.ts")) or any(project_dir.rglob("*.tsx")):
        for entry in ("node_modules/", "dist/", "coverage/"):
            if entry not in existing_exclude and entry.rstrip("/") not in existing_exclude:
                framework_excludes.append(entry)

    # Otto runtime entries
    otto_excludes = ["otto_logs/", "otto_arch/", ".otto-worktrees/", "tasks.yaml", ".tasks.lock", "otto.yaml"]
    missing_excludes = [e for e in otto_excludes if e not in existing_exclude]

    all_new_excludes = sorted(set(framework_excludes + missing_excludes))
    if all_new_excludes:
        with open(exclude_path, "a") as f:
            f.write("\n# otto auto-excludes\n")
            for entry in all_new_excludes:
                f.write(entry + "\n")
        if framework_excludes:
            label = ", ".join(d.rstrip("/") for d in framework_excludes[:3])
            console.print(f"  [dim]Updated .git/info/exclude (+{label})[/dim]")


    from otto.tasks import load_tasks, mutate_and_recompute

    # Recover stale transient tasks — if we're starting fresh, something crashed.
    # "merged" is NOT stale: it means code already landed on main.
    _stale_states = {"running", "verified", "merge_pending"}
    stale_tasks = [
        task for task in load_tasks(tasks_file)
        if task.get("status") in _stale_states
    ]
    if stale_tasks:
        stale_keys = {task["key"] for task in stale_tasks if task.get("key")}

        def _recover(tasks):
            for task in tasks:
                if task.get("key") in stale_keys:
                    task["status"] = "pending"
                    task.pop("error", None)
                    task.pop("session_id", None)

        mutate_and_recompute(tasks_file, _recover)
        for task in stale_tasks:
            console.print(f"  [yellow]Warning: Task #{task['id']} was stuck in '{task['status']}' -- reset to pending[/yellow]")

    tasks = load_tasks(tasks_file)
    pending = [t for t in tasks if t.get("status") == "pending"]
    if not pending:
        console.print("No pending tasks", style="dim")
        return (0, [])


    return (None, pending)


from otto.display import build_agent_tool_event as _build_agent_tool_event  # noqa: E402


async def coding_loop(
    task_plan: Any,  # otto.planner.TaskPlan
    context: Any,    # otto.context.PipelineContext
    config: dict[str, Any],
    project_dir: Path,
    telemetry: Any,  # otto.telemetry.Telemetry
    tasks_file: Path | None = None,
    task_work_dir: Path | None = None,
    qa_mode: str = QAMode.PER_TASK,
    sibling_context: str | None = None,
) -> Any:  # otto.context.TaskResult
    """v4 coding loop — run a single task through the v4.5 execution path.

    Passes an on_progress callback to run_task_v45() so phase events flow
    through the telemetry dual-write (legacy pilot_results.jsonl).
    Emits TaskMerged/TaskFailed at the actual completion time (not deferred).

    Args:
        project_dir: repo root — for git ops, config, logs.
        task_work_dir: where the coding agent works. Defaults to project_dir
            (serial mode). In parallel mode, this is a git worktree.
    """
    if task_work_dir is None:
        task_work_dir = project_dir
    from otto.context import TaskResult
    from otto.telemetry import AgentToolCall, TaskFailed, TaskMerged, TaskStarted

    task_key = task_plan.task_key
    cost_available = agent_provider(config) != "codex"

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
                # QA reasoning narration — default text (not dim)
                # Key findings and headers need to be readable
                text = data.get("text", "")
                if text:
                    console.print(f"      {rich_escape(text[:80])}")
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
                    proof_count=data.get("proof_count", 0),
                    proof_coverage=data.get("proof_coverage", ""),
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
                if event_type == "agent_tool":
                    return
                telemetry._emit_legacy_progress({
                    "tool": "progress",
                    "event": event_type,
                    "task_key": task_key,
                    **data,
                })
        except Exception:
            pass

    setattr(_on_progress, "_telemetry", telemetry)
    setattr(_on_progress, "_task_key", task_key)

    try:
        # v4.5: use run_task_v45 which passes context directly
        result = await run_task_v45(
            task, config, project_dir, tasks_file,
            context=context, on_progress=_on_progress,
            task_work_dir=task_work_dir,
            qa_mode=qa_mode,
            sibling_context=sibling_context,
        )

        duration = time.monotonic() - task_start
        cost = float(result.get("cost_usd", 0.0) or 0.0)
        cost_available = bool(result.get("cost_available", True))
        elapsed_str = display.stop()

        if result.get("success"):
            is_verified_only = result.get("status") == "verified"

            commit_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=task_work_dir, capture_output=True, text=True,
            ).stdout.strip()

            # Print task result — same indentation level as phase completions
            if is_verified_only:
                console.print(
                    f"  [blue]{time.strftime('%H:%M:%S')}  \u25c9 verified[/blue]  "
                    f"[dim]{elapsed_str}  {_display_cost_text(cost, available=cost_available)}  (merge pending)[/dim]"
                )
            else:
                console.print(
                    f"  [green]{time.strftime('%H:%M:%S')}  \u2713 passed[/green]  "
                    f"[dim]{elapsed_str}  {_display_cost_text(cost, available=cost_available)}[/dim]"
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
                parts.append(f"[green]{spec_count} specs[/green] verified")
            if attempts > 1:
                parts.append(f"{attempts} attempts")
            if parts:
                console.print(f"    {' · '.join(parts)}")
            proof_report = project_dir / "otto_logs" / task_key / "qa-proofs" / "proof-report.md"
            if proof_report.exists():
                console.print(f"    [dim]proofs: {proof_report}[/dim]")

            if not is_verified_only:
                # Emit TaskMerged NOW (not deferred to batch loop)
                telemetry.log(TaskMerged(
                    task_key=task_key, task_id=task_id,
                    cost_usd=cost, duration_s=duration,
                    cost_available=agent_provider(config) != "codex",
                    diff_summary=result.get("diff_summary", ""),
                ))

            return TaskResult(
                task_key=task_key, success=True,
                commit_sha=commit_sha,
                cost_usd=cost, duration_s=duration,
                qa_report=result.get("qa_report", ""),
                diff_summary=result.get("diff_summary", ""),
                token_usage=result.get("token_usage", {}) or {},
            )

        error = str(result.get("error", "") or "")
        console.print(
            f"    {time.strftime('%H:%M:%S')}  [red]\u2717[/red] failed  "
            f"[dim]{elapsed_str}  {_display_cost_text(cost, available=cost_available)}[/dim]"
        )
        telemetry.log(TaskFailed(
            task_key=task_key, task_id=task_id,
            error=error,
            cost_usd=cost, duration_s=duration,
            cost_available=agent_provider(config) != "codex",
        ))
        return TaskResult(
            task_key=task_key, success=False,
            cost_usd=cost, error=error,
            error_code=result.get("error_code"),
            duration_s=duration,
            qa_report=result.get("qa_report", ""),
            diff_summary=result.get("diff_summary", ""),
            review_ref=result.get("review_ref"),
            token_usage=result.get("token_usage", {}) or {},
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


# Aliases from display module
_format_duration = format_duration
_format_cost = format_cost


def _log_info(msg: str) -> None:
    console.print(f"  {msg}")


def _log_warn(msg: str) -> None:
    console.print(f"  [yellow]Warning: {rich_escape(msg)}[/yellow]")




# ---------------------------------------------------------------------------
# v4.5 — Bare CC coding, structured QA, risk-based tiering, candidate refs
# ---------------------------------------------------------------------------


def _write_log_safe(log_dir: Path, filename: str, content: str) -> None:
    """Write a log file with timestamp header, silently ignoring OS errors."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if filename.endswith(".json"):
            # For JSON: inject timestamp into the object
            try:
                import json as _json
                data = _json.loads(content)
                if isinstance(data, dict) and "timestamp" not in data:
                    data["_written_at"] = ts
                    content = _json.dumps(data, indent=2)
            except (ValueError, TypeError):
                pass
        elif filename.endswith((".md", ".log", ".sh", ".txt")):
            content = f"<!-- generated: {ts} -->\n{content}"
        (log_dir / filename).write_text(content)
    except OSError:
        pass


def _write_task_summary_safe(
    log_dir: Path,
    *,
    task_key: str,
    status: str,
    total_cost_usd: float,
    cost_available: bool = True,
    token_usage: dict[str, int] | None = None,
    phase_token_usage: dict[str, dict[str, int]] | None = None,
    total_duration_s: float,
    attempts: int,
    phase_timings: dict[str, float],
    phase_costs: dict[str, float],
    retry_reasons: list[str],
) -> None:
    write_json_file(
        log_dir / "task-summary.json",
        {
            "task_key": task_key,
            "status": status,
            "total_cost_usd": round(total_cost_usd, 4),
            "cost_available": cost_available,
            "token_usage": token_usage or {},
            "total_duration_s": round(total_duration_s, 1),
            "attempts": attempts,
            "phase_timings": {name: round(value, 1) for name, value in phase_timings.items() if value or name in ("prepare", "coding", "test", "qa", "merge")},
            "phase_costs": {name: round(value, 4) for name, value in phase_costs.items() if value},
            "phase_token_usage": phase_token_usage or {},
            "retry_reasons": retry_reasons,
            "_written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def _persist_qa_results(
    log_dir: Path,
    qa_report: str,
    verdict: dict,
    attempt_num: int = 0,
) -> None:
    """Persist QA report and verdict JSON to the task log directory.

    When attempt_num > 0, writes per-attempt files AND overwrites the latest
    versions so both historical and current views are available.
    """
    # Always write the "latest" file (for tools that read qa-report.md)
    _write_log_safe(log_dir, "qa-report.md", qa_report or "No QA output")
    if verdict:
        _write_log_safe(log_dir, "qa-verdict.json", json.dumps(verdict, indent=2))
    # Also write per-attempt copies (preserve retry history)
    if attempt_num >= 0:
        _write_log_safe(log_dir, f"attempt-{attempt_num + 1}-qa-report.md", qa_report or "No QA output")
        if verdict:
            _write_log_safe(log_dir, f"attempt-{attempt_num + 1}-qa-verdict.json", json.dumps(verdict, indent=2))


def _normalize_criterion_text(text: str) -> str:
    """Normalize criterion text for fuzzy matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def _is_visual_must_criterion(criterion: str, qa_spec: list | None) -> bool:
    """Return True when a QA criterion maps to a non-verifiable [must] spec."""
    if not qa_spec or not criterion:
        return False
    from otto.tasks import spec_binding, spec_is_verifiable, spec_text

    norm_criterion = _normalize_criterion_text(criterion)
    for item in qa_spec:
        if spec_binding(item) != "must" or spec_is_verifiable(item):
            continue
        norm_spec = _normalize_criterion_text(spec_text(item))
        if not norm_spec:
            continue
        if norm_spec[:40] in norm_criterion or norm_criterion[:40] in norm_spec:
            return True
    return False


def _build_qa_retry_error(failed_musts: list[dict], qa_report: str) -> str:
    """Build retry guidance that distinguishes tested failures from proof gaps."""
    if not failed_musts:
        return qa_report

    lines = ["QA found these issues:"]
    for item in failed_musts:
        criterion = item.get("criterion", "")
        evidence = item.get("evidence", "")
        has_proof = bool([p for p in (item.get("proof") or []) if str(p).strip()])
        lines.append(f"- [must] {criterion}")
        if evidence:
            lines.append(f"  why it failed: {evidence}")
        if not has_proof:
            lines.append("  note: QA did not record proof for this criterion")
    return "\n".join(lines)


def _audit_proof_sufficiency(
    verdict: dict,
    qa_spec: list | None,
    proofs_dir: Path,
    emit: Any,
) -> list[str]:
    """Warn when passed must items lack proof or required screenshots."""
    warnings: list[str] = []
    screenshot_files = [
        p for pattern in ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp")
        for p in proofs_dir.glob(pattern)
    ]

    for item in verdict.get("must_items", []):
        if item.get("status") != "pass":
            continue
        criterion = item.get("criterion", "").strip() or "Unnamed criterion"
        proof = [str(p).strip() for p in (item.get("proof") or []) if str(p).strip()]
        if not proof:
            warnings.append(f"Passed [must] missing proof: {criterion}")
        if _is_visual_must_criterion(criterion, qa_spec) and not screenshot_files:
            warnings.append(f"Passed [must ◈] missing screenshot in qa-proofs/: {criterion}")

    if warnings:
        for warning in warnings:
            _log_warn(warning)
            emit("qa_finding", text=f"[warning] {warning}")
        proof_report = proofs_dir / "proof-report.md"
        prefix = "" if proof_report.exists() else "# Proof Report\n\n"
        proof_lines = ["## Proof Sufficiency Warnings", *[f"- {warning}" for warning in warnings], ""]
        try:
            with open(proof_report, "a") as f:
                if prefix:
                    f.write(prefix)
                f.write("\n".join(proof_lines))
        except OSError:
            pass

    return warnings


def _build_coding_prompt(
    prompt: str,
    project_dir: Path,
    *,
    attempt: int,
    last_error: str | None,
    last_error_source: str | None,
    feedback: str,
    spec: list | None,
    context: Any | None,
    sibling_context: str | None = None,
) -> str:
    """Build the coding agent prompt for a given attempt.

    Round 1 (attempt=0, no last_error): bare prompt + learnings + feedback.
    Round 2+: adds spec, raw error output, and fix instructions.
    """
    coding_prompt = prompt

    if attempt == 0 and not last_error:
        # ROUND 1: bare coding agent — raw prompt + cross-task learnings + user feedback
        if sibling_context:
            coding_prompt += f"\n\n{sibling_context}"
        if feedback:
            coding_prompt += f"\n\nIMPORTANT feedback from the user:\n{feedback}"
        if context and hasattr(context, 'observed_learnings') and context.observed_learnings:
            coding_prompt += f"\n\nFactual observations from prior tasks:\n"
            coding_prompt += "\n".join(
                f"- [{l.source}] {l.text}" for l in context.observed_learnings
            )
    else:
        # ROUND 2+: raw prompt + feedback + specs + raw errors + learnings
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
            coding_prompt += (
                f"\n\nFix the specific failures above. Do not regress items that were passing."
            )
        if context and hasattr(context, 'observed_learnings') and context.observed_learnings:
            coding_prompt += f"\n\nFactual observations from prior tasks:\n"
            coding_prompt += "\n".join(
                f"- [{l.source}] {l.text}" for l in context.observed_learnings
            )

    coding_prompt += (
        f"\n\nYou are working in {project_dir}. Do not create git commits."
        f" Do not ask questions — make decisions yourself and implement."
        f"\n\nTest hygiene:"
        f"\n- Prefer extending existing test files over creating new ones."
        f"\n- Follow the repo's existing test conventions for permanent tests."
        f"\n- Reuse existing test helpers and mock data factories — do not duplicate."
    )
    return coding_prompt


async def _run_coding_agent(
    coding_prompt: str,
    config: dict[str, Any],
    work_dir: Path,
    *,
    session_id: str | None,
    emit: Any,
    log_dir: Path,
    attempt_num: int,
) -> tuple[str | None, float, list[str], Any]:
    """Run the coding agent and return (session_id, attempt_cost, log_lines, result_msg).

    work_dir is the task's working directory (worktree path).
    Raises on agent error (result_msg.is_error).
    """
    _coding_settings = config.get("coding_agent_settings", "project").split(",")
    agent_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(work_dir),
        setting_sources=_coding_settings,
        env=_subprocess_env(work_dir),
        # Use the Claude Code preset when the Claude provider is active.
        # Other providers may ignore it.
        system_prompt={"type": "preset", "preset": "claude_code"},
        # NO max_turns — agent finishes naturally
        provider=agent_provider(config),
    )
    if config.get("model"):
        agent_opts.model = config["model"]
    if session_id and agent_provider(config) != "codex":
        agent_opts.resume = session_id
    # Don't define custom subagents — the built-in Agent tool
    # is available by default. Custom definitions with vague prompts
    # ("Research APIs", "Search codebase") encourage unnecessary
    # dispatches that multiply exploration overhead.

    agent_log_lines: list[str] = []
    result_msg = None
    _last_block_name = ""
    _agent_start = time.monotonic()
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
                _elapsed = round(time.monotonic() - _agent_start, 1)
                _ts_prefix = f"[{_elapsed:6.1f}s] "
                if TextBlock and isinstance(block, TextBlock) and block.text:
                    agent_log_lines.append(f"{_ts_prefix}{block.text}")
                elif ToolUseBlock and isinstance(block, ToolUseBlock):
                    _last_block_name = block.name
                    agent_log_lines.append(f"{_ts_prefix}● {block.name}  {_tool_use_summary(block)}")
                    event = _build_agent_tool_event(block)
                    if event:
                        emit("agent_tool", **event)

    # Persist agent log
    _write_log_safe(log_dir, f"attempt-{attempt_num}-agent.log",
                    "\n".join(agent_log_lines))

    if result_msg and result_msg.is_error:
        raise RuntimeError(f"Agent error: {result_msg.result or 'unknown'}")

    return (
        getattr(result_msg, "session_id", None),
        result_msg,
        agent_log_lines,
    )


def _extract_attempt_cost(
    result_msg: Any,
    prev_session_id: str | None,
    prev_session_cost: float,
) -> tuple[float, str | None, float]:
    """Extract this attempt's cost from SDK result.

    SDK's total_cost_usd is cumulative within a session.
    If session_id changed, it's a new session — use raw cost.
    If same session (resumed), compute delta from previous.

    Returns (attempt_cost, current_session_id, updated_session_cost).
    """
    raw_cost = getattr(result_msg, "total_cost_usd", None)
    session_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
    current_session = getattr(result_msg, "session_id", None)
    if current_session and current_session == prev_session_id:
        # Same session (resumed) — cost is cumulative, use delta
        attempt_cost = max(0, session_cost - prev_session_cost)
    else:
        # New session — raw cost is this attempt's cost
        attempt_cost = session_cost
    return attempt_cost, current_session, session_cost


def _check_agent_changes(
    project_dir: Path,
    base_sha: str,
    pre_existing_untracked: set[str],
) -> tuple[bool, set[str]]:
    """Check if the coding agent made any changes.

    Returns (no_changes, new_untracked_files).
    """
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
    return no_changes, new_untracked



def _run_full_test_suite(
    project_dir: Path,
    candidate_sha: str,
    test_command: str | None,
    custom_test_cmd: str | None,
    timeout: int,
    log_dir: Path,
    attempt_num: int,
) -> tuple[Any, float, str]:
    """Run clean-worktree test suite and write logs.

    Returns (test_result, elapsed, detail_string).
    """
    test_start = time.monotonic()
    test_result = run_test_suite(
        project_dir=project_dir,
        candidate_sha=candidate_sha,
        test_command=test_command,
        custom_test_cmd=custom_test_cmd,
        timeout=timeout,
    )
    test_elapsed = round(time.monotonic() - test_start, 1)

    # Write test log
    _write_log_safe(
        log_dir, f"attempt-{attempt_num}-verify.log",
        "\n".join(f"{t.tier}: {'PASS' if t.passed else 'FAIL'}\n{t.output}"
                  for t in test_result.tiers),
    )

    test_detail = ""
    for tier in test_result.tiers:
        if tier.output:
            for line in reversed(tier.output.splitlines()):
                ls = line.strip()
                if any(kw in ls.lower() for kw in
                       ["passed", "failed", "error", "tests:", "test suites:"]):
                    if any(c.isdigit() for c in ls):
                        test_detail = ls[:70]
                        break

    return test_result, test_elapsed, test_detail


def _squash_and_commit(
    project_dir: Path,
    base_sha: str,
    prompt: str,
    task_id: int,
) -> str | None:
    """Squash working changes into a single commit on the task branch.

    Returns diff_summary string on success, or raises on failure.
    """
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

    diff_summary = subprocess.run(
        ["git", "diff", "--stat", base_sha, "HEAD"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    return diff_summary


def _build_retry_reason(last_error: str | None, last_error_source: str | None) -> str:
    """Extract a concise retry reason from the previous error."""
    if not last_error:
        return ""
    source = last_error_source or "unknown"
    if source == "test":
        # Find "N failed" or first error line
        m = re.search(r"\d+ (?:failed|error)", last_error)
        reason_text = m.group(0) if m else last_error.strip().splitlines()[0][:50]
    elif source == "qa":
        # Find failed criterion or concise summary
        reason_text = "QA found issues"
        for eline in last_error.splitlines():
            es = eline.strip()
            if "FAIL" in es or "\u2717" in es or "CRITICAL" in es:
                reason_text = es[:60]
                break
    elif source == "coding":
        reason_text = last_error.strip().splitlines()[0][:60]
    else:
        reason_text = last_error.strip().splitlines()[0][:60]
    return f"{source}: {reason_text}"


def _emit_qa_item_results(
    emit: Any,
    verdict: dict,
    qa_spec: list | None,
) -> None:
    """Emit per-item QA results with visual-only markers."""
    # Build spec lookup for ◈ marker — fuzzy match since QA paraphrases
    _visual_specs: list[str] = []  # normalized non-verifiable spec texts
    if qa_spec:
        from otto.tasks import spec_text, spec_is_verifiable
        for si in qa_spec:
            if not spec_is_verifiable(si):
                # Store first 40 chars normalized for fuzzy matching
                _visual_specs.append(spec_text(si).lower().strip()[:40])

    def _is_visual_criterion(criterion: str) -> bool:
        """Check if criterion matches any non-verifiable spec (fuzzy)."""
        if not _visual_specs:
            return False
        c = criterion.lower().strip()
        for vs in _visual_specs:
            # Match if either contains the other's first 30 chars
            if vs[:30] in c or c[:30] in vs:
                return True
        return False

    for item in verdict.get("must_items", []):
        passed = item.get("status") == "pass"
        evidence = item.get("evidence", "")[:80]
        criterion = item.get("criterion", "")[:70]
        marker = " \u25c8" if _is_visual_criterion(criterion) else ""
        icon = "\u2713" if passed else "\u2717"
        emit(
            "qa_item_result",
            text=f"{icon} [must{marker}] {criterion}",
            passed=passed,
            evidence=evidence,
        )
    should_notes = verdict.get("should_notes", [])
    for note in should_notes:
        obs = note.get("observation", "")[:60]
        criterion = note.get("criterion", "")[:70]
        marker = " \u25c8" if _is_visual_criterion(criterion) else ""
        emit("qa_item_result",
             text=f"[should{marker}] {criterion}",
             passed=None,
             evidence=obs)


async def _run_qa(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    *,
    prompt: str,
    spec: list | None,
    spec_generation_error: str,
    diff_info: dict,
    attempt: int,
    prev_failed_criteria: list[str],
    emit: Any,
    on_progress: Any | None,
    log_dir: Path,
    add_cost: Any,
    qa_spec_override: list | None = None,
) -> dict[str, Any]:
    """Run QA with risk-based tiering, verdict processing, and infra-error retry.

    Returns dict with keys:
        qa_report, qa_tier, verdict, must_passed, cost_usd, qa_elapsed,
        failed_musts, prev_failed_criteria.
    """
    qa_spec = qa_spec_override or spec
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

    qa_tier = determine_qa_tier(task, qa_spec, attempt, diff_info, log_dir=log_dir)

    qa_detail = f"tier {qa_tier}"
    if qa_warning:
        qa_detail += " — prompt-only fallback"
    emit("phase", name="qa", status="running", detail=qa_detail)
    qa_start = time.monotonic()

    from otto.tasks import spec_binding as _sb
    focus_items = [item for item in qa_spec if _sb(item) == "must"]

    qa_task = {"key": task.get("key", "unknown"), "prompt": prompt, "spec": qa_spec}
    qa_result = await run_qa(
        [qa_task], config, project_dir,
        diff=diff_info["full_diff"],
        focus_items=focus_items,
        prev_failed=prev_failed_criteria if prev_failed_criteria else None,
        on_progress=on_progress,
        log_dir=log_dir,
    )
    qa_elapsed = round(time.monotonic() - qa_start, 1)
    total_qa_cost = qa_result.get("cost_usd", 0.0)
    qa_usage = qa_result.get("usage", {}) or {}
    add_cost(total_qa_cost)
    qa_report = qa_result.get("raw_report", "")
    if qa_warning:
        qa_report = f"[warning] {qa_warning}\n\n{qa_report}".strip()
    verdict = qa_result.get("verdict", {})

    # Infrastructure error (API 500, connection drop) — retry QA once
    if qa_result.get("infrastructure_error") and not qa_result.get("_retried"):
        emit("phase", name="qa", status="fail", time_s=qa_elapsed,
             error="QA infrastructure error — retrying")
        await asyncio.sleep(5)  # brief backoff
        qa_result = await run_qa(
            [qa_task], config, project_dir,
            diff=diff_info["full_diff"],
            focus_items=focus_items,
            prev_failed=prev_failed_criteria,
            on_progress=on_progress,
            log_dir=log_dir,
        )
        qa_result["_retried"] = True
        retry_qa_cost = qa_result.get("cost_usd", 0.0)
        qa_usage = merge_usage(qa_usage, qa_result.get("usage", {}) or {})
        add_cost(retry_qa_cost)
        total_qa_cost += retry_qa_cost
        qa_elapsed = round(time.monotonic() - qa_start, 1)
        qa_report = qa_result.get("raw_report", "")
        if qa_warning:
            qa_report = f"[warning] {qa_warning}\n\n{qa_report}".strip()
        verdict = qa_result.get("verdict", {})

    _emit_qa_item_results(emit, verdict, qa_spec)

    # Persist the final QA report, including retried runs.
    _persist_qa_results(log_dir, qa_report, verdict, attempt_num=attempt)

    # Per-attempt copy of proof-report.md (mirrors qa-report.md pattern above)
    if attempt >= 0:
        proof_src = log_dir / "qa-proofs" / "proof-report.md"
        if proof_src.exists():
            try:
                shutil.copy2(
                    proof_src,
                    log_dir / "qa-proofs" / f"attempt-{attempt + 1}-proof-report.md",
                )
            except OSError:
                pass

    failed_musts = [
        item for item in verdict.get("must_items", [])
        if item.get("status") == "fail"
    ]

    if not qa_result["must_passed"]:
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
    else:
        emit("phase", name="qa", status="done", time_s=qa_elapsed,
             cost=total_qa_cost)

    return {
        "qa_report": qa_report,
        "qa_tier": qa_tier,
        "verdict": verdict,
        "must_passed": qa_result["must_passed"],
        "cost_usd": total_qa_cost,
        "usage": qa_usage,
        "qa_elapsed": qa_elapsed,
        "failed_musts": failed_musts,
        "prev_failed_criteria": [
            item.get("criterion", "") for item in failed_musts
        ],
        "proof_count": qa_result.get("proof_count", 0),
        "proof_coverage": qa_result.get("proof_coverage", ""),
    }


async def _handle_no_changes(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    *,
    prompt: str,
    spec: list | None,
    spec_task: asyncio.Task | None,
    agent_log_lines: list[str],
    coding_elapsed: float,
    attempt_cost: float,
    emit: Any,
    on_progress: Any | None,
    log_dir: Path,
    attempt: int,
    add_cost: Any,
    await_spec: Any,  # async callable
) -> dict[str, Any] | None:
    """Handle the case where coding agent made no changes.

    Returns a final result dict if the task should exit (pass or need-retry),
    or None if processing should continue (should not happen in practice — callers
    use the returned dict to decide pass/retry).
    """
    # Extract agent's last reasoning for display
    last_text = ""
    for line in reversed(agent_log_lines):
        if not line.startswith("\u25cf") and line.strip():
            last_text = line.strip()[:120]
            break
    reason = last_text if last_text else "no code changes"

    emit("phase", name="coding", status="done", time_s=coding_elapsed,
         cost=attempt_cost, detail=f"no changes — {reason}")

    # Agent made no changes. Could mean "feature already exists".
    # Wait for specs, then run QA to verify properly.
    # If QA passes, the task passes. If QA fails, retry with findings.
    if spec_task and not spec:
        if not spec_task.done():
            emit("phase", name="spec_gen", status="running",
                 detail="awaiting specs to verify existing code...")
        spec = await await_spec()

    qa_spec = spec
    if not qa_spec:
        # No spec available — can't verify, treat as failure
        emit("phase", name="coding", status="fail", time_s=0,
             error="no changes and no spec to verify against")
        return {
            "action": "retry",
            "last_error": f"No code changes produced and no spec available.\nAgent: {last_text}",
            "last_error_source": "coding",
            "cost_usd": 0.0,
            "token_usage": {},
        }

    # Run QA against existing (unchanged) code
    diff_info_nc = {"files": [], "full_diff": "(no changes)"}
    qa_tier_nc = max(determine_qa_tier(task, qa_spec, attempt, diff_info_nc, log_dir=log_dir), 1)
    emit("phase", name="qa", status="running",
         detail=f"tier {qa_tier_nc} — verifying existing code")
    qa_start_nc = time.monotonic()

    from otto.tasks import spec_binding as _sb_nc
    focus_nc = [item for item in qa_spec if _sb_nc(item) == "must"]

    qa_task_nc = {"key": task.get("key", "unknown"), "prompt": prompt, "spec": qa_spec}
    qa_result_nc = await run_qa(
        [qa_task_nc], config, project_dir,
        diff="(no code changes — agent believes feature already exists)",
        focus_items=focus_nc,
        on_progress=on_progress,
        log_dir=log_dir,
    )
    qa_elapsed_nc = round(time.monotonic() - qa_start_nc, 1)
    qa_cost_nc = qa_result_nc.get("cost_usd", 0.0)
    qa_usage_nc = qa_result_nc.get("usage", {}) or {}
    add_cost(qa_cost_nc)
    qa_report_nc = qa_result_nc.get("raw_report", "")

    # Emit per-item QA results
    verdict_nc = qa_result_nc.get("verdict", {})
    for item in verdict_nc.get("must_items", []):
        status_icon = "\u2713" if item.get("status") == "pass" else "\u2717"
        emit("qa_item_result",
             text=f"{status_icon} [must] {item.get('criterion', '')[:70]}",
             passed=item.get("status") == "pass",
             evidence=item.get("evidence", "")[:80] if item.get("status") != "pass" else "")

    _persist_qa_results(log_dir, qa_report_nc, verdict_nc)

    if qa_result_nc["must_passed"]:
        # QA confirms feature exists and works — pass
        _audit_proof_sufficiency(verdict_nc, qa_spec, log_dir / "qa-proofs", emit)
        emit("phase", name="qa", status="done", time_s=qa_elapsed_nc,
             cost=qa_cost_nc)
        emit("phase", name="merge", status="done", time_s=0,
             detail="no changes to merge")
        return {
            "action": "pass",
            "status": "passed",
            "qa_report": qa_report_nc,
            "diff_summary": "No changes needed — QA verified existing code",
            "cost_usd": qa_cost_nc,
            "token_usage": qa_usage_nc,
        }

    # QA found gaps — retry with QA findings
    failed_musts = [
        item for item in verdict_nc.get("must_items", [])
        if item.get("status") == "fail"
    ]
    for item in failed_musts:
        criterion = item.get("criterion", "")[:80]
        evidence = item.get("evidence", "")[:80]
        emit("qa_finding", text=f"[must] \u2717 {criterion}")
        if evidence:
            emit("qa_finding", text=f"       evidence: {evidence}")

    emit("phase", name="qa", status="fail", time_s=qa_elapsed_nc,
         error="existing code doesn't satisfy spec")
    return {
        "action": "retry",
        "last_error": _build_qa_retry_error(failed_musts, qa_report_nc),
        "last_error_source": "qa",
        "cost_usd": qa_cost_nc,
        "token_usage": qa_usage_nc,
    }


async def run_task_v45(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
    context: Any | None = None,  # PipelineContext
    on_progress: Any | None = None,
    task_work_dir: Path | None = None,
    qa_mode: str = QAMode.PER_TASK,
    sibling_context: str | None = None,
) -> dict[str, Any]:
    """v4.5 per-task execution loop — coding agent + parallel spec gen + verify + QA.

    Key differences from run_task_with_qa():
    - Attempt 1 = bare coding agent (raw prompt, no custom system prompt, no spec)
    - Spec gen runs in parallel with coding, awaited before QA
    - Structured JSON QA verdict with [must]/[should] binding
    - QA with browser available (agent decides per-item)
    - Durable candidate refs (never discard verified code)
    - Session resume on retry when supported by the provider

    Args:
        project_dir: repo root — for git ops, config, logs, tasks.yaml.
        task_work_dir: where the coding agent works. Defaults to project_dir
            (serial mode). In parallel mode, this is a per-task git worktree.

    Returns {success, status, cost_usd, error, diff_summary, qa_report,
             phase_timings, review_ref}.
    """
    if task_work_dir is None:
        task_work_dir = project_dir
    from otto.spec import async_generate_spec  # noqa: F401 — kept for backward compat

    key = task["key"]
    task_id = task["id"]
    prompt = task["prompt"]
    max_retries = task.get("max_retries", config["max_retries"])
    max_task_time = config.get("max_task_time", 3600)  # 1 hour circuit breaker
    default_branch = config["default_branch"]
    test_timeout = config["verify_timeout"]

    task_start = time.monotonic()
    total_cost = 0.0
    total_token_usage: dict[str, int] = {}
    session_id = None
    _prev_session_cost: float = 0.0  # tracks cumulative SDK cost for delta calculation
    _prev_session_id: str | None = None  # tracks which session the cost belongs to
    last_error = None
    last_error_source = None
    _prev_failed_criteria: list[str] = []  # QA failures from previous attempt
    prior_attempts = task.get("attempts", 0) or 0
    total_attempts = prior_attempts
    # empty_retries removed — prompt now includes working dir, should not happen
    phase_timings: dict[str, float] = {}
    summary_phase_timings: dict[str, float] = {
        "prepare": 0.0,
        "coding": 0.0,
        "test": 0.0,
        "qa": 0.0,
        "merge": 0.0,
    }
    phase_costs: dict[str, float] = {}
    retry_reasons: list[str] = []
    phase_token_usage: dict[str, dict[str, int]] = {}
    spec = task.get("spec")
    batch_qa_mode = qa_mode == QAMode.BATCH
    skip_qa_mode = qa_mode == QAMode.SKIP
    pre_existing_untracked: set[str] | None = None
    spec_task: asyncio.Task | None = None
    spec_started_at: float | None = None
    _spec_finish_time: float | None = None  # set by spec gen thread on completion
    spec_generation_error = ""
    _result_error_code_unset = object()
    log_dir = project_dir / "otto_logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)
    cost_available = agent_provider(config) != "codex"

    # Live state for otto status -w — per-task file under otto_logs/{key}/
    _live_state_file = log_dir / "live-state.json"
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
                status = str(data.get("status", "") or "")
                phase_time = float(data.get("time_s", 0.0) or 0.0)
                phase_cost = float(data.get("cost", 0.0) or 0.0)
                if name in _live_phases:
                    _live_phases[name]["status"] = status
                    if phase_time:
                        _live_phases[name]["time_s"] = phase_time
                    if data.get("error"):
                        _live_phases[name]["error"] = data["error"][:100]
                    elif "error" in _live_phases[name]:
                        _live_phases[name].pop("error", None)
                if phase_time and status in ("done", "fail", "skip"):
                    summary_key = str(name)
                    if summary_key != "candidate":
                        summary_phase_timings[summary_key] = round(
                            summary_phase_timings.get(summary_key, 0.0) + phase_time,
                            1,
                        )
                if phase_cost:
                    cost_key = "spec" if name == "spec_gen" else str(name)
                    phase_costs[cost_key] = round(phase_costs.get(cost_key, 0.0) + phase_cost, 4)
                phase_telemetry = getattr(on_progress, "_telemetry", None) if on_progress else None
                if phase_telemetry and name:
                    try:
                        from otto.telemetry import PhaseCompleted

                        phase_telemetry.log(PhaseCompleted(
                            task_key=key,
                            phase=str(name),
                            status=status,
                            time_s=phase_time,
                            cost_usd=phase_cost,
                            detail=str(data.get("detail") or data.get("error") or "")[:200],
                        ))
                    except Exception:
                        pass
            elif event == "attempt_boundary":
                reason = str(data.get("reason", "") or "").strip()
                if reason:
                    retry_reasons.append(reason)
            elif event == "agent_tool":
                detail = data.get("detail", "")
                tool_name = data.get("name", "")
                _live_tools.append(f"{tool_name}  {detail}" if detail else tool_name)
                if len(_live_tools) > 20:
                    _live_tools[:] = _live_tools[-20:]
                phase_telemetry = getattr(on_progress, "_telemetry", None) if on_progress else None
                if phase_telemetry:
                    try:
                        from otto.telemetry import AgentToolCall

                        phase_telemetry.log(AgentToolCall(
                            task_key=key,
                            name=str(tool_name or ""),
                            detail=str(detail or "")[:200],
                        ))
                    except Exception:
                        pass
            _live_state_file.write_text(json.dumps({
                "task_key": key, "task_id": task_id,
                "prompt": prompt[:80],
                "elapsed_s": round(time.monotonic() - task_start, 1),
                "cost_usd": total_cost,
                "cost_available": cost_available,
                "token_usage": total_token_usage,
                "completed": False,
                "phases": _live_phases,
                "recent_tools": list(_live_tools),
                "_updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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

    async def _await_spec_task() -> list | None:
        nonlocal spec, spec_task, total_cost, spec_generation_error, total_token_usage, phase_token_usage
        if not spec_task or spec:
            return spec

        # Check if spec gen already finished before we needed it
        already_done = spec_task.done()
        try:
            spec_result = await spec_task
        finally:
            spec_task = None

        if isinstance(spec_result, tuple) and len(spec_result) == 4:
            spec_items, spec_cost, spec_error, spec_usage = spec_result
        elif isinstance(spec_result, tuple) and len(spec_result) == 3:
            spec_items, spec_cost, spec_error = spec_result
            spec_usage = {}
        else:
            spec_items, spec_cost, spec_error, spec_usage = [], 0.0, "invalid spec result", {}

        # Use the thread's own finish timestamp for accurate runtime
        if _spec_finish_time and spec_started_at:
            spec_elapsed = round(_spec_finish_time - spec_started_at, 1)
        elif spec_started_at:
            spec_elapsed = round(time.monotonic() - spec_started_at, 1)
        else:
            spec_elapsed = 0.0
        total_cost += spec_cost
        if spec_usage:
            phase_token_usage["spec_gen"] = merge_usage(phase_token_usage.get("spec_gen"), spec_usage)
            total_token_usage = merge_usage(total_token_usage, spec_usage)
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
            if already_done:
                _breakdown += " (ready)"
            emit("phase", name="spec_gen", status="done", time_s=spec_elapsed,
                 detail=_breakdown, cost=spec_cost)
            from otto.tasks import spec_binding, spec_is_verifiable, spec_text
            for item in spec:
                binding = spec_binding(item)
                text = spec_text(item)
                marker = "" if spec_is_verifiable(item) else " \u25c8"
                emit("spec_item", text=f"[{binding}{marker}] {text}")
            emit("spec_items_done")
            return spec

        spec_generation_error = spec_error or "spec generation produced no items"
        emit("phase", name="spec_gen", status="fail", time_s=spec_elapsed,
             error=spec_generation_error[:100])
        return spec

    def _result(success: bool, status: str, error: str = "",
                diff_summary: str = "", qa_report: str = "",
                review_ref: str | None = None,
                error_code: Any = _result_error_code_unset) -> dict[str, Any]:
        duration = time.monotonic() - task_start
        # In batch mode, mark deferred phases so live-state isn't misleading
        final_phases = dict(_live_phases)
        if batch_qa_mode:
            for phase_name in ("spec_gen", "qa", "merge"):
                if final_phases.get(phase_name, {}).get("status") == "pending":
                    final_phases[phase_name] = {"status": "deferred_to_batch", "time_s": 0}
        try:
            _live_state_file.write_text(json.dumps({
                "task_key": key,
                "task_id": task_id,
                "prompt": prompt[:80],
                "elapsed_s": round(duration, 1),
                "cost_usd": total_cost,
                "cost_available": cost_available,
                "token_usage": total_token_usage,
                "status": status,
                "completed": True,
                "error": error[:200] if error else "",
                "phases": final_phases,
                "recent_tools": list(_live_tools),
                "_updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }))
        except Exception:
            pass
        _write_task_summary_safe(
            log_dir,
            task_key=key,
            status=status,
            total_cost_usd=total_cost,
            cost_available=cost_available,
            token_usage=total_token_usage,
            phase_token_usage=phase_token_usage,
            total_duration_s=duration,
            attempts=total_attempts,
            phase_timings=summary_phase_timings,
            phase_costs=phase_costs,
            retry_reasons=retry_reasons,
        )
        if tasks_file:
            try:
                from datetime import datetime, timezone
                updates: dict[str, Any] = {"status": status}
                if cost_available:
                    updates["cost_usd"] = total_cost
                else:
                    updates["cost_usd"] = None
                updates["token_usage"] = total_token_usage or None
                if duration > 0:
                    updates["duration_s"] = round(duration, 1)
                if status in ("passed", "failed", "blocked"):
                    updates["completed_at"] = datetime.now(timezone.utc).isoformat()
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
                "cost_available": cost_available,
                "token_usage": total_token_usage,
                "error": error,
                "diff_summary": diff_summary,
                "qa_report": qa_report,
                "phase_timings": phase_timings,
                "review_ref": review_ref,
            }

    def _add_cost(amount: float) -> None:
        nonlocal total_cost
        total_cost += amount

    try:
        # ── Step 1: Prepare ─────────────────────────────────────────────
        emit("phase", name="prepare", status="running")
        prep_start = time.monotonic()

        if tasks_file:
            update_task(tasks_file, key, status="running", review_ref=None)

        # Worktree is already at base_sha (detached HEAD).
        # No branch needed — agent works on detached HEAD, candidate is
        # anchored as a ref. Merge happens in the orchestrator.
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=task_work_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        pre_existing_untracked = _snapshot_untracked(task_work_dir)


        custom_test_cmd = task.get("verify")

        # Auto-detect test_command only if not explicitly set in config
        if "test_command" in config:
            test_command = config["test_command"]  # respect explicit null
        else:
            test_command = detect_test_command(task_work_dir)

        # Baseline test check
        baseline_detail = ""
        if test_command:
            from otto.testing import run_local_tests
            baseline = run_local_tests(task_work_dir, test_command, test_timeout)
            if not baseline.passed and not baseline.skipped:
                output = baseline.output or ""
                infra_keywords = [
                    "Cannot find module", "ModuleNotFoundError",
                    "command not found", "No module named",
                    "SyntaxError", "error: unrecognized arguments",
                    "errors during collection",
                ]
                if any(kw in output for kw in infra_keywords):
                    _log_warn(
                        "baseline tests hit infrastructure/setup issues; "
                        "continuing so the coding agent can fix dependencies"
                    )
                else:
                    prep_elapsed = round(time.monotonic() - prep_start, 1)
                    phase_timings["prepare"] = prep_elapsed
                    emit("phase", name="prepare", status="fail", time_s=prep_elapsed,
                         error="baseline tests already failing")
                    _cleanup_task_failure(
                        task_work_dir, key, default_branch, tasks_file,
                        pre_existing_untracked=pre_existing_untracked,
                        error=f"BASELINE_FAIL: {output[-500:]}",
                        error_code="baseline_fail",
                        cost_usd=total_cost,
                        duration_s=time.monotonic() - task_start,
                    )
                    return _result(
                        False,
                        "failed",
                        error=f"BASELINE_FAIL: baseline tests already failing\n{output[-500:]}",
                        error_code="baseline_fail",
                    )
            # Extract test count for display
            if baseline.passed and baseline.output:
                m = re.search(r"(\d+) passed", baseline.output)
                if m:
                    baseline_detail = f"baseline: {m.group(1)} tests passing"

        prep_elapsed = round(time.monotonic() - prep_start, 1)
        phase_timings["prepare"] = prep_elapsed
        emit("phase", name="prepare", status="done", time_s=prep_elapsed,
             detail=baseline_detail)

        # ── Step 2: Coding + Verify + QA loop ───────────────────────────
        remaining = max(0, max_retries + 1 - prior_attempts)
        if remaining == 0:
            await _cancel_spec_task()
            error = f"max retries already exhausted ({prior_attempts} prior)"
            _cleanup_task_failure(
                task_work_dir, key, default_branch, tasks_file,
                pre_existing_untracked=pre_existing_untracked,
                error=error,
                error_code="max_retries",
                cost_usd=total_cost,
                duration_s=time.monotonic() - task_start,
            )
            return _result(False, "failed", error=error, error_code="max_retries")

        # Fire spec gen in a separate thread for true parallelism.
        # asyncio.create_task shares the event loop and gets starved by coding.
        # to_thread runs generate_spec_sync() with its own event loop.
        if not spec and not config.get("skip_spec") and not batch_qa_mode and not skip_qa_mode:
            from otto.spec import generate_spec_sync

            _spec_settings = config.get("spec_agent_settings", "project").split(",")

            def _run_spec_gen_thread():
                nonlocal _spec_finish_time
                try:
                    result = generate_spec_sync(
                        prompt,
                        task_work_dir,
                        setting_sources=_spec_settings,
                        log_dir=log_dir,
                        config=config,
                    )
                    _spec_finish_time = time.monotonic()
                    return result
                except Exception as exc:
                    _spec_finish_time = time.monotonic()
                    return None, 0.0, f"spec generation failed: {exc}", {}

            emit("phase", name="spec_gen", status="running")
            spec_started_at = time.monotonic()
            spec_task = asyncio.create_task(asyncio.to_thread(_run_spec_gen_thread))

        for attempt in range(remaining):
            attempt_num = prior_attempts + attempt + 1
            total_attempts += 1

            # Emit retry boundary
            if attempt > 0 and last_error:
                retry_reason = _build_retry_reason(last_error, last_error_source)
            else:
                retry_reason = ""
            emit("attempt_boundary", attempt=attempt_num, reason=retry_reason)

            # Time budget check
            elapsed = time.monotonic() - task_start
            if elapsed > max_task_time and attempt > 0:
                await _cancel_spec_task()
                error = f"time budget exceeded ({int(elapsed)}s)"
                _cleanup_task_failure(
                    task_work_dir, key, default_branch, tasks_file,
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

            # ── Coding ──────────────────────────────────────────────────
            feedback = task.get("feedback", "")
            coding_prompt = _build_coding_prompt(
                prompt, task_work_dir,
                attempt=attempt,
                last_error=last_error,
                last_error_source=last_error_source,
                feedback=feedback,
                spec=spec,
                context=context,
                sibling_context=sibling_context,
            )

            if attempt == 0 and not last_error:
                coding_detail = "coding agent"
            else:
                reason = last_error_source or "unknown"
                coding_detail = f"attempt {attempt_num} — {reason} failed"
            emit("phase", name="coding", status="running", attempt=attempt_num,
                 detail=coding_detail)
            coding_start = time.monotonic()
            try:
                new_session_id, result_msg, agent_log_lines = await _run_coding_agent(
                    coding_prompt, config, task_work_dir,
                    session_id=session_id,
                    emit=emit,
                    log_dir=log_dir,
                    attempt_num=attempt_num,
                )

                if new_session_id:
                    session_id = new_session_id
                    if tasks_file:
                        update_task(tasks_file, key, session_id=session_id)

                coding_usage = normalize_usage(getattr(result_msg, "usage", None))
                if coding_usage:
                    phase_token_usage["coding"] = merge_usage(phase_token_usage.get("coding"), coding_usage)
                    total_token_usage = merge_usage(total_token_usage, coding_usage)

                attempt_cost, _prev_session_id, _prev_session_cost = _extract_attempt_cost(
                    result_msg, _prev_session_id, _prev_session_cost,
                )
                total_cost += attempt_cost

                coding_elapsed = round(time.monotonic() - coding_start, 1)
                phase_timings["coding"] = phase_timings.get("coding", 0) + coding_elapsed

                # Warn if SDK returned $0 for a non-trivial coding run
                # (known issue: concurrent parallel sessions may lose cost)
                if attempt_cost == 0 and coding_elapsed > 10:
                    if cost_available:
                        from otto.observability import append_text_log
                        append_text_log(
                            log_dir / "cost-warning.log",
                            [
                                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARNING: $0 cost for {coding_elapsed:.0f}s coding",
                                f"session_id: {session_id}",
                                f"result_msg.total_cost_usd: {getattr(result_msg, 'total_cost_usd', 'missing')}",
                                f"This may be an SDK bug with concurrent parallel sessions.",
                                "",
                            ],
                        )

            except Exception as e:
                coding_elapsed = round(time.monotonic() - coding_start, 1)
                emit("phase", name="coding", status="fail", time_s=coding_elapsed,
                     error=str(e)[:80], attempt=attempt_num)
                _restore_workspace_state(
                    task_work_dir,
                    reset_ref=base_sha,
                    pre_existing_untracked=pre_existing_untracked,
                )
                last_error = str(e)
                last_error_source = "coding"
                continue

            # ── Check for changes ───────────────────────────────────────
            no_changes, _ = _check_agent_changes(
                task_work_dir, base_sha, pre_existing_untracked,
            )

            if no_changes:
                if batch_qa_mode:
                    reason = ""
                    for line in reversed(agent_log_lines):
                        if not line.startswith("\u25cf") and line.strip():
                            reason = line.strip()[:120]
                            break
                    emit("phase", name="coding", status="done", time_s=coding_elapsed,
                         cost=attempt_cost, detail=f"no changes — {reason or 'existing code'}")
                    _restore_workspace_state(
                        task_work_dir,
                        reset_ref=base_sha,
                        pre_existing_untracked=pre_existing_untracked,
                    )
                    return _result(
                        True,
                        "verified",
                        diff_summary="No changes needed — batch QA will verify existing code",
                    )
                nc_result = await _handle_no_changes(
                    task, config, task_work_dir,
                    prompt=prompt,
                    spec=spec,
                    spec_task=spec_task,
                    agent_log_lines=agent_log_lines,
                    coding_elapsed=coding_elapsed,
                    attempt_cost=attempt_cost,
                    emit=emit,
                    on_progress=on_progress,
                    log_dir=log_dir,
                    attempt=attempt,
                    add_cost=_add_cost,
                    await_spec=_await_spec_task,
                )
                # _handle_no_changes may have awaited spec, update local ref
                if not spec_task or (spec_task and spec_task.done()):
                    spec_task = None
                nc_usage = nc_result.get("token_usage", {}) or {}
                if nc_usage:
                    phase_token_usage["qa"] = merge_usage(phase_token_usage.get("qa"), nc_usage)
                    total_token_usage = merge_usage(total_token_usage, nc_usage)
                if nc_result["action"] == "pass":
                    status = nc_result.get("status", "passed")
                    return _result(True, status,
                                   qa_report=nc_result["qa_report"],
                                   diff_summary=nc_result["diff_summary"])
                else:  # retry
                    last_error = nc_result["last_error"]
                    last_error_source = nc_result["last_error_source"]
                    continue

            # Coding succeeded — agent produced changes
            emit("phase", name="coding", status="done", time_s=coding_elapsed,
                 cost=attempt_cost, attempt=attempt_num)

            # ── Build candidate + verify ────────────────────────────────
            candidate_sha = build_candidate_commit(
                task_work_dir, base_sha, pre_existing_untracked,
            )

            # Re-detect test command
            if not config.get("test_command"):
                detected = detect_test_command(task_work_dir)
                if detected:
                    test_command = detected

            # TEST — clean disposable worktree, deterministic
            if config.get("skip_test"):
                from otto.testing import TestSuiteResult
                emit("phase", name="test", status="done", time_s=0,
                     detail="skipped (skip_test)")
                test_result = TestSuiteResult(passed=True, tiers=[], failure_output=None)
                test_elapsed = 0.0
                test_detail = "skipped"
            else:
                emit("phase", name="test", status="running")
                test_result, test_elapsed, test_detail = _run_full_test_suite(
                    task_work_dir, candidate_sha, test_command, custom_test_cmd,
                    test_timeout, log_dir, attempt_num,
                )
            phase_timings["test"] = phase_timings.get("test", 0) + test_elapsed

            if not test_result.passed:
                emit("phase", name="test", status="fail", time_s=test_elapsed,
                     error=(test_result.failure_output or "")[:80], detail=test_detail)
                subprocess.run(
                    ["git", "reset", "--mixed", base_sha],
                    cwd=task_work_dir, capture_output=True,
                )
                last_error = build_retry_excerpt(test_result.failure_output or "")
                last_error_source = "test"
                continue

            emit("phase", name="test", status="done", time_s=test_elapsed,
                 detail=test_detail)

            # ANCHOR verified candidate as durable git ref
            ref_name = _anchor_candidate_ref(task_work_dir, key, attempt_num, candidate_sha)
            emit("phase", name="candidate", status="done", detail=ref_name)

            # ── Squash + diff ───────────────────────────────────────────
            try:
                diff_summary = _squash_and_commit(
                    task_work_dir, base_sha, prompt, task_id,
                )
            except (subprocess.CalledProcessError, Exception) as e:
                stderr = getattr(e, "stderr", str(e))
                _cleanup_task_failure(
                    task_work_dir, key, default_branch, tasks_file,
                    pre_existing_untracked=pre_existing_untracked,
                    error=f"squash commit failed: {stderr}",
                    error_code="internal_error",
                    cost_usd=total_cost,
                    duration_s=time.monotonic() - task_start,
                )
                return _result(False, "failed", error=f"squash commit failed: {stderr}")

            diff_info = _get_diff_info(task_work_dir, base_sha)

            _write_log_safe(log_dir, "verify.log", "PASSED")

            # ── Claim verification (audit-only, non-blocking) ─────────
            try:
                agent_log_path = log_dir / f"attempt-{attempt_num}-agent.log"
                test_log_path = log_dir / f"attempt-{attempt_num}-verify.log"
                claim_findings = verify_claims(agent_log_path, test_log_path)
                if claim_findings:
                    claims_text = format_claim_findings(claim_findings)
                    _write_log_safe(log_dir, f"attempt-{attempt_num}-claims.md", claims_text)
            except Exception:
                pass

            # ── QA ──────────────────────────────────────────────────────
            qa_report = ""
            if skip_qa_mode or config.get("skip_qa"):
                await _cancel_spec_task()
                emit("phase", name="qa", status="done", time_s=0,
                     detail="skipped (skip_qa)")
            elif batch_qa_mode:
                await _cancel_spec_task()
            else:
                # Await specs before QA (if still generating)
                if spec_task and not spec:
                    if not spec_task.done():
                        emit("phase", name="spec_gen", status="running",
                             detail="awaiting specs before QA...")
                    await _await_spec_task()

                qa_out = await _run_qa(
                    task, config, task_work_dir,
                    prompt=prompt,
                    spec=spec,
                    spec_generation_error=spec_generation_error,
                    diff_info=diff_info,
                    attempt=attempt,
                    prev_failed_criteria=_prev_failed_criteria,
                    emit=emit,
                    on_progress=on_progress,
                    log_dir=log_dir,
                    add_cost=_add_cost,
                )
                qa_report = qa_out["qa_report"]
                qa_usage = qa_out.get("usage", {}) or {}
                if qa_usage:
                    phase_token_usage["qa"] = merge_usage(phase_token_usage.get("qa"), qa_usage)
                    total_token_usage = merge_usage(total_token_usage, qa_usage)
                phase_timings["qa"] = phase_timings.get("qa", 0) + qa_out["qa_elapsed"]

                if not qa_out["must_passed"]:
                    # Reset for retry — send structured failure info
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=task_work_dir, capture_output=True,
                    )
                    failed_musts = qa_out["failed_musts"]
                    if failed_musts:
                        last_error = _build_qa_retry_error(failed_musts, qa_report)
                    else:
                        last_error = qa_report  # fallback to raw report
                    last_error_source = "qa"
                    _prev_failed_criteria = qa_out["prev_failed_criteria"]
                    continue

            # ── Restore workspace before merge ──────────────────────────
            _restore_workspace_state(
                task_work_dir,
                reset_ref=candidate_sha,
                pre_existing_untracked=pre_existing_untracked,
            )

            # QA may leave commits or workspace drift behind. Restore the
            # verified candidate before merge so the branch HEAD matches the
            # exact state that passed testing.
            _restore_workspace_state(
                task_work_dir,
                reset_ref=candidate_sha,
                pre_existing_untracked=pre_existing_untracked,
            )

            if not skip_qa_mode and not config.get("skip_qa") and not batch_qa_mode:
                _audit_proof_sufficiency(qa_out["verdict"], spec, log_dir / "qa-proofs", emit)

            # ── Verified — merge is handled by orchestrator ──
            emit("phase", name="merge", status="pending",
                 detail="deferred to merge phase")
            return _result(True, "verified",
                           diff_summary=diff_summary, qa_report=qa_report)

        # All retries exhausted — find best verified candidate
        await _cancel_spec_task()
        best_ref = _find_best_candidate_ref(task_work_dir, key)
        last_err_detail = f"\nLast error:\n{last_error}" if last_error else ""
        _cleanup_task_failure(
            task_work_dir, key, default_branch, tasks_file,
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
                task_work_dir, key, default_branch, tasks_file,
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
    project_dir: Path | None = None,
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

        # Show proof path for passed tasks
        if success:
            proof_report = Path("otto_logs") / task_key / "qa-proofs" / "proof-report.md"
            if proof_report.exists():
                console.print(f"       [dim]proof: {proof_report}[/dim]")

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
