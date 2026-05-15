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
import logging
import shutil
import subprocess
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from otto import paths as _paths
from otto.lead import LeadResult, run_lead
from otto.v5_preflight import (
    check_scaffold_compiles,
    filter_blocked_descendants,
    run_preflight,
    smoke_clean_deploy,
)
from otto.v5_preflight_repair import (
    AgentRepairRequest,
    AgentRepairResult,
    PreflightRepairController,
)
from otto.queue.subtask import (
    read_pending,
    take_ready,
)
from otto.queue.task_graph import (
    aggregate_verdict,
    children_of,
    clear_verdict_for_retry,
    get_retry_count,
    get_retry_reason,
    get_task,
    read_graph,
    record_task,
    set_verdict,
    tree_total_cost,
)
from otto.spec_compile_flat import compile_flat_spec, FlatSpec
from otto.v5_branching import MergeWorktreeDirtyError

logger = logging.getLogger("otto.v5_runner")

ROOT_TASK_ID = "root"

# When scaffold preflight invalidates an architect's self-declared pass,
# the runner re-dispatches the architect with the failure summary
# prepended to its intent. This is the cap on those retries (architect
# is allowed 1 original attempt + ``MAX_ARCHITECT_RETRIES`` re-runs).
MAX_ARCHITECT_RETRIES = 2


class _DispatchLease:
    """Shared per-run child dispatch capacity.

    ``_process_children`` can be entered recursively, and tests can exercise
    two scheduler loops concurrently. Local ``in_flight`` dicts are not enough
    in that shape: every loop sees its own capacity. This lease is the single
    in-process source of truth for active child runs.
    """

    def __init__(self, max_parallel: int) -> None:
        self.max_parallel = max(1, int(max_parallel or 1))
        self._active: set[str] = set()
        self._condition = asyncio.Condition()

    async def active_task_ids(self) -> set[str]:
        async with self._condition:
            return set(self._active)

    async def try_acquire(self, task_id: str) -> bool:
        async with self._condition:
            if task_id in self._active:
                return False
            if len(self._active) >= self.max_parallel:
                return False
            self._active.add(task_id)
            return True

    async def release(self, task_id: str) -> None:
        async with self._condition:
            if task_id in self._active:
                self._active.remove(task_id)
                self._condition.notify_all()

    async def wait_for_change(self, timeout_s: float = 0.25) -> None:
        async with self._condition:
            try:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return


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


def _v5_root_branch(project_dir: Path, config: dict[str, Any]) -> str:
    """Branch that should receive root-level v5 integration work."""
    configured = str(config.get("default_branch") or "").strip()
    if configured:
        return configured
    try:
        from otto.config import detect_default_branch

        detected = detect_default_branch(project_dir)
        if detected:
            return detected
    except Exception:  # noqa: BLE001 - fall back to the historical default.
        pass
    return "main"


def _checkout_v5_branch_clean(
    *,
    project_dir: Path,
    branch: str,
    context: str,
    on_event: Any = None,
) -> dict[str, Any] | None:
    """Checkout ``branch`` for v5 orchestration.

    Dirty/failed checkout states are returned as preflight payloads so the
    repair loop can hand them to an agent. A clean checkout returns None.
    """
    from otto.v5_branching import (
        assert_clean_before_checkout,
        git_current_branch,
        git_status_porcelain,
    )

    is_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if is_repo.returncode != 0:
        return None
    current = git_current_branch(project_dir)
    if current == branch:
        try:
            remaining = git_status_porcelain(project_dir)
        except RuntimeError as exc:
            return _checkout_issue_payload(
                project_dir=project_dir,
                branch=branch,
                current_branch=current,
                context=context,
                kind="checkout_status_failed_at_phase",
                message=str(exc),
                status_lines=[],
                on_event=on_event,
            )
        if remaining:
            return _checkout_issue_payload(
                project_dir=project_dir,
                branch=branch,
                current_branch=current,
                context=context,
                kind="worktree_dirty_at_phase",
                message=(
                    f"already on {branch!r} during {context}, but worktree is dirty"
                ),
                status_lines=remaining,
                on_event=on_event,
            )
        return None
    try:
        assert_clean_before_checkout(
            project_dir=project_dir,
            source_branch=current,
            target_branch=branch,
        )
    except Exception as exc:  # noqa: BLE001 - repair agent decides the action.
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=current,
            context=context,
            kind="worktree_dirty_at_phase",
            message=str(exc),
            status_lines=getattr(exc, "dirty_status", ()),
            on_event=on_event,
        )
    cp = subprocess.run(
        ["git", "checkout", branch],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=current,
            context=context,
            kind="checkout_failed_at_phase",
            message=(
                f"checkout {branch!r} failed during {context} from "
                f"{current!r}: {detail}"
            ),
            status_lines=[],
            on_event=on_event,
        )
    actual = git_current_branch(project_dir)
    if actual != branch:
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=actual,
            context=context,
            kind="checkout_failed_at_phase",
            message=(
                f"checkout {branch!r} during {context} reported success but "
                f"current branch is {actual!r}"
            ),
            status_lines=[],
            on_event=on_event,
        )
    remaining = git_status_porcelain(project_dir)
    if remaining:
        return _checkout_issue_payload(
            project_dir=project_dir,
            branch=branch,
            current_branch=actual,
            context=context,
            kind="worktree_dirty_at_phase",
            message=f"checkout {branch!r} during {context} left dirty worktree",
            status_lines=remaining,
            on_event=on_event,
        )
    _emit(on_event, {
        "event": "project_branch_checked_out",
        "context": context,
        "from": current,
        "to": branch,
    })
    return None


def _checkout_issue_payload(
    *,
    project_dir: Path,
    branch: str,
    current_branch: str,
    context: str,
    kind: str,
    message: str,
    status_lines: Any,
    on_event: Any = None,
) -> dict[str, Any]:
    lines = [str(line) for line in (status_lines or []) if str(line).strip()]
    issue = {
        "kind": kind,
        "severity": "block",
        "message": (
            f"{message}\n"
            f"phase={context}; current_branch={current_branch}; target_branch={branch}\n"
            f"dirty_status:\n" + "\n".join(lines[:80])
        ).strip(),
        "phase": context,
        "current_branch": current_branch,
        "target_branch": branch,
        "paths": [_status_path(line) for line in lines if _status_path(line)],
        "diff_stat": _git_diff_stat(project_dir),
    }
    payload = {
        "check": "git_checkout_clean",
        "phase": context,
        "cwd": str(project_dir),
        "passed": False,
        "issues": [issue],
        "error": None,
    }
    _emit(on_event, {
        "event": "checkout_preflight_issue",
        "phase": context,
        "kind": kind,
        "message": issue["message"],
    })
    return payload


def _status_path(line: str) -> str:
    text = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in text:
        text = text.split(" -> ", 1)[1].strip()
    return text


def _git_diff_stat(project_dir: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


async def _checkout_v5_branch_clean_with_repair(
    *,
    project_dir: Path,
    branch: str,
    context: str,
    session_dir: Path,
    config: dict[str, Any],
    integration_branch: str | None,
    task_id: str,
    on_event: Any = None,
) -> dict[str, Any]:
    """Checkout a branch, repairing dirty/failed checkout states via agent."""

    def passed_payload() -> dict[str, Any]:
        return {
            "check": "git_checkout_clean",
            "phase": context,
            "cwd": str(project_dir),
            "passed": True,
            "issues": [],
            "error": None,
        }

    def run_once() -> dict[str, Any]:
        payload = _checkout_v5_branch_clean(
            project_dir=project_dir,
            branch=branch,
            context=context,
            on_event=on_event,
        )
        return payload or passed_payload()

    first = run_once()
    if not _integration_smoke_blocks(first):
        return first
    controller = PreflightRepairController(
        session_dir=session_dir,
        worktree_path=project_dir,
        agent_repair=lambda request: _run_preflight_repair_agent(
            request=request,
            task_id=task_id,
            project_dir=project_dir,
            config=config,
            integration_branch=integration_branch,
            on_event=on_event,
        ),
    )
    result = await controller.repair_until_clean(run_once, initial_payload=first)
    payload = result.preflight_payload
    payload["repair"] = result.to_jsonable()
    _emit(on_event, {
        "event": "checkout_repair_done",
        "task_id": task_id,
        "phase": context,
        "terminal_state": result.terminal_state,
        "attempts": len(result.attempts),
        "log_path": str(controller.log_path),
    })
    return payload


def _integration_restore_branch(
    project_dir: Path,
    task_id: str,
    config: dict[str, Any],
) -> str:
    """Where ``project_dir`` should be restored after a task integration."""
    task = get_task(project_dir, task_id) or {}
    branch = str(task.get("integration_branch") or "").strip()
    return branch or _v5_root_branch(project_dir, config)


def _commit_integration_agent_changes(
    *,
    project_dir: Path,
    task_id: str,
    worktree_path: Path,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Runner-owned commit for integration-agent edits."""
    if result.verdict == "catastrophic":
        return
    from otto.v5_branching import commit_integration_worktree

    ok, detail = commit_integration_worktree(
        worktree_path=worktree_path,
        task_id=task_id,
    )
    _emit(on_event, {
        "event": "integration_commit" if ok else "integration_commit_failed",
        "task_id": task_id,
        "worktree": str(worktree_path),
        "detail": detail,
    })
    if not ok:
        logger.warning("integration commit failed for %s: %s", task_id, detail)
        set_verdict(project_dir, task_id, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"


def _commit_root_inline_changes(
    *,
    project_dir: Path,
    root_branch: str,
    result: LeadResult,
    on_event: Any = None,
) -> None:
    """Runner-owned commit for root inline builds."""
    if result.decomposition != "inline" or result.verdict == "catastrophic":
        return

    from otto.v5_branching import commit_worktree, git_current_branch

    current_branch = git_current_branch(project_dir)
    if current_branch != root_branch:
        detail = (
            f"root inline finished on {current_branch!r}, expected {root_branch!r}; "
            "refusing runner commit"
        )
        _emit(on_event, {
            "event": "inline_commit_failed",
            "task_id": ROOT_TASK_ID,
            "worktree": str(project_dir),
            "detail": detail,
        })
        logger.warning(detail)
        set_verdict(project_dir, ROOT_TASK_ID, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"
        return

    ok, detail = commit_worktree(worktree_path=project_dir, message="v5 inline build")
    _emit(on_event, {
        "event": "inline_commit" if ok else "inline_commit_failed",
        "task_id": ROOT_TASK_ID,
        "worktree": str(project_dir),
        "detail": detail,
    })
    if not ok:
        logger.warning("root inline commit failed: %s", detail)
        set_verdict(project_dir, ROOT_TASK_ID, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"


def _propagate_subtree_integration(
    *,
    project_dir: Path,
    task_id: str,
) -> tuple[bool, str, str, str]:
    """Merge a decomposed child's integration branch into its parent target."""
    from otto.v5_branching import integration_branch_name, merge_branch_into

    child_entry = get_task(project_dir, task_id) or {}
    parent_id = child_entry.get("parent_task_id") or ROOT_TASK_ID
    target = "main" if parent_id == ROOT_TASK_ID else integration_branch_name(parent_id)
    source = integration_branch_name(task_id)
    if source == target:
        detail = (
            "refusing subtree integration self-merge: source and target are both "
            f"{source!r} for task {task_id!r} with parent_task_id={parent_id!r}; "
            "propagation would otherwise be a silent no-op"
        )
        set_verdict(project_dir, task_id, "merge_blocked")
        return False, detail, source, target

    try:
        ok, detail = merge_branch_into(
            project_dir=project_dir,
            source_branch=source,
            target_branch=target,
        )
    except MergeWorktreeDirtyError as exc:
        detail = str(exc)
        set_verdict(project_dir, task_id, "merge_blocked")
        return False, detail, source, target
    if not ok:
        set_verdict(project_dir, task_id, "merge_blocked")
    return ok, detail, source, target


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

        root_branch = _v5_root_branch(project_dir, config)
        root_session_id = _new_session_id()
        root_session_dir = _paths.session_dir(project_dir, root_session_id)
        root_session_dir.mkdir(parents=True, exist_ok=True)
        _emit(on_event, {"event": "session_open", "session_id": root_session_id})

        checkout_result = await _checkout_v5_branch_clean_with_repair(
            project_dir=project_dir,
            branch=root_branch,
            context="v5_pipeline_start",
            session_dir=root_session_dir,
            config=config,
            integration_branch=None,
            task_id=ROOT_TASK_ID,
            on_event=on_event,
        )
        if _preflight_repair_escalated(checkout_result) or _integration_smoke_blocks(checkout_result):
            result.verdict = "merge_blocked"
            result.failure_reason = _preflight_blocking_summary(
                "Pipeline start branch checkout could not be repaired",
                checkout_result,
            )
            return result

        # Clean up stale dev-server processes bound to this project's
        # declared ports. Each "port already in use" error inside an agent
        # session burns ~30-60s of agent time + tokens diagnosing it; one
        # cleanup pass up-front saves that across the whole run.
        try:
            from otto.v5_clean_verify import cleanup_stale_declared_ports

            killed = cleanup_stale_declared_ports(
                project_dir,
                logger_fn=lambda m: logger.info("preflight: %s", m),
            )
            if killed:
                _emit(on_event, {
                    "event": "stale_ports_cleaned",
                    "ports": sorted(killed),
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("port cleanup failed: %s", exc)

        # ---- Phase A: Root session setup ----

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
            decomp_runtime_context=_build_decomp_runtime_context(
                project_dir=project_dir,
                config=config,
                max_parallel=max_parallel,
                run_started_at=started,
                spec=spec,
            ),
        )
        result.root_lead_result = root_result
        _emit(on_event, {
            "event": "lead_done",
            "task_id": ROOT_TASK_ID,
            "verdict": root_result.verdict,
            "decomposition": root_result.decomposition,
            "emitted": len(root_result.emitted_subtask_ids),
        })
        _commit_root_inline_changes(
            project_dir=project_dir,
            root_branch=root_branch,
            result=root_result,
            on_event=on_event,
        )

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
                run_started_at=started,
            )
            # ---- Phase E: Run root integration ----
            child_summaries = _build_child_summaries(
                project_dir, ROOT_TASK_ID, result.child_results, result.integration_results
            )
            integration_session_dir = root_session_dir / "integration"
            integration_session_dir.mkdir(parents=True, exist_ok=True)
            # Copy the flat spec so the integration Lead's verify call can
            # find it; without this the verifier returns "unverified" even
            # when leaf children all passed (root integration doesn't get a
            # fresh sibling session — it lives under root_session_dir/).
            _root_spec = root_session_dir / "spec" / "spec.json"
            _integ_spec = integration_session_dir / "spec" / "spec.json"
            if _root_spec.exists() and not _integ_spec.exists():
                try:
                    _integ_spec.parent.mkdir(parents=True, exist_ok=True)
                    _integ_spec.write_text(
                        _root_spec.read_text(encoding="utf-8"), encoding="utf-8",
                    )
                except OSError as exc:
                    logger.warning("could not copy spec for root integration: %s", exc)
            _emit(on_event, {"event": "integration_start", "task_id": ROOT_TASK_ID})
            checkout_result = await _checkout_v5_branch_clean_with_repair(
                project_dir=project_dir,
                branch=root_branch,
                context="root_integration_start",
                session_dir=integration_session_dir,
                config=config,
                integration_branch=None,
                task_id=ROOT_TASK_ID,
                on_event=on_event,
            )
            if _preflight_repair_escalated(checkout_result) or _integration_smoke_blocks(checkout_result):
                preflight_result = checkout_result
            else:
                preflight_result = _run_integration_smoke_preflight(
                    worktree_path=project_dir,
                    task_id=ROOT_TASK_ID,
                    phase="pre_agent",
                    on_event=on_event,
                )
            if _preflight_repair_escalated(preflight_result):
                integration_result = _preflight_blocked_result(
                    task_id=ROOT_TASK_ID,
                    preflight_result=preflight_result,
                )
            else:
                integration_packet_path = _write_integration_packet(
                    project_dir=project_dir,
                    parent_task_id=ROOT_TASK_ID,
                    session_dir=integration_session_dir,
                    child_results=result.child_results,
                    integration_results=result.integration_results,
                    child_summaries=child_summaries,
                    preflight_result=preflight_result,
                    integration_branch=root_branch,
                    integration_worktree=project_dir,
                )
                integration_result = await run_lead(
                    task_id=ROOT_TASK_ID,
                    intent=intent,
                    project_dir=project_dir,
                    session_dir=integration_session_dir,
                    integration_branch=None,  # root integration ultimately merges to main
                    config=config,
                    kind="integration",
                    child_summaries=child_summaries,
                    preflight_result=preflight_result,
                    integration_packet_path=str(integration_packet_path),
                )
                _commit_integration_agent_changes(
                    project_dir=project_dir,
                    task_id=ROOT_TASK_ID,
                    worktree_path=project_dir,
                    result=integration_result,
                    on_event=on_event,
                )
            if _preflight_repair_escalated(preflight_result):
                post_preflight_result = preflight_result
            else:
                post_preflight_result = _run_integration_smoke_preflight(
                    worktree_path=project_dir,
                    task_id=ROOT_TASK_ID,
                    phase="post_agent",
                    on_event=on_event,
                )
            if integration_result.verify_result is None:
                integration_result.verify_result = {}
            if isinstance(integration_result.verify_result, dict):
                integration_result.verify_result["pre_integration_preflight"] = preflight_result
                integration_result.verify_result["post_integration_preflight"] = post_preflight_result
            if (
                integration_result.verdict != "catastrophic"
                and _integration_smoke_blocks(post_preflight_result)
            ):
                integration_result.verdict = "merge_blocked"
                integration_result.failure_reason = (
                    "Post-agent smoke_clean_deploy still has blocking issues: "
                    + "; ".join(
                        str(issue.get("message") or issue.get("kind"))
                        for issue in post_preflight_result.get("issues", [])
                        if isinstance(issue, dict)
                        and issue.get("severity") in ("error", "block")
                    )
                )
                if isinstance(integration_result.verify_result, dict):
                    integration_result.verify_result["verdict"] = "merge_blocked"
                    integration_result.verify_result["summary"] = integration_result.failure_reason
                set_verdict(
                    project_dir,
                    ROOT_TASK_ID,
                    "merge_blocked",
                    cost_usd=integration_result.cost_usd,
                )
                _emit(on_event, {
                    "event": "integration_smoke_failed",
                    "task_id": ROOT_TASK_ID,
                    "verdict": "merge_blocked",
                    "worktree": str(project_dir),
                })
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

            # ---- Phase F-pre: Reconcile recovered merge_blocked children ----
            # The integration agent's Step 0b can manually `git merge` the
            # build branches of merge_blocked children. When that succeeds,
            # the children's persisted verdict is still "merge_blocked"
            # (the runner has no way to know what the agent did). Without
            # reconciliation, aggregate_verdict() rolls up that stale
            # merge_blocked even when Step 0b restored everything.
            #
            # Check git: for each merge_blocked direct child of root, is
            # its build_branch an ancestor of the integration branch?
            # If yes, the integration agent merged it — update verdict to
            # "pass" so aggregate reflects the real product state.
            try:
                _reconcile_recovered_children(project_dir, ROOT_TASK_ID, on_event)
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("post-integration reconciliation failed: %s", exc)

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
    dispatch_lease: _DispatchLease | None = None,
    run_started_at: float | None = None,
) -> None:
    """Process the v5_pending queue for ``parent_task_id``'s subtree.

    Runs children concurrently (up to max_parallel), waits for all, then
    recursively handles any grandchildren. Returns when all descendants of
    ``parent_task_id`` have terminal verdicts.

    Best-effort: a child crash doesn't stop siblings.
    """
    completed: set[str] = set()
    in_flight: dict[str, asyncio.Task[Any]] = {}
    preflight_seen: set[str] = set()  # issue kinds already emitted, dedupe
    architect_preflight_done: set[tuple[str, int]] = set()
    if dispatch_lease is None:
        dispatch_lease = _DispatchLease(max_parallel)

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
                for leased_tid in list(in_flight):
                    await dispatch_lease.release(leased_tid)
                in_flight.clear()
            break

        # Pre-flight: deterministic checks on the task graph.
        graph = read_graph(project_dir)
        pending = read_pending(project_dir)
        issues = run_preflight(project_dir, graph, pending)
        for issue in issues:
            key = f"{issue.kind}:{issue.task_id or '-'}"
            if key in preflight_seen:
                continue
            preflight_seen.add(key)
            log_fn = logger.error if issue.severity in ("error", "block") else logger.warning
            log_fn("preflight %s [%s]: %s", issue.kind, issue.severity, issue.message)
            _emit(on_event, {
                "event": "preflight_issue",
                "kind": issue.kind,
                "severity": issue.severity,
                "message": issue.message,
                "task_id": issue.task_id,
            })

        # Find ready tasks not yet running.
        ready = take_ready(
            project_dir,
            completed_task_ids=completed,
            in_flight_task_ids=set(in_flight.keys()) | await dispatch_lease.active_task_ids(),
        )
        # Filter to descendants of parent_task_id.
        ready = [r for r in ready if _is_descendant_of(project_dir, r["task_id"], parent_task_id)]

        # Apply blocking pre-flight issues: drop blocked descendants from ready.
        if any(i.severity == "block" for i in issues):
            _filtered, blocked = filter_blocked_descendants(graph, ready, issues)
            if blocked:
                logger.warning(
                    "preflight blocked %d tasks from dispatching: %s",
                    len(blocked), sorted(blocked),
                )
            ready = _filtered

        # Post-architect scaffold compile check: run once when architect
        # transitions to verdict=pass, before feature children dispatch.
        # Catches "architect said pass but scaffold doesn't compile" —
        # otherwise discovered 20+ min later when features try to build on it.
        retry_architect = False
        tasks = (graph.get("tasks") or {})
        for architect_tid, architect_task in tasks.items():
            if not (
                (architect_task.get("intent") or "").lstrip().lower().startswith("architect")
                and architect_task.get("verdict") == "pass"
                and not (architect_task.get("depends_on") or [])
            ):
                continue
            retry_count = get_retry_count(project_dir, architect_tid)
            preflight_key = (architect_tid, retry_count)
            if preflight_key in architect_preflight_done:
                continue
            architect_preflight_done.add(preflight_key)
            logger.info("preflight: running scaffold compile check after architect-pass (task=%s)", architect_tid)
            compile_issues = check_scaffold_compiles(
                project_dir, architect_task_id=architect_tid
            )
            blocking_messages: list[str] = []
            for issue in compile_issues:
                key = f"{issue.kind}:scaffold:{architect_tid}:{retry_count}"
                if key in preflight_seen:
                    continue
                preflight_seen.add(key)
                log_fn = logger.error if issue.severity in ("error", "block") else logger.warning
                log_fn("preflight %s [%s]: %s", issue.kind, issue.severity, issue.message)
                _emit(on_event, {
                    "event": "preflight_issue",
                    "kind": issue.kind,
                    "severity": issue.severity,
                    "message": issue.message,
                })
                if issue.severity == "block":
                    blocking_messages.append(f"[{issue.kind}] {issue.message}")

            # Architect retry on blocking compile failure: scaffold
            # doesn't actually compile from a clean state. The
            # architect declared pass based on its in-session state
            # (where node_modules was populated). Invalidate the
            # verdict, surface the failure, and re-dispatch.
            if blocking_messages:
                current_retries = get_retry_count(project_dir, architect_tid)
                if current_retries < MAX_ARCHITECT_RETRIES:
                    reason = (
                        "Clean-state preflight failed for your scaffold. "
                        "The runner copied your output to a temp dir, ran "
                        "`script_valid`, `npm ci`, `npm run build`, and `py_compile`, and got "
                        "these errors:\n\n"
                        + "\n".join(f"  - {m}" for m in blocking_messages)
                        + "\n\nFix the underlying bug — don't lean on in-session "
                        "state (your existing node_modules, venv) that won't "
                        "survive handoff. Re-emit your scaffold."
                    )
                    new_count = clear_verdict_for_retry(
                        project_dir, architect_tid, reason
                    )
                    completed.discard(architect_tid)
                    child_results.pop(architect_tid, None)
                    logger.warning(
                        "architect %s scaffold preflight failed (attempt %d/%d): re-dispatching",
                        architect_tid,
                        new_count,
                        MAX_ARCHITECT_RETRIES,
                    )
                    _emit(on_event, {
                        "event": "architect_retry",
                        "task_id": architect_tid,
                        "retry_count": new_count,
                        "max_retries": MAX_ARCHITECT_RETRIES,
                        "reason_tail": blocking_messages[-1][:200],
                    })
                    # The architect is now eligible for re-dispatch, but
                    # the `ready` list computed at the top of this loop
                    # iteration is stale (the architect wasn't in it).
                    # Re-enter the loop so take_ready picks it up.
                    retry_architect = True
                    break
                logger.error(
                    "architect %s scaffold preflight failed after %d retries; "
                    "descendants will remain blocked",
                    architect_tid,
                    MAX_ARCHITECT_RETRIES,
                )
                _emit(on_event, {
                    "event": "architect_retry_exhausted",
                    "task_id": architect_tid,
                    "retry_count": current_retries,
                })
                continue

            # Architect passed AND scaffold preflight is clean.
            # Run shared toolchain preflight in the architect
            # worktree so ignored install dirs exist there before
            # propagation. This is non-blocking optimization state:
            # clean scaffold verification above remains the
            # correctness gate, while this preflight logs exactly
            # what dependency bootstrap commands ran.
            arch_worktree = project_dir / ".worktrees" / architect_tid
            try:
                from otto.v5_clean_verify import preflight_shared_toolchains

                toolchain_started = time.monotonic()
                toolchain_result = preflight_shared_toolchains(
                    arch_worktree,
                    timeout_s=300,
                    logger_fn=lambda m: logger.info("preflight: %s", m),
                )
                toolchain_duration_s = round(time.monotonic() - toolchain_started, 3)
                toolchain_payload = toolchain_result.to_jsonable()
                toolchain_payload["duration_s"] = toolchain_duration_s
                log_path = _write_toolchain_preflight_log(
                    project_dir=project_dir,
                    architect_task_id=architect_tid,
                    retry_count=retry_count,
                    result=toolchain_payload,
                )
                logger.info(
                    "preflight: toolchain preflight completed for architect %s in %.3fs "
                    "(passed=%s, commands=%d)",
                    architect_tid,
                    toolchain_duration_s,
                    toolchain_result.passed,
                    len(toolchain_result.commands),
                )
                _emit(on_event, {
                    "event": "toolchain_preflight_done",
                    "architect_task_id": architect_tid,
                    "passed": toolchain_result.passed,
                    "command_count": len(toolchain_result.commands),
                    "duration_s": toolchain_duration_s,
                    "log_path": str(log_path),
                })
                if not toolchain_result.passed:
                    logger.warning(
                        "toolchain preflight had failures for architect %s: %s",
                        architect_tid,
                        "; ".join(toolchain_result.failure_messages[:3]),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("toolchain preflight failed: %s", exc)

            # Propagate its node_modules/.venv into project_dir so
            # feature children's worktrees can symlink instead of
            # re-running `npm install` / `uv sync`.
            try:
                n = _propagate_install_dirs_from_architect(
                    project_dir, arch_worktree
                )
                logger.info(
                    "preflight: architect %s install-dir propagation complete (count=%d)",
                    architect_tid,
                    n,
                )
                _emit(on_event, {
                    "event": "install_dirs_propagated",
                    "architect_task_id": architect_tid,
                    "count": n,
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "propagate install dirs from architect failed: %s", exc
                )

            # Source-of-truth fix (Part A): build the capability
            # inventory from the actual scaffold + inject it into
            # CHARTER as a managed "Detected Infrastructure" block.
            # This gives feature children a deterministic source
            # for operational facts (scripts, deps, configs) that
            # the architect can't unintentionally contradict in
            # prose.
            inv = None
            try:
                from otto.v5_capability_inventory import (
                    build_inventory, render_inventory, inject_into_charter,
                    check_coherence,
                )
                inv = build_inventory(project_dir)
                rendered = render_inventory(inv)
                if inject_into_charter(project_dir, rendered):
                    logger.info(
                        "Detected Infrastructure section injected into CHARTER.md "
                        "(%d package.jsons, %d pyprojects, %d configs)",
                        len(inv.package_jsons),
                        len(inv.pyprojects),
                        len(inv.known_configs),
                    )
                    _emit(on_event, {
                        "event": "capability_inventory_injected",
                        "package_json_count": len(inv.package_jsons),
                        "pyproject_count": len(inv.pyprojects),
                        "known_config_count": len(inv.known_configs),
                    })
            except Exception as exc:  # noqa: BLE001
                logger.warning("capability inventory injection failed: %s", exc)

            # Source-of-truth fix (Part B): coherence gate
            # (warning-only). For each backticked reference in the
            # architect's "Agent operating notes" section, verify
            # the path/script/command actually resolves against the
            # scaffold. Emit warnings; don't block dispatch.
            try:
                if inv is not None:
                    findings = check_coherence(project_dir, inv)
                    for f in findings:
                        logger.warning(
                            "coherence: %s — %s (in CHARTER operating notes)",
                            f.kind, f.detail,
                        )
                        _emit(on_event, {
                            "event": "coherence_finding",
                            "kind": f.kind,
                            "reference": f.reference,
                            "detail": f.detail,
                        })
            except Exception as exc:  # noqa: BLE001
                logger.warning("coherence check raised: %s", exc)
        if retry_architect:
            continue

        # Spawn ready tasks up to max_parallel.
        spawned_any = False
        for entry in ready:
            tid = entry["task_id"]
            if not await dispatch_lease.try_acquire(tid):
                continue
            in_flight[tid] = asyncio.create_task(
                _run_child(
                    project_dir=project_dir,
                    entry=entry,
                    config=config,
                    max_parallel=max_parallel,
                    run_started_at=run_started_at,
                    on_event=on_event,
                )
            )
            spawned_any = True
            _emit(on_event, {"event": "child_dispatch", "task_id": tid})

        # If nothing in flight and nothing ready, we're done.
        if not in_flight and not ready:
            break
        if not in_flight and ready and not spawned_any:
            await dispatch_lease.wait_for_change()
            continue

        # Wait for at least one to complete.
        if in_flight:
            done, _pending = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for fut in done:
                # Find which task this future belongs to.
                tid = next(t for t, f in in_flight.items() if f is fut)
                in_flight.pop(tid, None)
                released = False
                try:
                    result: LeadResult = fut.result()
                    await dispatch_lease.release(tid)
                    released = True
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
                            dispatch_lease=dispatch_lease,
                            run_started_at=run_started_at,
                        )
                        # Run this child's integration Lead.
                        integ_result = await _run_integration(
                            project_dir=project_dir,
                            task_id=tid,
                            intent=(get_task(project_dir, tid) or {}).get("intent", ""),
                            config=config,
                            child_results=child_results,
                            integration_results=integration_results,
                            on_event=on_event,
                        )
                        # Propagate this subtree's integration up to the
                        # parent's integration branch. WITHOUT THIS, a
                        # decomposed child's work stays orphaned on
                        # i2p/integ/<tid> and never lands on main — the
                        # chat-platform decomp shipped a broken product
                        # because the web subtree never propagated.
                        if integ_result.verdict in ("pass", "partial", "unverified"):
                            try:
                                ok, detail, source, target = _propagate_subtree_integration(
                                    project_dir=project_dir,
                                    task_id=tid,
                                )
                                _emit(on_event, {
                                    "event": "subtree_propagated" if ok else "subtree_propagation_blocked",
                                    "task_id": tid,
                                    "source": source,
                                    "target": target,
                                    "detail": detail,
                                })
                                if not ok:
                                    logger.warning(
                                        "subtree integration propagation failed for %s: %s",
                                        tid, detail,
                                    )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "subtree propagation crashed for %s: %s",
                                    tid, exc,
                                )

                except Exception as exc:  # noqa: BLE001
                    if not released:
                        await dispatch_lease.release(tid)
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
    max_parallel: int = 1,
    run_started_at: float | None = None,
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
            # Share node_modules / .venv across worktrees. Without this,
            # every child re-downloads packages from scratch (~30-60s each,
            # ~5-7min total on a 7-task tree). Worktrees share the same
            # package.json / pyproject.toml at this branch so the deps are
            # identical. The earlier implementation only looked at
            # project_dir/{node_modules,.venv} — but real projects put them
            # in subdirs (frontend/node_modules, api/.venv), so the symlink
            # never fired. Glob for them now.
            linked_install_dirs = _link_shared_install_dirs(project_dir, child_worktree, tid)
            if linked_install_dirs:
                logger.info(
                    "linked %d shared install dir(s) into child %s",
                    linked_install_dirs,
                    tid,
                )
                _emit(on_event, {
                    "event": "install_dirs_linked",
                    "task_id": tid,
                    "count": linked_install_dirs,
                })
            _emit(on_event, {
                "event": "worktree_created",
                "task_id": tid,
                "path": str(child_worktree),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree setup for child %s failed: %s", tid, exc)

    context_slice_note = ""
    if _context_slicing_enabled(config):
        try:
            from otto.v5_context_slicer import write_context_slice_for_child

            full_charter_path = (child_worktree or project_dir) / "CHARTER.md"
            if not full_charter_path.exists():
                full_charter_path = project_dir / "CHARTER.md"
            slice_result = write_context_slice_for_child(
                project_dir=project_dir,
                child_session_dir=child_session_dir,
                child_scope=_child_scope_from_entry(tid, entry),
                parent_spec_path=parent_spec,
                full_charter_path=full_charter_path,
                child_spec_path=child_spec_path,
            )
            context_slice_note = slice_result.context_note
            _emit(on_event, {
                "event": "context_slice",
                "task_id": tid,
                "fallback_to_full": slice_result.audit.get("fallback_to_full"),
                "included_entities": slice_result.audit.get("included_entities", []),
                "excluded_entities": slice_result.audit.get("excluded_entities", []),
                "log_path": str(child_session_dir / "context_slice.json"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("context slicing failed for child %s: %s", tid, exc)
            log_path = _write_context_slice_failure_log(
                child_session_dir=child_session_dir,
                child_id=tid,
                reason=f"{type(exc).__name__}: {exc}",
            )
            context_slice_note = (
                "Context slicing was requested, but Otto fell back to full "
                f"context after a slicing error. Audit log: `{log_path}`."
            )

    # Augment intent with retry context if this is a re-dispatch after a
    # runner-side check (e.g., scaffold preflight) invalidated the agent's
    # prior verdict. The reason explains what failed; the agent is
    # responsible for fixing it before declaring pass again.
    intent = entry["intent"]
    retry_reason = get_retry_reason(project_dir, tid)
    if retry_reason:
        intent = (
            "## RETRY — previous attempt failed runner-side verification\n\n"
            f"{retry_reason}\n\n"
            "Your previous code is on the same branch; iterate on it, "
            "fix the underlying issue, and re-declare pass only after the "
            "build genuinely works.\n\n"
            "---\n\n"
            "## Original intent (your scope hasn't changed)\n\n"
            f"{intent}"
        )

    # Run the Lead. If we created a worktree, lead.py's _resolve_worktree picks it up.
    result = await _run_lead_with_fallback(
        task_id=tid,
        intent=intent,
        project_dir=project_dir,
        session_dir=child_session_dir,
        integration_branch=parent_integration_branch,
        config=config,
        kind="plan_or_inline",
        context_slice_note=context_slice_note,
        decomp_runtime_context=_build_decomp_runtime_context(
            project_dir=project_dir,
            config=config,
            max_parallel=max_parallel,
            run_started_at=run_started_at,
            spec_path=child_spec_path,
        ),
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
        # Sync the in-memory LeadResult so downstream consumers (event
        # emitters, child_summaries) see the same verdict as the graph.
        result.verdict = "merge_blocked"
        return

    try:
        ok, detail = merge_child_into_integration(
            project_dir=project_dir,
            child_task_id=child_task_id,
            parent_integration_branch=parent_integration_branch,
        )
    except MergeWorktreeDirtyError as exc:
        ok = False
        detail = str(exc)
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
        result.verdict = "merge_blocked"  # in-memory sync (see above)
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
    context_slice_note: str = "",
    decomp_runtime_context: dict[str, Any] | None = None,
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
        context_slice_note=context_slice_note,
        decomp_runtime_context=decomp_runtime_context,
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
        context_slice_note=context_slice_note,
        decomp_runtime_context=decomp_runtime_context,
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


def _preflight_issue_payload(issue: Any) -> dict[str, Any]:
    return {
        "kind": getattr(issue, "kind", "unknown"),
        "severity": getattr(issue, "severity", "warn"),
        "message": getattr(issue, "message", ""),
        "task_id": getattr(issue, "task_id", None),
    }


def _run_integration_smoke_preflight(
    *,
    worktree_path: Path,
    task_id: str,
    phase: str,
    on_event: Any = None,
) -> dict[str, Any]:
    """Run clean-deploy smoke for an integration worktree and serialize it."""
    payload: dict[str, Any] = {
        "check": "smoke_clean_deploy",
        "phase": phase,
        "task_id": task_id,
        "cwd": str(worktree_path),
        "passed": True,
        "issues": [],
        "error": None,
    }
    try:
        logger.info(
            "preflight: running %s integration clean-deploy check in %s",
            phase,
            worktree_path,
        )
        smoke_issues = smoke_clean_deploy(
            worktree_path,
            timeout_s=90,
            port_wait_s=12,
            logger_fn=lambda m: logger.info("preflight: %s", m),
        )
        payload["issues"] = [_preflight_issue_payload(issue) for issue in smoke_issues]
        payload["passed"] = not smoke_issues
        for issue in smoke_issues:
            issue_payload = _preflight_issue_payload(issue)
            log_fn = (
                logger.error
                if issue_payload["severity"] in ("error", "block")
                else logger.warning
            )
            log_fn(
                "preflight %s [%s]: %s",
                issue_payload["kind"],
                issue_payload["severity"],
                issue_payload["message"],
            )
            _emit(on_event, {
                "event": "preflight_issue",
                "phase": phase,
                "worktree": str(worktree_path),
                "kind": issue_payload["kind"],
                "severity": issue_payload["severity"],
                "message": issue_payload["message"],
            })
    except Exception as exc:  # noqa: BLE001 - report to agent, do not crash runner
        payload["passed"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("integration %s smoke check raised: %s", phase, exc)
        _emit(on_event, {
            "event": "preflight_issue",
            "phase": phase,
            "worktree": str(worktree_path),
            "kind": "clean_deploy_exception",
            "severity": "warn",
            "message": payload["error"],
        })
    return payload


def _integration_smoke_blocks(payload: dict[str, Any]) -> bool:
    issues = payload.get("issues") or []
    return any(
        isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
        for issue in issues
    )


def _preflight_repair_escalated(payload: dict[str, Any]) -> bool:
    repair = payload.get("repair")
    return isinstance(repair, dict) and repair.get("terminal_state") == "escalated"


def _preflight_blocked_result(
    *,
    task_id: str,
    preflight_result: dict[str, Any],
) -> LeadResult:
    reason = _preflight_blocking_summary(
        "Integration preflight repair escalated before agent dispatch",
        preflight_result,
    )
    return LeadResult(
        task_id=task_id,
        verdict="merge_blocked",
        verify_called=True,
        verify_result={
            "verdict": "merge_blocked",
            "summary": reason,
            "pre_integration_preflight": preflight_result,
        },
        failure_reason=reason,
    )


def _preflight_blocking_summary(prefix: str, payload: dict[str, Any]) -> str:
    messages = [
        str(issue.get("message") or issue.get("kind"))
        for issue in payload.get("issues", [])
        if isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
    ]
    return prefix + (": " + "; ".join(messages) if messages else "")


async def _run_preflight_repair_agent(
    *,
    request: AgentRepairRequest,
    task_id: str,
    project_dir: Path,
    config: dict[str, Any],
    integration_branch: str | None,
    on_event: Any = None,
) -> AgentRepairResult:
    """Dispatch a focused build Lead to repair a preflight failure."""
    from otto.safe_slug import safe_slug

    repair_slug = safe_slug(
        f"{task_id}-preflight-{request.failure_kind}-{request.attempt_index}",
        max_len=48,
    )
    repair_session_dir = request.session_dir / "preflight-repair" / repair_slug
    repair_session_dir.mkdir(parents=True, exist_ok=True)
    link_path = repair_session_dir / "worktree"
    if not link_path.exists():
        try:
            link_path.symlink_to(request.worktree_path)
        except OSError:
            pass

    scope_text = (
        "\n".join(f"- {path}" for path in request.workspace_paths)
        if request.workspace_paths
        else "- No precise file path was detected; inspect the failure surface from context."
    )
    git_status = _git_status_short(request.worktree_path)
    intent = (
        "PRE-FLIGHT REPAIR ONLY.\n\n"
        f"Failure kind: {request.failure_kind}\n"
        f"Failure message: {request.issue.get('message')}\n\n"
        "Raw issue JSON:\n"
        f"{json.dumps(request.issue, indent=2, sort_keys=True, default=str)}\n\n"
        "Current git status:\n"
        f"{git_status or '(clean)'}\n\n"
        "Likely paths, if any:\n"
        f"{scope_text}\n\n"
        f"{request.instruction}\n\n"
        "You may edit the directly relevant files needed to fix this failure. "
        "Run the narrowest relevant check, write a canonical verdict.json, and stop."
    )
    _emit(on_event, {
        "event": "preflight_repair_agent_start",
        "task_id": task_id,
        "failure_kind": request.failure_kind,
        "workspace_paths": list(request.workspace_paths),
        "session_dir": str(repair_session_dir),
    })
    result = await _run_lead_with_fallback(
        task_id=repair_slug,
        intent=intent,
        project_dir=project_dir,
        session_dir=repair_session_dir,
        integration_branch=integration_branch,
        config=config,
        kind="plan_or_inline",
        context_slice_note=(
            "This is a focused runner preflight repair. Stay within the "
            "listed workspace paths unless the failure message proves a "
            "directly adjacent file is required."
        ),
        on_event=on_event,
    )
    ok = result.verdict != "catastrophic"
    _emit(on_event, {
        "event": "preflight_repair_agent_done",
        "task_id": task_id,
        "failure_kind": request.failure_kind,
        "verdict": result.verdict,
        "cost_usd": result.cost_usd,
    })
    return AgentRepairResult(
        ok=ok,
        cost_usd=result.cost_usd,
        summary=result.failure_reason or result.final_text[:300] or result.verdict,
    )


def _git_status_short(worktree_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


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

    # The integration Lead's verify call needs spec.json (same shape as build
    # children get via _run_child). Find any earlier session that has it and
    # copy. Fall back silently if no spec exists yet — verifier handles that.
    target_spec = integration_session_dir / "spec" / "spec.json"
    if not target_spec.exists():
        try:
            sessions_root = project_dir / "otto_logs" / "sessions"
            if sessions_root.exists():
                for sib in sorted(sessions_root.iterdir()):
                    candidate = sib / "spec" / "spec.json"
                    if candidate.exists():
                        target_spec.parent.mkdir(parents=True, exist_ok=True)
                        target_spec.write_text(
                            candidate.read_text(encoding="utf-8"),
                            encoding="utf-8",
                        )
                        break
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("could not copy spec for integration session: %s", exc)

    # Provide child summaries to the integration Lead's prompt.
    summaries = _build_child_summaries(project_dir, task_id, child_results, integration_results)

    # The integration Lead must run in a worktree that holds the merged
    # children's work — a worktree checked out to THIS task's integration
    # branch (where its children merged). Without this, the Lead defaults
    # to project_dir (typically `main`) and verify sees an empty workspace.
    #
    # Each task in the graph stores `integration_branch` = the branch the
    # task itself merges INTO (one level up). For the integration session
    # we instead want THIS task's OWN integration branch (where its children
    # merged), namespaced as `i2p/<task_id>/integration`.
    from otto.v5_branching import integration_branch_name as _integ
    own_integration_branch = _integ(task_id)
    integration_worktree: Path | None = None
    try:
        from otto.v5_branching import (
            child_worktree_path,
            ensure_branch_exists,
        )
        from otto.worktree import add_worktree

        # Probe whether this branch is already checked out somewhere (e.g.
        # the project_dir itself, in greenfield-where-root-merges-to-main
        # cases). If so, use that path directly — git refuses to check the
        # same branch out twice.
        existing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_dir), capture_output=True, text=True,
        )
        existing_path: Path | None = None
        if existing.returncode == 0:
            block_path: str | None = None
            for line in existing.stdout.splitlines():
                if line.startswith("worktree "):
                    block_path = line[len("worktree "):].strip()
                elif line.startswith("branch ") and block_path:
                    if line.endswith(f"/{own_integration_branch}") or line.endswith(own_integration_branch):
                        existing_path = Path(block_path)
                        break

        if existing_path is not None and existing_path.exists():
            integration_worktree = existing_path
        else:
            ensure_branch_exists(project_dir, own_integration_branch, base_ref="main")
            wt_path = child_worktree_path(project_dir, f"integ-{task_id}")
            if not (wt_path.exists() and (wt_path / ".git").exists()):
                try:
                    add_worktree(
                        project_dir=project_dir,
                        worktree_path=wt_path,
                        branch=own_integration_branch,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "integration worktree add failed for %s: %s; falling back",
                        task_id, exc,
                    )
                    wt_path = None  # type: ignore[assignment]
            if wt_path is not None and wt_path.exists():
                integration_worktree = wt_path

        if integration_worktree is not None:
            link_path = integration_session_dir / "worktree"
            if not link_path.exists():
                try:
                    link_path.symlink_to(integration_worktree)
                except OSError as exc:
                    logger.warning("symlink worktree failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("integration worktree setup failed: %s", exc)

    parent_integration_branch = own_integration_branch
    integration_cwd = integration_worktree or project_dir
    preflight_result = _run_integration_smoke_preflight(
        worktree_path=integration_cwd,
        task_id=task_id,
        phase="pre_agent",
        on_event=on_event,
    )

    _emit(on_event, {"event": "integration_start", "task_id": task_id})
    if _preflight_repair_escalated(preflight_result):
        result = _preflight_blocked_result(
            task_id=task_id,
            preflight_result=preflight_result,
        )
    else:
        integration_packet_path = _write_integration_packet(
            project_dir=project_dir,
            parent_task_id=task_id,
            session_dir=integration_session_dir,
            child_results=child_results,
            integration_results=integration_results,
            child_summaries=summaries,
            preflight_result=preflight_result,
            integration_branch=parent_integration_branch,
            integration_worktree=integration_cwd,
        )
        result = await run_lead(
            task_id=task_id,
            intent=intent,
            project_dir=project_dir,
            session_dir=integration_session_dir,
            integration_branch=parent_integration_branch,
            config=config,
            kind="integration",
            child_summaries=summaries,
            preflight_result=preflight_result,
            integration_packet_path=str(integration_packet_path),
        )
        _commit_integration_agent_changes(
            project_dir=project_dir,
            task_id=task_id,
            worktree_path=integration_cwd,
            result=result,
            on_event=on_event,
        )
    if _preflight_repair_escalated(preflight_result):
        post_preflight_result = preflight_result
    else:
        post_preflight_result = _run_integration_smoke_preflight(
            worktree_path=integration_cwd,
            task_id=task_id,
            phase="post_agent",
            on_event=on_event,
        )
    if result.verify_result is None:
        result.verify_result = {}
    if isinstance(result.verify_result, dict):
        result.verify_result["pre_integration_preflight"] = preflight_result
        result.verify_result["post_integration_preflight"] = post_preflight_result
    if result.verdict != "catastrophic" and _integration_smoke_blocks(post_preflight_result):
        result.verdict = "merge_blocked"
        result.failure_reason = (
            "Post-agent smoke_clean_deploy still has blocking issues: "
            + "; ".join(
                str(issue.get("message") or issue.get("kind"))
                for issue in post_preflight_result.get("issues", [])
                if isinstance(issue, dict)
                and issue.get("severity") in ("error", "block")
            )
        )
        if isinstance(result.verify_result, dict):
            result.verify_result["verdict"] = "merge_blocked"
            result.verify_result["summary"] = result.failure_reason
        set_verdict(project_dir, task_id, "merge_blocked", cost_usd=result.cost_usd)
        _emit(on_event, {
            "event": "integration_smoke_failed",
            "task_id": task_id,
            "verdict": "merge_blocked",
            "worktree": str(integration_cwd),
        })
    integration_results[task_id] = result
    _emit(on_event, {"event": "integration_done", "task_id": task_id, "verdict": result.verdict})
    restore_branch = _integration_restore_branch(project_dir, task_id, config)
    restore_result = await _checkout_v5_branch_clean_with_repair(
        project_dir=project_dir,
        branch=restore_branch,
        context=f"integration_return:{task_id}",
        session_dir=integration_session_dir,
        config=config,
        integration_branch=parent_integration_branch,
        task_id=task_id,
        on_event=on_event,
    )
    if _preflight_repair_escalated(restore_result) or _integration_smoke_blocks(restore_result):
        detail = _preflight_blocking_summary(
            f"could not restore project_dir after integration {task_id} to {restore_branch}",
            restore_result,
        )
        logger.warning(
            "could not restore project_dir after integration %s to %s: %s",
            task_id, restore_branch, detail,
        )
        _emit(on_event, {
            "event": "integration_restore_failed",
            "task_id": task_id,
            "target": restore_branch,
            "detail": detail,
        })
        set_verdict(project_dir, task_id, "merge_blocked", cost_usd=result.cost_usd)
        result.verdict = "merge_blocked"
    return result


def _write_integration_packet(
    *,
    project_dir: Path,
    parent_task_id: str,
    session_dir: Path,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
    child_summaries: list[dict[str, Any]],
    preflight_result: dict[str, Any],
    integration_branch: str,
    integration_worktree: Path,
) -> Path:
    from otto.v5_branching import child_branch_name, integration_branch_name

    children: list[dict[str, Any]] = []
    for cid in children_of(project_dir, parent_task_id):
        entry = get_task(project_dir, cid) or {}
        result = integration_results.get(cid) or child_results.get(cid)
        build_branch = child_branch_name(cid)
        subtree_branch = integration_branch_name(cid)
        verdict_payload = (
            result.verify_result
            if result is not None and isinstance(result.verify_result, dict)
            else {}
        )
        children.append({
            "task_id": cid,
            "intent": entry.get("intent", ""),
            "task_graph": entry,
            "session_dir": _find_session_dir_for_task(project_dir, cid),
            "agent_session_id": result.agent_session_id if result else "",
            "branches": {"build": build_branch, "integration": subtree_branch},
            "changed_files": {
                "build": _changed_files_for_branch(project_dir, build_branch),
                "integration": _changed_files_for_branch(project_dir, subtree_branch),
            },
            "verdict": result.verdict if result else entry.get("verdict"),
            "verdict_json": verdict_payload,
            "intent_coverage": verdict_payload.get("intent_coverage") if verdict_payload else None,
            "decisions_appended": verdict_payload.get("decisions_appended") if verdict_payload else [],
            "runner_checks": verdict_payload.get("runner_checks") if verdict_payload else [],
        })
    packet = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parent_task_id": parent_task_id,
        "integration_branch": integration_branch,
        "integration_worktree": str(integration_worktree),
        "child_summaries": child_summaries,
        "children": children,
        "preflight_results": {"pre_agent": preflight_result},
        "applicable_journey_ids": _journey_ids_from_spec(session_dir / "spec" / "spec.json"),
    }
    path = session_dir / "integration_packet.json"
    path.write_text(json.dumps(packet, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def _find_session_dir_for_task(project_dir: Path, task_id: str) -> str:
    sessions_root = project_dir / "otto_logs" / "sessions"
    if not sessions_root.exists():
        return ""
    matches: list[Path] = []
    for summary in sessions_root.glob("*/summary.json"):
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            matches.append(summary.parent)
    return str(sorted(matches)[-1]) if matches else ""


def _changed_files_for_branch(project_dir: Path, branch: str) -> list[str]:
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return []
    base = "main"
    base_exists = subprocess.run(
        ["git", "rev-parse", "--verify", base],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if base_exists.returncode != 0:
        base = "HEAD"
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{branch}"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _journey_ids_from_spec(spec_path: Path) -> list[str]:
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    journeys = payload.get("behavior_journeys") if isinstance(payload, dict) else []
    return [
        str(journey.get("id"))
        for journey in journeys or []
        if isinstance(journey, dict) and journey.get("id")
    ]


def _build_child_summaries(
    project_dir: Path,
    parent_task_id: str,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult] | None = None,
) -> list[dict[str, Any]]:
    """Build the child summary list passed to integration Lead's prompt.

    For merge_blocked children, include the build branch name and a
    pointer so the integration Lead can choose to hand-merge instead of
    re-implementing from scratch. The work is preserved on the branch;
    only the mechanical merge failed.
    """
    from otto.v5_branching import child_branch_name
    integration_results = integration_results or {}
    out: list[dict[str, Any]] = []
    for cid in children_of(project_dir, parent_task_id):
        entry = get_task(project_dir, cid) or {}
        result = child_results.get(cid)
        # Task graph verdict is authoritative for merge outcomes — the
        # in-memory `result.verdict` stays at the agent's self-declared
        # value (e.g., "pass") even after `_merge_child_branch()` writes
        # "merge_blocked" to the graph. If we trust the stale result,
        # the integration agent never sees the merge failure and skips
        # Step 0b recovery. Prefer the graph verdict when it's terminal-
        # for-the-merge-path; fall back to result.verdict otherwise.
        graph_verdict = entry.get("verdict")
        if graph_verdict == "merge_blocked":
            verdict = "merge_blocked"
        elif result is not None:
            verdict = result.verdict
        else:
            verdict = graph_verdict or "unknown"
        record: dict[str, Any] = {
            "task_id": cid,
            "intent": entry.get("intent", ""),
            "verdict": verdict,
            "summary": (result.final_text if result else "")[:200],
            "cost_usd": result.cost_usd if result else float(entry.get("cost_usd", 0.0)),
        }
        if verdict == "pending_children":
            reconstructed = _reconstruct_decomposed_child_summary(
                project_dir=project_dir,
                task_id=cid,
                child_results=child_results,
                integration_results=integration_results,
            )
            if reconstructed is not None:
                record.update(reconstructed)
        # Surface the build branch for merge_blocked children so the
        # integration Lead can recover their work via git rather than
        # dispatching the build agent to rewrite it.
        if verdict == "merge_blocked":
            record["build_branch"] = child_branch_name(cid)
            record["recovery_hint"] = (
                f"Work passed verify but failed to merge. Try "
                f"`git merge {record['build_branch']}` in this worktree, "
                f"resolve any remaining conflicts by hand (most are likely "
                f"trivial), and commit. DO NOT re-implement the feature "
                f"from scratch — the source files exist on that branch."
            )
        out.append(record)
    return out


_SUMMARY_VERDICT_SEVERITY = {
    "pass": 0,
    "pending_children": 1,
    "partial": 2,
    "unverified": 3,
    "merge_blocked": 4,
    "catastrophic": 5,
}


def _coverage_from_result(result: LeadResult | None) -> dict[str, Any] | None:
    payload = result.verify_result if result is not None else None
    if not isinstance(payload, dict):
        return None
    coverage = payload.get("intent_coverage")
    return coverage if isinstance(coverage, dict) else None


def _merge_intent_coverage(items: list[dict[str, Any] | None]) -> dict[str, Any]:
    merged: dict[str, Any] = {"built": [], "partial": [], "skipped": []}
    for coverage in items:
        if not isinstance(coverage, dict):
            continue
        for key in ("built", "partial", "skipped"):
            values = coverage.get(key) or []
            if isinstance(values, list):
                merged[key].extend(values)
    return merged


def _summary_record_from_result(
    task_id: str,
    result: LeadResult,
    *,
    source: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "verdict": result.verdict,
        "summary": (result.final_text or "")[:200],
        "cost_usd": result.cost_usd,
        "reconstructed_from": source,
    }
    coverage = _coverage_from_result(result)
    if coverage is not None:
        record["intent_coverage"] = coverage
    if result.agent_session_id:
        record["agent_session_id"] = result.agent_session_id
    record["source_task_id"] = task_id
    return record


def _reconstruct_decomposed_child_summary(
    *,
    project_dir: Path,
    task_id: str,
    child_results: dict[str, LeadResult],
    integration_results: dict[str, LeadResult],
) -> dict[str, Any] | None:
    """Replace a stale planning verdict with resolved subtree integration.

    A parent Lead that decomposed has ``verdict=pending_children`` from its
    planning session. Once its own integration session runs, that integration
    verdict is the relevant summary for the parent/root integrator. If the
    integration is not present yet, preserve pending semantics for back-compat.
    """
    integration_result = integration_results.get(task_id)
    if integration_result is not None:
        return _summary_record_from_result(
            task_id,
            integration_result,
            source="subtree_integration",
        )

    descendant_records: list[dict[str, Any]] = []
    for child_id in children_of(project_dir, task_id):
        child_entry = get_task(project_dir, child_id) or {}
        child_result = integration_results.get(child_id) or child_results.get(child_id)
        child_verdict = (
            child_result.verdict
            if child_result is not None
            else child_entry.get("verdict")
        )
        if child_verdict == "pending_children":
            nested = _reconstruct_decomposed_child_summary(
                project_dir=project_dir,
                task_id=child_id,
                child_results=child_results,
                integration_results=integration_results,
            )
            if nested is None:
                return None
            nested = dict(nested)
            nested["task_id"] = child_id
            descendant_records.append(nested)
            continue
        if child_result is not None:
            record = _summary_record_from_result(
                child_id,
                child_result,
                source="descendant_result",
            )
        elif child_verdict:
            record = {
                "verdict": child_verdict,
                "summary": "",
                "cost_usd": float(child_entry.get("cost_usd", 0.0)),
                "reconstructed_from": "descendant_graph",
                "source_task_id": child_id,
            }
        else:
            return None
        record["task_id"] = child_id
        descendant_records.append(record)

    if not descendant_records:
        return None

    worst = max(
        (str(record.get("verdict") or "pending_children") for record in descendant_records),
        key=lambda verdict: _SUMMARY_VERDICT_SEVERITY.get(verdict, 0),
    )
    cost = sum(float(record.get("cost_usd") or 0.0) for record in descendant_records)
    coverage = _merge_intent_coverage([
        record.get("intent_coverage") if isinstance(record.get("intent_coverage"), dict) else None
        for record in descendant_records
    ])
    summary = (
        "Reconstructed from resolved descendant verdicts: "
        + ", ".join(
            f"{record.get('task_id')}={record.get('verdict')}"
            for record in descendant_records
        )
    )
    reconstructed: dict[str, Any] = {
        "verdict": worst,
        "summary": summary[:200],
        "cost_usd": cost,
        "reconstructed_from": "descendant_verdicts",
        "descendant_summaries": descendant_records,
    }
    if any(coverage.values()):
        reconstructed["intent_coverage"] = coverage
    return reconstructed


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


def _reconcile_recovered_children(
    project_dir: Path,
    parent_task_id: str,
    on_event: Any = None,
) -> int:
    """Update verdicts of merge_blocked children whose work the
    integration agent successfully recovered via Step 0b.

    Algorithm: for each direct child of ``parent_task_id`` currently
    marked ``merge_blocked``, check whether its build branch is an
    ancestor of the parent's integration branch. If yes, the
    integration session's ``git merge`` brought the work in — the
    child has effectively passed (via recovery). Update its verdict
    from ``merge_blocked`` to ``pass``.

    Without this, ``aggregate_verdict()`` keeps reporting the stale
    ``merge_blocked`` even though Step 0b succeeded.

    Returns the number of children whose verdict was updated.
    """
    from otto.v5_branching import child_branch_name

    parent = get_task(project_dir, parent_task_id) or {}
    integration_branch = parent.get("integration_branch") or "main"

    reconciled = 0
    for cid in children_of(project_dir, parent_task_id):
        child = get_task(project_dir, cid) or {}
        if child.get("verdict") != "merge_blocked":
            continue
        build_branch = child_branch_name(cid)
        # is-ancestor: build branch is reachable from integration branch
        try:
            proc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", build_branch, integration_branch],
                cwd=str(project_dir),
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning(
                "reconcile: could not check is-ancestor for %s: %s", cid, exc,
            )
            continue
        if proc.returncode != 0:
            # Either not an ancestor (real failure that wasn't recovered)
            # or git couldn't resolve the branch (deleted, etc.). Don't
            # update the verdict — preserve the honest merge_blocked.
            continue
        # Ancestor confirmed: integration agent merged it.
        set_verdict(project_dir, cid, "pass", cost_usd=float(child.get("cost_usd", 0.0)))
        reconciled += 1
        logger.info(
            "reconcile: child %s recovered by integration (build=%s); "
            "verdict merge_blocked → pass",
            cid, build_branch,
        )
        _emit(on_event, {
            "event": "child_recovery_reconciled",
            "task_id": cid,
            "build_branch": build_branch,
            "integration_branch": integration_branch,
        })
    return reconciled


def _emit(on_event: Any, payload: dict[str, Any]) -> None:
    """Best-effort event emission."""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass


def _build_decomp_runtime_context(
    *,
    project_dir: Path,
    config: dict[str, Any],
    max_parallel: int,
    run_started_at: float | None,
    spec: FlatSpec | None = None,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    spec_payload = _spec_payload(spec=spec, spec_path=spec_path)
    graph = read_graph(project_dir)
    pending = read_pending(project_dir)
    tasks = graph.get("tasks") if isinstance(graph.get("tasks"), dict) else {}
    terminal = {"pass", "partial", "unverified", "merge_blocked", "catastrophic"}
    done = {
        tid for tid, task in tasks.items()
        if isinstance(task, dict) and task.get("verdict") in terminal
    }
    runnable = [
        entry for entry in pending
        if entry.get("task_id")
        and entry.get("task_id") not in done
        and entry.get("review_state") in ("approved", None)
    ]
    ready = [
        entry for entry in runnable
        if all(dep in done for dep in (entry.get("depends_on") or []))
    ]
    elapsed = int(time.monotonic() - run_started_at) if run_started_at else 0
    budget = int(config.get("run_budget_seconds") or 3600)
    provider = (
        config.get("provider")
        or (config.get("defaults", {}) or {}).get("provider")
        or "claude"
    )
    return {
        "max_parallel": max(1, int(max_parallel or 1)),
        "run_budget_seconds": budget,
        "run_elapsed_seconds": max(0, elapsed),
        "cost_model_s": {
            "worktree_setup_s": 60,
            "prompt_render_s": 10,
            "min_leaf_runtime_s": 300,
        },
        "queue_state": {
            "active": 0,
            "queued": len(runnable),
            "ready": len(ready),
            "waiting_on_deps": max(0, len(runnable) - len(ready)),
            "free_slots": max(1, int(max_parallel or 1)),
        },
        "spec_profile": _spec_profile(spec_payload),
        "runtime_policy": {
            "tier": str(config.get("v5_tier") or "auto"),
            "review_first_decomp": bool(config.get("v5_review_first_decomp")),
            "context_slicing": _context_slicing_enabled(config),
            "provider": str(provider),
        },
    }


def _spec_payload(*, spec: FlatSpec | None, spec_path: Path | None) -> dict[str, Any]:
    if spec is not None:
        return {
            "project_kind": spec.project_kind,
            "intent_claims": spec.intent_claims,
            "core_entities": spec.core_entities,
            "behavior_journeys": spec.behavior_journeys,
        }
    if spec_path and spec_path.exists():
        try:
            payload = json.loads(spec_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _spec_profile(spec: dict[str, Any]) -> dict[str, Any]:
    entities = spec.get("core_entities") if isinstance(spec.get("core_entities"), list) else []
    journeys = spec.get("behavior_journeys") if isinstance(spec.get("behavior_journeys"), list) else []
    actions = 0
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        raw_actions = entity.get("primary_actions") or entity.get("actions") or []
        if isinstance(raw_actions, list):
            actions += len(raw_actions)
    entry_routes = sorted({
        str(journey.get("entry_route"))
        for journey in journeys
        if isinstance(journey, dict) and journey.get("entry_route")
    })
    return {
        "project_kind": str(spec.get("project_kind") or "unknown"),
        "intent_claims": len(spec.get("intent_claims") or []),
        "core_entities": len(entities),
        "primary_actions": actions,
        "behavior_journeys": len(journeys),
        "entry_routes": entry_routes,
    }


def _context_slicing_enabled(config: dict[str, Any]) -> bool:
    """Return whether child context slicing is explicitly enabled.

    Default is full context. `--full-context` sets `v5_context_slicing=False`
    as a safe escape hatch even if config enables slicing later.
    """
    if not isinstance(config, dict):
        return False
    if config.get("v5_context_slicing") is False:
        return False
    if config.get("v5_full_context") is True:
        return False
    slicing = config.get("context_slicing")
    if isinstance(slicing, dict):
        return bool(slicing.get("enabled"))
    if isinstance(slicing, bool):
        return slicing
    return bool(config.get("v5_context_slicing"))


def _child_scope_from_entry(child_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Build the slicer's child scope payload from queue metadata."""
    return {
        "child_id": child_id,
        "task_intent": str(entry.get("intent") or ""),
        "owned_paths": list(entry.get("owned_paths") or []),
        "action_ids": list(entry.get("action_ids") or []),
    }


def _write_context_slice_failure_log(
    *,
    child_session_dir: Path,
    child_id: str,
    reason: str,
) -> Path:
    path = child_session_dir / "context_slice.json"
    payload = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "child_id": child_id,
        "included_entities": [],
        "excluded_entities": [],
        "included_intent_claims_n": 0,
        "excluded_intent_claims_n": 0,
        "fallback_to_full": True,
        "fallback_reason": reason,
        "artifacts": {},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_toolchain_preflight_log(
    *,
    project_dir: Path,
    architect_task_id: str,
    retry_count: int,
    result: dict[str, Any],
) -> Path:
    log_dir = project_dir / "otto_logs" / "preflight"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"toolchain-preflight-{architect_task_id}-attempt-{retry_count + 1}.json"
    payload = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "architect_task_id": architect_task_id,
        "retry_count": retry_count,
        **result,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# Names that mark a dir as install state we want to share across worktrees.
_INSTALL_DIR_NAMES = ("node_modules", ".venv")
# Dir names that should never have an install dir nested inside considered.
_NOISE_PARENTS = frozenset({
    ".git", ".worktrees", "node_modules", ".venv", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache",
})


def _iter_install_dirs(root: Path) -> Iterator[tuple[Path, Path]]:
    """Walk ``root`` for node_modules/.venv dirs, skipping nested noise.

    Yields ``(found_path, rel_to_root)`` pairs. A nested install dir
    (e.g., ``frontend/node_modules/foo/node_modules``) is skipped — only
    the top-level shared install dir per subsystem matters.
    """
    if not root.exists():
        return
    for name in _INSTALL_DIR_NAMES:
        for found in root.rglob(name):
            if not (found.is_dir() or found.is_symlink()):
                continue
            try:
                rel = found.relative_to(root)
            except ValueError:
                continue
            # If any parent part is itself an install/noise dir, this is
            # nested. Skip.
            if any(part in _NOISE_PARENTS for part in rel.parts[:-1]):
                continue
            yield found, rel


def _link_shared_install_dirs(
    project_dir: Path, child_worktree: Path, task_id: str
) -> int:
    """Symlink project_dir's install dirs (node_modules/.venv) into
    child_worktree at the same relative paths.

    Returns the count of symlinks created (for logging/tests).
    """
    created = 0
    for found, rel in _iter_install_dirs(project_dir):
        dst = child_worktree / rel
        if dst.exists() or dst.is_symlink():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(found.resolve())
            created += 1
        except OSError as exc:
            logger.warning(
                "could not symlink %s into child %s: %s", rel, task_id, exc,
            )
    return created


def _project_uses_playwright(project_dir: Path) -> bool:
    """Best-effort: does any package.json declare @playwright/test?"""
    for pkg in project_dir.rglob("package.json"):
        # Skip noise paths (don't read inside node_modules).
        if any(part in _NOISE_PARENTS for part in pkg.parts):
            continue
        try:
            text = pkg.read_text(encoding="utf-8")
        except OSError:
            continue
        if "@playwright/test" in text:
            return True
    return False


def _ensure_playwright_browsers(project_dir: Path) -> bool:
    """Run ``npx playwright install chromium`` once if needed.

    Agents reflexively run this command (22% of recent logs) and pay
    the ~30-60s download per run when chromium isn't cached. Playwright
    caches browsers at ``~/.cache/ms-playwright/`` by default; once
    chromium-* exists there, subsequent install calls become ~1-2s
    no-ops. Doing one install up-front (when chromium is genuinely
    missing) saves the agents that 30-60s.

    Skips entirely if the project doesn't use Playwright. Best-effort:
    failures are logged but don't block the pipeline.

    Returns True if browsers are now present (either pre-existing or
    freshly installed), False otherwise.
    """
    if not _project_uses_playwright(project_dir):
        return False

    import os
    cache_root = Path(os.path.expanduser("~/.cache/ms-playwright"))
    # Chromium dirs are named like `chromium-1140`. If any exists, we're set.
    if cache_root.exists():
        for entry in cache_root.iterdir():
            if entry.name.startswith("chromium-") and entry.is_dir():
                return True

    npx = shutil.which("npx")
    if not npx:
        logger.info("playwright preinstall skipped: npx not on PATH")
        return False
    logger.info("playwright preinstall: chromium not cached, installing once")
    try:
        proc = subprocess.run(
            [npx, "playwright", "install", "chromium"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-300:]
            logger.warning(
                "playwright preinstall failed (exit %d): %s",
                proc.returncode, tail,
            )
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("playwright preinstall raised: %s", exc)
        return False


def _propagate_install_dirs_from_architect(
    project_dir: Path, architect_worktree: Path
) -> int:
    """Symlink the architect's install dirs into project_dir.

    After the architect's session, its worktree typically holds
    ``frontend/node_modules`` and/or ``api/.venv`` (it ran them while
    verifying the scaffold). These don't merge into the integration
    branch — git ignores them. Without this propagation, every feature
    child re-runs ``npm install``/``uv sync`` from scratch (~30-60s
    each, ~5min on a 5-child tree).

    Returns the count of symlinks created.
    """
    if not architect_worktree.exists():
        return 0
    created = 0
    for found, rel in _iter_install_dirs(architect_worktree):
        target = project_dir / rel
        if target.exists() or target.is_symlink():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(found.resolve())
            created += 1
            logger.info(
                "propagated install dir from architect: %s → %s",
                target, found.resolve(),
            )
        except OSError as exc:
            logger.warning(
                "could not propagate %s from architect: %s", target, exc,
            )
    return created


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
