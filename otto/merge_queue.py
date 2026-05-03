"""Merge queue — Step 5 of the unified intent-to-product pipeline.

Eligibility-gated FIFO merge queue per `{project, target_branch}`. Each
slice's build agent (still alive after build) executes its own merge:
refresh target → rebase → rerun slice + cross-slice checks pre-land →
land atomically → post-land verify.

Phase A simplification — single-worktree mode:
    All slices share one worktree. There is no per-slice branch in v1;
    build agents accumulate edits into the integration worktree
    sequentially. The "merge" step in this mode reduces to:

        * verify the slice's checks STILL pass against the integrated state
          (covering side-effects of subsequent slices)
        * verify cross-slice checks pass against the integrated state
        * commit the integration state with a slice-tagged commit
        * on failure: re-engage the slice's build agent for repair,
          bounded by the merge-retry budget

Multi-worktree mode (per-slice branches with rebase + conflict repair) is
deferred to a follow-up. The data model and eligibility logic here are
designed to extend; the executor is the part that grows.

Naming note: the file is `merge_queue.py` (not `merge.py`) to avoid
clashing with the existing `otto/merge/` package, which carries the
legacy multi-mode merge orchestrator. That package stays put for the
old `otto build` / `otto certify` paths during Phase A coexistence.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from otto.build import (
    BuildAgentCallable,
    BuildAgentInput,
    BuildResult,
    SliceStatus,
)
from otto.checks import Evidence, run_checks
from otto.spec_compile import Slice, Spec
from otto.spec_state import emit

logger = logging.getLogger("otto.merge_queue")


class MergeStatus(str, Enum):
    PENDING = "pending"
    LANDED = "landed"
    BLOCKED = "blocked"


@dataclass
class MergeCandidate:
    """A slice that build.py marked PASSING — eligible to land."""

    slice_id: str
    branch: str
    base_branch: str  # the integration target (typically "main")
    worktree: Path


@dataclass
class MergeResult:
    """Per-slice outcome of the merge queue."""

    slice_id: str
    status: MergeStatus
    landed_commit: str = ""  # short hash of the landed integration commit
    cross_slice_evidence: list[Evidence] = field(default_factory=list)
    slice_recheck_evidence: list[Evidence] = field(default_factory=list)
    failure_narrative: str = ""
    repair_attempts: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0


@dataclass
class MergeBudget:
    """Bounds for the merge phase."""

    per_slice_repair_retries: int = 2
    per_slice_wall_s: int = 15 * 60  # 15 min per slice for merge repair


@dataclass
class MergeQueueResult:
    """Aggregate result of run_merge_queue."""

    landed_ids: list[str] = field(default_factory=list)
    blocked_ids: list[str] = field(default_factory=list)
    results: list[MergeResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_wall_s: float = 0.0


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def eligible_candidates(
    spec: Spec,
    *,
    passing_ids: Iterable[str],
    landed_ids: Iterable[str],
    blocked_ids: Iterable[str] = (),
) -> list[Slice]:
    """Return slices that are merge candidates, in dep-topological FIFO order.

    Eligibility:
        * slice.id is in `passing_ids` (build.py reported PASSING)
        * slice.id is NOT in `landed_ids` (already landed)
        * slice.id is NOT in `blocked_ids` (terminally failed merge)
        * all of slice.deps are in `landed_ids`

    Ordering is the spec's slice declaration order, which is also the
    natural FIFO order — earlier slices in the spec land first.
    """
    passing = set(passing_ids)
    landed = set(landed_ids)
    blocked = set(blocked_ids)
    eligible: list[Slice] = []
    for s in spec.slices:
        if s.id not in passing:
            continue
        if s.id in landed or s.id in blocked:
            continue
        if not all(dep in landed for dep in (s.deps or [])):
            continue
        eligible.append(s)
    return eligible


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def run_merge_queue(
    spec: Spec,
    build_result: BuildResult,
    *,
    project_dir: Path,
    session_dir: Path,
    base_branch: str = "main",
    base_url: str | None = None,
    build_agent: BuildAgentCallable | None = None,
    budget: MergeBudget | None = None,
    branch_for_slice: Callable[[Slice], str] | None = None,
    git_runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
) -> MergeQueueResult:
    """Process all merge candidates from a BuildResult.

    Args:
        spec: The approved Spec.
        build_result: Output of run_build — supplies the passing slice ids.
        project_dir: Project root.
        session_dir: Session dir for journal events.
        base_branch: Integration branch (default "main").
        base_url: Optional base URL for ApiProbe / StateInvariant http_get.
        build_agent: Optional build-agent callable for repair attempts.
            If None, a slice that fails merge checks is BLOCKED immediately
            (no repair). This makes the merge queue testable without an
            agent and matches Phase A coexistence — repair lands later.
        budget: Per-slice repair bounds.
        branch_for_slice: Branch naming. Default mirrors build.py.
        git_runner: Subprocess hook for tests. Default uses real git.
    """
    budget = budget or MergeBudget()
    branch_for_slice = branch_for_slice or (lambda s: f"i2p/{session_dir.name}/{s.id}")
    git = git_runner or _git

    passing_ids = list(build_result.passing_ids)
    landed_ids: list[str] = []
    blocked_ids: list[str] = []
    results: list[MergeResult] = []
    total_t0 = time.monotonic()
    total_cost = 0.0

    while True:
        eligible = eligible_candidates(
            spec,
            passing_ids=passing_ids,
            landed_ids=landed_ids,
            blocked_ids=blocked_ids,
        )
        if not eligible:
            break
        slice_obj = eligible[0]  # FIFO within eligible
        candidate = MergeCandidate(
            slice_id=slice_obj.id,
            branch=branch_for_slice(slice_obj),
            base_branch=base_branch,
            worktree=project_dir,
        )
        emit(
            session_dir,
            "slice.merge.started",
            slice_id=slice_obj.id,
            detail=f"branch={candidate.branch} base={base_branch}",
        )
        result = await _process_candidate(
            spec=spec,
            slice_obj=slice_obj,
            candidate=candidate,
            project_dir=project_dir,
            session_dir=session_dir,
            base_url=base_url,
            build_agent=build_agent,
            budget=budget,
            git=git,
        )
        total_cost += result.cost_usd
        results.append(result)
        if result.status == MergeStatus.LANDED:
            landed_ids.append(slice_obj.id)
            emit(
                session_dir,
                "slice.merge.landed",
                slice_id=slice_obj.id,
                detail=result.landed_commit,
            )
        else:
            blocked_ids.append(slice_obj.id)
            emit(
                session_dir,
                "slice.blocked",
                slice_id=slice_obj.id,
                detail=result.failure_narrative,
            )

    return MergeQueueResult(
        landed_ids=landed_ids,
        blocked_ids=blocked_ids,
        results=results,
        total_cost_usd=total_cost,
        total_wall_s=time.monotonic() - total_t0,
    )


async def _process_candidate(
    *,
    spec: Spec,
    slice_obj: Slice,
    candidate: MergeCandidate,
    project_dir: Path,
    session_dir: Path,
    base_url: str | None,
    build_agent: BuildAgentCallable | None,
    budget: MergeBudget,
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
) -> MergeResult:
    """Run the slice's merge step.

    1. Refresh integration state (Phase A: read current HEAD as base).
    2. Rerun slice + cross-slice checks against the integrated worktree.
    3. If pass: commit a slice-tagged integration commit; LANDED.
    4. If fail and build_agent provided: invoke for repair, bounded retries.
    5. If repair exhausts budget: BLOCKED.
    """
    t0 = time.monotonic()
    cost_total = 0.0
    repair_attempts = 0
    last_failure = ""
    raw_log_dir = session_dir / "merge" / slice_obj.id

    while True:
        wall = time.monotonic() - t0
        if wall >= budget.per_slice_wall_s:
            return MergeResult(
                slice_id=slice_obj.id,
                status=MergeStatus.BLOCKED,
                cross_slice_evidence=[],
                failure_narrative=f"merge wall budget exhausted after {wall:.0f}s",
                repair_attempts=repair_attempts,
                cost_usd=cost_total,
                wall_s=wall,
            )

        # 1 + 2: rerun slice's checks AND cross-slice checks against the
        # current integrated worktree.
        slice_pairs = run_checks(
            list(slice_obj.checks),
            project_dir=project_dir,
            cwd=candidate.worktree,
            base_url=base_url,
            raw_log_dir=raw_log_dir / f"slice-attempt-{repair_attempts:02d}",
        )
        slice_evidence = [ev for _check, ev in slice_pairs]
        slice_pass = all(ev.passed for ev in slice_evidence)

        cross_pairs = run_checks(
            list(spec.cross_slice_checks),
            project_dir=project_dir,
            cwd=candidate.worktree,
            base_url=base_url,
            raw_log_dir=raw_log_dir / f"cross-attempt-{repair_attempts:02d}",
        )
        cross_evidence = [ev for _check, ev in cross_pairs]
        cross_pass = all(ev.passed for ev in cross_evidence) if cross_evidence else True

        if slice_pass and cross_pass:
            commit_hash = _commit_integration(
                git, candidate.worktree, slice_id=slice_obj.id, branch=candidate.branch
            )
            return MergeResult(
                slice_id=slice_obj.id,
                status=MergeStatus.LANDED,
                landed_commit=commit_hash,
                cross_slice_evidence=cross_evidence,
                slice_recheck_evidence=slice_evidence,
                repair_attempts=repair_attempts,
                cost_usd=cost_total,
                wall_s=time.monotonic() - t0,
            )

        # Failure: prepare narrative.
        slice_failed_summaries = [ev.detail for ev in slice_evidence if not ev.passed]
        cross_failed_summaries = [ev.detail for ev in cross_evidence if not ev.passed]
        last_failure = "merge verification failed"
        if slice_failed_summaries:
            last_failure += f" — slice: {'; '.join(slice_failed_summaries[:3])}"
        if cross_failed_summaries:
            last_failure += f" — cross-slice: {'; '.join(cross_failed_summaries[:3])}"

        # If we have no agent or no repair retries left → BLOCKED.
        if build_agent is None:
            return MergeResult(
                slice_id=slice_obj.id,
                status=MergeStatus.BLOCKED,
                cross_slice_evidence=cross_evidence,
                slice_recheck_evidence=slice_evidence,
                failure_narrative=last_failure + " (no build_agent for repair)",
                repair_attempts=repair_attempts,
                cost_usd=cost_total,
                wall_s=time.monotonic() - t0,
            )
        if repair_attempts >= budget.per_slice_repair_retries:
            return MergeResult(
                slice_id=slice_obj.id,
                status=MergeStatus.BLOCKED,
                cross_slice_evidence=cross_evidence,
                slice_recheck_evidence=slice_evidence,
                failure_narrative=last_failure + " (repair retries exhausted)",
                repair_attempts=repair_attempts,
                cost_usd=cost_total,
                wall_s=time.monotonic() - t0,
            )

        # 4: invoke the build agent for repair.
        repair_attempts += 1
        agent_input = BuildAgentInput(
            spec=spec,
            slice=slice_obj,
            project_dir=project_dir,
            worktree=candidate.worktree,
            branch=candidate.branch,
            attempt=repair_attempts,
            last_failure_narrative=last_failure,
            log_dir=raw_log_dir / f"repair-attempt-{repair_attempts:02d}",
        )
        try:
            agent_output = await build_agent(agent_input)
            cost_total += agent_output.cost_usd
            if not agent_output.succeeded:
                last_failure = agent_output.detail or "agent reported failure during merge repair"
                emit(
                    session_dir,
                    "slice.attempt.failed",
                    slice_id=slice_obj.id,
                    attempt=repair_attempts,
                    detail=last_failure,
                )
                # Loop back to retry verification (which will fail again
                # and either re-invoke the agent or block).
                continue
        except Exception as exc:
            last_failure = (
                f"merge repair agent crashed on attempt {repair_attempts}: "
                f"{type(exc).__name__}: {exc}"
            )
            emit(
                session_dir,
                "slice.attempt.failed",
                slice_id=slice_obj.id,
                attempt=repair_attempts,
                detail=last_failure,
            )
            continue


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand, capture text output, no exception on non-zero."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_integration(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    *,
    slice_id: str,
    branch: str,
) -> str:
    """Stage and commit current worktree state as a slice-tagged commit.

    No-op if there are no changes (idempotent re-run case). Returns the
    short commit hash of HEAD after the commit (or before, if no changes).
    """
    git(["add", "-A"], worktree)
    status = git(["status", "--porcelain"], worktree)
    if status.stdout.strip():
        msg = f"i2p({slice_id}): land slice from {branch}"
        commit = git(["commit", "-q", "-m", msg, "--no-verify"], worktree)
        if commit.returncode != 0:
            logger.warning("git commit failed for slice %s: %s", slice_id, commit.stderr)
    head = git(["rev-parse", "--short", "HEAD"], worktree)
    return (head.stdout or "").strip()


# ---------------------------------------------------------------------------
# Convenience for tests / call sites
# ---------------------------------------------------------------------------


def passing_slice_ids(build_result: BuildResult) -> list[str]:
    """Return the slice ids the build loop marked PASSING (merge candidates)."""
    return [r.slice_id for r in build_result.slice_results if r.status == SliceStatus.PASSING]


__all__ = [
    "MergeBudget",
    "MergeCandidate",
    "MergeQueueResult",
    "MergeResult",
    "MergeStatus",
    "eligible_candidates",
    "passing_slice_ids",
    "run_merge_queue",
]


# Pin imports for static checkers — these are part of the public flow even
# though only some are referenced directly above.
_ = (Any,)
