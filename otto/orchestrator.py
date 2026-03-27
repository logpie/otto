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
from otto.context import Learning, PipelineContext, TaskResult
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
from otto.tasks import load_tasks, update_task
from otto.telemetry import (
    AllDone,
    BatchCompleted,
    PlanCreated,
    TaskMerged,
    TaskFailed,
    Telemetry,
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
            console.print("  [yellow]Planner returned invalid task coverage; falling back to default plan[/yellow]")
            execution_plan = default_plan(pending)

        telemetry.log(PlanCreated(
            total_batches=len(execution_plan.batches),
            total_tasks=execution_plan.total_tasks,
        ))
        console.print(f"  [dim]Plan: {len(execution_plan.batches)} batch(es), {execution_plan.total_tasks} tasks[/dim]")

        # Step 3: PER loop
        max_parallel = config.get("max_parallel", 1) or 1
        batch_idx = 0
        while not execution_plan.is_empty and not context.interrupted:
            batch = execution_plan.batches[0]
            batch_idx += 1
            batch_size = len(batch.tasks)

            use_parallel = max_parallel > 1 and batch_size > 1
            if batch_size > 1:
                mode_label = "parallel" if use_parallel else "sequential"
                console.print(f"\n  [bold]Batch {batch_idx}[/bold]  [dim]{batch_size} tasks ({mode_label})[/dim]")
            else:
                console.print(f"\n  [bold]Batch {batch_idx}[/bold]  [dim]1 task[/dim]")

            if use_parallel:
                batch_results = await _run_batch_parallel(
                    batch, context, config, project_dir, telemetry, tasks_file,
                    max_parallel=max_parallel,
                )
                # Serial merge phase: merge verified candidates onto main
                if not context.interrupted:
                    batch_results = merge_parallel_results(
                        batch_results, config, project_dir, tasks_file, telemetry,
                    )

                # Auto-retry merge_failed tasks on updated main.
                # Most merge "conflicts" are just concurrent additions to the
                # same file — git merge handles these. Only truly unresolvable
                # conflicts need a full coding re-run.
                if not context.interrupted:
                    merge_failed = [
                        r for r in batch_results
                        if not r.success and r.error_code == "merge_failed"
                    ]
                    if merge_failed:
                        console.print(
                            f"\n  [yellow]Re-running {len(merge_failed)} merge-failed "
                            f"task(s) on updated main...[/yellow]"
                        )
                        for failed_result in merge_failed:
                            if context.interrupted:
                                break
                            fkey = failed_result.task_key
                            tp = next(
                                (t for t in batch.tasks if t.task_key == fkey), None,
                            )
                            if not tp:
                                continue
                            # Reset task for re-run
                            try:
                                update_task(
                                    tasks_file, fkey,
                                    status="pending",
                                    error=None, error_code=None, session_id=None,
                                )
                            except Exception:
                                continue
                            # Re-run on updated main (sees other tasks' code)
                            retry_result = await coding_loop(
                                tp, context, config, project_dir,
                                telemetry, tasks_file,
                            )
                            batch_results = [
                                retry_result if r.task_key == fkey else r
                                for r in batch_results
                            ]
            else:
                # Serial execution — tasks share the main checkout
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

            # Persist any new learnings after each batch
            persist_learnings(project_dir, context)

            telemetry.log(BatchCompleted(
                batch_index=batch_idx - 1,
                tasks_passed=batch_passed,
                tasks_failed=batch_failed,
            ))

            pass_str = f"[green]{batch_passed} passed[/green]"
            fail_str = f"[red]{batch_failed} failed[/red]" if batch_failed else f"{batch_failed} failed"
            console.print(f"  Batch {batch_idx}: {pass_str}, {fail_str}")

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
            _print_summary(results, run_duration, total_cost=context.total_cost,
                           project_dir=project_dir)
        except Exception as exc:
            console.print(f"  [yellow]Warning: summary display error: {exc}[/yellow]")

        # Record run history
        _record_run_history(project_dir, results, run_duration, context.total_cost)

        return 1 if context.failed_count > 0 or context.interrupted or missing_keys else 0

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


def merge_parallel_results(
    results: list[TaskResult],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path,
    telemetry: Any,
) -> list[TaskResult]:
    """Serial merge phase: merge verified candidates onto main sequentially.

    For each verified task (sorted by task_key for determinism):
      1. Merge the candidate ref onto current main HEAD
      2. If conflict -> mark merge_failed
      3. Run test suite on the merged result (post-merge verification)
      4. If tests fail -> mark merge_failed
      5. Fast-forward main to the new commit
      6. Mark task passed

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

        try:
            update_task(tasks_file, task_key, status="merge_pending")
        except Exception:
            pass

        # Find the best candidate ref for this task
        candidate_ref = _find_best_candidate_ref(project_dir, task_key)
        if not candidate_ref:
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
            continue

        # Merge the candidate onto a temp branch rooted at the current main
        # HEAD, but do not fast-forward main until verification passes.
        success, new_sha = merge_candidate(
            project_dir, candidate_ref, default_branch,
        )
        if not success:
            console.print(f"    [red]\u2717[/red] #{task_key[:8]}  merge conflict")
            error = "merge_failed: merge conflict"
            try:
                update_task(tasks_file, task_key, status="merge_failed",
                            error=error, error_code="merge_conflict")
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
            continue

        # Post-merge verification: run test suite in a fresh disposable worktree.
        # This tests the exact commit that will become main HEAD.
        # Strict mode: no "local pass beats worktree fail" heuristic.
        if not config.get("skip_test"):
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
                    error_code="merge_failed",
                    error=error, cost_usd=result.cost_usd,
                    duration_s=result.duration_s,
                    diff_summary=result.diff_summary,
                    qa_report=result.qa_report,
                ))
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
            continue

        # Merge succeeded
        console.print(f"    [green]\u2713[/green] #{task_key[:8]}  merged ({new_sha[:7]})")
        try:
            update_task(tasks_file, task_key, status="passed")
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

    return merged_results


async def _run_batch_parallel(
    batch: Any,  # planner.Batch
    context: Any,  # PipelineContext
    config: dict[str, Any],
    project_dir: Path,
    telemetry: Any,  # Telemetry
    tasks_file: Path,
    *,
    max_parallel: int,
) -> list[TaskResult]:
    """Run a batch of tasks in parallel using per-task git worktrees.

    Each task gets its own worktree created from the current HEAD.
    Tasks run concurrently up to max_parallel via asyncio.Semaphore.
    Worktrees are cleaned up after all tasks complete.
    """
    from otto.git_ops import create_task_worktree, cleanup_task_worktree
    from otto.testing import _install_deps

    default_branch = config["default_branch"]
    install_timeout = config.get("install_timeout", config.get("verify_timeout", 300))

    # Get base SHA (current HEAD on default branch)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    semaphore = asyncio.Semaphore(max_parallel)
    worktree_paths: dict[str, Path] = {}

    async def _run_one(task_plan: Any) -> TaskResult:
        task_key = task_plan.task_key
        worktree_path: Path | None = None
        try:
            async with semaphore:
                # Bound setup concurrency too: worktree creation and dependency
                # installation can be the most expensive parallel operations.
                worktree_path = await asyncio.to_thread(
                    create_task_worktree, project_dir, task_key, base_sha,
                )
                worktree_paths[task_key] = worktree_path

                await asyncio.to_thread(
                    _install_deps, worktree_path, install_timeout,
                )

                if context.interrupted:
                    return TaskResult(
                        task_key=task_key, success=False,
                        error="interrupted before start",
                    )
                result = await coding_loop(
                    task_plan, context, config, project_dir,
                    telemetry, tasks_file,
                    task_work_dir=worktree_path,
                )
                return result

        except Exception as exc:
            error = f"parallel execution error: {exc}"
            try:
                update_task(
                    tasks_file,
                    task_key,
                    status="failed",
                    error=error,
                    error_code="parallel_setup_failed",
                )
            except Exception:
                pass
            return TaskResult(
                task_key=task_key, success=False,
                error=error,
            )
        finally:
            # Clean up worktree
            if worktree_path:
                try:
                    await asyncio.to_thread(
                        cleanup_task_worktree, project_dir, task_key,
                    )
                except Exception as e:
                    console.print(f"[yellow]⚠ Worktree cleanup failed for {task_key}: {e}[/yellow]")

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
            "orchestrator": "v4.5",
        }
        with open(history_file, "a") as hf:
            hf.write(json.dumps(entry) + "\n")
    except Exception:
        pass
