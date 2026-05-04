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
    BuildBudget,
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
    # Pattern A fix: distinguish a real new commit from a no-op merge
    # (worktree had no changes to commit, e.g., because an earlier slice
    # already wrote this slice's work). REDUNDANT means the slice's
    # checks passed but no new code was added — the slice didn't
    # contribute meaningfully. Treated as a non-pass for verdict
    # purposes; surfaces the over-reach pattern instead of hiding it.
    REDUNDANT = "redundant"


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
    # Pattern A: slices whose checks passed but produced no new commit.
    # Symptom of over-reach (earlier slice wrote this slice's work).
    redundant_ids: list[str] = field(default_factory=list)
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
    shared_budget: BuildBudget | None = None,
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
    redundant_ids: list[str] = []
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
            shared_budget=shared_budget,
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
        elif result.status == MergeStatus.REDUNDANT:
            # Pattern A: don't lie. The slice's checks passed but it
            # produced no new commit. Surface this as a distinct
            # outcome — usually means an earlier slice over-reached
            # and wrote this slice's work too. Counts toward
            # `landed_ids` for dep-flow purposes (downstream slices
            # depending on this one can still proceed, since the
            # required state is in HEAD via the over-reaching slice).
            # `redundant_ids` is the diagnostic side-channel.
            landed_ids.append(slice_obj.id)
            redundant_ids.append(slice_obj.id)
            emit(
                session_dir,
                "slice.merge.redundant",
                slice_id=slice_obj.id,
                detail=result.failure_narrative or "no new diff",
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
        redundant_ids=redundant_ids,
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
    shared_budget: BuildBudget | None,
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

        # V2 fix: merge-first-then-verify. Old order ran slice + cross-slice
        # checks BEFORE merging — but with Pattern D real branches, the
        # slice's deliverables live on its branch, not on `base_branch`.
        # Running the slice's check pre-merge meant testing a state that
        # didn't include the slice's contributions, so any check that
        # tested its own deliverable (e.g., "Templates exist") failed
        # spuriously. New order: merge first → run all checks against
        # the integrated post-merge state → rollback if checks fail.
        slice_evidence: list[Evidence] = []
        cross_evidence: list[Evidence] = []
        outcome = _merge_slice_branch(
            git, candidate.worktree,
            slice_id=slice_obj.id, branch=candidate.branch,
            base_branch=candidate.base_branch,
        )

        # REDUNDANT semantics (Pattern A): preserve.
        merge_status = outcome.status
        if merge_status == MergeStatus.REDUNDANT and not (slice_obj.tasks or slice_obj.owned_paths):
            merge_status = MergeStatus.LANDED

        # Conflict → route to repair (B1 path).
        if merge_status == MergeStatus.BLOCKED and "merge conflict" in (outcome.detail or "").lower():
            last_failure = outcome.detail or "merge conflict"
            slice_failed_summaries = []
            cross_failed_summaries = []
        elif merge_status in (MergeStatus.LANDED, MergeStatus.REDUNDANT):
            # Merge succeeded — worktree is now `base + this_slice`.
            # Run slice + cross-slice checks against this integrated state.
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
                return MergeResult(
                    slice_id=slice_obj.id,
                    status=merge_status,
                    landed_commit=outcome.head_after,
                    cross_slice_evidence=cross_evidence,
                    slice_recheck_evidence=slice_evidence,
                    repair_attempts=repair_attempts,
                    cost_usd=cost_total,
                    wall_s=time.monotonic() - t0,
                    failure_narrative="",
                )

            # Post-merge checks failed — rollback the merge so the slice
            # branch can be repaired and re-merged. Without rollback the
            # bad merge stays on base_branch and corrupts subsequent
            # slices' parent state.
            if outcome.head_before and outcome.status == MergeStatus.LANDED:
                git(["reset", "--hard", outcome.head_before], candidate.worktree)
            slice_failed_summaries = None
            cross_failed_summaries = None
        else:
            # Other BLOCKED (e.g., git failure other than conflict).
            last_failure = outcome.detail or "merge failed"
            slice_failed_summaries = []
            cross_failed_summaries = []

        # Failure: prepare narrative.
        # If we fell through here from a merge-conflict or other merge
        # error, slice_failed_summaries is [] and last_failure is set.
        # If we fell through from post-merge check failure (rollback path),
        # both summaries are None and we compute them here.
        if slice_failed_summaries is None:
            slice_failed_summaries = [ev.detail for ev in slice_evidence if not ev.passed]
        if cross_failed_summaries is None:
            cross_failed_summaries = [ev.detail for ev in cross_evidence if not ev.passed]
            last_failure = "post-merge verification failed"
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
        # B1 fix: when a slice branch exists, the repair MUST happen on
        # the slice's branch — not on base_branch. After
        # `_merge_slice_branch` aborts a conflict, the worktree is left
        # on base_branch; if the agent edits there, the slice branch
        # never gets the fix and the next merge attempt repeats the
        # same conflict. Checkout the slice's branch first, repair,
        # commit on the slice branch, then let the next loop iteration
        # re-merge.
        repair_attempts += 1
        on_slice_branch = False
        if _branch_exists(git, candidate.worktree, candidate.branch):
            co = git(["checkout", candidate.branch], candidate.worktree)
            on_slice_branch = co.returncode == 0
            if not on_slice_branch:
                last_failure = (
                    f"could not checkout slice branch {candidate.branch} for repair: "
                    f"{co.stderr.strip()[:200]}"
                )
                emit(
                    session_dir, "slice.attempt.failed",
                    slice_id=slice_obj.id, attempt=repair_attempts, detail=last_failure,
                )
                continue
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
        # C1 fix: bail out if the shared cost pool is exhausted.
        # Without this, repair retries can drain past the global cap.
        if shared_budget is not None and shared_budget.remaining_total_cost_usd() <= 0:
            last_failure = (
                f"shared cost budget exhausted "
                f"(${shared_budget._spent_cost_usd:.2f} >= "
                f"${shared_budget.total_cost_usd:.2f})"
            )
            emit(
                session_dir, "slice.attempt.failed",
                slice_id=slice_obj.id, attempt=repair_attempts, detail=last_failure,
            )
            if on_slice_branch:
                git(["checkout", candidate.base_branch], candidate.worktree)
            continue
        try:
            agent_output = await build_agent(agent_input)
            cost_total += agent_output.cost_usd
            if shared_budget is not None:
                shared_budget.charge_cost(agent_output.cost_usd)
            if not agent_output.succeeded:
                last_failure = agent_output.detail or "agent reported failure during merge repair"
                emit(
                    session_dir,
                    "slice.attempt.failed",
                    slice_id=slice_obj.id,
                    attempt=repair_attempts,
                    detail=last_failure,
                )
                if on_slice_branch:
                    git(["checkout", candidate.base_branch], candidate.worktree)
                # Loop back to retry verification (which will fail again
                # and either re-invoke the agent or block).
                continue
            # Agent succeeded — commit the repair to the slice branch
            # so the next merge attempt sees the fix.
            if on_slice_branch:
                add = git(["add", "-A"], candidate.worktree)
                if add.returncode == 0:
                    status = git(["status", "--porcelain"], candidate.worktree)
                    if (status.stdout or "").strip():
                        msg = f"i2p({slice_obj.id}): repair on {candidate.branch} (attempt {repair_attempts})"
                        commit = git(
                            ["commit", "-q", "-m", msg, "--no-verify"],
                            candidate.worktree,
                        )
                        if commit.returncode != 0:
                            logger.warning(
                                "repair commit failed for slice %s: %s",
                                slice_obj.id, commit.stderr,
                            )
                git(["checkout", candidate.base_branch], candidate.worktree)
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
            if on_slice_branch:
                git(["checkout", candidate.base_branch], candidate.worktree)
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


@dataclass
class CommitOutcome:
    """Result of `_commit_integration`. Pattern A fix — events must
    reflect git reality, not be derived from an unconditional return.

    Status semantics:
      - LANDED: a new commit was created (HEAD advanced).
      - REDUNDANT: nothing to commit (worktree had no changes against
        HEAD). Slice contributed no new code. NOT a failure but also
        NOT a real landing.
      - BLOCKED: git operation failed (commit returned non-zero,
        rev-parse failed, identity not configured, etc.).
    """
    status: MergeStatus
    head_before: str
    head_after: str
    detail: str = ""


def _branch_exists(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    branch: str,
) -> bool:
    """True if `branch` resolves to a commit in `worktree`."""
    proc = git(["rev-parse", "--verify", branch], worktree)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _merge_slice_branch(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    *,
    slice_id: str,
    branch: str,
    base_branch: str,
) -> CommitOutcome:
    """Real `git merge --no-ff` of slice's branch into `base_branch`.

    Pattern D — replaces the old "git add -A && git commit in shared
    worktree" model. The slice's branch already has its work as a
    commit (build.py committed it). This function:

      1. Checks out `base_branch`.
      2. Runs `git merge --no-ff <slice-branch>` to produce a merge
         commit (or fast-forward to slice tip if base is unchanged).
      3. Reports LANDED with the new HEAD, REDUNDANT if no commits to
         merge, or BLOCKED on conflict / git failure.

    On conflict, aborts the merge cleanly so the worktree is left on
    `base_branch` ready for the caller to repair or skip the slice.
    """
    # 1. Verify slice branch exists. If not, fall back to commit-in-worktree.
    if not _branch_exists(git, worktree, branch):
        return _commit_integration(git, worktree, slice_id=slice_id, branch=branch)

    # 2. Checkout base_branch.
    co_base = git(["checkout", base_branch], worktree)
    if co_base.returncode != 0:
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before="",
            head_after="",
            detail=f"checkout {base_branch} failed: {co_base.stderr.strip()[:200]}",
        )

    head_before_proc = git(["rev-parse", "--short", "HEAD"], worktree)
    head_before = (head_before_proc.stdout or "").strip()

    # 3. Check if slice branch has anything beyond base.
    range_check = git(["log", "--format=%H", f"{base_branch}..{branch}"], worktree)
    if range_check.returncode == 0 and not (range_check.stdout or "").strip():
        # No commits between base and slice tip — REDUNDANT (no diff).
        return CommitOutcome(
            status=MergeStatus.REDUNDANT,
            head_before=head_before,
            head_after=head_before,
            detail=f"slice branch {branch} has no commits beyond {base_branch}",
        )

    # 4. Real merge.
    msg = f"i2p({slice_id}): merge slice branch {branch}"
    merge = git(["merge", "--no-ff", "-m", msg, branch], worktree)
    if merge.returncode != 0:
        # Conflict — abort cleanly.
        git(["merge", "--abort"], worktree)
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before=head_before,
            head_after=head_before,
            detail=f"merge conflict on slice branch {branch}: {merge.stderr.strip()[:200]}",
        )

    head_after_proc = git(["rev-parse", "--short", "HEAD"], worktree)
    head_after = (head_after_proc.stdout or "").strip()
    return CommitOutcome(
        status=MergeStatus.LANDED,
        head_before=head_before,
        head_after=head_after,
        detail="",
    )


def _commit_integration(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    *,
    slice_id: str,
    branch: str,
) -> CommitOutcome:
    """Stage and commit current worktree state as a slice-tagged commit.

    Phase A fallback path — used when the slice branch doesn't exist
    (no per-slice branches were set up, e.g., test fixtures without
    git, or non-git project_dir). When a slice branch DOES exist,
    `_merge_slice_branch` is used instead.

    Honestly reports what happened. Captures HEAD before and after,
    distinguishes real commits from no-ops and from failures.
    """
    head_before_proc = git(["rev-parse", "--short", "HEAD"], worktree)
    head_before = (head_before_proc.stdout or "").strip()
    if head_before_proc.returncode != 0 or not head_before:
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before="",
            head_after="",
            detail=f"rev-parse HEAD failed: {head_before_proc.stderr.strip()[:200]}",
        )

    add_proc = git(["add", "-A"], worktree)
    if add_proc.returncode != 0:
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before=head_before,
            head_after=head_before,
            detail=f"git add -A failed: {add_proc.stderr.strip()[:200]}",
        )

    status = git(["status", "--porcelain"], worktree)
    if not status.stdout.strip():
        # No changes to commit — slice didn't contribute new code.
        return CommitOutcome(
            status=MergeStatus.REDUNDANT,
            head_before=head_before,
            head_after=head_before,
            detail="no changes to commit (slice produced no diff)",
        )

    msg = f"i2p({slice_id}): land slice from {branch}"
    commit = git(["commit", "-q", "-m", msg, "--no-verify"], worktree)
    if commit.returncode != 0:
        logger.warning("git commit failed for slice %s: %s", slice_id, commit.stderr)
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before=head_before,
            head_after=head_before,
            detail=f"git commit failed: {commit.stderr.strip()[:200]}",
        )

    head_after_proc = git(["rev-parse", "--short", "HEAD"], worktree)
    head_after = (head_after_proc.stdout or "").strip()
    if head_after_proc.returncode != 0 or not head_after:
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before=head_before,
            head_after="",
            detail=f"rev-parse HEAD after commit failed: {head_after_proc.stderr.strip()[:200]}",
        )
    if head_after == head_before:
        # Commit returned 0 but HEAD didn't change — anomalous.
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before=head_before,
            head_after=head_after,
            detail="commit reported success but HEAD did not advance",
        )
    return CommitOutcome(
        status=MergeStatus.LANDED,
        head_before=head_before,
        head_after=head_after,
        detail="",
    )


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
