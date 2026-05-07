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
    ComponentResult,
    ComponentStatus,
    GroupResult,
    GroupStatus,
    detect_scope_violations,
    resolve_integration_base_branch,
)
from otto.checks import Evidence, run_checks
from otto.setup_gitignore import non_product_paths_from_porcelain
from otto.spec_compile import Group, Spec
from otto.spec_state import aborted_group_ids, emit

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

    group_id: str
    branch: str
    base_branch: str  # the integration target (typically "main")
    worktree: Path  # slice branch worktree used for repair
    merge_worktree: Path | None = None  # integration target worktree


@dataclass
class MergeResult:
    """Per-slice outcome of the merge queue."""

    group_id: str
    status: MergeStatus
    landed_commit: str = ""  # short hash of the landed integration commit
    cross_slice_evidence: list[Evidence] = field(default_factory=list)
    group_recheck_evidence: list[Evidence] = field(default_factory=list)
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
) -> list[Group]:
    """Return Groups that are merge candidates, in dep-topological FIFO order.

    Eligibility:
        * group.id is in `passing_ids` (build.py reported PASSING)
        * group.id is NOT in `landed_ids` (already landed)
        * group.id is NOT in `blocked_ids` (terminally failed merge)
        * every dep in `group.dependencies` is in `landed_ids`
          — deps may reference other Group ids OR Component ids
            (research §2.6). The caller is responsible for passing a
            `landed_ids` set that is the union of landed Groups +
            landed Components, so that a Group can wait on a
            Component to land first (and vice versa).

    Ordering is the spec's group declaration order, which is also the
    natural FIFO order — earlier groups in the spec land first.

    Reads `group.dependencies` (canonical new-design name); the Group
    dataclass exposes `.dependencies` as an alias for the legacy
    `.dependencies` field, so callers that still set `Group(deps=[...])`
    continue to work.
    """
    passing = set(passing_ids)
    landed = set(landed_ids)
    blocked = set(blocked_ids)
    eligible: list[Group] = []
    for s in spec.groups:
        if s.id not in passing:
            continue
        if s.id in landed or s.id in blocked:
            continue
        if not all(dep in landed for dep in (s.dependencies or [])):
            continue
        eligible.append(s)
    return eligible


# ---------------------------------------------------------------------------
# A1c: Component eligibility (research §2.6) — Components dispatch like
# Groups but produce no Feature verdict. Their merge-queue treatment is
# identical to Groups: dep-topological FIFO, deps must be landed first.
# Components and Groups can depend on each other.
# ---------------------------------------------------------------------------


def eligible_components(
    spec: Spec,
    *,
    passing_ids: Iterable[str],
    landed_ids: Iterable[str],
    blocked_ids: Iterable[str] = (),
) -> list:  # type: list[Component]; avoid forward-import circularity here
    """Return Components that are merge candidates, in dep-topological FIFO order.

    Eligibility mirrors `eligible_candidates` for Groups:
        * component.id is in `passing_ids` (build.py reported PASSING)
        * component.id is NOT in `landed_ids` (already landed)
        * component.id is NOT in `blocked_ids` (terminally failed merge)
        * all of component.dependencies are in `landed_ids`
          (deps may include Group ids OR other Component ids;
           landed_ids must be the union of landed Groups + Components
           when the caller mixes both kinds)

    Ordering is the spec's components declaration order. Components and
    Groups land into the same target branch via the merge queue;
    eligibility is computed independently but lands serialise through
    the same git base.
    """
    passing = set(passing_ids)
    landed = set(landed_ids)
    blocked = set(blocked_ids)
    eligible: list = []
    for c in spec.components:
        if c.id not in passing:
            continue
        if c.id in landed or c.id in blocked:
            continue
        if not all(dep in landed for dep in (c.dependencies or [])):
            continue
        eligible.append(c)
    return eligible


def _component_as_merge_slice(component: Any) -> Group:
    """Adapt a Component to the Slice surface for the merge queue.

    Mirrors `otto.build._component_as_slice`: owned_paths, dependencies,
    and checks pass through verbatim so the conflict-repair flow (which
    routes through `BuildAgentInput.slice` and `detect_scope_violations`)
    pins repair edits to the Component's owned_paths just like a Group's.

    Components have no user-facing tasks, so we synthesize a single task
    line from the component's description / name to give the build agent
    a concrete brief during repair.
    """
    description = getattr(component, "description", "") or component.name
    return Group(
        id=component.id,
        name=component.name,
        feature_ids=[description] if description else [],
        dependencies=list(getattr(component, "dependencies", []) or []),
        owned_paths=list(getattr(component, "owned_paths", []) or []),
        checks=list(getattr(component, "checks", []) or []),
    )


def shared_paths_set(spec: Spec) -> set[str]:
    """Return the spec's shared_paths as a set for quick membership checks
    (research §2.6).

    Files in `shared_paths` belong to no single Group/Component — every
    dispatched agent may freely add or modify them. The merge queue
    serialises lands across any Groups/Components that touched a
    shared_path so that simultaneous edits don't collide; the actual
    git serialisation happens transparently because lands are sequential
    by design.
    """
    return set(spec.shared_paths or [])


def _latest_group_results(build_result: BuildResult) -> dict[str, GroupResult]:
    """Latest result per Group id; later entries supersede earlier ones."""
    latest: dict[str, GroupResult] = {}
    for result in build_result.group_results:
        latest[result.group_id] = result
    return latest


def _latest_component_results(build_result: BuildResult) -> dict[str, ComponentResult]:
    """Latest result per Component id; later entries supersede earlier ones."""
    latest: dict[str, ComponentResult] = {}
    for result in getattr(build_result, "component_results", []) or []:
        latest[result.component_id] = result
    return latest


def _candidate_branch(
    group_obj: Group,
    *,
    unit_kind: str,
    branch_for_group: Callable[[Group], str],
    latest_group_results: dict[str, GroupResult],
    latest_component_results: dict[str, ComponentResult],
) -> str:
    """Use the actual passing attempt's branch, falling back to naming policy."""
    if unit_kind == "component":
        result = latest_component_results.get(group_obj.id)
    else:
        result = latest_group_results.get(group_obj.id)
    branch = str(getattr(result, "branch", "") or "")
    return branch or branch_for_group(group_obj)


def _candidate_worktree(
    group_obj: Group,
    *,
    unit_kind: str,
    project_dir: Path,
    latest_group_results: dict[str, GroupResult],
    latest_component_results: dict[str, ComponentResult],
) -> Path:
    """Use the actual passing attempt's worktree, falling back to project root."""
    if unit_kind == "component":
        result = latest_component_results.get(group_obj.id)
    else:
        result = latest_group_results.get(group_obj.id)
    worktree = getattr(result, "worktree", None)
    return Path(worktree) if worktree else project_dir


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def run_merge_queue(
    spec: Spec,
    build_result: BuildResult,
    *,
    project_dir: Path,
    session_dir: Path,
    base_branch: str | None = None,
    base_url: str | None = None,
    build_agent: BuildAgentCallable | None = None,
    config: dict[str, Any] | None = None,
    budget: MergeBudget | None = None,
    shared_budget: BuildBudget | None = None,
    branch_for_group: Callable[[Group], str] | None = None,
    git_runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
    skip_components: Iterable[str] | None = None,
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
        branch_for_group: Branch naming. Default mirrors build.py.
        git_runner: Subprocess hook for tests. Default uses real git.
        skip_components: ids already landed in a prior attempt during
            resume. They seed the merge queue's landed set so dependent
            units can proceed without re-merging prior work.
    """
    config = dict(config or {})
    base_branch = (
        base_branch
        or build_result.base_branch
        or resolve_integration_base_branch(project_dir)
    )
    budget = budget or MergeBudget()
    branch_for_group = branch_for_group or (lambda s: f"i2p/{session_dir.name}/{s.id}")
    git = git_runner or _git

    latest_group_results = _latest_group_results(build_result)
    passing_ids = [
        group_id
        for group_id, result in latest_group_results.items()
        if result.status == GroupStatus.PASSING
    ]
    # A1c.3: Components participate in the same FIFO queue as Groups so
    # their branches go through the same conflict-repair flow. We pull
    # Component-passing ids from the BuildResult (older builds without
    # `passing_component_ids` produce an empty list — back-compat).
    latest_component_results = _latest_component_results(build_result)
    passing_component_ids: list[str] = [
        component_id
        for component_id, result in latest_component_results.items()
        if result.status == ComponentStatus.PASSING
    ]
    skip_set = {str(s) for s in (skip_components or ())}
    ordered_unit_ids = [g.id for g in spec.groups] + [
        c.id for c in (getattr(spec, "components", None) or [])
    ]
    landed_ids: list[str] = [unit_id for unit_id in ordered_unit_ids if unit_id in skip_set]
    # A7: pre-populate blocked_ids with operator-aborted groups so the
    # eligibility check skips them. The build phase already returns
    # BLOCKED for aborted groups (see otto/build.py `_run_slice`), so
    # they shouldn't appear in `passing_ids`; this is a defense-in-depth
    # belt-and-suspenders for cases where a group passed build but the
    # operator aborted before merge could pick it up.
    aborted_ids = set(aborted_group_ids(session_dir))
    blocked_ids: list[str] = [unit_id for unit_id in ordered_unit_ids if unit_id in aborted_ids]
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
        # A1c.3: Components are dispatched alongside Groups. Both share
        # `landed_ids` and `blocked_ids` because Group<->Component cross
        # deps reference the same flat id space.
        eligible_comps = eligible_components(
            spec,
            passing_ids=passing_component_ids,
            landed_ids=landed_ids,
            blocked_ids=blocked_ids,
        )
        if not eligible and not eligible_comps:
            break
        # FIFO ordering: Groups first (preserves stable ordering for the
        # existing slice-only tests), then Components. Cross-kind dep
        # ordering is already enforced by eligibility — a Component that
        # depends on a Group will simply not appear in `eligible_comps`
        # until that Group has landed.
        if eligible:
            unit = eligible[0]
            group_obj = unit
            unit_kind = "group"
        else:
            unit = eligible_comps[0]
            # Adapt the Component to the Slice surface so the existing
            # `_process_candidate` flow (and its conflict-repair path)
            # works verbatim — Component.owned_paths / dependencies /
            # checks pass through exactly like Groups (mirroring the
            # build.py adapter `_component_as_slice`).
            group_obj = _component_as_merge_slice(unit)
            unit_kind = "component"
        candidate = MergeCandidate(
            group_id=group_obj.id,
            branch=_candidate_branch(
                group_obj,
                unit_kind=unit_kind,
                branch_for_group=branch_for_group,
                latest_group_results=latest_group_results,
                latest_component_results=latest_component_results,
            ),
            base_branch=base_branch,
            worktree=_candidate_worktree(
                group_obj,
                unit_kind=unit_kind,
                project_dir=project_dir,
                latest_group_results=latest_group_results,
                latest_component_results=latest_component_results,
            ),
            merge_worktree=project_dir,
        )
        emit(
            session_dir,
            "group.merge.started",
            group_id=group_obj.id,
            detail=f"branch={candidate.branch} base={base_branch} kind={unit_kind}",
        )
        result = await _process_candidate(
            spec=spec,
            group_obj=group_obj,
            candidate=candidate,
            project_dir=project_dir,
            session_dir=session_dir,
            base_url=base_url,
            build_agent=build_agent,
            config=config,
            budget=budget,
            shared_budget=shared_budget,
            git=git,
        )
        total_cost += result.cost_usd
        results.append(result)
        if result.status == MergeStatus.LANDED:
            landed_ids.append(group_obj.id)
            emit(
                session_dir,
                "group.merge.landed",
                group_id=group_obj.id,
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
            landed_ids.append(group_obj.id)
            redundant_ids.append(group_obj.id)
            emit(
                session_dir,
                "group.merge.redundant",
                group_id=group_obj.id,
                detail=result.failure_narrative or "no new diff",
            )
        else:
            blocked_ids.append(group_obj.id)
            emit(
                session_dir,
                "group.blocked",
                group_id=group_obj.id,
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
    group_obj: Group,
    candidate: MergeCandidate,
    project_dir: Path,
    session_dir: Path,
    base_url: str | None,
    build_agent: BuildAgentCallable | None,
    config: dict[str, Any],
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
    repair_session_id = ""
    raw_log_dir = session_dir / "merge" / group_obj.id
    merge_worktree = candidate.merge_worktree or candidate.worktree
    shared_merge_and_repair_worktree = _same_worktree(
        candidate.worktree,
        merge_worktree,
    )

    while True:
        wall = time.monotonic() - t0
        if wall >= budget.per_slice_wall_s:
            return MergeResult(
                group_id=group_obj.id,
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
        group_evidence: list[Evidence] = []
        cross_evidence: list[Evidence] = []
        outcome = _merge_group_branch(
            git,
            merge_worktree,
            group_id=group_obj.id, branch=candidate.branch,
            base_branch=candidate.base_branch,
        )

        # REDUNDANT semantics (Pattern A): preserve.
        merge_status = outcome.status
        if merge_status == MergeStatus.REDUNDANT and not (group_obj.feature_ids or group_obj.owned_paths):
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
                list(group_obj.checks),
                project_dir=project_dir,
                cwd=merge_worktree,
                base_url=base_url,
                raw_log_dir=raw_log_dir / f"slice-attempt-{repair_attempts:02d}",
            )
            group_evidence = [ev for _check, ev in slice_pairs]
            slice_pass = all(ev.passed for ev in group_evidence)

            cross_pairs = run_checks(
                list(spec.cross_group_checks),
                project_dir=project_dir,
                cwd=merge_worktree,
                base_url=base_url,
                raw_log_dir=raw_log_dir / f"cross-attempt-{repair_attempts:02d}",
            )
            cross_evidence = [ev for _check, ev in cross_pairs]
            cross_pass = all(ev.passed for ev in cross_evidence) if cross_evidence else True

            if slice_pass and cross_pass:
                return MergeResult(
                    group_id=group_obj.id,
                    status=merge_status,
                    landed_commit=outcome.head_after,
                    cross_slice_evidence=cross_evidence,
                    group_recheck_evidence=group_evidence,
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
                git(["reset", "--hard", outcome.head_before], merge_worktree)
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
            slice_failed_summaries = _failed_evidence_summaries(group_evidence)
        if cross_failed_summaries is None:
            cross_failed_summaries = _failed_evidence_summaries(cross_evidence)
            last_failure = "post-merge verification failed"
            if slice_failed_summaries:
                last_failure += f" — slice: {'; '.join(slice_failed_summaries[:3])}"
            if cross_failed_summaries:
                last_failure += f" — cross-slice: {'; '.join(cross_failed_summaries[:3])}"

        # If we have no agent or no repair retries left → BLOCKED.
        if build_agent is None:
            return MergeResult(
                group_id=group_obj.id,
                status=MergeStatus.BLOCKED,
                cross_slice_evidence=cross_evidence,
                group_recheck_evidence=group_evidence,
                failure_narrative=last_failure + " (no build_agent for repair)",
                repair_attempts=repair_attempts,
                cost_usd=cost_total,
                wall_s=time.monotonic() - t0,
            )
        if repair_attempts >= budget.per_slice_repair_retries:
            return MergeResult(
                group_id=group_obj.id,
                status=MergeStatus.BLOCKED,
                cross_slice_evidence=cross_evidence,
                group_recheck_evidence=group_evidence,
                failure_narrative=last_failure + " (repair retries exhausted)",
                repair_attempts=repair_attempts,
                cost_usd=cost_total,
                wall_s=time.monotonic() - t0,
            )

        # 4: invoke the build agent for repair.
        # B1 fix: when a slice branch exists, the repair MUST happen on
        # the slice's branch — not on base_branch. After
        # `_merge_group_branch` aborts a conflict, the worktree is left
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
                    session_dir, "group.attempt.failed",
                    group_id=group_obj.id, attempt=repair_attempts, detail=last_failure,
                )
                continue
        agent_input = BuildAgentInput(
            spec=spec,
            group=group_obj,
            project_dir=project_dir,
            worktree=candidate.worktree,
            branch=candidate.branch,
            attempt=repair_attempts,
            last_failure_narrative=last_failure,
            log_dir=raw_log_dir / f"repair-attempt-{repair_attempts:02d}",
            agent_session_id=repair_session_id,
            config=config,
            merge_repair=True,
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
                session_dir, "group.attempt.failed",
                group_id=group_obj.id, attempt=repair_attempts, detail=last_failure,
            )
            if on_slice_branch and shared_merge_and_repair_worktree:
                git(["checkout", candidate.base_branch], candidate.worktree)
            continue
        try:
            agent_output = await build_agent(agent_input)
            cost_total += agent_output.cost_usd
            if agent_output.session_id:
                repair_session_id = agent_output.session_id
            if shared_budget is not None:
                shared_budget.charge_cost(agent_output.cost_usd)
            if not agent_output.succeeded:
                last_failure = agent_output.detail or "agent reported failure during merge repair"
                emit(
                    session_dir,
                    "group.attempt.failed",
                    group_id=group_obj.id,
                    attempt=repair_attempts,
                    detail=last_failure,
                )
                if on_slice_branch and shared_merge_and_repair_worktree:
                    git(["checkout", candidate.base_branch], candidate.worktree)
                # Loop back to retry verification (which will fail again
                # and either re-invoke the agent or block).
                continue
            # Agent succeeded. Before committing, enforce the repair
            # write-scope that the design promised for conflict/failure
            # repair. Build-time scope crossings are warnings because a
            # user may intentionally broaden work during initial build;
            # merge repair is narrower: it should only touch this unit's
            # owned/dependency/shared paths while fixing the merge/check
            # failure.
            if on_slice_branch:
                modified = _modified_paths_for_repair(git, candidate.worktree)
                scope_violations = detect_scope_violations(
                    group_obj,
                    spec,
                    modified,
                    project_root=candidate.worktree,
                )
                if scope_violations:
                    last_failure = (
                        "merge repair scope violation: modified "
                        + ", ".join(scope_violations[:5])
                    )
                    emit(
                        session_dir,
                        "scope.warning",
                        group_id=group_obj.id,
                        attempt=repair_attempts,
                        detail=last_failure,
                        paths=list(scope_violations),
                    )
                    emit(
                        session_dir,
                        "group.attempt.failed",
                        group_id=group_obj.id,
                        attempt=repair_attempts,
                        detail=last_failure,
                    )
                    _discard_uncommitted_repair(git, candidate.worktree)
                    if shared_merge_and_repair_worktree:
                        git(["checkout", candidate.base_branch], candidate.worktree)
                    return MergeResult(
                        group_id=group_obj.id,
                        status=MergeStatus.BLOCKED,
                        cross_slice_evidence=cross_evidence,
                        group_recheck_evidence=group_evidence,
                        failure_narrative=last_failure,
                        repair_attempts=repair_attempts,
                        cost_usd=cost_total,
                        wall_s=time.monotonic() - t0,
                    )

            # Commit the repair to the slice branch so the next merge
            # attempt sees the fix.
            if on_slice_branch:
                add = git(["add", "-A"], candidate.worktree)
                if add.returncode == 0:
                    status = git(["status", "--porcelain"], candidate.worktree)
                    if (status.stdout or "").strip():
                        msg = f"i2p({group_obj.id}): repair on {candidate.branch} (attempt {repair_attempts})"
                        commit = git(
                            ["commit", "-q", "-m", msg, "--no-verify"],
                            candidate.worktree,
                        )
                        if commit.returncode != 0:
                            logger.warning(
                                "repair commit failed for slice %s: %s",
                                group_obj.id, commit.stderr,
                            )
                if shared_merge_and_repair_worktree:
                    git(["checkout", candidate.base_branch], candidate.worktree)
        except Exception as exc:
            last_failure = (
                f"merge repair agent crashed on attempt {repair_attempts}: "
                f"{type(exc).__name__}: {exc}"
            )
            emit(
                session_dir,
                "group.attempt.failed",
                group_id=group_obj.id,
                attempt=repair_attempts,
                detail=last_failure,
            )
            if on_slice_branch and shared_merge_and_repair_worktree:
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


def _modified_paths_for_repair(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
) -> list[str]:
    """Return uncommitted paths a merge-repair attempt changed."""
    paths: list[str] = []
    for args in (
        ["diff", "--name-only"],
        ["diff", "--name-only", "--cached"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        proc = git(args, worktree)
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            path = line.strip()
            if path and path not in paths:
                paths.append(path)
    return paths


def _failed_evidence_summaries(evidence: list[Evidence]) -> list[str]:
    return [_evidence_failure_summary(ev) for ev in evidence if not ev.passed]


def _evidence_failure_summary(evidence: Evidence) -> str:
    detail = evidence.detail or "check failed"
    raw = evidence.raw or {}
    stdout = str(raw.get("stdout") or "")
    stderr = str(raw.get("stderr") or "")
    excerpt = _interesting_failure_excerpt(stdout + "\n" + stderr)
    if not excerpt:
        return detail
    return f"{detail} — {excerpt}"


def _interesting_failure_excerpt(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    needles = (
        "error:",
        "error ",
        "expected",
        "received",
        "timeout",
        "failed",
        "failing",
        "assert",
        "✘",
    )
    picked: list[str] = []
    for line in lines:
        lower = line.lower()
        if any(needle in lower for needle in needles):
            picked.append(line)
        if len(picked) >= 10:
            break
    if not picked:
        picked = lines[-10:]
    excerpt = " | ".join(picked)
    return excerpt[:1200]


def _discard_uncommitted_repair(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
) -> None:
    """Discard a rejected repair attempt while preserving Otto/session files."""
    git(["reset", "--hard", "HEAD"], worktree)
    git(
        [
            "clean",
            "-fdx",
            "-e",
            ".otto/",
            "-e",
            "_otto_*",
            "-e",
            "_session/",
            "-e",
            "otto_logs/",
            "-e",
            "otto.yaml",
            "-e",
            "intent.md",
        ],
        worktree,
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


def _unstage_non_product_paths(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
) -> subprocess.CompletedProcess[str]:
    """Remove runtime/generated files from the index after `git add -A`."""
    status = git(["status", "--porcelain"], worktree)
    if status.returncode != 0:
        return status
    for non_product_path in non_product_paths_from_porcelain(status.stdout or ""):
        git(["reset", "HEAD", "--", non_product_path], worktree)
        git(
            ["rm", "--cached", "-rf", "--ignore-unmatch", "--quiet", non_product_path],
            worktree,
        )
    return git(["status", "--porcelain"], worktree)


def _branch_exists(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    branch: str,
) -> bool:
    """True if `branch` resolves to a commit in `worktree`."""
    proc = git(["rev-parse", "--verify", branch], worktree)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _merge_group_branch(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    *,
    group_id: str,
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
        return _commit_integration(git, worktree, group_id=group_id, branch=branch)

    # V9 fix: abort any in-progress merge/rebase/cherry-pick BEFORE the
    # reset+clean. Without this, MERGE_HEAD persists and `git checkout
    # base_branch` fails (observed in P2 audit: repeated "mid-MERGE_HEAD"
    # warnings).
    from otto.build import _ensure_clean_git_state
    _ensure_clean_git_state(worktree)
    # V6 fix: ensure the worktree is clean before checkout. Post-merge
    # checks (V2) can leave runtime artifacts modified (e.g. a Flask
    # check that imports the app mutates instance/db.sqlite3). Without
    # this, the next slice's `git checkout base_branch` fails with
    # "Your local changes would be overwritten by checkout". Hard-reset
    # to HEAD discards uncommitted changes to tracked files; clean
    # (preserving log/session dirs) removes untracked transient files.
    git(["reset", "--hard", "HEAD"], worktree)
    # V18: preserve user-owned project config files alongside Otto runtime paths.
    git(["clean", "-fdx",
         "-e", ".otto/", "-e", "_otto_*", "-e", "_session/", "-e", "otto_logs/",
         "-e", "otto.yaml", "-e", "intent.md"], worktree)

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
    msg = f"i2p({group_id}): merge slice branch {branch}"
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


def _same_worktree(left: Path, right: Path) -> bool:
    """Return True when two paths identify the same working tree root."""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return Path(left) == Path(right)


def _commit_integration(
    git: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
    worktree: Path,
    *,
    group_id: str,
    branch: str,
) -> CommitOutcome:
    """Stage and commit current worktree state as a slice-tagged commit.

    Phase A fallback path — used when the slice branch doesn't exist
    (no per-slice branches were set up, e.g., test fixtures without
    git, or non-git project_dir). When a slice branch DOES exist,
    `_merge_group_branch` is used instead.

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

    status = _unstage_non_product_paths(git, worktree)
    if status.returncode != 0:
        return CommitOutcome(
            status=MergeStatus.BLOCKED,
            head_before=head_before,
            head_after=head_before,
            detail=f"git status failed: {status.stderr.strip()[:200]}",
        )
    if not status.stdout.strip():
        # No changes to commit — slice didn't contribute new code.
        return CommitOutcome(
            status=MergeStatus.REDUNDANT,
            head_before=head_before,
            head_after=head_before,
            detail="no changes to commit (slice produced no diff)",
        )

    msg = f"i2p({group_id}): land slice from {branch}"
    commit = git(["commit", "-q", "-m", msg, "--no-verify"], worktree)
    if commit.returncode != 0:
        logger.warning("git commit failed for slice %s: %s", group_id, commit.stderr)
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


def passing_group_ids(build_result: BuildResult) -> list[str]:
    """Return latest Group ids the build loop marked PASSING.

    If the same Group appears multiple times, later results supersede
    earlier ones. This mirrors merge-queue eligibility and prevents an
    older PASSING result from leaking through after a later BLOCKED or
    repaired attempt for the same id.
    """
    return [
        group_id
        for group_id, result in _latest_group_results(build_result).items()
        if result.status == GroupStatus.PASSING
    ]


__all__ = [
    "MergeBudget",
    "MergeCandidate",
    "MergeQueueResult",
    "MergeResult",
    "MergeStatus",
    "eligible_candidates",
    "eligible_components",
    "passing_group_ids",
    "run_merge_queue",
    "shared_paths_set",
]


# Pin imports for static checkers — these are part of the public flow even
# though only some are referenced directly above.
_ = (Any,)
