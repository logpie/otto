"""Otto v4 orchestrator — Plan-Execute-Replan (PER) deterministic loop.

Replaces the v3 LLM pilot with a Python-driven orchestrator. LLM is invoked
only at decision points (initial plan + replan on failure). Agents coordinate
through in-memory shared state. Tasks execute sequentially within each batch,
or in parallel when max_parallel > 1 (each task gets its own git worktree).

Entry point: run_per()
"""
import asyncio
import fcntl
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from otto.config import git_meta_dir
from otto.context import Learning, PipelineContext, QAMode, TaskResult
from otto.display import console, rich_escape
from otto.planner import (
    ExecutionPlan,
    plan,
    replan,
    serial_plan,
)
from otto.observability import append_text_log
from otto.runner import (
    _print_summary,
    coding_loop,
    preflight_checks,
)
from otto.qa import run_qa
from otto.tasks import (
    load_tasks,
    mutate_and_recompute,
    planner_input_fingerprint,
    update_task,
)
from otto.telemetry import (
    AllDone,
    BatchCompleted,
    PlanCreated,
    TaskMerged,
    TaskFailed,
    Telemetry,
)


def _stamp_proof_commit_sha(project_dir: Path, task_key: str, commit_sha: str) -> None:
    """Append commit SHA to a task's proof report after successful merge."""
    try:
        proof_path = project_dir / "otto_logs" / task_key / "qa-proofs" / "proof-report.md"
        if proof_path.exists() and commit_sha:
            with open(proof_path, "a") as f:
                f.write(f"\n**Commit:** {commit_sha}\n")
    except Exception:
        pass


def _orchestrator_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "orchestrator.log", lines)


def _format_batch_structure(batch: Any, pending_by_key: dict[str, dict[str, Any]], *, mode: str) -> list[str]:
    lines = [f"batch structure: mode={mode} size={len(batch.tasks)}"]
    for task_plan in batch.tasks:
        task = pending_by_key.get(task_plan.task_key, {})
        lines.append(
            f"- #{task.get('id', '?')} {task_plan.task_key}: "
            f"{str(task.get('prompt', '') or '')[:120]}"
        )
    return lines


def _summarize_batch_structure(plan: ExecutionPlan) -> str:
    if not plan.batches:
        return "(none)"
    return "; ".join(
        f"batch {idx}: {', '.join(task.task_key for task in batch.tasks)}"
        for idx, batch in enumerate(plan.batches, start=1)
    )


def load_learnings(project_dir: Path, context: PipelineContext) -> None:
    """Load persisted learnings from otto_logs/learnings.jsonl into context.

    Deduplicates by text — only adds entries not already present.
    """
    learnings_file = project_dir / "otto_logs" / "learnings.jsonl"
    if not learnings_file.exists():
        return
    existing_texts = {l.text for l in context.learnings}
    try:
        for line in learnings_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = entry.get("text", "")
            if text and text not in existing_texts:
                context.add_learning(
                    text=text,
                    source=entry.get("source", "prior-run"),
                    kind=entry.get("kind", "observed"),
                )
                existing_texts.add(text)
    except OSError:
        pass


def persist_learnings(project_dir: Path, context: PipelineContext) -> None:
    """Append new observed learnings to otto_logs/learnings.jsonl.

    Deduplicates by text — only appends entries not already in the file.
    """
    learnings = context.observed_learnings
    if not learnings:
        return

    learnings_file = project_dir / "otto_logs" / "learnings.jsonl"
    learnings_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing texts for dedup
    existing_texts: set[str] = set()
    if learnings_file.exists():
        try:
            for line in learnings_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    existing_texts.add(entry.get("text", ""))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    # Append new entries
    new_entries = [l for l in learnings if l.text not in existing_texts]
    if not new_entries:
        return

    try:
        from datetime import datetime
        with open(learnings_file, "a") as f:
            for l in new_entries:
                entry = {
                    "text": l.text,
                    "source": l.source,
                    "kind": l.kind,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
                f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def cleanup_orphaned_worktrees(project_dir: Path) -> None:
    """Remove orphaned otto worktrees from previous crashed runs."""
    from otto.git_ops import cleanup_all_worktrees
    cleanup_all_worktrees(project_dir)
    # Legacy path for backward compat
    wt_dir = project_dir / ".worktrees"
    if not wt_dir.exists():
        return
    for child in wt_dir.iterdir():
        if child.name.startswith("otto-") and child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def _planner_conflict_and_blocked_keys(
    execution_plan: ExecutionPlan,
    pending: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Compute direct conflicts plus downstream blocked closure."""
    task_by_key = {
        str(task.get("key", "")): task
        for task in pending
        if task.get("key")
    }
    id_to_key = {
        int(task["id"]): str(task["key"])
        for task in pending
        if isinstance(task.get("id"), int) and task.get("key")
    }
    reverse_deps: dict[str, set[str]] = {}
    for task in pending:
        task_key = str(task.get("key", "") or "")
        if not task_key:
            continue
        for dep_id in task.get("depends_on") or []:
            dep_key = id_to_key.get(dep_id)
            if dep_key:
                reverse_deps.setdefault(dep_key, set()).add(task_key)

    direct_conflicts: set[str] = set()
    for conflict in execution_plan.conflicts:
        keys = [
            str(task_key)
            for task_key in conflict.get("tasks") or []
            if str(task_key) in task_by_key
        ]
        if len(keys) >= 2:
            direct_conflicts.update(keys)

    blocked: set[str] = set()
    for root_key in direct_conflicts:
        queue = [root_key]
        visited = {root_key}
        while queue:
            parent_key = queue.pop(0)
            for child_key in reverse_deps.get(parent_key, set()):
                if child_key in direct_conflicts:
                    continue
                blocked.add(child_key)
                if child_key not in visited:
                    visited.add(child_key)
                    queue.append(child_key)

    return direct_conflicts, blocked


def _build_sibling_context(
    task_key: str,
    batch: Any,
    pending_by_key: dict[str, dict[str, Any]],
    analysis: list[dict[str, Any]],
) -> str | None:
    """Build context about sibling tasks for the coding agent.

    Tells the agent what other tasks exist, what they modify, and what
    integration risks to watch for. This prevents bugs like "items API
    doesn't filter by authenticated user" when a sibling task adds auth.
    """
    siblings = [
        tp for tp in batch.tasks
        if tp.task_key != task_key and tp.task_key in pending_by_key
    ]
    if not siblings:
        return None

    lines = ["OTHER TASKS IN THIS BATCH (consider interactions):"]
    for sib in siblings:
        sib_task = pending_by_key.get(sib.task_key, {})
        sib_prompt = sib_task.get("prompt", "")
        lines.append(f"- {sib_prompt}")

    # Add planner relationship analysis for this task
    relevant = [
        item for item in analysis
        if task_key in (item.get("task_a"), item.get("task_b"))
        and item.get("relationship") not in ("INDEPENDENT", None)
    ]
    if relevant:
        lines.append("\nPlanner analysis (integration risks):")
        for item in relevant:
            other = item["task_b"] if item["task_a"] == task_key else item["task_a"]
            other_prompt = pending_by_key.get(other, {}).get("prompt", other)
            lines.append(
                f"- {item.get('relationship', '?')} with \"{other_prompt[:100]}\": "
                f"{item.get('reason', '')}"
            )

    return "\n".join(lines)


def _plan_covers_pending(execution_plan: ExecutionPlan, pending: list[dict[str, Any]]) -> bool:
    """Return True when planned + conflicted + blocked covers pending exactly once."""
    pending_keys = {
        str(task.get("key", ""))
        for task in pending
        if task.get("key")
    }
    planned_keys = [
        task_plan.task_key
        for batch in execution_plan.batches
        for task_plan in batch.tasks
    ]
    if len(planned_keys) != len(set(planned_keys)):
        return False

    planned_set = set(planned_keys)
    direct_conflicts, blocked_keys = _planner_conflict_and_blocked_keys(execution_plan, pending)
    if planned_set & direct_conflicts:
        return False
    if planned_set & blocked_keys:
        return False
    if direct_conflicts & blocked_keys:
        return False

    covered = planned_set | direct_conflicts | blocked_keys
    return covered == pending_keys


def _apply_planner_outcomes(
    tasks_file: Path,
    pending: list[dict[str, Any]],
    execution_plan: ExecutionPlan,
) -> None:
    """Persist direct conflicts, then recompute blocked closure atomically."""
    pending_by_key = {
        str(task.get("key", "")): task
        for task in pending
        if task.get("key")
    }
    pending_keys = set(pending_by_key)
    conflict_entries_by_key: dict[str, list[dict[str, Any]]] = {}

    for conflict in execution_plan.conflicts:
        keys = [
            str(task_key)
            for task_key in conflict.get("tasks") or []
            if str(task_key) in pending_by_key
        ]
        if len(keys) < 2:
            continue
        fingerprints = {
            key: planner_input_fingerprint(pending_by_key[key])
            for key in keys
        }
        entry = {
            "tasks": keys,
            "description": str(conflict.get("description", "") or ""),
            "suggestion": str(conflict.get("suggestion", "") or ""),
            "fingerprints": fingerprints,
        }
        for key in keys:
            conflict_entries_by_key.setdefault(key, []).append(entry)

    def _mutate(tasks: list[dict[str, Any]]) -> None:
        for task in tasks:
            task_key = str(task.get("key", "") or "")
            if task_key not in pending_keys:
                continue
            task["planner_fingerprint"] = planner_input_fingerprint(task)
            if task_key in conflict_entries_by_key:
                task["planner_conflicts"] = conflict_entries_by_key[task_key]
            else:
                task.pop("planner_conflicts", None)
            task.pop("blocked_by", None)
            task.pop("blocked_reason", None)
            if task.get("status") in ("conflict", "blocked"):
                task["status"] = "pending"
                task.pop("error", None)
                task.pop("error_code", None)
                task.pop("completed_at", None)

    mutate_and_recompute(tasks_file, _mutate)


def _print_planner_findings(pending: list[dict[str, Any]], tasks_file: Path) -> None:
    pending_by_key = {
        str(task.get("key", "")): task
        for task in pending
        if task.get("key")
    }
    persisted = {
        str(task.get("key", "")): task
        for task in load_tasks(tasks_file)
        if task.get("key") in pending_by_key
    }

    conflicts = [task for task in persisted.values() if task.get("status") == "conflict"]
    blocked = [task for task in persisted.values() if task.get("status") == "blocked"]
    if not conflicts and not blocked:
        return

    if conflicts:
        console.print("\n  [yellow]Planner detected conflicting tasks:[/yellow]")
        shown: set[str] = set()
        for task in conflicts:
            entries = task.get("planner_conflicts") or []
            for entry in entries:
                pair = "|".join(sorted(str(key) for key in entry.get("tasks") or []))
                if pair in shown:
                    continue
                shown.add(pair)
                labels = ", ".join(
                    f"#{pending_by_key[key].get('id', '?')}"
                    for key in entry.get("tasks") or []
                    if key in pending_by_key
                )
                console.print(
                    f"    [yellow]⚠[/yellow] {labels}  {rich_escape(str(entry.get('description', '') or 'incompatible tasks'))}"
                )
                if entry.get("suggestion"):
                    console.print(f"      [dim]{rich_escape(str(entry['suggestion'])[:160])}[/dim]")

    if blocked:
        console.print("  [yellow]Blocked tasks:[/yellow]")
        for task in blocked:
            console.print(
                f"    [yellow]⚠[/yellow] #{task.get('id', '?')}  {rich_escape(str(task.get('error', '') or 'blocked by conflict'))}"
            )


def _fallback_batch_spec(task: dict[str, Any], error: str) -> list[dict[str, Any]]:
    return [{
        "text": (
            "Implementation fulfills the original task prompt and integrates cleanly "
            f"with the merged codebase ({error or 'structured spec unavailable'})."
        ),
        "binding": "must",
    }]


async def _run_batch_qa(
    merged_tasks: list[dict],
    config: dict,
    project_dir: Path,
    tasks_file: Path,
    telemetry: Any,
    context: Any,
    *,
    pre_batch_sha: str | None = None,
    prior_tasks: list[dict] | None = None,
    focus_task_keys: set[str] | None = None,
    planner_analysis: list[dict[str, Any]] | None = None,
) -> dict:
    """Run combined QA on integrated codebase. Returns verdict."""
    if not merged_tasks:
        return {"must_passed": True, "verdict": {}, "raw_report": "", "failed_task_keys": [], "cost_usd": 0.0}

    tasks_with_specs: list[dict[str, Any]] = []
    spec_cost = 0.0
    spec_errors: list[str] = []
    spec_settings = config.get("spec_agent_settings", "project").split(",")

    from otto.spec import async_generate_spec

    async def _ensure_spec(task: dict[str, Any]) -> dict[str, Any]:
        nonlocal spec_cost
        if task.get("spec"):
            return task
        if config.get("skip_spec"):
            spec_errors.append(f"{task.get('key')}: skip_spec enabled")
            return {**task, "spec": _fallback_batch_spec(task, "skip_spec enabled")}

        log_dir = project_dir / "otto_logs" / task["key"]
        log_dir.mkdir(parents=True, exist_ok=True)
        spec_items, cost, error = await async_generate_spec(
            task.get("prompt", ""),
            project_dir,
            setting_sources=spec_settings,
            log_dir=log_dir,
        )
        spec_cost += cost
        if spec_items:
            try:
                update_task(tasks_file, task["key"], spec=spec_items)
            except Exception:
                pass
            return {**task, "spec": spec_items}

        spec_errors.append(f"{task.get('key')}: {error or 'structured spec unavailable'}")
        return {**task, "spec": _fallback_batch_spec(task, error or "structured spec unavailable")}

    tasks_with_specs = list(await asyncio.gather(*(_ensure_spec(task) for task in merged_tasks)))

    # Run-scoped batch QA directory (prevents overwriting between runs)
    import time as _time_mod
    batch_qa_ts = _time_mod.strftime("%Y%m%d-%H%M%S")
    log_dir = project_dir / "otto_logs" / f"batch-qa-{batch_qa_ts}"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Compute real diff so QA can see what changed (QA1 fix)
    if pre_batch_sha:
        from otto.git_ops import _get_diff_info
        diff_info = _get_diff_info(project_dir, pre_batch_sha)
        diff = diff_info.get("full_diff", "") or "(no diff available)"
    else:
        diff = "(pre-batch SHA not available — inspect repository state directly)"
    if planner_analysis:
        merged_keys = {str(task.get("key", "")) for task in merged_tasks if task.get("key")}
        merged_ids = {
            str(task.get("key", "")): task.get("id", "?")
            for task in merged_tasks
            if task.get("key")
        }
        lines = []
        for item in planner_analysis:
            task_a = str(item.get("task_a", "") or "")
            task_b = str(item.get("task_b", "") or "")
            if task_a not in merged_keys or task_b not in merged_keys:
                continue
            relation = str(item.get("relationship", "") or "")
            if relation == "INDEPENDENT":
                continue
            reason = str(item.get("reason", "") or "")
            lines.append(
                f"- Tasks #{merged_ids.get(task_a, '?')} and #{merged_ids.get(task_b, '?')}: "
                f"{relation} — {reason or 'planner relationship'}"
            )
        if lines:
            diff = (
                f"{diff}\n\nPlanner relationship analysis:\n"
                + "\n".join(lines)
            )

    # Prior tasks: include prompt + key must specs for cross-task awareness.
    # Only enough context for integration checking — NOT full spec lists
    # (full specs cause the agent to re-verify them, wasting time).
    if prior_tasks:
        from otto.tasks import spec_text, spec_binding
        prior_lines = []
        for t in prior_tasks:
            line = f"- #{t.get('id', '?')} {t.get('key', '?')[:8]}: {t.get('prompt', '')}"
            # Include first 2 must specs as contract summary
            specs = t.get("spec") or []
            must_specs = [spec_text(s) for s in specs if spec_binding(s) == "must"][:2]
            if must_specs:
                line += "\n    Key contracts: " + "; ".join(must_specs)
            prior_lines.append(line)
        diff = (
            f"{diff}\n\n"
            f"PRIOR TASKS (already verified — only check integration with current task, "
            f"do NOT re-verify their specs):\n"
            + "\n".join(prior_lines)
        )
    if focus_task_keys:
        qa_result = await run_qa(
            tasks_with_specs,
            config,
            project_dir,
            diff=diff,
            retried_task_keys=focus_task_keys,
            log_dir=log_dir,
        )
    else:
        def _batch_qa_progress(event_type: str, data: dict) -> None:
            """Display batch QA progress in terminal."""
            if event_type == "agent_tool":
                tool_name = data.get("name", "")
                detail = data.get("detail", "")[:60]
                console.print(f"        {tool_name}  {rich_escape(detail)}", style="dim")

        qa_result = await run_qa(
            tasks_with_specs,
            config,
            project_dir,
            diff=diff,
            on_progress=_batch_qa_progress,
            log_dir=log_dir,
        )
    total_cost = spec_cost + float(qa_result.get("cost_usd", 0.0) or 0.0)
    raw_report = qa_result.get("raw_report", "")
    if spec_errors:
        raw_report = (
            "[warning] Some task specs were regenerated with prompt-only fallback:\n"
            + "\n".join(f"- {msg}" for msg in spec_errors)
            + ("\n\n" + raw_report if raw_report else "")
        )

    return {
        **qa_result,
        "raw_report": raw_report,
        "cost_usd": total_cost,
    }


def _summarize_batch_qa_failure(batch_qa: dict[str, Any]) -> str:
    """Create a concise failure summary for merged tasks left pending QA."""
    verdict = batch_qa.get("verdict", {}) or {}
    for item in verdict.get("must_items", []) or []:
        if item.get("status") == "fail":
            task_key = item.get("task_key", "unknown")
            criterion = item.get("criterion", "")[:120]
            return f"batch QA failed for {task_key}: {criterion}" if criterion else f"batch QA failed for {task_key}"
    for item in verdict.get("integration_findings", []) or []:
        if item.get("status") == "fail":
            desc = item.get("description", "")[:120]
            return f"batch QA integration failure: {desc}" if desc else "batch QA integration failure"
    if batch_qa.get("test_suite_passed") is False:
        return "batch QA failed: full test suite regression check failed"
    raw_report = batch_qa.get("raw_report", "")
    for line in raw_report.splitlines():
        stripped = line.strip()
        if stripped and len(stripped) > 10 and not stripped.startswith("["):
            return f"batch QA failed: {stripped[:120]}"
    return "batch QA failed"


def _build_batch_qa_feedback(task_key: str, batch_qa: dict[str, Any]) -> str:
    """Build retry feedback for one task from a batch QA verdict."""
    verdict = batch_qa.get("verdict", {}) or {}
    lines = [
        "Batch QA found issues after your task was merged onto current main.",
        "Fix these issues without regressing the other merged tasks.",
    ]

    must_failures = [
        item for item in verdict.get("must_items", []) or []
        if item.get("status") == "fail" and item.get("task_key") == task_key
    ]
    integration_failures = [
        item for item in verdict.get("integration_findings", []) or []
        if item.get("status") == "fail" and task_key in (item.get("tasks_involved") or [])
    ]
    regressions = verdict.get("regressions", []) or []

    if must_failures:
        lines.append("")
        lines.append("Failed [must] items:")
        for item in must_failures:
            criterion = (item.get("criterion") or "unspecified criterion").strip()
            evidence = (item.get("evidence") or "").strip()
            proof = [str(entry).strip() for entry in (item.get("proof") or []) if str(entry).strip()]
            lines.append(f"- {criterion}")
            if evidence:
                lines.append(f"  evidence: {evidence[:400]}")
            if proof:
                lines.append(f"  proof: {'; '.join(proof[:3])[:400]}")

    if integration_failures:
        lines.append("")
        lines.append("Cross-task failures involving this task:")
        for item in integration_failures:
            desc = (item.get("description") or "integration failure").strip()
            test = (item.get("test") or "").strip()
            lines.append(f"- {desc}")
            if test:
                lines.append(f"  test: {test[:400]}")

    if regressions:
        lines.append("")
        lines.append("Regression notes:")
        for regression in regressions[:5]:
            lines.append(f"- {str(regression).strip()[:400]}")

    raw_report = (batch_qa.get("raw_report") or "").strip()
    if raw_report and not must_failures and not integration_failures:
        lines.append("")
        lines.append("QA report excerpt:")
        for line in raw_report.splitlines():
            line = line.strip()
            if line:
                lines.append(f"- {line[:400]}")
                if len(lines) >= 8:
                    break

    return "\n".join(lines)


def _combine_task_results(previous: TaskResult, current: TaskResult) -> TaskResult:
    """Carry forward prior cost/time when a task is retried within the same batch."""
    return TaskResult(
        task_key=current.task_key,
        success=current.success,
        commit_sha=current.commit_sha,
        worktree=current.worktree,
        cost_usd=(previous.cost_usd or 0.0) + (current.cost_usd or 0.0),
        error=current.error,
        error_code=current.error_code,
        qa_report=current.qa_report or previous.qa_report,
        diff_summary=current.diff_summary or previous.diff_summary,
        duration_s=(previous.duration_s or 0.0) + (current.duration_s or 0.0),
        review_ref=current.review_ref or previous.review_ref,
    )


def _current_head_sha(project_dir: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _rollback_main_to_sha(project_dir: Path, sha: str | None) -> bool:
    if not sha:
        return False
    result = subprocess.run(
        ["git", "reset", "--hard", sha],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print("  [red]Failed to roll back main after batch QA failure[/red]")
        return False
    return True


async def run_per(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """PER orchestrator entry point. Replaces run_piloted() for v4.

    Returns exit code (0=all passed, 1=any failed, 2=error).
    """
    default_branch = config["default_branch"]

    # Acquire process lock
    lock_path = git_meta_dir(project_dir) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("Another otto process is running", style="red")
        return 2

    # Clean up orphaned worktrees from previous crashed runs
    cleanup_orphaned_worktrees(project_dir)

    # Clean up stale task lock files
    tasks_lock = project_dir / ".tasks.lock"
    if tasks_lock.exists():
        try:
            tasks_lock.unlink()
        except OSError:
            pass

    # Set up context and telemetry
    context = PipelineContext()
    load_learnings(project_dir, context)
    log_dir = project_dir / "otto_logs"
    telemetry = Telemetry(log_dir)
    telemetry.enable_legacy_write()

    # Signal handling — clean up worktrees on interrupt
    def _signal_handler(signum, frame):
        if context.interrupted:
            console.print("\nForce exit — cleaning up worktrees", style="red")
            # Kill tracked subprocesses
            for pid in context.pids:
                try:
                    import os
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
            # Best-effort worktree cleanup on force exit
            try:
                cleanup_orphaned_worktrees(project_dir)
            except Exception:
                pass
            sys.exit(1)
        context.interrupted = True
        console.print("\n[yellow]Warning: Interrupted — finishing current task, will clean up worktrees[/yellow]")
        # Send SIGTERM to tracked subprocesses
        for pid in context.pids:
            try:
                import os
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    run_start = time.monotonic()

    # Run delimiter in logs for easier single-run review
    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _orchestrator_log(project_dir, f"\n{'='*60}", f"RUN START: {run_ts}", f"{'='*60}")
    from otto.observability import append_text_log as _append_log
    _append_log(
        project_dir / "otto_logs" / "planner.log",
        [f"\n{'='*60}", f"RUN START: {run_ts}", f"{'='*60}"],
    )

    try:
        # Step 1: Preflight checks
        error_code, pending = preflight_checks(config, tasks_file, project_dir)
        if error_code is not None:
            return error_code

        # Display task summary
        total_specs = sum(len(t.get("spec") or []) for t in pending)
        pending_keys = {t["key"] for t in pending}
        pending_by_key = {t["key"]: t for t in pending}
        task_ids = {t["key"]: t.get("id", 0) for t in pending}
        if config.get("skip_qa"):
            qa_mode = QAMode.SKIP
        elif len(pending) == 1:
            qa_mode = QAMode.PER_TASK
        else:
            qa_mode = QAMode.BATCH
        console.print()
        spec_label = f", [dim]{total_specs} specs[/dim]" if total_specs > 0 else ""
        console.print(
            f"  [bold]{len(pending)} task{'s' if len(pending) != 1 else ''}[/bold]"
            f"{spec_label}  [dim](v4.5 PER)[/dim]"
        )
        for t in pending:
            deps = t.get("depends_on", [])
            dep_str = f" [dim]\u2192 #{', #'.join(str(d) for d in deps)}[/dim]" if deps else ""
            spec_count = len(t.get("spec") or [])
            spec_str = f"({spec_count} spec)" if spec_count else "(spec at runtime)"
            console.print(
                f"    [dim]\u25cb[/dim] [bold]#{t['id']}[/bold]  "
                f"{rich_escape(t.get('prompt', '')[:55])}  [dim]{spec_str}{dep_str}[/dim]"
            )
        console.print()
        console.print(f"{'─' * 60}", style="dim")

        # Step 2: Plan
        console.print("  Planning...", style="dim")
        execution_plan = await plan(pending, config, project_dir)
        if not _plan_covers_pending(execution_plan, pending):
            console.print("  [yellow]Planner returned invalid task coverage; falling back to serial plan[/yellow]")
            execution_plan = serial_plan(pending)
            _orchestrator_log(
                project_dir,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] replan",
                "replan triggered: planner coverage invalid; fell back to serial plan",
                f"new structure: {_summarize_batch_structure(execution_plan)}",
                "",
            )

        _apply_planner_outcomes(tasks_file, pending, execution_plan)
        _print_planner_findings(pending, tasks_file)

        telemetry.log(PlanCreated(
            total_batches=len(execution_plan.batches),
            total_tasks=execution_plan.total_tasks,
        ))
        console.print(f"  [dim]Plan: {len(execution_plan.batches)} batch(es), {execution_plan.total_tasks} tasks[/dim]")

        # Step 3: PER loop
        max_parallel = config.get("max_parallel", 1) or 1
        batch_idx = 0
        post_run_suite_failed = False
        post_run_suite_output = ""
        while not execution_plan.is_empty and not context.interrupted:
            batch = execution_plan.batches[0]
            batch_idx += 1
            batch_size = len(batch.tasks)
            abort_after_batch = False

            use_parallel = max_parallel > 1 and batch_size > 1
            if batch_size > 1:
                mode_label = "parallel" if use_parallel else "sequential"
                console.print(f"\n  [bold]Batch {batch_idx}[/bold]  [dim]{batch_size} tasks ({mode_label})[/dim]")
            else:
                console.print(f"\n  [bold]Batch {batch_idx}[/bold]  [dim]1 task[/dim]")
            _orchestrator_log(
                project_dir,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] batch {batch_idx}",
                *_format_batch_structure(batch, pending_by_key, mode="parallel" if use_parallel else "serial"),
            )

            if use_parallel:
                # Build sibling context for each task in the batch
                sib_ctxs = {
                    tp.task_key: _build_sibling_context(
                        tp.task_key, batch, pending_by_key, execution_plan.analysis,
                    )
                    for tp in batch.tasks
                }
                batch_results = await _run_batch_parallel(
                    batch, context, config, project_dir, telemetry, tasks_file,
                    max_parallel=max_parallel,
                    qa_mode=qa_mode,
                    sibling_contexts=sib_ctxs,
                )
            else:
                # Serial execution — each task gets its own worktree
                base_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project_dir, capture_output=True, text=True, check=True,
                ).stdout.strip()
                batch_results: list[TaskResult] = []
                for task_plan in batch.tasks:
                    sibling_ctx = _build_sibling_context(
                        task_plan.task_key, batch, pending_by_key, execution_plan.analysis,
                    )
                    result = await _run_task_in_worktree(
                        task_plan, context, config, project_dir, telemetry, tasks_file,
                        base_sha, qa_mode=qa_mode, sibling_context=sibling_ctx,
                    )
                    batch_results.append(result)
                    if context.interrupted:
                        break

            pre_batch_sha = None
            if qa_mode == QAMode.BATCH and not context.interrupted:
                pre_batch_sha = _current_head_sha(project_dir)

            if not context.interrupted:
                batch_results = merge_batch_results(
                    batch_results, config, project_dir, tasks_file, telemetry,
                    qa_mode=qa_mode,
                )

                merge_failed = [
                    r for r in batch_results
                    if not r.success and r.error_code in ("merge_conflict", "post_merge_test_fail")
                ]
                if merge_failed:
                    console.print(
                        f"\n  [yellow]Re-applying {len(merge_failed)} merge-failed task(s) on updated main...[/yellow]"
                    )
                    for failed_result in merge_failed:
                        if context.interrupted:
                            break
                        fkey = failed_result.task_key
                        tp = next((t for t in batch.tasks if t.task_key == fkey), None)
                        if not tp:
                            continue

                        # Get full diff from the candidate ref
                        from otto.git_ops import _find_best_candidate_ref
                        candidate_ref = _find_best_candidate_ref(project_dir, fkey)
                        full_diff = ""
                        if candidate_ref:
                            base_result = subprocess.run(
                                ["git", "rev-parse", f"{candidate_ref}^"],
                                cwd=project_dir, capture_output=True, text=True,
                            )
                            if base_result.returncode == 0:
                                diff_result = subprocess.run(
                                    ["git", "diff", f"{base_result.stdout.strip()}..{candidate_ref}"],
                                    cwd=project_dir, capture_output=True, text=True,
                                )
                                if diff_result.returncode == 0:
                                    full_diff = diff_result.stdout.strip()

                        # One path for all merge failures: coding_loop with
                        # intelligent feedback. The agent applies the diff first
                        # (cheap/fast), explores more if needed (expensive but smart).
                        error_type = failed_result.error_code or "merge_failed"
                        diff_stat = failed_result.diff_summary or ""

                        if error_type == "post_merge_test_fail":
                            merge_feedback = (
                                "MERGE CONFLICT CONTEXT (read this before coding):\n\n"
                                "Your previous implementation for this task was verified and passed "
                                "all tests. It merged cleanly onto main, but tests FAILED on the "
                                "integrated codebase — another task's code is now on main and "
                                "conflicts semantically with yours.\n\n"
                                "Strategy:\n"
                                "1. Read the diff below to understand what YOU previously implemented\n"
                                "2. Read the current codebase to see the OTHER task's changes\n"
                                "3. Re-apply your changes, adapting where needed to work with both\n"
                                "4. Run tests to verify the integration works\n\n"
                                f"Files you previously changed:\n{diff_stat}\n\n"
                                f"Your previous implementation (full diff):\n{full_diff or '(not available)'}"
                            )
                        else:
                            merge_feedback = (
                                "MERGE CONFLICT CONTEXT (read this before coding):\n\n"
                                "Your previous implementation for this task was verified and passed "
                                "all tests, but caused a git merge conflict with another task's "
                                "changes that are now on main.\n\n"
                                "Strategy:\n"
                                "1. Read the diff below to understand what YOU previously implemented\n"
                                "2. Read the current files on main to see the OTHER task's changes\n"
                                "3. Apply your changes using Edit, adapting to avoid conflicts\n"
                                "4. Do NOT start from scratch — use the diff as your guide\n"
                                "5. Run tests to verify everything works together\n\n"
                                f"Files you previously changed:\n{diff_stat}\n\n"
                                f"Your previous implementation (full diff):\n{full_diff or '(not available)'}"
                            )

                        _orchestrator_log(
                            project_dir,
                            f"merge retry: {fkey[:8]} error={error_type} → coding_loop with full diff feedback",
                        )
                        try:
                            update_task(
                                tasks_file, fkey,
                                status="pending",
                                error=None, error_code=None, session_id=None,
                                feedback=merge_feedback,
                            )
                        except Exception:
                            continue
                        retry_base_sha = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=project_dir, capture_output=True, text=True, check=True,
                        ).stdout.strip()
                        retry_result = await _run_task_in_worktree(
                            tp, context, config, project_dir,
                            telemetry, tasks_file, retry_base_sha,
                            qa_mode=qa_mode,
                        )
                        if retry_result.success:
                            rerun_merged = merge_batch_results(
                                [retry_result], config, project_dir, tasks_file, telemetry,
                                qa_mode=qa_mode,
                            )
                            retry_result = rerun_merged[0]
                        batch_results = [
                            retry_result if r.task_key == fkey else r
                            for r in batch_results
                        ]

                # Post-batch deterministic test suite: run once on integrated HEAD
                # instead of N per-task post-merge tests. Catches integration failures
                # (e.g., task A renames module, task B uses old name — merge succeeds
                # but tests fail). Only for BATCH mode with 2+ merged tasks.
                # Hard gate: if tests fail, rollback the batch.
                if qa_mode == QAMode.BATCH and not context.interrupted and not config.get("skip_test"):
                    merged_count = sum(1 for r in batch_results if r.success)
                    if merged_count >= 2:
                        test_command = config.get("test_command")
                        if test_command:
                            from otto.testing import run_test_suite as _run_suite
                            _test_timeout = config.get("verify_timeout", 300)
                            console.print("\n  [bold]Post-batch integration test[/bold]")
                            _orchestrator_log(project_dir, f"post-batch integration test: {merged_count} merged tasks")
                            _pb_result = _run_suite(
                                project_dir=project_dir,
                                candidate_sha="HEAD",
                                test_command=test_command,
                                custom_test_cmd=None,
                                timeout=_test_timeout,
                            )
                            if _pb_result.passed:
                                console.print("    [green]\u2713[/green] integration tests passed")
                                _orchestrator_log(project_dir, "post-batch integration test: PASSED")
                            else:
                                console.print("    [red]\u2717[/red] integration tests failed — rolling back batch")
                                _orchestrator_log(
                                    project_dir,
                                    f"post-batch integration test: FAILED — rollback\n{(_pb_result.failure_output or '')[:500]}",
                                )
                                # Hard gate: rollback all merged tasks and abort batch
                                _rollback_main_to_sha(project_dir, pre_batch_sha)
                                for r in batch_results:
                                    if r.success:
                                        try:
                                            update_task(tasks_file, r.task_key, status="pending",
                                                        error=None, error_code=None, feedback=None)
                                        except Exception:
                                            pass
                                batch_results = [
                                    TaskResult(
                                        task_key=r.task_key,
                                        success=False,
                                        error="post-batch integration test failed",
                                        error_code="post_batch_test_fail",
                                        cost_usd=r.cost_usd,
                                        duration_s=r.duration_s,
                                    ) if r.success else r
                                    for r in batch_results
                                ]

                if qa_mode == QAMode.BATCH and not context.interrupted:
                    batch_keys = {tp.task_key for tp in batch.tasks}
                    batch_qa_costs: dict[str, float] = {}
                    qa_reports_by_task: dict[str, str] = {}
                    # Load current batch's merged tasks for QA verification
                    all_persisted = load_tasks(tasks_file)
                    persisted_tasks = {
                        task["key"]: task
                        for task in all_persisted
                        if task.get("key") in batch_keys
                    }
                    merged_tasks = [
                        task for task in persisted_tasks.values()
                        if task.get("status") == "merged"
                    ]
                    # QA2 fix: include prior batches' tasks as context
                    # (not for re-verification, but for cross-task awareness)
                    prior_tasks = [
                        task for task in all_persisted
                        if task.get("key") in pending_keys
                        and task.get("key") not in batch_keys
                        and task.get("status") in ("passed", "merged")
                    ]
                    if merged_tasks:
                        merged_keys = {task["key"] for task in merged_tasks}
                        context_label = f" + {len(prior_tasks)} prior" if prior_tasks else ""
                        console.print(f"\n  [bold]Batch QA[/bold]  [dim]{len(merged_tasks)} merged task(s){context_label}[/dim]")
                        _orchestrator_log(
                            project_dir,
                            f"batch QA: triggered for {len(merged_tasks)} merged task(s)",
                        )
                        batch_qa = await _run_batch_qa(
                            merged_tasks, config, project_dir, tasks_file, telemetry, context,
                            pre_batch_sha=pre_batch_sha,
                            prior_tasks=prior_tasks if prior_tasks else None,
                            planner_analysis=execution_plan.analysis,
                        )
                        initial_share = float(batch_qa.get("cost_usd", 0.0) or 0.0) / max(len(merged_tasks), 1)
                        for key in merged_keys:
                            batch_qa_costs[key] = batch_qa_costs.get(key, 0.0) + initial_share
                            qa_reports_by_task[key] = batch_qa.get("raw_report", "")

                        final_batch_qa = batch_qa
                        max_qa_retries = config.get("max_retries", 3)
                        for qa_retry_round in range(max_qa_retries):
                            if context.interrupted:
                                break
                            if final_batch_qa.get("must_passed") or final_batch_qa.get("infrastructure_error"):
                                break

                            qa_failed_keys = set(final_batch_qa.get("failed_task_keys") or [])
                            if not qa_failed_keys:
                                qa_failed_keys = set(merged_keys)
                            failure_summary = _summarize_batch_qa_failure(final_batch_qa)

                            console.print(
                                f"\n  [bold]Batch QA Retry[/bold]  [dim]round {qa_retry_round + 1}/{max_qa_retries}, "
                                f"{len(qa_failed_keys)} failed task(s)[/dim]"
                            )
                            _orchestrator_log(
                                project_dir,
                                f"batch QA retry round {qa_retry_round + 1}/{max_qa_retries}: "
                                f"{len(qa_failed_keys)} failed task(s): {sorted(qa_failed_keys)}",
                            )

                            for fkey in qa_failed_keys:
                                feedback = _build_batch_qa_feedback(fkey, final_batch_qa)
                                try:
                                    update_task(
                                        tasks_file,
                                        fkey,
                                        status="merged",
                                        error=failure_summary,
                                        error_code="batch_qa_failed",
                                        feedback=feedback,
                                        session_id=None,
                                    )
                                except Exception:
                                    pass

                            retried_keys: set[str] = set()
                            for fkey in sorted(qa_failed_keys):
                                if context.interrupted:
                                    break
                                task_plan = next((t for t in batch.tasks if t.task_key == fkey), None)
                                previous_result = next((r for r in batch_results if r.task_key == fkey), None)
                                if not task_plan or previous_result is None:
                                    continue

                                feedback = _build_batch_qa_feedback(fkey, final_batch_qa)
                                try:
                                    update_task(
                                        tasks_file,
                                        fkey,
                                        status="pending",
                                        error=None,
                                        error_code=None,
                                        feedback=feedback,
                                        session_id=None,
                                    )
                                except Exception:
                                    pass

                                qa_retry_base_sha = subprocess.run(
                                    ["git", "rev-parse", "HEAD"],
                                    cwd=project_dir, capture_output=True, text=True, check=True,
                                ).stdout.strip()
                                raw_retry_result = await _run_task_in_worktree(
                                    task_plan,
                                    context,
                                    config,
                                    project_dir,
                                    telemetry,
                                    tasks_file,
                                    qa_retry_base_sha,
                                    qa_mode=qa_mode,
                                )
                                retry_result = _combine_task_results(previous_result, raw_retry_result)
                                if raw_retry_result.success:
                                    rerun_merged = merge_batch_results(
                                        [raw_retry_result], config, project_dir, tasks_file, telemetry,
                                        qa_mode=qa_mode,
                                    )
                                    retry_result = _combine_task_results(previous_result, rerun_merged[0])
                                    retried_keys.add(fkey)
                                else:
                                    try:
                                        update_task(
                                            tasks_file,
                                            fkey,
                                            status="failed",
                                            error=retry_result.error,
                                            error_code=retry_result.error_code,
                                        )
                                    except Exception:
                                        pass

                                batch_results = [
                                    retry_result if r.task_key == fkey else r
                                    for r in batch_results
                                ]

                            persisted_tasks = {
                                task["key"]: task
                                for task in load_tasks(tasks_file)
                                if task.get("key") in batch_keys
                            }
                            retried_merged_keys = {
                                key for key in retried_keys
                                if persisted_tasks.get(key, {}).get("status") == "merged"
                            }
                            if retried_merged_keys and not context.interrupted:
                                merged_tasks = [
                                    task for task in persisted_tasks.values()
                                    if task.get("status") == "merged"
                                ]
                                _orchestrator_log(
                                    project_dir,
                                    f"batch QA re-verify: {len(retried_merged_keys)} retried task(s)",
                                )
                                final_batch_qa = await _run_batch_qa(
                                    merged_tasks,
                                    config,
                                    project_dir,
                                    tasks_file,
                                    telemetry,
                                    context,
                                    pre_batch_sha=pre_batch_sha,
                                    prior_tasks=prior_tasks if prior_tasks else None,
                                    focus_task_keys=retried_merged_keys,
                                    planner_analysis=execution_plan.analysis,
                                )
                                retry_share = float(final_batch_qa.get("cost_usd", 0.0) or 0.0) / max(len(retried_merged_keys), 1)
                                for key in retried_merged_keys:
                                    batch_qa_costs[key] = batch_qa_costs.get(key, 0.0) + retry_share
                                    qa_reports_by_task[key] = final_batch_qa.get("raw_report", "")

                        persisted_tasks = {
                            task["key"]: task
                            for task in load_tasks(tasks_file)
                            if task.get("key") in batch_keys
                        }
                        rollback_pending_keys: set[str] = set()
                        final_failed_keys: set[str] = {
                            result.task_key for result in batch_results if not result.success
                        }
                        final_failure_summary = None
                        if final_batch_qa.get("infrastructure_error"):
                            rollback_pending_keys = {
                                key for key, task in persisted_tasks.items()
                                if task.get("status") == "merged"
                            }
                            _rollback_main_to_sha(project_dir, pre_batch_sha)
                            _orchestrator_log(
                                project_dir,
                                "rollback: main reset to pre-batch SHA due to batch QA infrastructure error",
                            )
                            for task_key in rollback_pending_keys:
                                try:
                                    update_task(
                                        tasks_file,
                                        task_key,
                                        status="pending",
                                        error=None,
                                        error_code=None,
                                        feedback=None,
                                        session_id=None,
                                    )
                                except Exception:
                                    pass
                            abort_after_batch = True
                            console.print(
                                "  [yellow]Batch QA infrastructure error; rolled back merged batch and stopped the run[/yellow]"
                            )
                        elif not final_batch_qa.get("must_passed"):
                            final_failed_keys.update(final_batch_qa.get("failed_task_keys") or [])
                            if not final_failed_keys:
                                final_failed_keys = set(merged_keys)
                            final_failure_summary = _summarize_batch_qa_failure(final_batch_qa)
                            rollback_pending_keys = {
                                key for key, task in persisted_tasks.items()
                                if task.get("status") == "merged"
                                and key not in final_failed_keys
                            }
                            _rollback_main_to_sha(project_dir, pre_batch_sha)
                            _orchestrator_log(
                                project_dir,
                                f"rollback: main reset to pre-batch SHA because {final_failure_summary or 'batch QA failed'}",
                            )
                            for task_key in rollback_pending_keys:
                                try:
                                    update_task(
                                        tasks_file,
                                        task_key,
                                        status="pending",
                                        error=None,
                                        error_code=None,
                                        feedback=None,
                                        session_id=None,
                                    )
                                except Exception:
                                    pass
                            # Don't abort — continue with remaining batches (best effort).
                            # Replan will account for failed tasks when scheduling later batches.
                            console.print(
                                f"  [yellow]Batch QA rejected after {max_qa_retries} retries; "
                                f"rolled back batch, continuing with remaining batches[/yellow]"
                            )
                        _orchestrator_log(
                            project_dir,
                            "batch QA result: "
                            + (
                                "infrastructure error"
                                if final_batch_qa.get("infrastructure_error")
                                else ("pass" if final_batch_qa.get("must_passed") else (final_failure_summary or "fail"))
                            ),
                            "",
                        )
                        for task_key in merged_keys:
                            if task_key in final_failed_keys:
                                final_result = next((r for r in batch_results if r.task_key == task_key), None)
                                retry_failed = bool(final_result) and not final_result.success and final_result.error_code not in (None, "batch_qa_failed")
                                feedback = _build_batch_qa_feedback(task_key, final_batch_qa)
                                try:
                                    update_task(
                                        tasks_file,
                                        task_key,
                                        status="failed",
                                        error=(
                                            final_result.error
                                            if retry_failed and final_result and final_result.error
                                            else final_failure_summary or next(
                                                (r.error for r in batch_results if r.task_key == task_key and r.error),
                                                "batch QA failed",
                                            )
                                        ),
                                        error_code=(
                                            final_result.error_code
                                            if retry_failed and final_result
                                            else "batch_qa_failed"
                                        ),
                                        feedback=feedback,
                                    )
                                except Exception:
                                    pass
                            elif task_key in rollback_pending_keys:
                                try:
                                    update_task(
                                        tasks_file,
                                        task_key,
                                        status="pending",
                                        error=None,
                                        error_code=None,
                                        feedback=None,
                                    )
                                except Exception:
                                    pass
                            else:
                                try:
                                    update_task(
                                        tasks_file,
                                        task_key,
                                        status="passed",
                                        error=None,
                                        error_code=None,
                                        feedback=None,
                                    )
                                except Exception:
                                    pass
                                # Stamp commit SHA now that batch QA confirmed this task passed
                                commit_sha = next(
                                    (r.commit_sha for r in batch_results if r.task_key == task_key and r.commit_sha),
                                    None,
                                )
                                if commit_sha:
                                    _stamp_proof_commit_sha(project_dir, task_key, commit_sha)

                        batch_results = [
                            TaskResult(
                                task_key=result.task_key,
                                success=(
                                    result.success
                                    and result.task_key not in final_failed_keys
                                    and result.task_key not in rollback_pending_keys
                                ),
                                commit_sha=result.commit_sha,
                                worktree=result.worktree,
                                cost_usd=result.cost_usd + batch_qa_costs.get(result.task_key, 0.0),
                                error=(
                                    "batch QA infrastructure error"
                                    if final_batch_qa.get("infrastructure_error")
                                    and result.task_key in rollback_pending_keys
                                    else (
                                        "batch rolled back after batch QA failure"
                                        if result.task_key in rollback_pending_keys
                                        else (
                                            final_failure_summary
                                            if result.task_key in final_failed_keys and result.success
                                            else result.error
                                        )
                                    )
                                ),
                                error_code=(
                                    "batch_qa_infrastructure_error"
                                    if final_batch_qa.get("infrastructure_error")
                                    and result.task_key in rollback_pending_keys
                                    else (
                                        "batch_qa_rolled_back"
                                        if result.task_key in rollback_pending_keys
                                        else (
                                            "batch_qa_failed"
                                            if result.task_key in final_failed_keys
                                            and (result.success or result.error_code == "batch_qa_failed")
                                            else result.error_code
                                        )
                                    )
                                ),
                                qa_report=qa_reports_by_task.get(result.task_key, result.qa_report),
                                diff_summary=result.diff_summary,
                                duration_s=result.duration_s,
                                review_ref=result.review_ref,
                            )
                            for result in batch_results
                        ]
                    else:
                        _orchestrator_log(
                            project_dir,
                            "batch QA: skipped (no merged tasks in batch)",
                            "",
                        )
                else:
                    reason = "interrupted" if context.interrupted else f"mode={qa_mode}"
                    _orchestrator_log(
                        project_dir,
                        f"batch QA: skipped ({reason})",
                        "",
                    )

            # Process results (TaskMerged/TaskFailed already emitted by coding_loop)
            batch_passed = 0
            batch_failed = 0
            for result in batch_results:
                if result.success:
                    context.add_success(result)
                    batch_passed += 1
                else:
                    context.add_failure(result)
                    batch_failed += 1

            # Persist any new learnings after each batch
            persist_learnings(project_dir, context)

            # Update product context for successful tasks (i2p shared context)
            context_path = project_dir / "context.md"
            if context_path.exists() or len(pending) >= 3:
                for result in batch_results:
                    if result.success:
                        try:
                            from otto.product_context import update_product_context
                            task = pending_by_key.get(result.task_key, {})
                            # Get git diff for context update
                            diff_text = result.diff_summary or ""
                            if result.commit_sha:
                                try:
                                    import subprocess as sp
                                    diff_result = sp.run(
                                        ["git", "diff", f"{result.commit_sha}~1..{result.commit_sha}", "--stat"],
                                        cwd=str(project_dir), capture_output=True, text=True, timeout=10,
                                    )
                                    if diff_result.returncode == 0:
                                        diff_text = diff_result.stdout
                                except Exception:
                                    pass
                            await update_product_context(
                                project_dir=project_dir,
                                task_key=result.task_key,
                                task_prompt=str(task.get("prompt", "")),
                                diff=diff_text,
                                config=config,
                            )
                        except Exception as exc:
                            _orchestrator_log(
                                project_dir,
                                f"product context update failed for {result.task_key}: {exc}",
                            )

                # Commit context.md so worktree-based tasks can see it
                if context_path.exists():
                    try:
                        import subprocess as sp
                        sp.run(["git", "add", "context.md"], cwd=str(project_dir),
                               capture_output=True, timeout=5)
                        sp.run(["git", "commit", "-m", "otto: update product context",
                                "--allow-empty"], cwd=str(project_dir),
                               capture_output=True, timeout=5)
                    except Exception:
                        pass

            telemetry.log(BatchCompleted(
                batch_index=batch_idx - 1,
                tasks_passed=batch_passed,
                tasks_failed=batch_failed,
            ))

            pass_str = f"[green]{batch_passed} passed[/green]"
            fail_str = f"[red]{batch_failed} failed[/red]" if batch_failed else f"{batch_failed} failed"
            console.print(f"  Batch {batch_idx}: {pass_str}, {fail_str}")

            if abort_after_batch:
                break

            # Remove completed batch — only remove tasks that are truly done
            # (passed or permanently failed). Rolled-back tasks stay in the plan
            # so replan can re-schedule them in later batches.
            completed_keys = {
                r.task_key for r in batch_results
                if r.success or r.error_code not in ("batch_qa_rolled_back", "batch_qa_infrastructure_error")
            }
            execution_plan = execution_plan.remaining_after(completed_keys)

            # Track which tasks failed permanently vs were rolled back
            batch_failed_keys = {
                r.task_key for r in batch_results
                if not r.success and r.error_code not in ("batch_qa_rolled_back", "batch_qa_infrastructure_error")
            }
            batch_rolled_back_keys = {
                r.task_key for r in batch_results
                if r.error_code in ("batch_qa_rolled_back", "batch_qa_infrastructure_error")
            }
            all_failed_keys = {
                key for key, result in context.results.items() if not result.success
            }

            if not execution_plan.is_empty and batch_failed > 0 and not context.interrupted:
                # Replan with accumulated context
                console.print("  Replanning...", style="dim")
                remaining_plan = execution_plan
                remaining_pending = [
                    pending_by_key[task_plan.task_key]
                    for remaining_batch in remaining_plan.batches
                    for task_plan in remaining_batch.tasks
                    if task_plan.task_key in pending_by_key
                ]
                replanned = await replan(
                    context, remaining_plan, config, project_dir,
                    failed_keys=all_failed_keys,
                    rolled_back_keys=batch_rolled_back_keys,
                    pending_by_key=pending_by_key,
                )
                if _plan_covers_pending(replanned, remaining_pending):
                    execution_plan = replanned
                    _orchestrator_log(
                        project_dir,
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] replan",
                        "replan triggered after batch failures",
                        f"old structure: {_summarize_batch_structure(remaining_plan)}",
                        f"new structure: {_summarize_batch_structure(replanned)}",
                        "",
                    )
                else:
                    console.print("  [yellow]Replan returned invalid task coverage; keeping existing remaining plan[/yellow]")
                    execution_plan = remaining_plan
                    _orchestrator_log(
                        project_dir,
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] replan",
                        "replan triggered after batch failures but was rejected due to invalid coverage",
                        f"kept structure: {_summarize_batch_structure(remaining_plan)}",
                        "",
                    )

        # Step 4: Post-run test suite
        # Final deterministic check on integrated HEAD. Runs for --no-qa mode
        # (only integration gate) and is redundant but harmless for other modes
        # since per-batch integration tests already ran above.
        total_passed = context.passed_count
        if (
            qa_mode == QAMode.SKIP and
            total_passed >= 2
            and context.failed_count == 0
            and not config.get("skip_test")
            and not context.interrupted
        ):
            from otto.testing import run_test_suite
            test_command = config.get("test_command")
            test_timeout = config.get("verify_timeout", 300)
            if test_command:
                console.print("\n  [bold]Post-run test suite[/bold]")
                final_test_result = run_test_suite(
                    project_dir=project_dir,
                    candidate_sha="HEAD",
                    test_command=test_command,
                    custom_test_cmd=None,
                    timeout=test_timeout,
                )
                if final_test_result.passed:
                    console.print("    [green]\u2713[/green] test suite passed")
                else:
                    failure_output = final_test_result.failure_output or "post-run test suite failed"
                    post_run_suite_failed = True
                    post_run_suite_output = failure_output[:500]
                    console.print(
                        "    [red]\u2717[/red] post-run test suite failed"
                    )

        # Step 5: Summary
        run_duration = time.monotonic() - run_start
        final_tasks = load_tasks(tasks_file)
        terminal_nonexecuted = {
            str(task.get("key", ""))
            for task in final_tasks
            if task.get("key") in pending_keys
            and task.get("status") in ("conflict", "blocked")
        }
        missing_keys = pending_keys - set(context.results) - terminal_nonexecuted

        # AllDone is intentionally task-scoped; run-level post-run suite failures
        # are surfaced via the summary, run history, and exit code instead.
        telemetry.log(AllDone(
            total_passed=context.passed_count,
            total_failed=context.failed_count + len(terminal_nonexecuted),
            total_missing_or_interrupted=len(missing_keys),
            total_cost=context.total_cost,
            total_duration_s=run_duration,
        ))

        # Build results list for _print_summary
        pending_keys = {t["key"] for t in pending}
        results: list[tuple[dict, bool]] = []
        for t in final_tasks:
            if t.get("key") in pending_keys:
                result_obj = context.results.get(t.get("key"))
                passed = t.get("status") == "passed" or (
                    bool(result_obj and result_obj.success)
                    and t.get("status") not in ("failed", "merge_failed", "conflict", "blocked")
                )
                results.append((t, passed))

        try:
            _print_summary(
                results,
                run_duration,
                integration_passed=not post_run_suite_failed,
                total_cost=context.total_cost,
                project_dir=project_dir,
            )
        except Exception as exc:
            console.print(f"  [yellow]Warning: summary display error: {exc}[/yellow]")

        if post_run_suite_failed:
            console.print(
                "  [yellow]Run exited non-zero due to post-run test suite failure[/yellow]"
            )
            if post_run_suite_output:
                console.print(f"  [dim]{rich_escape(post_run_suite_output)}[/dim]")

        # Record run history
        _record_run_history(
            project_dir,
            results,
            run_duration,
            context.total_cost,
            post_run_suite_failed=post_run_suite_failed,
            post_run_suite_output=post_run_suite_output,
        )

        return 1 if (
            context.failed_count > 0
            or terminal_nonexecuted
            or post_run_suite_failed
            or context.interrupted
            or missing_keys
        ) else 0

    finally:
        # Clean up any leftover worktrees from parallel execution
        cleanup_orphaned_worktrees(project_dir)
        # Ensure we're back on default branch
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True,
        )
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def merge_batch_results(
    results: list[TaskResult],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path,
    telemetry: Any,
    qa_mode: str = QAMode.PER_TASK,
) -> list[TaskResult]:
    """Serial merge phase: merge verified candidates onto main sequentially.

    For each verified task (sorted by task_key for determinism):
      1. Merge the candidate ref onto current main HEAD
      2. If conflict -> mark merge_failed
      3. Run test suite on the merged result (post-merge verification)
      4. If tests fail -> mark merge_failed
      5. Fast-forward main to the new commit
      6. Mark task merged/passed based on QA mode

    Returns a new list of TaskResults with updated success/error fields.
    """
    from otto.git_ops import (
        merge_candidate,
        _find_best_candidate_ref,
    )
    from otto.testing import run_test_suite
    from otto.config import detect_test_command

    default_branch = config["default_branch"]
    test_timeout = config["verify_timeout"]
    test_command = config.get("test_command") or detect_test_command(project_dir)

    # Ensure we're on default branch
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, check=True,
    )

    tasks_by_key = {task.get("key"): task for task in load_tasks(tasks_file)}

    # Separate verified (needs merge) from already-passed and failed results.
    verified_results = [
        r for r in results
        if r.success and tasks_by_key.get(r.task_key, {}).get("status") == "verified"
    ]
    passthrough_results = [
        r for r in results
        if not (r.success and tasks_by_key.get(r.task_key, {}).get("status") == "verified")
    ]

    # Sort verified tasks by key for deterministic merge order
    verified_results.sort(key=lambda r: r.task_key)

    if not verified_results:
        return results  # nothing to merge

    console.print()
    console.print(f"  [bold]Merging[/bold]  [dim]{len(verified_results)} verified tasks[/dim]")

    merged_results: list[TaskResult] = list(passthrough_results)

    for result in verified_results:
        task_key = result.task_key
        task_meta = tasks_by_key.get(task_key, {})
        task_id = task_meta.get("id", 0)
        custom_test_cmd = task_meta.get("verify")
        _orchestrator_log(
            project_dir,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] merge attempt",
            f"task={task_key} status=started qa_mode={qa_mode}",
        )

        try:
            update_task(tasks_file, task_key, status="merge_pending")
        except Exception:
            pass

        # Find the best candidate ref for this task
        candidate_ref = _find_best_candidate_ref(project_dir, task_key)
        if not candidate_ref:
            if qa_mode == QAMode.BATCH and (result.diff_summary or "").startswith("No changes needed"):
                try:
                    update_task(tasks_file, task_key, status="merged", error=None, error_code=None, feedback=None)
                except Exception:
                    pass
                merged_results.append(TaskResult(
                    task_key=task_key, success=True,
                    commit_sha=None,
                    cost_usd=result.cost_usd,
                    duration_s=result.duration_s,
                    diff_summary=result.diff_summary,
                    qa_report=result.qa_report,
                ))
                _orchestrator_log(project_dir, f"task={task_key} merge result=skip no-changes-batch")
                continue
            console.print(f"    [red]\u2717[/red] #{task_key[:8]}  no candidate ref found")
            error = "merge_failed: no candidate ref"
            try:
                update_task(tasks_file, task_key, status="merge_failed", error=error)
            except Exception:
                pass
            merged_results.append(TaskResult(
                task_key=task_key, success=False,
                error_code="merge_failed",
                error=error, cost_usd=result.cost_usd,
                duration_s=result.duration_s,
            ))
            _orchestrator_log(project_dir, f"task={task_key} merge result=fail reason=no candidate ref")
            continue

        # Merge the candidate onto a temp branch rooted at the current main
        # HEAD, but do not fast-forward main until verification passes.
        success, new_sha = merge_candidate(
            project_dir, candidate_ref, default_branch,
        )
        if not success:
            console.print(f"    [yellow]⚠[/yellow] #{task_key[:8]}  merge conflict — queued for re-apply")
            error = "merge_conflict: will re-apply on updated main"
            try:
                update_task(tasks_file, task_key, status="merge_failed",
                            error=error, error_code="merge_conflict")
            except Exception:
                pass
            merged_results.append(TaskResult(
                task_key=task_key, success=False,
                error_code="merge_conflict",
                error=error, cost_usd=result.cost_usd,
                duration_s=result.duration_s,
                diff_summary=result.diff_summary,
                qa_report=result.qa_report,
            ))
            _orchestrator_log(project_dir, f"task={task_key} merge result=conflict")
            continue

        # Per-task post-merge test: only in --no-qa mode where it's the sole
        # integration gate. In BATCH mode, one post-batch suite replaces N per-task
        # runs. In PER_TASK single-task mode, the task's own test phase already
        # verified this exact code.
        if qa_mode == QAMode.SKIP and not config.get("skip_test"):
            test_result = run_test_suite(
                project_dir=project_dir,
                candidate_sha=new_sha,
                test_command=test_command,
                custom_test_cmd=custom_test_cmd,
                timeout=test_timeout,
            )
            if not test_result.passed:
                failure_output = test_result.failure_output or "post-merge tests failed"
                console.print(
                    f"    [red]\u2717[/red] #{task_key[:8]}  post-merge test failure"
                )
                error = f"merge_failed: post-merge tests failed\n{failure_output[:500]}"
                try:
                    update_task(tasks_file, task_key, status="merge_failed",
                                error=error, error_code="post_merge_test_fail")
                except Exception:
                    pass
                telemetry.log(TaskFailed(
                    task_key=task_key, task_id=task_id,
                    error=error, cost_usd=result.cost_usd,
                ))
                merged_results.append(TaskResult(
                    task_key=task_key, success=False,
                    error_code="post_merge_test_fail",
                    error=error, cost_usd=result.cost_usd,
                    duration_s=result.duration_s,
                    diff_summary=result.diff_summary,
                    qa_report=result.qa_report,
                ))
                _orchestrator_log(project_dir, f"task={task_key} merge result=fail reason=post-merge test failure")
                continue

        ff = subprocess.run(
            ["git", "merge", "--ff-only", new_sha],
            cwd=project_dir, capture_output=True, text=True,
        )
        if ff.returncode != 0:
            console.print(f"    [red]\u2717[/red] #{task_key[:8]}  fast-forward failed")
            error = f"merge_failed: fast-forward failed\n{(ff.stderr or '')[:500]}"
            try:
                update_task(tasks_file, task_key, status="merge_failed",
                            error=error, error_code="merge_ff_failed")
            except Exception:
                pass
            telemetry.log(TaskFailed(
                task_key=task_key, task_id=task_id,
                error=error, cost_usd=result.cost_usd,
            ))
            merged_results.append(TaskResult(
                task_key=task_key, success=False,
                error_code="merge_failed",
                error=error, cost_usd=result.cost_usd,
                duration_s=result.duration_s,
                diff_summary=result.diff_summary,
                qa_report=result.qa_report,
            ))
            _orchestrator_log(project_dir, f"task={task_key} merge result=fail reason=fast-forward failed")
            continue

        # Merge succeeded
        console.print(f"    [green]\u2713[/green] #{task_key[:8]}  merged ({new_sha[:7]})")
        try:
            update_task(
                tasks_file,
                task_key,
                status="merged" if qa_mode == QAMode.BATCH else "passed",
                error=None,
                error_code=None,
                feedback=None,
            )
        except Exception:
            pass

        telemetry.log(TaskMerged(
            task_key=task_key, task_id=task_id,
            cost_usd=result.cost_usd,
            duration_s=result.duration_s,
            diff_summary=result.diff_summary,
        ))

        merged_results.append(TaskResult(
            task_key=task_key, success=True,
            commit_sha=new_sha,
            cost_usd=result.cost_usd,
            duration_s=result.duration_s,
            diff_summary=result.diff_summary,
            qa_report=result.qa_report,
        ))
        _orchestrator_log(project_dir, f"task={task_key} merge result=success commit={new_sha[:12]}")

        # Post-merge bookkeeping: cleanup testgen dir
        try:
            from otto.config import git_meta_dir
            testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / task_key
            if testgen_dir.exists():
                import shutil
                shutil.rmtree(testgen_dir, ignore_errors=True)
        except Exception:
            pass
        # Stamp commit SHA on proof report only for non-batch QA (per-task/skip).
        # For batch QA, SHA is stamped after batch QA passes (rollback could
        # invalidate the commit).
        if qa_mode != QAMode.BATCH:
            _stamp_proof_commit_sha(project_dir, task_key, new_sha)

    return merged_results


async def _run_task_in_worktree(
    task_plan: Any,
    context: Any,
    config: dict[str, Any],
    project_dir: Path,
    telemetry: Any,
    tasks_file: Path,
    base_sha: str,
    *,
    qa_mode: str = QAMode.PER_TASK,
    sibling_context: str | None = None,
) -> TaskResult:
    """Run a single task in an isolated git worktree.

    Creates worktree → installs deps → runs coding_loop → cleans up worktree.
    Used by both serial and parallel execution paths.
    """
    from otto.git_ops import create_task_worktree, cleanup_task_worktree
    from otto.testing import _install_deps

    task_key = task_plan.task_key
    install_timeout = config.get("install_timeout", config.get("verify_timeout", 300))
    worktree_path: Path | None = None
    try:
        wt_start = time.monotonic()
        worktree_path = await asyncio.to_thread(
            create_task_worktree, project_dir, task_key, base_sha,
        )
        wt_elapsed = time.monotonic() - wt_start
        _orchestrator_log(project_dir, f"  worktree: {task_key[:8]} created ({wt_elapsed:.1f}s)")

        install_start = time.monotonic()
        try:
            await asyncio.to_thread(_install_deps, worktree_path, install_timeout)
            install_elapsed = time.monotonic() - install_start
            _orchestrator_log(project_dir, f"  worktree: {task_key[:8]} deps installed ({install_elapsed:.1f}s)")
        except Exception as install_exc:
            install_elapsed = time.monotonic() - install_start
            _orchestrator_log(project_dir, f"  worktree: {task_key[:8]} deps install FAILED ({install_elapsed:.1f}s): {install_exc}")
            # Continue — dep install is best-effort, coding agent may still succeed

        if context.interrupted:
            return TaskResult(task_key=task_key, success=False, error="interrupted before start")

        result = await coding_loop(
            task_plan, context, config, project_dir,
            telemetry, tasks_file,
            task_work_dir=worktree_path,
            qa_mode=qa_mode,
            sibling_context=sibling_context,
        )
        return result

    except Exception as exc:
        error = f"worktree execution error: {exc}"
        _orchestrator_log(project_dir, f"  worktree: {task_key[:8]} EXCEPTION: {exc}")
        try:
            update_task(tasks_file, task_key, status="failed", error=error, error_code="worktree_setup_failed")
        except Exception:
            pass
        return TaskResult(task_key=task_key, success=False, error=error)
    finally:
        if worktree_path:
            try:
                await asyncio.to_thread(cleanup_task_worktree, project_dir, task_key)
                _orchestrator_log(project_dir, f"  worktree: {task_key[:8]} cleaned up")
            except Exception as e:
                _orchestrator_log(project_dir, f"  worktree: {task_key[:8]} cleanup FAILED: {e}")
                console.print(f"[yellow]Warning: worktree cleanup failed for {task_key}: {e}[/yellow]")


async def _run_batch_parallel(
    batch: Any,  # planner.Batch
    context: Any,  # PipelineContext
    config: dict[str, Any],
    project_dir: Path,
    telemetry: Any,  # Telemetry
    tasks_file: Path,
    *,
    max_parallel: int,
    qa_mode: str = QAMode.PER_TASK,
    sibling_contexts: dict[str, str | None] | None = None,
) -> list[TaskResult]:
    """Run a batch of tasks in parallel using per-task git worktrees.

    Each task gets its own worktree created from the current HEAD.
    Tasks run concurrently up to max_parallel via asyncio.Semaphore.
    Worktrees are cleaned up after all tasks complete.
    """
    # Get base SHA (current HEAD on default branch)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    semaphore = asyncio.Semaphore(max_parallel)

    async def _run_one(task_plan: Any) -> TaskResult:
        task_key = task_plan.task_key
        _orchestrator_log(project_dir, f"  parallel: {task_key[:8]} waiting for semaphore (max_parallel={max_parallel})")
        async with semaphore:
            _orchestrator_log(project_dir, f"  parallel: {task_key[:8]} acquired semaphore")
            sib_ctx = (sibling_contexts or {}).get(task_key)
            return await _run_task_in_worktree(
                task_plan, context, config, project_dir,
                telemetry, tasks_file, base_sha,
                qa_mode=qa_mode, sibling_context=sib_ctx,
            )

    # Launch all tasks concurrently
    tasks = [_run_one(tp) for tp in batch.tasks]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to failed TaskResults
    batch_results: list[TaskResult] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            task_key = batch.tasks[i].task_key
            batch_results.append(TaskResult(
                task_key=task_key, success=False,
                error=f"unexpected exception: {result}",
            ))
        else:
            batch_results.append(result)

    return batch_results


def _record_run_history(
    project_dir: Path,
    results: list[tuple[dict, bool]],
    run_duration: float,
    total_cost: float,
    post_run_suite_failed: bool = False,
    post_run_suite_output: str = "",
) -> None:
    """Record run to otto_logs/run-history.jsonl."""
    import json
    from datetime import datetime

    try:
        history_file = project_dir / "otto_logs" / "run-history.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        tasks_passed = sum(1 for _, s in results if s)
        tasks_failed = sum(1 for _, s in results if not s)
        failure_summary = ""
        if tasks_failed > 0:
            failed_tasks = [(t, s) for t, s in results if not s]
            if len(failed_tasks) == 1:
                ft = failed_tasks[0][0]
                failure_summary = f"task #{ft.get('id', '?')} failed: {ft.get('error', 'unknown')[:40]}"
            else:
                failure_summary = f"{tasks_failed} tasks failed"
        elif post_run_suite_failed:
            failure_summary = "post-run test suite failed"
            if post_run_suite_output:
                detail = next(
                    (line.strip() for line in reversed(post_run_suite_output.splitlines()) if line.strip()),
                    "",
                )
                if detail:
                    post_run_suite_output = detail
                failure_summary = (
                    f"{failure_summary}: {post_run_suite_output[:40]}"
                )
        commit_sha = ""
        try:
            sha_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            )
            if sha_result.returncode == 0:
                commit_sha = sha_result.stdout.strip()
        except Exception:
            pass
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tasks_total": len(results),
            "tasks_passed": tasks_passed,
            "tasks_failed": tasks_failed,
            "cost_usd": round(total_cost, 4),
            "time_s": round(run_duration, 1),
            "commit": commit_sha,
            "failure_summary": failure_summary,
            "orchestrator": "v4.5",
        }
        with open(history_file, "a") as hf:
            hf.write(json.dumps(entry) + "\n")
    except Exception:
        pass
