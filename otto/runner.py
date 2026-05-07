"""Top-level Run orchestrator (Phase A1.6).

Single source of truth for the new-stack pipeline chain:

    intent → compile_spec → seed_fixtures → run_build → run_merge_queue
           → run_audit    → repair_failing_features (Layer 2)
           → render_run

Before this module landed, the chain was duplicated inline inside
``otto/cli_run.py:orchestrate_run`` and partially mirrored in
``orchestrate_certify`` / ``orchestrate_improve``. The seed stage
(``otto/seed.py``) and the Layer 2 repair loop (``otto/audit_loop.py``)
existed but were not wired into the live pipeline.

This module owns ONLY the async chain — it is intentionally headless.
CLI concerns (lock acquisition, intent resolution, sys.exit, console
output, ``--from-spec``) stay in ``otto/cli_run.py``; that module now
delegates the chain to ``run_pipeline``.

Design notes (honest gaps):

* ``run_audit`` retains an internal Group-level repair loop for
  direct/legacy callers when ``fix_agent`` is provided. This runner
  deliberately calls it with ``fix_agent=None`` and reserves repair for
  ``repair_failing_features`` — the Feature-level Layer 2 loop. Without
  ``fix_agent`` (e.g. certify mode), repair is skipped.
* Brownfield mode skips ``run_build`` / ``run_merge_queue`` entirely:
  the existing project IS the integrated worktree, so there are no
  slices to drive. ``BuildResult`` / ``MergeQueueResult`` are honestly
  empty.
* Seed failure halts the run pre-audit with ``verdict=blocked``.
  Auditing a half-seeded product produces meaningless verdicts (per
  research §4 / ``otto/seed.py`` docstring).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from otto import paths as _paths
from otto.audit import (
    AuditBudget,
    AuditResult,
    AuditVerdict,
    AuditAgentCallable,
    WalkthroughCallable,
    default_walkthrough_from_spec,
    run_audit,
)
from otto.audit_loop import (
    FailingFeature,
    RepairAttempt,
    RepairResult,
    features_to_repair,
    repair_failing_features,
)
from otto.build import (
    BuildAgentCallable,
    BuildAgentInput,
    BuildBudget,
    BuildResult,
    ComponentResult,
    ComponentStatus,
    GroupResult,
    GroupStatus,
    resolve_integration_base_branch,
    run_build,
)
from otto.merge_queue import (
    MergeBudget,
    MergeQueueResult,
    run_merge_queue,
)
from otto.render import render_run
from otto.resume import ResumePlan
from otto.seed import SeedResult, seed_fixtures
from otto.spec_compile import Feature, Group, Spec, compile_spec
from otto.spec_state import emit, is_run_paused_by_user, iter_events

logger = logging.getLogger("otto.runner")


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Aggregate outcome of one new-stack pipeline run.

    All fields are populated honestly — phases that did not run leave
    their corresponding result attribute as ``None`` or an empty
    placeholder (NOT a fake "PASSED" / cost=0 lie). The CLI uses
    ``verdict`` and ``halted_reason`` to choose the exit code.
    """

    spec: Spec
    seed_result: SeedResult | None = None
    build_result: BuildResult | None = None
    merge_result: MergeQueueResult | None = None
    audit_result: AuditResult | None = None
    repair_result: RepairResult | None = None
    html_path: Path | None = None
    json_path: Path | None = None
    wall_s: float = 0.0
    cost_usd: float = 0.0
    halted_reason: str = ""  # "" = ran to completion; else a phase short-circuited

    @property
    def verdict(self) -> AuditVerdict:
        """Final verdict. ``BLOCKED`` if seed/audit never produced one."""
        if self.audit_result is not None:
            return self.audit_result.verdict
        return AuditVerdict.BLOCKED


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def run_pipeline(
    intent: str,
    project_dir: Path,
    session_dir: Path,
    *,
    project_kind: str = "webapp",
    brownfield: bool = False,
    base_url: str | None = None,
    config: dict[str, Any] | None = None,
    build_agent: BuildAgentCallable,
    audit_agent: AuditAgentCallable,
    fix_agent: BuildAgentCallable | None = None,
    walkthrough: WalkthroughCallable | None = None,
    base_branch: str | None = None,
    spec: Spec | None = None,
    audit_budget: AuditBudget | None = None,
    on_phase: "Callable[[str], None] | None" = None,
    resume_plan: ResumePlan | None = None,
    review_gate: bool = False,
    gate_timeout_s: float = 24 * 60 * 60.0,  # 24h default; A13
    gate_poll_s: float = 1.0,
    gate_announce: "Callable[[str], None] | None" = None,
    command: str = "run",
) -> RunResult:
    """Drive the full intent-to-product pipeline.

    Args:
        intent: User's verbatim intent. Ignored when ``spec`` is provided
            (caller compiled the spec elsewhere, e.g. ``--from-spec``).
        project_dir: Project root (git worktree top).
        session_dir: Session log directory (already allocated by caller).
        project_kind: Hint for the compile agent's structure schema.
        brownfield: If True, dispatch the brownfield compile prompt and
            SKIP the build + merge phases (the project IS the integrated
            worktree). Used by the certify / improve flows.
        base_url: Optional base URL for HTTP-based checks during the
            build / merge / audit phases.
        config: Loaded ``otto.yaml`` (forwarded to ``compile_spec``).
            Required when ``spec`` is None. Ignored otherwise.
        build_agent: Callable for build-phase slice agents AND merge-phase
            repair attempts. Production wires ``default_build_agent``;
            tests pass a stub.
        audit_agent: Callable for the LLM judge in ``run_audit``.
        fix_agent: Optional. When provided AND audit verdict is non-PASS,
            the runner invokes Layer 2 (``repair_failing_features``) to
            route failing Features back to their owning Group's agent for
            one repair attempt each. ALSO threaded into ``run_audit`` as
            its inner-loop fix_agent. ``None`` disables both repair loops
            (certify mode: judge what's there, no repair).
        walkthrough: Optional walkthrough hook (default: derived from
            spec via ``default_walkthrough_from_spec``).
        base_branch: Integration branch for build/merge. Defaults to the
            active git branch in ``project_dir`` and falls back to ``main``.
        spec: If non-None, the runner skips the compile phase and uses
            this spec directly. Caller still owns writing it to disk.
        audit_budget: Audit phase bounds. ``None`` uses library default.

    Returns:
        ``RunResult`` with every phase result populated honestly.
        Phases that did not run (compile-skip, brownfield build/merge,
        no-op seed) leave their attribute at ``None`` / empty.

    Never raises for normal pipeline failures — seed errors, audit
    crashes, etc. surface through ``RunResult.halted_reason`` +
    ``audit_result.verdict``. Programmer errors (bad arguments) DO
    raise, since they indicate a bug in the caller.
    """
    base_branch = base_branch or resolve_integration_base_branch(project_dir)
    # config is required only when we actually need to compile.
    if spec is None and config is None:
        raise ValueError(
            "run_pipeline: config must be provided when spec is None "
            "(needed by compile_spec)."
        )

    run_t0 = time.monotonic()

    def _phase(name: str) -> None:
        # A7: between phases, honor an operator-initiated pause. We poll
        # the spec-state journal here (rather than SIGSTOP-ing the
        # process) because phases may hold async I/O (LLM calls,
        # subprocesses, file handles); freezing them mid-IO is unsafe.
        # The runner enters a passive sleep until either a
        # `run.resumed_by_user` event is appended or the operator cancels
        # via the existing cancel path (cancel terminates the process,
        # so the loop will be unblocked by SIGTERM). On the first call
        # this is a near no-op (no events yet). The poll cadence is 1s,
        # which is fast enough to feel responsive without hammering the
        # filesystem during a paused run.
        _wait_while_paused(session_dir)
        if on_phase is not None:
            try:
                on_phase(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_phase(%r) callback raised: %s", name, exc)

    try:
        emit(session_dir, "run.started", detail="run start")
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning("emit run start failed: %s", exc)

    # Round-3 audit gap 3 — surface A7 pause + A6 prior invalidations.
    # We don't refuse: the operator may have explicitly invoked
    # `otto build --resume` knowing they paused, and the same poll-flag
    # design that powers `_wait_while_paused` will trip on the first
    # phase boundary anyway. A loud one-line log is the right contract.
    if resume_plan is not None:
        if resume_plan.paused_by_user:
            logger.warning(
                "resume: prior session was paused by user; runner will sleep "
                "at the first phase boundary until a resume event is appended "
                "(session=%s)", session_dir.name,
            )
        if resume_plan.prior_invalidated_group_ids:
            logger.warning(
                "resume: %d Group(s) carry prior `group.invalidated_by_spec_edit` "
                "events with no terminal phase since (ids=%s); they will flow "
                "through the post-build re-dispatch path along with any new "
                "invalidations (session=%s)",
                len(resume_plan.prior_invalidated_group_ids),
                sorted(resume_plan.prior_invalidated_group_ids),
                session_dir.name,
            )

    # ---- 1. Compile ----
    if spec is None:
        _phase("compile")
        assert config is not None  # narrowed above
        run_dir = session_dir / "spec"
        run_dir.mkdir(parents=True, exist_ok=True)
        spec = await compile_spec(
            intent,
            project_dir,
            run_dir,
            config,
            project_kind=project_kind,
            brownfield=brownfield,
        )

    result = RunResult(spec=spec)
    _mark_i2p_run_active(
        project_dir=project_dir,
        session_dir=session_dir,
        command=command,
        intent=spec.intent or intent,
    )

    # ---- 1.5. Review gate (A13, optional) ----
    # When the operator passes ``--review-gate`` (default off), pause here
    # between compile and build so the spec can be reviewed / edited via
    # Mission Control before any agent runs against it. The gate is
    # implemented as a journal-event poll: emit ``spec.review_pending``,
    # surface a URL via ``gate_announce``, then wait for a
    # ``spec.review_approved`` event (written by the operator's POST to
    # ``/api/specs/<session>/approve`` — see ``otto/web/spec_review_routes.py``).
    # On timeout the run halts with verdict=BLOCKED honestly.
    if review_gate and resume_plan is None:
        approved = await _wait_for_review_approval(
            session_dir=session_dir,
            timeout_s=gate_timeout_s,
            poll_s=gate_poll_s,
            announce=gate_announce,
        )
        if not approved:
            result.halted_reason = (
                f"review_gate_timeout: no approval within {gate_timeout_s:.0f}s"
            )
            result.audit_result = AuditResult(
                verdict=AuditVerdict.BLOCKED,
                narrative=(
                    "Run halted before build: review gate timed out waiting "
                    "for spec.review_approved."
                ),
            )
            result.wall_s = time.monotonic() - run_t0
            try:
                emit(
                    session_dir,
                    "run.finished",
                    detail=result.halted_reason,
                    verdict=AuditVerdict.BLOCKED.value,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("emit run.finished failed: %s", exc)
            return result

    # ---- 2. Seed (research §4 audit fixtures) ----
    # Empty audit_fixtures is a no-op success (SeedResult.detail says so).
    # Failure halts the run honestly: a half-seeded product produces
    # meaningless audit verdicts.
    _phase("seed")
    seed_result = seed_fixtures(spec, project_dir, session_dir=session_dir)
    result.seed_result = seed_result
    if not seed_result.succeeded:
        result.halted_reason = f"seed_failed: {seed_result.detail}"
        # Synthesize a BLOCKED audit verdict so the CLI exit code is honest.
        result.audit_result = AuditResult(
            verdict=AuditVerdict.BLOCKED,
            narrative=(
                f"Run halted before audit: seed stage failed. "
                f"{seed_result.detail}"
            ),
        )
        result.wall_s = time.monotonic() - run_t0
        try:
            emit(
                session_dir,
                "run.finished",
                detail=result.halted_reason,
                verdict=AuditVerdict.BLOCKED.value,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("emit run.finished failed: %s", exc)
        _mark_i2p_run_complete(
            project_dir=project_dir,
            session_dir=session_dir,
            total_cost=result.cost_usd,
            total_duration=result.wall_s,
        )
        return result

    # Shared budget threaded across build, merge, audit so the documented
    # "$X total" ceiling is enforced as one pool (matches the C1 fix in
    # the legacy orchestrate_run path).
    shared_budget = BuildBudget()
    # Resume cost-carry: prior attempt's spend counts against the shared
    # cap by default. ``orchestrate_run --reset-budget`` is the escape
    # hatch — the CLI zeroes ``resume_plan.prior_cost_usd`` before
    # passing the plan in, so this branch becomes a no-op.
    if resume_plan is not None and resume_plan.prior_cost_usd > 0.0:
        shared_budget.charge_cost(resume_plan.prior_cost_usd)

    # ---- 3. Build (greenfield only) ----
    if brownfield:
        # Brownfield: project IS the integrated worktree. No slices to
        # drive. spec_session_dir matches what cli_run did.
        build_result = BuildResult(spec_session_dir=session_dir / "spec")
    else:
        _phase("build")
        # Resume: skip already-LANDED Components/Groups. ``run_build``
        # synthesises BLOCKED-status entries for skipped ids from the
        # prior run so render's accounting still sees them.
        skip_components: set[str] = (
            set(resume_plan.landed_components) if resume_plan is not None else set()
        )
        resume_agent_sessions: dict[str, str] = (
            dict(resume_plan.agent_session_ids) if resume_plan is not None else {}
        )
        if resume_plan is not None and resume_plan.prior_invalidated_group_ids:
            # Outer product truth wins over inner-agent memory. If a unit was
            # invalidated by a spec edit, do not resume a thread that was
            # primed with the old contract.
            for invalidated_id in resume_plan.prior_invalidated_group_ids:
                resume_agent_sessions.pop(invalidated_id, None)
        # A6: open the mid-build edit window. Spec-review's POST /edit
        # accepts edits while lifecycle == "editing_in_flight"; after
        # build (and any re-dispatch) returns we revert to "approved"
        # so downstream amendment-flow promises hold. Best-effort:
        # write failures are logged, never fatal.
        _set_lifecycle_best_effort(session_dir, "editing_in_flight")
        try:
            build_result = await run_build(
                spec,
                project_dir=project_dir,
                session_dir=session_dir,
                build_agent=build_agent,
                config=config,
                base_url=base_url,
                budget=shared_budget,
                base_branch=base_branch,
                skip_components=skip_components,
                resume_agent_sessions=resume_agent_sessions,
            )
            # A6: if mid-build spec edits invalidated any Groups, re-dispatch
            # the affected Groups against the (now persisted) post-edit Spec.
            # We re-load from disk because the edit endpoint persisted the
            # canonical post-edit Spec there. One pass: a second edit during
            # re-dispatch is observable in the journal but doesn't trigger
            # a third dispatch (operator runs `otto build --resume`).
            redispatch_ids = _invalidated_group_ids(session_dir)
            if redispatch_ids:
                logger.info(
                    "spec edit invalidation: re-dispatching groups %s",
                    sorted(redispatch_ids),
                )
                spec, build_result = await _redispatch_invalidated_groups(
                    redispatch_ids=redispatch_ids,
                    spec=spec,
                    prior_build_result=build_result,
                    project_dir=project_dir,
                    session_dir=session_dir,
                    build_agent=build_agent,
                    config=config,
                    base_url=base_url,
                    shared_budget=shared_budget,
                    base_branch=base_branch,
                    skip_components=skip_components,
                    resume_agent_sessions=resume_agent_sessions,
                )
                result.spec = spec
        finally:
            _set_lifecycle_best_effort(session_dir, "approved")
    result.build_result = build_result

    # ---- 4. Merge (greenfield only) ----
    if brownfield:
        merge_result = MergeQueueResult()
    else:
        _phase("merge")
        merge_result = await run_merge_queue(
            spec,
            build_result,
            project_dir=project_dir,
            session_dir=session_dir,
            base_url=base_url,
            base_branch=base_branch,
            build_agent=build_agent,
            config=config,
            budget=MergeBudget(),
            shared_budget=shared_budget,
            skip_components=skip_components,
        )
        build_result, merge_result = await _rebuild_dependency_setup_blocked_units(
            spec=spec,
            build_result=build_result,
            merge_result=merge_result,
            project_dir=project_dir,
            session_dir=session_dir,
            build_agent=build_agent,
            config=config,
            base_url=base_url,
            shared_budget=shared_budget,
            base_branch=base_branch,
            skip_components=skip_components,
            resume_agent_sessions=resume_agent_sessions,
        )
        result.build_result = build_result
    result.merge_result = merge_result

    # ---- 5. Audit ----
    _phase("audit")
    walk = walkthrough or default_walkthrough_from_spec(spec, base_url=base_url)
    # Resume short-circuit: when the prior run already ran the audit
    # to completion (audit.finished + non-empty verdict), skip the
    # whole phase. We synthesise an AuditResult from the journal so
    # render still produces a proof packet — re-running the audit
    # would burn $0.50–$2 to reproduce a known answer.
    if resume_plan is not None and resume_plan.audit_finished:
        audit_result = _audit_result_from_resume_plan(resume_plan)
    else:
        audit_result = await run_audit(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_result=build_result,
            merge_result=merge_result,
            audit_agent=audit_agent,
            config=config,
            base_url=base_url,
            walkthrough=walk,
            # The live i2p runner uses only the Feature-scoped Layer 2
            # repair loop below. Passing fix_agent into run_audit would
            # stack the old Group-level compatibility loop on top of it.
            fix_agent=None,
            budget=audit_budget or AuditBudget(),
            shared_budget=shared_budget,
            base_branch=base_branch,
        )
    result.audit_result = audit_result
    audit_cost_total = float(audit_result.cost_usd or 0.0)
    repair_cost_total = 0.0

    # ---- 6. Layer 2 repair (research §4 retry layers) ----
    # Triggered only on non-PASS verdicts and only when a fix_agent is
    # available. ``run_audit`` already does its own slice-level repair;
    # this is the FEATURE-level Layer 2 loop.
    repair_spec = _spec_with_group_feature_fallbacks(spec)
    if (
        audit_result.verdict != AuditVerdict.PASSED
        and fix_agent is not None
        and repair_spec.features
    ):
        # Keep downstream proof/render Feature-aware for compact specs, but
        # do not rewrite the persisted spec just to repair a legacy shape.
        spec = repair_spec
        result.spec = spec
        feature_verdicts = _repair_verdicts_for_audit(spec, audit_result)
        if feature_verdicts:
            _phase("repair")
            bridge = _make_layer2_fix_agent(
                fix_agent=fix_agent,
                spec=spec,
                project_dir=project_dir,
                session_dir=session_dir,
                base_url=base_url,
                config=config,
                shared_budget=shared_budget,
            )
            audit_budget_for_recheck = audit_budget or AuditBudget()

            async def _re_audit_feature_subset(
                feature_ids: list[str],
            ) -> list[dict[str, Any]]:
                # The feature id list is useful as repair-loop focus, but
                # the final verdict must remain product-wide. A scoped
                # re-audit can prove the just-repaired Feature while
                # omitting older failures that did not fit in the retry
                # cap, which turns a partial product into a false pass.
                _ = feature_ids
                nonlocal audit_result, audit_cost_total
                recheck = await run_audit(
                    spec,
                    project_dir=project_dir,
                    session_dir=session_dir,
                    build_result=build_result,
                    merge_result=merge_result,
                    audit_agent=audit_agent,
                    config=config,
                    base_url=base_url,
                    walkthrough=walk,
                    fix_agent=None,
                    budget=AuditBudget(
                        audit_retries=0,
                        walk_timeout_s=audit_budget_for_recheck.walk_timeout_s,
                        judge_timeout_s=audit_budget_for_recheck.judge_timeout_s,
                    ),
                    shared_budget=shared_budget,
                    base_branch=base_branch,
                )
                audit_cost_total += float(recheck.cost_usd or 0.0)
                audit_result = replace(recheck, cost_usd=audit_cost_total)
                result.audit_result = audit_result
                return _repair_verdicts_for_audit(spec, audit_result)

            try:
                repair_result = await repair_failing_features(
                    spec=spec,
                    feature_verdicts=feature_verdicts,
                    fix_agent=bridge,
                    re_audit=_re_audit_feature_subset,
                )
                repair_cost_total = sum(
                    float(a.cost_usd or 0.0) for a in repair_result.attempts
                )
                result.repair_result = repair_result
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Layer 2 repair_failing_features raised: %s: %s",
                    type(exc).__name__,
                    exc,
                )

    # ---- 7. Render ----
    _phase("render")
    wall = time.monotonic() - run_t0
    cost = (
        build_result.total_cost_usd
        + merge_result.total_cost_usd
        + audit_result.cost_usd
        + repair_cost_total
    )
    html_path, json_path = render_run(
        spec,
        session_dir=session_dir,
        build_result=build_result,
        merge_result=merge_result,
        audit_result=audit_result,
        wall_s=wall,
        cost_usd=cost,
    )
    result.html_path = html_path
    result.json_path = json_path
    result.wall_s = wall
    result.cost_usd = cost

    try:
        emit(
            session_dir,
            "run.finished",
            detail=f"verdict={audit_result.verdict.value}",
            verdict=audit_result.verdict.value,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("emit run.finished failed: %s", exc)
    _mark_i2p_run_complete(
        project_dir=project_dir,
        session_dir=session_dir,
        total_cost=result.cost_usd,
        total_duration=result.wall_s,
    )

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Pause poll cadence. 1s feels responsive without hammering the
# filesystem during a long pause. Tests monkeypatch this to 0 to make
# pause-resume cycles deterministic.
PAUSE_POLL_INTERVAL_S = 1.0


def _set_lifecycle_best_effort(session_dir: Path, lifecycle: str) -> None:
    """Write the spec lifecycle state file. Logs and swallows IO errors.

    A6: the runner doesn't own the spec-review HTTP endpoint, but it
    does own the lifecycle window for in-flight edits. Writing
    `lifecycle.json` directly avoids a circular dependency on
    `otto.web.spec_review_routes`.
    """
    spec_subdir = session_dir / "spec"
    if not spec_subdir.exists():
        return
    try:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        (spec_subdir / "lifecycle.json").write_text(
            _json.dumps(
                {
                    "lifecycle": lifecycle,
                    "updated_at": _dt.now(tz=_tz.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("set_lifecycle(%s) failed: %s", lifecycle, exc)


def _invalidated_group_ids(session_dir: Path) -> set[str]:
    """Return Group IDs flagged invalidated by spec edit AND not yet
    superseded by a terminal phase event.

    A6: a Group whose LAST relevant journal event is
    `group.invalidated_by_spec_edit` needs re-dispatch. If the build
    loop already wrote a `group.merge.landed`, `group.blocked`, or
    `group.aborted_by_user`
    AFTER the invalidation, the signal was acted on (or made moot)
    and we skip it.
    """
    invalidated: set[str] = set()
    last_kind_for_group: dict[str, str] = {}
    try:
        for event in iter_events(session_dir):
            if not event.group_id:
                continue
            if event.kind == "group.invalidated_by_spec_edit":
                invalidated.add(event.group_id)
                last_kind_for_group[event.group_id] = event.kind
            elif event.kind in (
                "group.merge.landed",
                "group.blocked",
                "group.aborted_by_user",
            ):
                last_kind_for_group[event.group_id] = event.kind
    except OSError as exc:
        logger.warning("scan invalidated groups failed: %s", exc)
        return set()
    return {
        gid for gid in invalidated
        if last_kind_for_group.get(gid) == "group.invalidated_by_spec_edit"
    }


async def _redispatch_invalidated_groups(
    *,
    redispatch_ids: set[str],
    spec: Spec,
    prior_build_result: BuildResult,
    project_dir: Path,
    session_dir: Path,
    build_agent: BuildAgentCallable,
    config: dict[str, Any],
    base_url: str | None,
    shared_budget: BuildBudget,
    base_branch: str,
    skip_components: set[str],
    resume_agent_sessions: dict[str, str] | None = None,
) -> tuple[Spec, BuildResult]:
    """Re-load the post-edit spec, re-build invalidated Groups,
    and merge the new GroupResult entries into the prior BuildResult.

    A6: the spec-review edit endpoint persisted the post-edit Spec to
    disk, so we re-load from there. We then run a second `run_build`
    pass with everything EXCEPT the invalidated Groups added to
    skip_components — the readiness gate inside `run_build` will then
    dispatch only the invalidated set in dep-topological order.

    The returned BuildResult merges: prior GroupResults survive for
    non-invalidated ids, second-pass GroupResults replace invalidated
    ids, Component results are preserved from the prior pass.
    """
    from otto.spec_compile import load_spec

    spec_path = session_dir / "spec" / "spec.json"
    try:
        post_edit_spec = load_spec(spec_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "re-dispatch: failed to re-load post-edit spec from %s: %s — "
            "keeping prior build result", spec_path, exc,
        )
        return spec, prior_build_result

    # Build the second-pass skip set: every Group/Component except
    # those flagged for re-dispatch.
    second_skip: set[str] = set(skip_components)
    for g in post_edit_spec.groups:
        if g.id not in redispatch_ids:
            second_skip.add(g.id)
    for c in (post_edit_spec.components or []):
        second_skip.add(c.id)

    try:
        second_result = await run_build(
            post_edit_spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_agent=build_agent,
            config=config,
            base_url=base_url,
            budget=shared_budget,
            base_branch=base_branch,
            skip_components=second_skip,
            resume_agent_sessions=resume_agent_sessions,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the runner here
        logger.warning(
            "re-dispatch: run_build raised %s: %s — keeping prior build result",
            type(exc).__name__, exc,
        )
        return post_edit_spec, prior_build_result

    # Merge results.
    merged_groups: list[GroupResult] = []
    seen_ids: set[str] = set()
    for sr in second_result.group_results:
        if sr.group_id in redispatch_ids:
            merged_groups.append(sr)
            seen_ids.add(sr.group_id)
    for pr in prior_build_result.group_results:
        if pr.group_id in seen_ids:
            continue
        merged_groups.append(pr)

    merged = BuildResult(
        spec_session_dir=prior_build_result.spec_session_dir,
        group_results=merged_groups,
        component_results=list(prior_build_result.component_results),
        total_cost_usd=(
            prior_build_result.total_cost_usd + second_result.total_cost_usd
        ),
        total_wall_s=(
            prior_build_result.total_wall_s + second_result.total_wall_s
        ),
    )
    return post_edit_spec, merged


async def _rebuild_dependency_setup_blocked_units(
    *,
    spec: Spec,
    build_result: BuildResult,
    merge_result: MergeQueueResult,
    project_dir: Path,
    session_dir: Path,
    build_agent: BuildAgentCallable,
    config: dict[str, Any],
    base_url: str | None,
    shared_budget: BuildBudget,
    base_branch: str,
    skip_components: set[str],
    resume_agent_sessions: dict[str, str] | None = None,
) -> tuple[BuildResult, MergeQueueResult]:
    """Give downstream units one pass after dependency merge repair.

    The first build pass may block a downstream Group/Component while trying to
    create a branch from multiple raw sibling dependency branches. The merge
    queue can later repair and land those sibling branches. Once that happens,
    the downstream unit should build against the repaired integrated branch
    instead of staying permanently blocked on stale pre-repair dependency refs.
    """

    retry_ids = _dependency_setup_retry_ids(
        spec=spec,
        build_result=build_result,
        merge_result=merge_result,
    )
    if not retry_ids:
        return build_result, merge_result

    all_unit_ids = {g.id for g in spec.groups}
    all_unit_ids.update(c.id for c in (getattr(spec, "components", None) or []))
    second_skip = set(skip_components)
    second_skip.update(uid for uid in all_unit_ids if uid not in retry_ids)

    try:
        retry_build = await run_build(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_agent=build_agent,
            config=config,
            base_url=base_url,
            budget=shared_budget,
            base_branch=base_branch,
            skip_components=second_skip,
            resume_agent_sessions=resume_agent_sessions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dependency-retry: run_build raised %s: %s",
            type(exc).__name__,
            exc,
        )
        return build_result, merge_result

    merged_build = _merge_retry_build_results(
        prior=build_result,
        retry=retry_build,
        retry_ids=retry_ids,
    )
    retry_merge_build = _filter_build_result_for_retry_merge(
        merged_build,
        allowed_ids=set(merge_result.landed_ids) | retry_ids,
    )
    try:
        retry_merge = await run_merge_queue(
            spec,
            retry_merge_build,
            project_dir=project_dir,
            session_dir=session_dir,
            base_url=base_url,
            base_branch=base_branch,
            build_agent=build_agent,
            config=config,
            budget=MergeBudget(),
            shared_budget=shared_budget,
            skip_components=set(merge_result.landed_ids),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dependency-retry: run_merge_queue raised %s: %s",
            type(exc).__name__,
            exc,
        )
        return merged_build, merge_result

    return merged_build, _combine_merge_results(merge_result, retry_merge)


def _dependency_setup_retry_ids(
    *,
    spec: Spec,
    build_result: BuildResult,
    merge_result: MergeQueueResult,
) -> set[str]:
    landed = set(merge_result.landed_ids)
    latest_groups: dict[str, GroupResult] = {}
    for result in build_result.group_results:
        latest_groups[result.group_id] = result
    latest_components: dict[str, ComponentResult] = {}
    for result in build_result.component_results:
        latest_components[result.component_id] = result

    retry: set[str] = set()
    for group in spec.groups:
        result = latest_groups.get(group.id)
        if (
            result is not None
            and result.status == GroupStatus.BLOCKED
            and _is_dependency_setup_failure(result.failure_narrative)
            and all(dep in landed for dep in (group.dependencies or []))
        ):
            retry.add(group.id)
    for component in (getattr(spec, "components", None) or []):
        result = latest_components.get(component.id)
        if (
            result is not None
            and result.status == ComponentStatus.BLOCKED
            and _is_dependency_setup_failure(result.failure_narrative)
            and all(dep in landed for dep in (component.dependencies or []))
        ):
            retry.add(component.id)
    return retry


def _is_dependency_setup_failure(narrative: str | None) -> bool:
    return (narrative or "").startswith("dependency branch setup failed:")


def _merge_retry_build_results(
    *,
    prior: BuildResult,
    retry: BuildResult,
    retry_ids: set[str],
) -> BuildResult:
    retry_groups = {
        result.group_id: result
        for result in retry.group_results
        if result.group_id in retry_ids
    }
    retry_components = {
        result.component_id: result
        for result in retry.component_results
        if result.component_id in retry_ids
    }

    merged_groups: list[GroupResult] = []
    seen_groups: set[str] = set()
    for result in prior.group_results:
        replacement = retry_groups.get(result.group_id)
        if replacement is not None:
            merged_groups.append(replacement)
            seen_groups.add(result.group_id)
        else:
            merged_groups.append(result)
    for group_id, result in retry_groups.items():
        if group_id not in seen_groups:
            merged_groups.append(result)

    merged_components: list[ComponentResult] = []
    seen_components: set[str] = set()
    for result in prior.component_results:
        replacement = retry_components.get(result.component_id)
        if replacement is not None:
            merged_components.append(replacement)
            seen_components.add(result.component_id)
        else:
            merged_components.append(result)
    for component_id, result in retry_components.items():
        if component_id not in seen_components:
            merged_components.append(result)

    return BuildResult(
        spec_session_dir=prior.spec_session_dir,
        group_results=merged_groups,
        component_results=merged_components,
        total_cost_usd=prior.total_cost_usd + retry.total_cost_usd,
        total_wall_s=prior.total_wall_s + retry.total_wall_s,
    )


def _filter_build_result_for_retry_merge(
    build_result: BuildResult,
    *,
    allowed_ids: set[str],
) -> BuildResult:
    return BuildResult(
        spec_session_dir=build_result.spec_session_dir,
        group_results=[
            result
            for result in build_result.group_results
            if result.group_id in allowed_ids
        ],
        component_results=[
            result
            for result in build_result.component_results
            if result.component_id in allowed_ids
        ],
        total_cost_usd=0.0,
        total_wall_s=0.0,
    )


def _combine_merge_results(
    first: MergeQueueResult,
    second: MergeQueueResult,
) -> MergeQueueResult:
    landed = _dedupe([*first.landed_ids, *second.landed_ids])
    redundant = _dedupe([*first.redundant_ids, *second.redundant_ids])
    landed_set = set(landed)
    blocked = _dedupe(
        [
            group_id
            for group_id in [*first.blocked_ids, *second.blocked_ids]
            if group_id not in landed_set
        ]
    )
    return MergeQueueResult(
        landed_ids=landed,
        blocked_ids=blocked,
        redundant_ids=redundant,
        results=[*first.results, *second.results],
        total_cost_usd=first.total_cost_usd + second.total_cost_usd,
        total_wall_s=first.total_wall_s + second.total_wall_s,
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _wait_while_paused(session_dir: Path) -> None:
    """Block until the run is no longer in the paused-by-user state.

    Reads the spec-state journal each tick — append-only ensures we
    will see a `run.resumed_by_user` event written from the web layer.
    The loop is a simple polling sleep; no condition variables or
    inotify because operator-driven pauses are rare and the cost of a
    1s tick is negligible compared to a build phase. Cancellation is
    NOT handled here: cancel sends SIGTERM to the runner process, which
    terminates the sleep and the surrounding asyncio task naturally.
    """
    if not is_run_paused_by_user(session_dir):
        return
    logger.info("runner: pause detected; sleeping until resumed (session=%s)", session_dir.name)
    while is_run_paused_by_user(session_dir):
        time.sleep(PAUSE_POLL_INTERVAL_S)
    logger.info("runner: pause cleared; resuming (session=%s)", session_dir.name)


def _managed_session_id(project_dir: Path, session_dir: Path) -> str | None:
    """Return the session id when ``session_dir`` uses Otto's log layout.

    Unit tests sometimes pass an arbitrary temp directory as ``session_dir``.
    The pointer/checkpoint helpers operate on
    ``<project>/otto_logs/sessions/<id>``, so skip unmanaged dirs instead
    of writing a misleading pointer to a sibling path.
    """
    try:
        resolved_session = Path(session_dir).resolve()
        resolved_root = _paths.sessions_root(project_dir).resolve()
    except OSError:
        return None
    if resolved_session.parent != resolved_root:
        return None
    if not resolved_session.name:
        return None
    return resolved_session.name


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _mark_i2p_run_active(
    *,
    project_dir: Path,
    session_dir: Path,
    command: str,
    intent: str,
) -> None:
    """Persist the paused pointer/checkpoint consumed by i2p ``--resume``."""
    session_id = _managed_session_id(project_dir, session_dir)
    if session_id is None:
        return
    spec_path = Path(session_dir) / "spec" / "spec.json"
    spec_hash = _file_sha256(spec_path) if spec_path.exists() else ""
    try:
        from otto.checkpoint import write_checkpoint

        write_checkpoint(
            project_dir,
            run_id=session_id,
            command=command,
            status="in_progress",
            phase="i2p",
            intent=intent,
            spec_path=str(spec_path) if spec_path.exists() else "",
            spec_hash=spec_hash,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("i2p checkpoint write failed for %s: %s", session_id, exc)
    try:
        _paths.set_pointer(project_dir, _paths.LATEST_POINTER, session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("i2p latest pointer write failed for %s: %s", session_id, exc)


def _mark_i2p_run_complete(
    *,
    project_dir: Path,
    session_dir: Path,
    total_cost: float,
    total_duration: float,
) -> None:
    """Mark the i2p checkpoint terminal and clear the paused pointer."""
    session_id = _managed_session_id(project_dir, session_dir)
    if session_id is None:
        return
    try:
        from otto.checkpoint import complete_checkpoint

        complete_checkpoint(
            project_dir,
            run_id=session_id,
            total_cost=total_cost,
            total_duration=total_duration,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("i2p checkpoint completion failed for %s: %s", session_id, exc)


def _feature_audits_to_verdicts(
    spec: Spec, audit_result: AuditResult
) -> list[dict[str, Any]]:
    """Project ``audit_result.feature_audits`` onto Feature ids.

    Mirrors the projection in ``otto/render.py:compose_proof_packet`` so
    the Layer 2 repair loop sees the same view as the proof packet.
    Features without an audit entry are omitted (no verdict to feed).

    Audit agents sometimes return a group-level entry, using the Group id
    or name as ``feature_id``/``name``. Layer 2 repairs Features, not
    Groups, so route that entry to the most likely Feature in the group
    instead of dropping a fixable failure on the floor.
    """
    if not spec.features:
        return []
    audits_by_key: dict[str, Any] = {}
    for fa in audit_result.feature_audits:
        if fa.feature_id:
            audits_by_key[fa.feature_id] = fa
        audits_by_key[fa.name] = fa
    out: list[dict[str, Any]] = []
    seen_feature_ids: set[str] = set()
    for feature in spec.features:
        fa = audits_by_key.get(feature.id) or audits_by_key.get(feature.name)
        if fa is None:
            continue
        out.append(_feature_verdict_payload(feature.id, fa))
        seen_feature_ids.add(feature.id)

    for fa in audit_result.feature_audits:
        if fa.feature_id in seen_feature_ids or fa.name in seen_feature_ids:
            continue
        for feature_id in _feature_ids_for_group_audit(spec, fa):
            if feature_id in seen_feature_ids:
                continue
            out.append(_feature_verdict_payload(feature_id, fa))
            seen_feature_ids.add(feature_id)
    return out


def _repair_verdicts_for_audit(
    spec: Spec, audit_result: AuditResult
) -> list[dict[str, Any]]:
    """Return feature verdict payloads that should be considered for repair.

    Feature audits are the primary signal. Product-wide quality findings are a
    secondary signal: if the product verdict is still non-PASS and no feature
    audit is actionable, route those quality findings to the most relevant
    Features so Layer 2 can improve product quality instead of ending as an
    opaque "25/25 stories passed but run failed" result.
    """
    verdicts = _feature_audits_to_verdicts(spec, audit_result)
    if (
        audit_result.verdict == AuditVerdict.PASSED
        or not audit_result.quality_findings
        or features_to_repair(spec, verdicts, max_attempts_per_run=1)
    ):
        return verdicts

    return [
        *verdicts,
        *_quality_findings_to_feature_verdicts(spec, audit_result.quality_findings),
    ]


def _quality_findings_to_feature_verdicts(
    spec: Spec, quality_findings: list[str]
) -> list[dict[str, Any]]:
    if not spec.features or not quality_findings:
        return []

    findings_by_feature: dict[str, list[str]] = {
        feature.id: [] for feature in spec.features
    }
    for finding in quality_findings:
        text = str(finding or "").strip()
        if not text:
            continue
        for feature_id in _feature_ids_for_quality_finding(spec, text):
            findings_by_feature.setdefault(feature_id, []).append(text)

    if not any(findings_by_feature.values()):
        for feature in _first_feature_per_group(spec):
            findings_by_feature.setdefault(feature.id, []).extend(
                str(f).strip() for f in quality_findings if str(f).strip()
            )

    verdicts: list[dict[str, Any]] = []
    for feature in spec.features:
        findings = findings_by_feature.get(feature.id) or []
        if not findings:
            continue
        detail = "Product-wide quality finding requires repair for this feature:\n" + "\n".join(
            f"- {finding}" for finding in findings[:5]
        )
        verdicts.append(
            {
                "feature_id": feature.id,
                "verdict": "partial",
                "detail": detail,
                "evidence_refs": [],
                "quality_findings": findings,
            }
        )
    return verdicts


def _feature_ids_for_quality_finding(spec: Spec, finding: str) -> list[str]:
    finding_tokens = _audit_match_tokens(finding)
    if not finding_tokens:
        return []

    scored: list[tuple[int, int, str]] = []
    for index, feature in enumerate(spec.features):
        feature_tokens = _audit_match_tokens(
            " ".join(
                [
                    feature.id,
                    feature.name,
                    feature.description,
                    feature.acceptance_detail,
                ]
            )
        )
        score = len(finding_tokens & feature_tokens)
        if score > 0:
            scored.append((score, index, feature.id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [feature_id for _score, _index, feature_id in scored]


def _first_feature_per_group(spec: Spec) -> list[Feature]:
    first_by_group: dict[str, Feature] = {}
    for feature in spec.features:
        if not feature.group_id:
            continue
        first_by_group.setdefault(feature.group_id, feature)
    return [
        first_by_group[group.id]
        for group in spec.groups
        if group.id in first_by_group
    ]


def _spec_with_group_feature_fallbacks(spec: Spec) -> Spec:
    """Return a repair-ready Spec when only Group.feature_ids exist.

    Compact specs can omit top-level Feature rows while still listing concrete
    user-visible work in each Group's ``feature_ids``. Build can use that
    shape, but Layer 2 repair routes by Feature. Synthesize in-memory Feature
    records here so repair does not silently skip compact specs.
    """
    if spec.features:
        return spec
    features: list[Feature] = []
    seen: set[str] = set()
    for group in spec.groups:
        for raw_feature_id in group.feature_ids or []:
            feature_id = str(raw_feature_id or "").strip()
            if not feature_id or feature_id in seen:
                continue
            features.append(
                Feature(
                    id=feature_id,
                    name=feature_id,
                    description=feature_id,
                    group_id=group.id,
                )
            )
            seen.add(feature_id)
    if not features:
        return spec
    return replace(spec, features=features)


def _feature_verdict_payload(feature_id: str, feature_audit: Any) -> dict[str, Any]:
    return {
        "feature_id": feature_id,
        "verdict": feature_audit.status,
        "detail": feature_audit.detail,
        "evidence_refs": list(feature_audit.evidence_refs),
    }


def _feature_ids_for_group_audit(spec: Spec, feature_audit: Any) -> list[str]:
    key_values = {
        str(feature_audit.feature_id or "").strip(),
        str(feature_audit.name or "").strip(),
    }
    groups = [
        group
        for group in spec.groups
        if group.id in key_values or group.name in key_values
    ]
    if not groups:
        return []

    group_feature_ids = set()
    for group in groups:
        group_feature_ids.update(group.feature_ids)
        group_feature_ids.update(
            feature.id for feature in spec.features if feature.group_id == group.id
        )
    candidates = [
        feature for feature in spec.features if feature.id in group_feature_ids
    ]
    if not candidates:
        return []

    best = _best_matching_features_for_audit(candidates, feature_audit)
    return [feature.id for feature in best or candidates]


def _best_matching_features_for_audit(
    features: list[Any], feature_audit: Any
) -> list[Any]:
    audit_tokens = _audit_match_tokens(str(feature_audit.detail or ""))
    if not audit_tokens:
        audit_tokens = _audit_match_tokens(
            " ".join(
                [
                    str(feature_audit.name or ""),
                    str(feature_audit.feature_id or ""),
                ]
            )
        )
    if not audit_tokens:
        return []

    scored: list[tuple[int, Any]] = []
    for feature in features:
        feature_tokens = _audit_match_tokens(
            " ".join(
                [
                    feature.id,
                    feature.name,
                    feature.description,
                    feature.acceptance_detail,
                ]
            )
        )
        score = len(audit_tokens & feature_tokens)
        if score > 0:
            scored.append((score, feature))
    if not scored:
        return []

    # Group-level audits often aggregate several missing behaviors in one
    # sentence. Route every positively matched Feature, highest-signal first,
    # instead of picking only the top token match and leaving sibling failures
    # unrepaired.
    scored.sort(key=lambda item: item[0], reverse=True)
    return [feature for _score, feature in scored]


_AUDIT_MATCH_STOPWORDS = {
    "a",
    "all",
    "and",
    "app",
    "are",
    "as",
    "be",
    "but",
    "by",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "part",
    "the",
    "to",
    "with",
    "work",
    "works",
}


def _audit_match_tokens(value: str) -> set[str]:
    import re

    tokens = set(re.findall(r"[a-z0-9]+", value.casefold()))
    return {token for token in tokens if len(token) > 2 and token not in _AUDIT_MATCH_STOPWORDS}


def _make_layer2_fix_agent(
    *,
    fix_agent: BuildAgentCallable,
    spec: Spec,
    project_dir: Path,
    session_dir: Path,
    base_url: str | None,
    config: dict[str, Any] | None = None,
    shared_budget: BuildBudget | None = None,
):
    """Adapt a ``BuildAgentCallable`` to the ``FixAgentCallable`` contract.

    ``BuildAgentCallable`` is the (BuildAgentInput → BuildAgentOutput)
    protocol used by the build/merge phases; ``FixAgentCallable`` is the
    Layer 2 contract: ``(FailingFeature, Group) → RepairAttempt``.

    The bridge:

    1. Constructs a ``BuildAgentInput`` whose ``feature_id`` is the
       failing feature's id — this triggers the "FIX ONLY THIS FEATURE"
       preamble in ``otto.build._build_agent_prompt``.
    2. Threads the audit's failure detail through
       ``last_failure_narrative`` so the agent knows WHY the feature
       failed audit, not just THAT it failed.
    3. Awaits the build agent and translates ``BuildAgentOutput`` into
       a ``RepairAttempt``.

    Exceptions raised by the build agent are caught here too — Layer 2
    is best-effort and a single agent crash should NOT take down the
    whole repair loop. The crash is recorded as a failed RepairAttempt
    so the proof packet shows "Layer 2 tried, failed" honestly.

    The worktree is the project_dir itself: brownfield repairs run
    against the integrated tree (greenfield Layer 2 today also operates
    on the merged main branch produced by the merge queue). The branch
    name is left empty — Layer 2 does not own a dedicated branch yet.
    """
    _ = (session_dir, base_url)  # reserved for future per-feature log routing
    session_by_feature: dict[str, str] = {}

    async def bridge(failing: FailingFeature, group: Group) -> RepairAttempt:
        if shared_budget is not None and shared_budget.remaining_total_cost_usd() <= 0:
            return RepairAttempt(
                feature_id=failing.feature_id,
                group_id=group.id,
                attempt_number=1,
                succeeded=False,
                new_verdict=None,
                detail="layer 2 repair skipped: shared cost budget exhausted",
                cost_usd=0.0,
                wall_s=0.0,
            )
        agent_input = BuildAgentInput(
            spec=spec,
            group=group,
            project_dir=project_dir,
            worktree=project_dir,
            branch="",
            attempt=1,
            last_failure_narrative=failing.detail,
            log_dir=None,
            feature_id=failing.feature_id,
            agent_session_id=session_by_feature.get(failing.feature_id, ""),
            config=dict(config or {}),
        )
        t0 = time.monotonic()
        try:
            output = await fix_agent(agent_input)
        except Exception as exc:  # noqa: BLE001 — Layer 2 must never crash the run
            return RepairAttempt(
                feature_id=failing.feature_id,
                group_id=group.id,
                attempt_number=1,
                succeeded=False,
                new_verdict=None,
                detail=(
                    f"layer 2 fix_agent crashed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                cost_usd=0.0,
                wall_s=time.monotonic() - t0,
            )
        if shared_budget is not None:
            shared_budget.charge_cost(float(output.cost_usd or 0.0))
        if output.session_id:
            session_by_feature[failing.feature_id] = output.session_id
        succeeded = bool(output.succeeded)
        detail = output.detail
        if succeeded:
            try:
                from otto.build import _commit_group_work, _is_git_repo

                if _is_git_repo(project_dir):
                    committed = _commit_group_work(
                        project_dir,
                        group_id=group.id,
                        branch=f"layer2/{_branch_slug(failing.feature_id)}",
                    )
                    if not committed:
                        succeeded = False
                        detail = (
                            (detail + "\n\n") if detail else ""
                        ) + "layer 2 repair failed to commit integrated changes"
            except Exception as exc:  # noqa: BLE001
                succeeded = False
                detail = (
                    (detail + "\n\n") if detail else ""
                ) + f"layer 2 repair commit failed: {type(exc).__name__}: {exc}"
        return RepairAttempt(
            feature_id=failing.feature_id,
            group_id=group.id,
            attempt_number=1,
            succeeded=succeeded,
            new_verdict=None,
            detail=detail,
            cost_usd=float(output.cost_usd or 0.0),
            wall_s=float(output.wall_s or 0.0),
        )

    return bridge


def _branch_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(value or "")).strip("-./")
    return slug[:80] or "repair"


def _audit_result_from_resume_plan(plan: ResumePlan) -> AuditResult:
    """Reconstruct a minimal AuditResult from a ResumePlan when the
    prior run already finished its audit phase.

    We don't re-hydrate slice / feature verdicts here — the proof packet
    renderer reads those off the run state journal and the prior run's
    persisted artifacts. The synthesised result carries enough for
    ``RunResult.verdict`` and the cost-accounting summary to be honest:
    the verdict that was written, plus a narrative pointing the operator
    at the prior session's audit artifacts.
    """
    verdict_str = plan.audit_verdict.lower().strip()
    try:
        verdict = AuditVerdict(verdict_str)
    except ValueError:
        # Unknown / malformed verdict on the journal — treat as BLOCKED
        # so the run halts honestly rather than masquerading as PASSED.
        verdict = AuditVerdict.BLOCKED
    return AuditResult(
        verdict=verdict,
        narrative=(
            f"Audit phase short-circuited on resume: prior session "
            f"{plan.session_id} already produced verdict={verdict.value}. "
            "See the prior session's audit artifacts for evidence."
        ),
    )


async def _wait_for_review_approval(
    *,
    session_dir: Path,
    timeout_s: float,
    poll_s: float,
    announce: "Callable[[str], None] | None",
) -> bool:
    """Block until a ``spec.review_approved`` event lands on the journal,
    or until ``timeout_s`` elapses.

    Implementation note (A13): this is a journal-event poll, NOT an
    inotify / queue watcher. The journal is append-only and small (one
    line per event), so re-scanning the whole file each tick is cheap
    even at 1Hz; the upper bound is ``timeout_s`` (default 24h).

    Returns:
        ``True`` on approval, ``False`` on timeout. Caller is responsible
        for halting the run honestly on ``False``.

    Side effects:
        Emits one ``spec.review_pending`` event when the wait starts so
        Mission Control / log readers can see the gate is active. Calls
        ``announce(message)`` once with the operator-facing instructions
        before entering the polling loop.
    """
    try:
        emit(session_dir, "spec.review_pending", detail="awaiting approval")
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning("emit spec.review_pending failed: %s", exc)
    if announce is not None:
        try:
            announce(session_dir.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("review-gate announce raised: %s", exc)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    sleep_s = max(0.05, float(poll_s))
    while True:
        if _has_review_approved_event(session_dir):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(sleep_s)


def _has_review_approved_event(session_dir: Path) -> bool:
    """True when the journal contains at least one ``spec.review_approved``."""
    try:
        for event in iter_events(session_dir):
            if event.kind == "spec.review_approved":
                return True
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning("review-gate journal read failed: %s", exc)
        return False
    return False


__all__ = [
    "RunResult",
    "run_pipeline",
    "_wait_for_review_approval",
    "_has_review_approved_event",
]
