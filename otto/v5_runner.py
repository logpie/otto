"""v5 hierarchical run coordinator.

Drives a full v5 run end-to-end:
    1. Compile flat spec at root.
    2. Run root Lead.
    3. If root emitted children: process the v5_pending queue, dispatching
       children up to a concurrency cap, respecting depends_on.
    4. When all children of a parent resolve, spawn an integration Lead for
       that parent.
    5. Continue until root has its own verdict.
    6. Render proof packet + summary.

Phase 2 design notes:

- Children run in-process (asyncio tasks), not as subprocess. This is simpler
  than spawning fresh `otto v5 run-child` subprocesses and works at the scale
  v5 targets. If we hit context-budget issues with deep trees, Phase 4 can
  revisit subprocess isolation.

- Per-parent integration branches: ``i2p/<parent_task_id>/integration``.
  Children's worktrees are NOT physically separate yet — Phase 2 keeps
  children operating on the same project_dir for simplicity. Real worktrees
  are wired in Phase 2.5 if needed.

- Best-effort everywhere: any child crash → its verdict becomes catastrophic;
  parent's integration runs anyway with whatever children produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto import paths as _paths
from otto.lead import LeadResult, run_lead
from otto.queue.subtask import (
    read_pending,
    take_ready,
    v5_pending_path,
)
from otto.queue.task_graph import (
    aggregate_verdict,
    children_of,
    get_task,
    record_task,
    set_verdict,
    tree_total_cost,
)
from otto.spec_compile_flat import compile_flat_spec, FlatSpec

logger = logging.getLogger("otto.v5_runner")

ROOT_TASK_ID = "root"


@dataclass
class V5RunResult:
    """Top-level result of a v5 run."""

    root_task_id: str = ROOT_TASK_ID
    spec: FlatSpec | None = None
    root_lead_result: LeadResult | None = None
    integration_results: dict[str, LeadResult] = field(default_factory=dict)
    child_results: dict[str, LeadResult] = field(default_factory=dict)
    verdict: str = "unverified"
    total_cost_usd: float = 0.0
    duration_s: float = 0.0
    failure_reason: str = ""


def _new_session_id() -> str:
    return time.strftime("%Y-%m-%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


async def run_v5_pipeline(
    *,
    project_dir: Path,
    intent: str,
    config: dict[str, Any],
    max_parallel: int = 3,
    tree_budget_usd: float = 25.0,
    on_event: Any = None,  # optional callback(event_dict) for streaming
) -> V5RunResult:
    """Run a full v5 hierarchical pipeline against ``intent``.

    Best-effort: on any error in any phase, write a verdict and continue.
    Returns the final V5RunResult after the root has its terminal verdict.
    """
    started = time.monotonic()
    result = V5RunResult()

    try:
        # ---- Phase A0: Repo hygiene ----
        # Greenfield projects often start with `git init` and no commits.
        # Without an initial commit, every `git branch i2p/...` creation fails
        # with "not a valid object name: 'HEAD'", which cascades into worktree
        # failures and serialised execution.
        try:
            from otto.v5_branching import ensure_initial_commit
            ensure_initial_commit(project_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ensure_initial_commit failed: %s", exc)

        # ---- Phase A: Root session setup ----
        root_session_id = _new_session_id()
        root_session_dir = _paths.session_dir(project_dir, root_session_id)
        root_session_dir.mkdir(parents=True, exist_ok=True)
        _emit(on_event, {"event": "session_open", "session_id": root_session_id})

        # ---- Phase B: Compile flat spec ----
        _emit(on_event, {"event": "compile_start"})
        try:
            spec = await compile_flat_spec(
                project_dir=project_dir,
                session_dir=root_session_dir,
                intent=intent,
                config=config,
            )
            result.spec = spec
            _emit(on_event, {
                "event": "compile_done",
                "journey_count": len(spec.behavior_journeys),
                "lint_warnings": len(spec.lint_warnings),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("flat spec compile failed")
            result.verdict = "catastrophic"
            result.failure_reason = f"spec_compile: {type(exc).__name__}: {exc}"
            return result

        # Record root in task graph.
        record_task(
            project_dir,
            task_id=ROOT_TASK_ID,
            intent=intent,
            integration_branch=None,
        )

        # ---- Phase C: Run root Lead ----
        _emit(on_event, {"event": "lead_start", "task_id": ROOT_TASK_ID})
        root_result = await run_lead(
            task_id=ROOT_TASK_ID,
            intent=intent,
            project_dir=project_dir,
            session_dir=root_session_dir,
            integration_branch=None,
            config=config,
            kind="plan_or_inline",
        )
        result.root_lead_result = root_result
        _emit(on_event, {
            "event": "lead_done",
            "task_id": ROOT_TASK_ID,
            "verdict": root_result.verdict,
            "decomposition": root_result.decomposition,
            "emitted": len(root_result.emitted_subtask_ids),
        })

        # ---- Phase C.5: Optional review pause for root's emitted children ----
        if (
            root_result.decomposition == "emit"
            and root_result.emitted_subtask_ids
            and bool(config.get("v5_review_first_decomp"))
        ):
            from otto.v5_review import list_pending_review, mark_pending_review

            n = mark_pending_review(project_dir, parent_task_id=ROOT_TASK_ID)
            _emit(on_event, {
                "event": "review_pause",
                "task_id": ROOT_TASK_ID,
                "pending_count": n,
            })
            # Wait until all root children are out of pending_review state.
            # Either approved (proceed), or cancelled (treated as not-emitted).
            await _wait_for_review(
                project_dir, parent_task_id=ROOT_TASK_ID, on_event=on_event
            )
            # Drop cancelled children from emitted list so we don't try to run them.
            still_pending = list_pending_review(project_dir, parent_task_id=ROOT_TASK_ID)
            assert still_pending == []  # post-condition
            _emit(on_event, {"event": "review_resume", "task_id": ROOT_TASK_ID})

        # ---- Phase D: Process emitted children, if any ----
        if root_result.decomposition == "emit" and root_result.emitted_subtask_ids:
            await _process_children(
                project_dir=project_dir,
                parent_task_id=ROOT_TASK_ID,
                config=config,
                max_parallel=max_parallel,
                tree_budget_usd=tree_budget_usd,
                child_results=result.child_results,
                integration_results=result.integration_results,
                on_event=on_event,
            )
            # ---- Phase E: Run root integration ----
            child_summaries = _build_child_summaries(
                project_dir, ROOT_TASK_ID, result.child_results
            )
            integration_session_dir = root_session_dir / "integration"
            integration_session_dir.mkdir(parents=True, exist_ok=True)
            _emit(on_event, {"event": "integration_start", "task_id": ROOT_TASK_ID})
            integration_result = await run_lead(
                task_id=ROOT_TASK_ID,
                intent=intent,
                project_dir=project_dir,
                session_dir=integration_session_dir,
                integration_branch=None,  # root integration ultimately merges to main
                config=config,
                kind="integration",
                child_summaries=child_summaries,
            )
            result.integration_results[ROOT_TASK_ID] = integration_result
            _emit(on_event, {
                "event": "integration_done",
                "task_id": ROOT_TASK_ID,
                "verdict": integration_result.verdict,
            })
            # Override root's verdict with the integration verdict (which audits the FULL product).
            set_verdict(
                project_dir, ROOT_TASK_ID, integration_result.verdict,
                cost_usd=root_result.cost_usd + integration_result.cost_usd,
            )

        # ---- Phase F: Aggregate final verdict ----
        result.verdict = aggregate_verdict(project_dir, ROOT_TASK_ID)
        result.total_cost_usd = tree_total_cost(project_dir, ROOT_TASK_ID)

    except Exception as exc:  # noqa: BLE001 — top-level safety net
        logger.exception("v5 pipeline crashed")
        result.verdict = "catastrophic"
        result.failure_reason = f"pipeline: {type(exc).__name__}: {exc}"

    finally:
        result.duration_s = time.monotonic() - started

    return result


async def _process_children(
    *,
    project_dir: Path,
    parent_task_id: str,
    config: dict[str, Any],
    max_parallel: int,
    tree_budget_usd: float,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    on_event: Any = None,
) -> None:
    """Process the v5_pending queue for ``parent_task_id``'s subtree.

    Runs children concurrently (up to max_parallel), waits for all, then
    recursively handles any grandchildren. Returns when all descendants of
    ``parent_task_id`` have terminal verdicts.

    Best-effort: a child crash doesn't stop siblings.
    """
    completed: set[str] = set()
    in_flight: dict[str, asyncio.Task[Any]] = {}

    while True:
        # Check tree budget cap.
        if tree_total_cost(project_dir, ROOT_TASK_ID) > tree_budget_usd:
            logger.warning("tree budget cap exceeded; refusing new dispatches")
            _emit(on_event, {
                "event": "budget_cap_hit",
                "spent": tree_total_cost(project_dir, ROOT_TASK_ID),
                "cap": tree_budget_usd,
            })
            # Wait for in-flight to drain, then exit.
            if in_flight:
                await asyncio.gather(*in_flight.values(), return_exceptions=True)
            break

        # Find ready tasks not yet running.
        ready = take_ready(
            project_dir,
            completed_task_ids=completed,
            in_flight_task_ids=set(in_flight.keys()),
        )
        # Filter to descendants of parent_task_id.
        ready = [r for r in ready if _is_descendant_of(project_dir, r["task_id"], parent_task_id)]

        # Spawn ready tasks up to max_parallel.
        for entry in ready:
            if len(in_flight) >= max_parallel:
                break
            tid = entry["task_id"]
            in_flight[tid] = asyncio.create_task(
                _run_child(
                    project_dir=project_dir,
                    entry=entry,
                    config=config,
                    on_event=on_event,
                )
            )
            _emit(on_event, {"event": "child_dispatch", "task_id": tid})

        # If nothing in flight and nothing ready, we're done.
        if not in_flight and not ready:
            break

        # Wait for at least one to complete.
        if in_flight:
            done, _pending = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for fut in done:
                # Find which task this future belongs to.
                tid = next(t for t, f in in_flight.items() if f is fut)
                in_flight.pop(tid, None)
                try:
                    result: LeadResult = fut.result()
                    child_results[tid] = result
                    completed.add(tid)
                    _emit(on_event, {
                        "event": "child_done",
                        "task_id": tid,
                        "verdict": result.verdict,
                    })

                    # If this child itself emitted grandchildren, recursively process.
                    if result.decomposition == "emit" and result.emitted_subtask_ids:
                        await _process_children(
                            project_dir=project_dir,
                            parent_task_id=tid,
                            config=config,
                            max_parallel=max_parallel,
                            tree_budget_usd=tree_budget_usd,
                            child_results=child_results,
                            integration_results=integration_results,
                            on_event=on_event,
                        )
                        # Run this child's integration Lead.
                        await _run_integration(
                            project_dir=project_dir,
                            task_id=tid,
                            intent=(get_task(project_dir, tid) or {}).get("intent", ""),
                            config=config,
                            child_results=child_results,
                            integration_results=integration_results,
                            on_event=on_event,
                        )

                except Exception as exc:  # noqa: BLE001
                    logger.exception("child task wrapper crashed: %s", tid)
                    set_verdict(project_dir, tid, "catastrophic")
                    completed.add(tid)
                    _emit(on_event, {
                        "event": "child_crash",
                        "task_id": tid,
                        "error": str(exc),
                    })


async def _run_child(
    *,
    project_dir: Path,
    entry: dict[str, Any],
    config: dict[str, Any],
    on_event: Any = None,
) -> LeadResult:
    """Run one child Lead in its own session + worktree, with provider fallback."""
    tid = entry["task_id"]
    child_session_id = _new_session_id()
    child_session_dir = _paths.session_dir(project_dir, child_session_id)
    child_session_dir.mkdir(parents=True, exist_ok=True)

    # Copy parent's spec so child can read frozen journeys.
    parent_session_dir = Path(entry.get("parent_session_dir", str(child_session_dir)))
    parent_spec = parent_session_dir / "spec" / "spec.json"
    child_spec_dir = child_session_dir / "spec"
    child_spec_dir.mkdir(parents=True, exist_ok=True)
    child_spec_path = child_spec_dir / "spec.json"
    if parent_spec.exists() and not child_spec_path.exists():
        try:
            child_spec_path.write_text(parent_spec.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not copy parent spec to child session: %s", exc)

    # Set up the child's per-task worktree off the parent's integration branch.
    parent_integration_branch = entry.get("integration_branch") or "main"
    child_worktree: Path | None = None
    try:
        from otto.v5_branching import setup_child_worktree

        child_worktree = setup_child_worktree(
            project_dir=project_dir,
            child_task_id=tid,
            parent_integration_branch=parent_integration_branch,
        )
        if child_worktree is not None:
            # Symlink the worktree under the child's session_dir for Lead's CWD discovery.
            link_path = child_session_dir / "worktree"
            try:
                if not link_path.exists():
                    link_path.symlink_to(child_worktree)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not symlink worktree: %s", exc)
            _emit(on_event, {
                "event": "worktree_created",
                "task_id": tid,
                "path": str(child_worktree),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree setup for child %s failed: %s", tid, exc)

    # Run the Lead. If we created a worktree, lead.py's _resolve_worktree picks it up.
    result = await _run_lead_with_fallback(
        task_id=tid,
        intent=entry["intent"],
        project_dir=project_dir,
        session_dir=child_session_dir,
        integration_branch=parent_integration_branch,
        config=config,
        kind="plan_or_inline",
        on_event=on_event,
    )

    # Merge child's branch into parent's integration branch (best-effort).
    if child_worktree is not None and result.verdict in ("pass", "partial", "unverified"):
        await _merge_child_branch(
            project_dir=project_dir,
            child_task_id=tid,
            child_worktree=child_worktree,
            parent_integration_branch=parent_integration_branch,
            result=result,
            on_event=on_event,
        )

    return result


async def _merge_child_branch(
    *,
    project_dir: Path,
    child_task_id: str,
    child_worktree: Path,
    parent_integration_branch: str,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Commit the child's worktree changes and merge into parent's integration branch.

    Best-effort: on any failure, mark the child's verdict as merge_blocked
    (without crashing the parent run).
    """
    from otto.queue.task_graph import set_verdict
    from otto.v5_branching import commit_worktree, merge_child_into_integration

    commit_msg = f"v5 task {child_task_id}: {result.verdict}"
    ok, detail = commit_worktree(worktree_path=child_worktree, message=commit_msg)
    if not ok:
        logger.warning("commit_worktree(%s) failed: %s", child_task_id, detail)
        _emit(on_event, {
            "event": "merge_failed",
            "task_id": child_task_id,
            "phase": "commit",
            "detail": detail,
        })
        set_verdict(project_dir, child_task_id, "merge_blocked", cost_usd=result.cost_usd)
        return

    ok, detail = merge_child_into_integration(
        project_dir=project_dir,
        child_task_id=child_task_id,
        parent_integration_branch=parent_integration_branch,
    )
    if not ok:
        logger.warning("merge_child_into_integration(%s) failed: %s", child_task_id, detail)
        _emit(on_event, {
            "event": "merge_failed",
            "task_id": child_task_id,
            "phase": "merge",
            "detail": detail,
        })
        # Per philosophy: best-effort. Mark blocked, sibling continues.
        set_verdict(project_dir, child_task_id, "merge_blocked", cost_usd=result.cost_usd)
        return

    _emit(on_event, {
        "event": "merged",
        "task_id": child_task_id,
        "into": parent_integration_branch,
    })


async def _run_lead_with_fallback(
    *,
    task_id: str,
    intent: str,
    project_dir: Path,
    session_dir: Path,
    integration_branch: str | None,
    config: dict[str, Any],
    kind: str = "plan_or_inline",
    child_summaries: list[dict[str, Any]] | None = None,
    on_event: Any = None,
) -> LeadResult:
    """Run a Lead with task-level provider fallback.

    First attempt uses the configured provider. If it returns
    verdict=catastrophic with a provider-exhausted-style failure_reason, AND
    a fallback_provider is configured, swap providers and retry once with
    the same task_id (preserving lineage).

    Per philosophy: never infinite-loop; cap fallback retries at 1.
    """
    import time as _time

    from otto.v5_provider_fallback import (
        append_attempt,
        fallback_provider as _fallback_provider,
        should_fallback,
    )

    started = _time.monotonic()
    attempt_started = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())

    # Attempt 1: configured provider.
    provider_a = (
        config.get("provider")
        or (config.get("defaults", {}) or {}).get("provider")
        or "claude"
    )
    result_a = await run_lead(
        task_id=task_id,
        intent=intent,
        project_dir=project_dir,
        session_dir=session_dir,
        integration_branch=integration_branch,
        config=config,
        kind=kind,  # type: ignore[arg-type]
        child_summaries=child_summaries,
    )
    duration_a = _time.monotonic() - started
    append_attempt(
        session_dir / "summary.json",
        provider=provider_a,
        cost_usd=result_a.cost_usd,
        outcome=result_a.verdict,
        duration_s=duration_a,
        started_at=attempt_started,
    )

    if result_a.verdict != "catastrophic":
        return result_a

    do_fallback, reason = should_fallback(result_a.failure_reason, config)
    if not do_fallback:
        return result_a

    fb = _fallback_provider(config)
    if not fb or fb == provider_a:
        return result_a

    _emit(on_event, {
        "event": "provider_fallback",
        "task_id": task_id,
        "from": provider_a,
        "to": fb,
        "reason": reason,
    })

    # Attempt 2: fallback provider (mutate a copy of config).
    fallback_config = dict(config)
    fallback_config["provider"] = fb
    overrides = dict(fallback_config.get("_cli_overrides") or {})
    overrides["provider"] = fb
    fallback_config["_cli_overrides"] = overrides

    fallback_started = _time.monotonic()
    fallback_started_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
    result_b = await run_lead(
        task_id=task_id,
        intent=intent,
        project_dir=project_dir,
        session_dir=session_dir,
        integration_branch=integration_branch,
        config=fallback_config,
        kind=kind,  # type: ignore[arg-type]
        child_summaries=child_summaries,
    )
    append_attempt(
        session_dir / "summary.json",
        provider=fb,
        cost_usd=result_b.cost_usd,
        outcome=result_b.verdict,
        duration_s=_time.monotonic() - fallback_started,
        started_at=fallback_started_iso,
        fallback_reason=reason,
    )
    return result_b


async def _run_integration(
    *,
    project_dir: Path,
    task_id: str,
    intent: str,
    config: dict[str, Any],
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    on_event: Any = None,
) -> LeadResult:
    """Run an integration Lead for ``task_id`` after children have resolved."""
    integration_session_id = _new_session_id()
    integration_session_dir = _paths.session_dir(project_dir, integration_session_id)
    integration_session_dir.mkdir(parents=True, exist_ok=True)

    # Provide child summaries to the integration Lead's prompt.
    summaries = _build_child_summaries(project_dir, task_id, child_results)

    _emit(on_event, {"event": "integration_start", "task_id": task_id})
    result = await run_lead(
        task_id=task_id,
        intent=intent,
        project_dir=project_dir,
        session_dir=integration_session_dir,
        integration_branch=(get_task(project_dir, task_id) or {}).get("integration_branch"),
        config=config,
        kind="integration",
        child_summaries=summaries,
    )
    integration_results[task_id] = result
    _emit(on_event, {"event": "integration_done", "task_id": task_id, "verdict": result.verdict})
    return result


def _build_child_summaries(
    project_dir: Path,
    parent_task_id: str,
    child_results: dict[str, LeadResult],
) -> list[dict[str, Any]]:
    """Build the child summary list passed to integration Lead's prompt."""
    out: list[dict[str, Any]] = []
    for cid in children_of(project_dir, parent_task_id):
        entry = get_task(project_dir, cid) or {}
        result = child_results.get(cid)
        out.append({
            "task_id": cid,
            "intent": entry.get("intent", ""),
            "verdict": (result.verdict if result else entry.get("verdict") or "unknown"),
            "summary": (result.final_text if result else "")[:200],
            "cost_usd": result.cost_usd if result else float(entry.get("cost_usd", 0.0)),
        })
    return out


def _is_descendant_of(project_dir: Path, candidate_id: str, ancestor_id: str) -> bool:
    """Walk parent chain to confirm candidate_id is in ancestor_id's subtree."""
    if candidate_id == ancestor_id:
        return False
    cur = candidate_id
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        entry = get_task(project_dir, cur) or {}
        parent = entry.get("parent_task_id")
        if parent == ancestor_id:
            return True
        if parent is None:
            return ancestor_id == ROOT_TASK_ID and cur == ROOT_TASK_ID
        cur = parent
    return False


def _emit(on_event: Any, payload: dict[str, Any]) -> None:
    """Best-effort event emission."""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass


async def _wait_for_review(
    project_dir: Path,
    *,
    parent_task_id: str,
    poll_interval_s: float = 1.0,
    timeout_s: float = 24 * 3600.0,
    on_event: Any = None,
) -> None:
    """Wait until no children of ``parent_task_id`` are in pending_review state.

    Best-effort: on timeout, auto-approve all remaining pending tasks (per
    plan-v5 §13: every layer terminates; no infinite waits).
    """
    from otto.v5_review import approve, list_pending_review

    deadline = time.monotonic() + timeout_s
    poll_count = 0
    while True:
        pending = list_pending_review(project_dir, parent_task_id=parent_task_id)
        if not pending:
            return
        poll_count += 1
        if poll_count == 1 or poll_count % 10 == 0:
            _emit(on_event, {
                "event": "review_waiting",
                "task_id": parent_task_id,
                "pending_count": len(pending),
                "elapsed_s": time.monotonic() - (deadline - timeout_s),
            })
        if time.monotonic() > deadline:
            # Auto-approve on timeout (per philosophy invariant).
            n = approve(project_dir, parent_task_id=parent_task_id)
            _emit(on_event, {
                "event": "review_timeout_auto_approve",
                "task_id": parent_task_id,
                "auto_approved": n,
            })
            return
        await asyncio.sleep(poll_interval_s)
