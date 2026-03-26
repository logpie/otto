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
    query,
    tool_use_summary as _tool_use_summary,
)

from otto.config import git_meta_dir, detect_test_command
from otto.display import _truncate_at_word, console, format_cost, format_duration, rich_escape
from otto.git_ops import (
    check_clean_tree,
    create_task_branch,
    build_candidate_commit,
    merge_to_default,
    cleanup_branch,
    rebase_and_merge,
    _should_stage_untracked,
    _anchor_candidate_ref,
    _find_best_candidate_ref,
    _get_diff_info,
    _cleanup_task_failure,
    _restore_workspace_state,
    _snapshot_untracked,
)
from otto.qa import (
    QA_SYSTEM_PROMPT_V45,
    format_spec_v45,
    determine_qa_tier,
    _parse_qa_verdict_json,
    run_qa_agent_v45,
)
from otto.tasks import load_tasks, update_task
from otto.testing import run_test_suite, _subprocess_env
from otto.claim_verify import verify_claims, format_claim_findings
from otto.retry_excerpt import build_retry_excerpt
from otto.flaky import extract_failing_tests, failures_are_baseline_only


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

    # Suggest CLAUDE.md if missing — coding agents read it for project conventions
    if not (project_dir / "CLAUDE.md").exists():
        _suggest_claude_md(project_dir)

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
    otto_excludes = ["otto_logs/", "otto_arch/", ".otto-scratch/", "tasks.yaml", ".tasks.lock"]
    missing_excludes = [e for e in otto_excludes if e not in existing_exclude]
    if missing_excludes:
        with open(exclude_path, "a") as f:
            f.write("\n# otto runtime files\n")
            for entry in missing_excludes:
                f.write(entry + "\n")

    # Baseline check moved to run_task_v45() — runs after branch creation
    # with auto-detected test command for consistent results.

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


    return (None, pending)


from otto.display import build_agent_tool_event as _build_agent_tool_event  # noqa: E402


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

            # Print task result — same indentation level as phase completions
            console.print(
                f"  [green]{time.strftime('%H:%M:%S')}  \u2713 passed[/green]  "
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
                parts.append(f"[green]{spec_count} specs[/green] verified")
            if attempts > 1:
                parts.append(f"{attempts} attempts")
            if parts:
                console.print(f"    {' · '.join(parts)}")
            proof_report = project_dir / "otto_logs" / task_key / "qa-proofs" / "proof-report.md"
            if proof_report.exists():
                console.print(f"    [dim]proofs: {proof_report}[/dim]")

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
    """Write a log file, silently ignoring OS errors."""
    try:
        (log_dir / filename).write_text(content)
    except OSError:
        pass


def _persist_qa_results(
    log_dir: Path,
    qa_report: str,
    verdict: dict,
) -> None:
    """Persist QA report and verdict JSON to the task log directory."""
    _write_log_safe(log_dir, "qa-report.md", qa_report or "No QA output")
    if verdict:
        _write_log_safe(log_dir, "qa-verdict.json", json.dumps(verdict, indent=2))


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
) -> str:
    """Build the coding agent prompt for a given attempt.

    Round 1 (attempt=0, no last_error): bare prompt + learnings + feedback.
    Round 2+: adds spec, raw error output, and fix instructions.
    """
    coding_prompt = prompt

    if attempt == 0 and not last_error:
        # ROUND 1: Bare CC — raw prompt + cross-task learnings + user feedback
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
        f"\n- Use .otto-scratch/ for throwaway verification scripts."
        f"\n- Follow the repo's existing test conventions for permanent tests."
        f"\n- Reuse existing test helpers and mock data factories — do not duplicate."
        f"\n- Prefer extending existing test files over creating new ones."
    )
    return coding_prompt


async def _run_coding_agent(
    coding_prompt: str,
    config: dict[str, Any],
    project_dir: Path,
    *,
    session_id: str | None,
    emit: Any,
    log_dir: Path,
    attempt_num: int,
) -> tuple[str | None, float, list[str], Any]:
    """Run the coding agent and return (session_id, attempt_cost, log_lines, result_msg).

    Raises on agent error (result_msg.is_error).
    """
    _coding_settings = config.get("coding_agent_settings", "project").split(",")
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


def _run_pre_test(
    project_dir: Path,
    test_command: str | None,
    timeout: int,
) -> Any | None:
    """Run pre-test (quick local test) if test_command is set.

    Returns the check result, or None if skipped.
    """
    if not test_command:
        return None
    from otto.testing import run_local_tests
    return run_local_tests(project_dir, test_command, timeout)


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

    qa_tier = determine_qa_tier(task, qa_spec, attempt, diff_info)
    if qa_warning:
        qa_tier = max(qa_tier, 2)

    if qa_tier < 1:
        # Tier 0 — skip QA
        emit("phase", name="qa", status="done", time_s=0,
             detail="tier 0 — skipped")
        return {
            "qa_report": "",
            "qa_tier": qa_tier,
            "verdict": {},
            "must_passed": True,
            "cost_usd": 0.0,
            "qa_elapsed": 0.0,
            "failed_musts": [],
            "prev_failed_criteria": prev_failed_criteria,
        }

    qa_detail = f"tier {qa_tier}"
    if qa_warning:
        qa_detail += " — prompt-only fallback"
    emit("phase", name="qa", status="running", detail=qa_detail)
    qa_start = time.monotonic()

    from otto.tasks import spec_binding as _sb
    focus_items = [item for item in qa_spec if _sb(item) == "must"]

    qa_result = await run_qa_agent_v45(
        task, qa_spec, config, project_dir,
        original_prompt=prompt,
        diff=diff_info["full_diff"],
        tier=qa_tier,
        focus_items=focus_items,
        prev_failed=prev_failed_criteria if prev_failed_criteria else None,
        on_progress=on_progress,
        log_dir=log_dir,
    )
    qa_elapsed = round(time.monotonic() - qa_start, 1)
    total_qa_cost = qa_result.get("cost_usd", 0.0)
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
        qa_result = await run_qa_agent_v45(
            task, qa_spec, config, project_dir,
            original_prompt=prompt,
            diff=diff_info["full_diff"],
            tier=qa_tier,
            focus_items=focus_items,
            prev_failed=prev_failed_criteria,
            on_progress=on_progress,
            log_dir=log_dir,
        )
        qa_result["_retried"] = True
        retry_qa_cost = qa_result.get("cost_usd", 0.0)
        add_cost(retry_qa_cost)
        total_qa_cost += retry_qa_cost
        qa_elapsed += round(time.monotonic() - qa_start, 1)
        qa_report = qa_result.get("raw_report", "")
        if qa_warning:
            qa_report = f"[warning] {qa_warning}\n\n{qa_report}".strip()
        verdict = qa_result.get("verdict", {})

    _emit_qa_item_results(emit, verdict, qa_spec)

    # Persist the final QA report, including retried runs.
    _persist_qa_results(log_dir, qa_report, verdict)

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
    default_branch: str,
    key: str,
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
        }

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
        log_dir=log_dir,
    )
    qa_elapsed_nc = round(time.monotonic() - qa_start_nc, 1)
    qa_cost_nc = qa_result_nc.get("cost_usd", 0.0)
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
             detail="no changes needed")
        subprocess.run(["git", "checkout", default_branch],
                       cwd=project_dir, capture_output=True)
        cleanup_branch(project_dir, key, default_branch)
        return {
            "action": "pass",
            "qa_report": qa_report_nc,
            "diff_summary": "No changes needed — QA verified existing code",
            "cost_usd": qa_cost_nc,
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
    spec = task.get("spec")
    pre_existing_untracked: set[str] | None = None
    spec_task: asyncio.Task | None = None
    spec_started_at: float | None = None
    _spec_finish_time: float | None = None  # set by spec gen thread on completion
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

    async def _await_spec_task() -> list | None:
        nonlocal spec, spec_task, total_cost, spec_generation_error
        if not spec_task or spec:
            return spec

        # Check if spec gen already finished before we needed it
        already_done = spec_task.done()
        try:
            spec_items, spec_cost, spec_error = await spec_task
        finally:
            spec_task = None

        # Use the thread's own finish timestamp for accurate runtime
        if _spec_finish_time and spec_started_at:
            spec_elapsed = round(_spec_finish_time - spec_started_at, 1)
        elif spec_started_at:
            spec_elapsed = round(time.monotonic() - spec_started_at, 1)
        else:
            spec_elapsed = 0.0
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
        try:
            if _live_state_file.exists():
                _live_state_file.unlink()
        except OSError:
            pass
        duration = time.monotonic() - task_start
        if tasks_file:
            try:
                from datetime import datetime, timezone
                updates: dict[str, Any] = {"status": status, "cost_usd": total_cost}
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
            update_task(tasks_file, key, status="running", attempts=0, review_ref=None)

        base_sha = create_task_branch(project_dir, key, default_branch, task=task)
        pre_existing_untracked = _snapshot_untracked(project_dir)

        log_dir = project_dir / "otto_logs" / key
        log_dir.mkdir(parents=True, exist_ok=True)

        # Create scratch area for ephemeral test verification
        scratch_dir = project_dir / ".otto-scratch"
        scratch_dir.mkdir(exist_ok=True)

        custom_test_cmd = task.get("verify")

        # Auto-detect test_command only if not explicitly set in config
        if "test_command" in config:
            test_command = config["test_command"]  # respect explicit null
        else:
            test_command = detect_test_command(project_dir)

        # Baseline test check
        baseline_detail = ""
        baseline_failures: set[str] = set()
        if test_command:
            from otto.testing import run_local_tests
            baseline = run_local_tests(project_dir, test_command, test_timeout)
            if baseline.output:
                baseline_failures = extract_failing_tests(baseline.output)
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
                project_dir, key, default_branch, tasks_file,
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
        if not spec:
            from otto.spec import generate_spec_sync

            _spec_settings = config.get("spec_agent_settings", "project").split(",")

            def _run_spec_gen_thread():
                nonlocal _spec_finish_time
                try:
                    result = generate_spec_sync(prompt, project_dir, setting_sources=_spec_settings)
                    _spec_finish_time = time.monotonic()
                    return result
                except Exception as exc:
                    _spec_finish_time = time.monotonic()
                    return None, 0.0, f"spec generation failed: {exc}"

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

            # ── Coding ──────────────────────────────────────────────────
            feedback = task.get("feedback", "")
            coding_prompt = _build_coding_prompt(
                prompt, project_dir,
                attempt=attempt,
                last_error=last_error,
                last_error_source=last_error_source,
                feedback=feedback,
                spec=spec,
                context=context,
            )

            if attempt == 0 and not last_error:
                coding_detail = "bare CC"
            else:
                reason = last_error_source or "unknown"
                coding_detail = f"attempt {attempt_num} — {reason} failed"
            emit("phase", name="coding", status="running", attempt=attempt_num,
                 detail=coding_detail)
            coding_start = time.monotonic()
            try:
                new_session_id, result_msg, agent_log_lines = await _run_coding_agent(
                    coding_prompt, config, project_dir,
                    session_id=session_id,
                    emit=emit,
                    log_dir=log_dir,
                    attempt_num=attempt_num,
                )

                if new_session_id:
                    session_id = new_session_id
                    if tasks_file:
                        update_task(tasks_file, key, session_id=session_id)

                attempt_cost, _prev_session_id, _prev_session_cost = _extract_attempt_cost(
                    result_msg, _prev_session_id, _prev_session_cost,
                )
                total_cost += attempt_cost

                coding_elapsed = round(time.monotonic() - coding_start, 1)
                phase_timings["coding"] = phase_timings.get("coding", 0) + coding_elapsed

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

            # ── Check for changes ───────────────────────────────────────
            no_changes, _ = _check_agent_changes(
                project_dir, base_sha, pre_existing_untracked,
            )

            if no_changes:
                nc_result = await _handle_no_changes(
                    task, config, project_dir,
                    prompt=prompt,
                    spec=spec,
                    spec_task=spec_task,
                    agent_log_lines=agent_log_lines,
                    coding_elapsed=coding_elapsed,
                    attempt_cost=attempt_cost,
                    emit=emit,
                    on_progress=on_progress,
                    log_dir=log_dir,
                    default_branch=default_branch,
                    key=key,
                    attempt=attempt,
                    add_cost=_add_cost,
                    await_spec=_await_spec_task,
                )
                # _handle_no_changes may have awaited spec, update local ref
                if not spec_task or (spec_task and spec_task.done()):
                    spec_task = None
                if nc_result["action"] == "pass":
                    return _result(True, "passed",
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
                project_dir, base_sha, pre_existing_untracked,
            )

            # Re-detect test command
            if not config.get("test_command"):
                detected = detect_test_command(project_dir)
                if detected:
                    test_command = detected

            # PRE-TEST: quick sanity check in the working directory
            pre_test = _run_pre_test(project_dir, test_command, test_timeout)
            if pre_test and not pre_test.passed and not pre_test.skipped:
                # Check if failures are only pre-existing (flaky) baseline failures
                current_failures = extract_failing_tests(pre_test.output or "")
                if baseline_failures and failures_are_baseline_only(baseline_failures, current_failures):
                    # Only flaky/baseline failures — proceed instead of retrying
                    pass
                else:
                    emit("phase", name="coding", status="fail", time_s=coding_elapsed,
                         error="tests fail in working dir", cost=attempt_cost)
                    last_error = build_retry_excerpt(pre_test.output or "tests failed locally")
                    last_error_source = "test"
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=project_dir, capture_output=True,
                    )
                    continue

            # TEST — clean worktree, deterministic
            emit("phase", name="test", status="running")
            test_result, test_elapsed, test_detail = _run_full_test_suite(
                project_dir, candidate_sha, test_command, custom_test_cmd,
                test_timeout, log_dir, attempt_num,
            )
            phase_timings["test"] = phase_timings.get("test", 0) + test_elapsed

            if not test_result.passed:
                if test_command and pre_test and pre_test.passed:
                    # Tests pass locally but fail in cold worktree — trust local
                    emit("phase", name="test", status="done", time_s=test_elapsed,
                         detail=test_detail)
                    # Fall through to the test-passed path below
                else:
                    emit("phase", name="test", status="fail", time_s=test_elapsed,
                         error=(test_result.failure_output or "")[:80], detail=test_detail)
                    subprocess.run(
                        ["git", "reset", "--mixed", base_sha],
                        cwd=project_dir, capture_output=True,
                    )
                    last_error = build_retry_excerpt(test_result.failure_output or "")
                    last_error_source = "test"
                    continue

            if test_result.passed:
                emit("phase", name="test", status="done", time_s=test_elapsed,
                     detail=test_detail)

            # ANCHOR verified candidate as durable git ref
            ref_name = _anchor_candidate_ref(project_dir, key, attempt_num, candidate_sha)
            emit("phase", name="candidate", status="done", detail=ref_name)

            # ── Squash + diff ───────────────────────────────────────────
            try:
                diff_summary = _squash_and_commit(
                    project_dir, base_sha, prompt, task_id,
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

            diff_info = _get_diff_info(project_dir, base_sha)

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
            # Await specs before QA (if still generating)
            if spec_task and not spec:
                if not spec_task.done():
                    emit("phase", name="spec_gen", status="running",
                         detail="awaiting specs before QA...")
                await _await_spec_task()

            qa_out = await _run_qa(
                task, config, project_dir,
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
            phase_timings["qa"] = phase_timings.get("qa", 0) + qa_out["qa_elapsed"]

            if not qa_out["must_passed"]:
                # Reset for retry — send structured failure info
                subprocess.run(
                    ["git", "reset", "--mixed", base_sha],
                    cwd=project_dir, capture_output=True,
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
                project_dir,
                reset_ref=candidate_sha,
                pre_existing_untracked=pre_existing_untracked,
            )

            # QA may leave commits or workspace drift behind. Restore the
            # verified candidate before merge so the branch HEAD matches the
            # exact state that passed testing.
            _restore_workspace_state(
                project_dir,
                reset_ref=candidate_sha,
                pre_existing_untracked=pre_existing_untracked,
            )

            _audit_proof_sufficiency(qa_out["verdict"], spec, log_dir / "qa-proofs", emit)

            # ── Merge ───────────────────────────────────────────────────
            emit("phase", name="merge", status="running")
            merge_start = time.monotonic()
            if merge_to_default(project_dir, key, default_branch):
                merge_elapsed = round(time.monotonic() - merge_start, 1)
                phase_timings["merge"] = merge_elapsed
                emit("phase", name="merge", status="done", time_s=merge_elapsed)
                testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
                if testgen_dir.exists():
                    shutil.rmtree(testgen_dir, ignore_errors=True)
                # Append commit SHA to proof report
                try:
                    commit_sha = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=project_dir, capture_output=True, text=True,
                    ).stdout.strip()
                    proof_report_path = log_dir / "qa-proofs" / "proof-report.md"
                    if proof_report_path.exists() and commit_sha:
                        with open(proof_report_path, "a") as f:
                            f.write(f"\n**Commit:** {commit_sha}\n")
                except Exception:
                    pass
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
