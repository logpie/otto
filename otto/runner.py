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

* ``run_audit`` already runs an internal slice-level repair loop when
  ``fix_agent`` is provided. ``repair_failing_features`` is the
  research §4 Layer 2 retry — Feature-level, distinct from run_audit's
  inner loop. The runner invokes Layer 2 only when ``run_audit``
  returns a non-PASS verdict and a ``fix_agent`` is wired. Without
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

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    repair_failing_features,
)
from otto.build import (
    BuildAgentCallable,
    BuildAgentInput,
    BuildBudget,
    BuildResult,
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
from otto.spec_compile import Group, Spec, compile_spec
from otto.spec_state import emit

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
    base_branch: str = "main",
    spec: Spec | None = None,
    audit_budget: AuditBudget | None = None,
    on_phase: "Callable[[str], None] | None" = None,
    resume_plan: ResumePlan | None = None,
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
        base_branch: Integration branch for build/merge (default ``main``).
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
    # config is required only when we actually need to compile.
    if spec is None and config is None:
        raise ValueError(
            "run_pipeline: config must be provided when spec is None "
            "(needed by compile_spec)."
        )

    run_t0 = time.monotonic()

    def _phase(name: str) -> None:
        if on_phase is not None:
            try:
                on_phase(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_phase(%r) callback raised: %s", name, exc)

    try:
        emit(session_dir, "audit.started", detail="run start")
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning("emit run start failed: %s", exc)

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
        build_result = await run_build(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_agent=build_agent,
            base_url=base_url,
            budget=shared_budget,
            base_branch=base_branch,
            skip_components=skip_components,
        )
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
            budget=MergeBudget(),
            shared_budget=shared_budget,
        )
    result.merge_result = merge_result

    # ---- 5. Audit ----
    _phase("audit")
    walk = walkthrough or default_walkthrough_from_spec(spec)
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
            base_url=base_url,
            walkthrough=walk,
            fix_agent=fix_agent,
            budget=audit_budget or AuditBudget(),
            shared_budget=shared_budget,
            base_branch=base_branch,
        )
    result.audit_result = audit_result

    # ---- 6. Layer 2 repair (research §4 retry layers) ----
    # Triggered only on non-PASS verdicts and only when a fix_agent is
    # available. ``run_audit`` already does its own slice-level repair;
    # this is the FEATURE-level Layer 2 loop.
    if (
        audit_result.verdict != AuditVerdict.PASSED
        and fix_agent is not None
        and spec.features
    ):
        feature_verdicts = _feature_audits_to_verdicts(spec, audit_result)
        if feature_verdicts:
            _phase("repair")
            bridge = _make_layer2_fix_agent(
                fix_agent=fix_agent,
                spec=spec,
                project_dir=project_dir,
                session_dir=session_dir,
                base_url=base_url,
            )
            try:
                repair_result = await repair_failing_features(
                    spec=spec,
                    feature_verdicts=feature_verdicts,
                    fix_agent=bridge,
                    re_audit=None,  # re-audit is the audit module's responsibility
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

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feature_audits_to_verdicts(
    spec: Spec, audit_result: AuditResult
) -> list[dict[str, Any]]:
    """Project ``audit_result.feature_audits`` onto Feature ids.

    Mirrors the projection in ``otto/render.py:compose_proof_packet`` so
    the Layer 2 repair loop sees the same view as the proof packet.
    Features without an audit entry are omitted (no verdict to feed).
    """
    if not spec.features:
        return []
    audits_by_key: dict[str, Any] = {}
    for fa in audit_result.feature_audits:
        audits_by_key[fa.name] = fa
    out: list[dict[str, Any]] = []
    for feature in spec.features:
        fa = audits_by_key.get(feature.name) or audits_by_key.get(feature.id)
        if fa is None:
            continue
        out.append(
            {
                "feature_id": feature.id,
                "verdict": fa.status,
                "detail": fa.detail,
                "evidence_refs": list(fa.evidence_refs),
            }
        )
    return out


def _make_layer2_fix_agent(
    *,
    fix_agent: BuildAgentCallable,
    spec: Spec,
    project_dir: Path,
    session_dir: Path,
    base_url: str | None,
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

    async def bridge(failing: FailingFeature, group: Group) -> RepairAttempt:
        agent_input = BuildAgentInput(
            spec=spec,
            slice=group,
            project_dir=project_dir,
            worktree=project_dir,
            branch="",
            attempt=1,
            last_failure_narrative=failing.detail,
            log_dir=None,
            feature_id=failing.feature_id,
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
        return RepairAttempt(
            feature_id=failing.feature_id,
            group_id=group.id,
            attempt_number=1,
            succeeded=bool(output.succeeded),
            new_verdict=None,
            detail=output.detail,
            cost_usd=float(output.cost_usd or 0.0),
            wall_s=float(output.wall_s or 0.0),
        )

    return bridge


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


__all__ = [
    "RunResult",
    "run_pipeline",
]
