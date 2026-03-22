"""Otto v4 orchestrator — Plan-Execute-Replan (PER) deterministic loop.

Replaces the v3 LLM pilot with a Python-driven orchestrator. LLM is invoked
only at decision points (initial plan + replan on failure). Agents coordinate
through in-memory shared state. Tasks execute sequentially within each batch.

Entry point: run_per()
"""
import fcntl
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from otto.config import git_meta_dir
from otto.context import PipelineContext, TaskResult
from otto.display import console, rich_escape
from otto.planner import (
    ExecutionPlan,
    default_plan,
    plan,
    replan,
)
from otto.runner import (
    _print_summary,
    coding_loop,
    preflight_checks,
)
from otto.tasks import load_tasks
from otto.telemetry import (
    AllDone,
    BatchCompleted,
    PlanCreated,
    Telemetry,
)


def cleanup_orphaned_worktrees(project_dir: Path) -> None:
    """Remove orphaned otto worktrees from previous crashed runs."""
    wt_dir = project_dir / ".worktrees"
    if not wt_dir.exists():
        return
    for child in wt_dir.iterdir():
        if child.name.startswith("otto-") and child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def _plan_covers_pending(execution_plan: ExecutionPlan, pending: list[dict[str, Any]]) -> bool:
    """Return True when the plan covers each pending task key exactly once."""
    pending_keys = [str(task.get("key", "")) for task in pending if task.get("key")]
    planned_keys = [
        task_plan.task_key
        for batch in execution_plan.batches
        for task_plan in batch.tasks
    ]
    return (
        len(planned_keys) == len(pending_keys)
        and len(set(planned_keys)) == len(planned_keys)
        and set(planned_keys) == set(pending_keys)
    )


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

    # Clean up stale task lock files
    tasks_lock = project_dir / ".tasks.lock"
    if tasks_lock.exists():
        try:
            tasks_lock.unlink()
        except OSError:
            pass

    # Set up context and telemetry
    context = PipelineContext()
    log_dir = project_dir / "otto_logs"
    telemetry = Telemetry(log_dir)
    telemetry.enable_legacy_write()

    # Signal handling
    def _signal_handler(signum, frame):
        if context.interrupted:
            console.print("\nForce exit", style="red")
            # Kill tracked subprocesses
            for pid in context.pids:
                try:
                    import os
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
            sys.exit(1)
        context.interrupted = True
        console.print("\n[yellow]Warning: Interrupted — finishing current task[/yellow]")
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
        console.print()
        console.print(f"  [bold]{len(pending)} task{'s' if len(pending) != 1 else ''}[/bold], [dim]{total_specs} specs[/dim]  [dim](v4 PER)[/dim]")
        for t in pending:
            deps = t.get("depends_on", [])
            dep_str = f" [dim]\u2192 #{', #'.join(str(d) for d in deps)}[/dim]" if deps else ""
            spec_count = len(t.get("spec") or [])
            console.print(f"    [dim]\u25cb[/dim] [bold]#{t['id']}[/bold]  {rich_escape(t.get('prompt', '')[:55])}  [dim]({spec_count} spec){dep_str}[/dim]")
        console.print()
        console.print(f"{'─' * 60}", style="dim")

        # Step 2: Plan
        console.print("  Planning...", style="dim")
        execution_plan = await plan(pending, config, project_dir)
        if not _plan_covers_pending(execution_plan, pending):
            console.print("  [yellow]Planner returned invalid task coverage; falling back to default plan[/yellow]")
            execution_plan = default_plan(pending)

        telemetry.log(PlanCreated(
            total_batches=len(execution_plan.batches),
            total_tasks=execution_plan.total_tasks,
        ))
        console.print(f"  [dim]Plan: {len(execution_plan.batches)} batch(es), {execution_plan.total_tasks} tasks[/dim]")

        # Step 3: PER loop
        batch_idx = 0
        while not execution_plan.is_empty and not context.interrupted:
            batch = execution_plan.batches[0]
            batch_idx += 1
            batch_size = len(batch.tasks)

            if batch_size > 1:
                console.print(f"\n  [bold]Batch {batch_idx}[/bold]  [dim]{batch_size} tasks (sequential)[/dim]")
            else:
                console.print(f"\n  [bold]Batch {batch_idx}[/bold]  [dim]1 task[/dim]")

            # Execute batch — sequential within batch because tasks share a checkout.
            batch_results: list[TaskResult] = []
            for task_plan in batch.tasks:
                result = await coding_loop(
                    task_plan, context, config, project_dir, telemetry, tasks_file,
                )
                batch_results.append(result)
                if context.interrupted:
                    break

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

            telemetry.log(BatchCompleted(
                batch_index=batch_idx - 1,
                tasks_passed=batch_passed,
                tasks_failed=batch_failed,
            ))

            console.print(f"  [dim]Batch {batch_idx}: {batch_passed} passed, {batch_failed} failed[/dim]")

            # Remove completed batch and check for replan
            completed_keys = {r.task_key for r in batch_results}
            execution_plan = execution_plan.remaining_after(completed_keys)

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
                replanned = await replan(context, remaining_plan, config, project_dir)
                if _plan_covers_pending(replanned, remaining_pending):
                    execution_plan = replanned
                else:
                    console.print("  [yellow]Replan returned invalid task coverage; keeping existing remaining plan[/yellow]")
                    execution_plan = remaining_plan

        # Step 4: Summary
        run_duration = time.monotonic() - run_start
        missing_keys = pending_keys - set(context.results)

        telemetry.log(AllDone(
            total_passed=context.passed_count,
            total_failed=context.failed_count,
            total_missing_or_interrupted=len(missing_keys),
            total_cost=context.total_cost,
            total_duration_s=run_duration,
        ))

        # Build results list for _print_summary
        final_tasks = load_tasks(tasks_file)
        pending_keys = {t["key"] for t in pending}
        results: list[tuple[dict, bool]] = []
        for t in final_tasks:
            if t.get("key") in pending_keys:
                results.append((t, t.get("status") == "passed"))

        try:
            _print_summary(results, run_duration, total_cost=context.total_cost)
        except Exception as exc:
            console.print(f"  [yellow]Warning: summary display error: {exc}[/yellow]")

        # Record run history
        _record_run_history(project_dir, results, run_duration, context.total_cost)

        return 1 if context.failed_count > 0 or context.interrupted or missing_keys else 0

    finally:
        # Ensure we're back on default branch
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True,
        )
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _record_run_history(
    project_dir: Path,
    results: list[tuple[dict, bool]],
    run_duration: float,
    total_cost: float,
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
            "orchestrator": "v4",
        }
        with open(history_file, "a") as hf:
            hf.write(json.dumps(entry) + "\n")
    except Exception:
        pass
