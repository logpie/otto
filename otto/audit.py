"""Audit — Step 6 of the unified intent-to-product pipeline.

The audit is one LLM pass at the end of a run, against the integrated
product. It is the final verification gate before render, distinct from
per-slice deterministic checks because:

* **Scope**: integrated product, not per-slice.
* **Method**: end-to-end user journeys, cross-slice navigation; can
  invoke a "walkthrough" subprocess (Playwright runner, etc.) to capture
  screenshots and video.
* **Output**: per-slice verdict, narrative report, artifact paths
  (screenshots, video, raw transcripts) for the proof packet.
* **Role**: produces the human-trustable evidence. Deterministic checks
  proved correctness; the audit produces what a human can scan.

Codex-i2p's `otto/certifier/__init__.py` had a multi-mode dispatch
(fast / standard / thorough / target / hillclimb). For v1 we keep ONE
mode: the thorough end-of-run audit. The legacy package stays put for
the old `otto build` / `otto certify` paths during Phase A coexistence.

If the audit's verdict is `partial` or `blocked` and `audit_agent` is
provided, the audit can route findings to the fix loop: the relevant
slice's build agent re-engages, and the audit re-runs (bounded by
`AuditBudget.audit_retries`).

For testability, the LLM judge is abstracted via `AuditAgentCallable`.
A trivial `default_audit_agent` implementation is provided that
delegates to `otto.agent.run_agent_with_timeout`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from otto.build import (
    BuildAgentCallable,
    BuildAgentInput,
    BuildResult,
    SliceStatus,
)
from otto.checks import Evidence, run_checks
from otto.merge_queue import MergeQueueResult
from otto.spec_compile import Spec
from otto.spec_state import emit

logger = logging.getLogger("otto.audit")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class AuditVerdict(str, Enum):
    PASSED = "passed"
    PARTIAL = "partial"
    BLOCKED = "blocked"


@dataclass
class SliceVerdict:
    """Per-slice judgment from the audit."""

    slice_id: str
    passed: bool
    detail: str = ""
    artifacts: list[Path] = field(default_factory=list)


@dataclass
class AuditResult:
    """Aggregate audit outcome."""

    verdict: AuditVerdict
    narrative: str
    slice_verdicts: list[SliceVerdict] = field(default_factory=list)
    cross_slice_evidence: list[Evidence] = field(default_factory=list)
    walkthrough_artifacts: list[Path] = field(default_factory=list)
    contract_test_passed: bool | None = None  # None if no test_command configured
    contract_test_detail: str = ""
    retries: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0


@dataclass
class AuditBudget:
    """Bounds for the audit phase."""

    audit_retries: int = 2
    walk_timeout_s: int = 600  # walkthrough subprocess wall budget
    judge_timeout_s: int = 300  # LLM judge wall budget


# ---------------------------------------------------------------------------
# Audit agent abstraction (mockable LLM judge)
# ---------------------------------------------------------------------------


@dataclass
class AuditAgentInput:
    """Input passed to the audit-agent callable for one judging pass."""

    spec: Spec
    project_dir: Path
    integrated_worktree: Path
    build_summary: dict
    merge_summary: dict
    cross_slice_evidence: list[Evidence]
    walkthrough_artifacts: list[Path]
    log_dir: Path | None = None


@dataclass
class AuditAgentOutput:
    """What an audit-agent callable returns."""

    verdict: AuditVerdict
    narrative: str
    slice_verdicts: list[SliceVerdict] = field(default_factory=list)
    cost_usd: float = 0.0
    wall_s: float = 0.0


class AuditAgentCallable(Protocol):
    async def __call__(self, agent_input: AuditAgentInput) -> AuditAgentOutput:
        ...


# ---------------------------------------------------------------------------
# Walkthrough hook — abstracted subprocess invocation
# ---------------------------------------------------------------------------


@dataclass
class WalkthroughResult:
    """Output of a walkthrough subprocess + glob."""

    succeeded: bool
    detail: str
    artifacts: list[Path] = field(default_factory=list)


WalkthroughCallable = Callable[[Path, Path, int], WalkthroughResult]
"""(project_dir, log_dir, timeout_s) → WalkthroughResult.

Invokes a project-defined walkthrough (Playwright runner, Cypress, etc.)
that drives the integrated product end-to-end and produces evidence
artifacts. Tests pass a stub; production wires this to a configurable
project command.
"""


def no_op_walkthrough(_project_dir: Path, _log_dir: Path, _timeout_s: int) -> WalkthroughResult:
    """Default walkthrough: no-op. Production projects override."""
    return WalkthroughResult(succeeded=True, detail="no walkthrough configured", artifacts=[])


# ---------------------------------------------------------------------------
# The audit driver
# ---------------------------------------------------------------------------


async def run_audit(
    spec: Spec,
    *,
    project_dir: Path,
    session_dir: Path,
    build_result: BuildResult,
    merge_result: MergeQueueResult,
    audit_agent: AuditAgentCallable,
    base_url: str | None = None,
    walkthrough: WalkthroughCallable | None = None,
    fix_agent: BuildAgentCallable | None = None,
    budget: AuditBudget | None = None,
) -> AuditResult:
    """Run the end-of-run audit.

    Steps per attempt:
        1. Run the cross-slice checks against the integrated worktree
           (final independent verification).
        2. Invoke the walkthrough hook to capture screenshots/video.
        3. Invoke the audit agent (LLM judge) with everything assembled.
        4. If verdict != PASSED and `fix_agent` provided, route findings
           to the fix loop: re-engage the build agent for each slice
           with a failing verdict, then loop back to step 1.
        5. Bounded by `budget.audit_retries`; if exceeded, return the
           latest result with verdict PARTIAL or BLOCKED.

    Args:
        spec: The approved Spec.
        project_dir: Project root.
        session_dir: Session dir (state journal lives here).
        build_result: Output of run_build.
        merge_result: Output of run_merge_queue.
        audit_agent: LLM judge callable.
        base_url: Optional base URL for HTTP-based cross-slice checks.
        walkthrough: Optional walkthrough subprocess hook (default: no-op).
        fix_agent: Optional build-agent callable for repair on partial /
            blocked verdicts. If None, audit returns the LLM verdict
            without further repair.
        budget: Audit phase bounds.

    Returns:
        AuditResult with verdict + narrative + slice verdicts +
        cross-slice evidence + walkthrough artifacts.
    """
    budget = budget or AuditBudget()
    walk = walkthrough or no_op_walkthrough
    t0 = time.monotonic()
    cost_total = 0.0
    retries = 0
    last_result: AuditResult | None = None

    emit(session_dir, "audit.started")

    while retries <= budget.audit_retries:
        retries_this_pass = retries
        # 1: cross-slice checks against integrated worktree
        cross_pairs = run_checks(
            list(spec.cross_slice_checks),
            project_dir=project_dir,
            cwd=project_dir,
            base_url=base_url,
            raw_log_dir=session_dir / "audit" / f"attempt-{retries:02d}" / "cross-slice",
        )
        cross_evidence = [ev for _check, ev in cross_pairs]

        # 1b: project contract — if otto.yaml declares a `test_command`,
        # run it as a deterministic gate the audit can't argue with.
        # This is the "does the integrated product actually satisfy the
        # contract the project declared" check, distinct from per-slice
        # tests the build agents wrote themselves.
        contract_passed, contract_detail = _run_project_contract_test(
            project_dir, log_dir=session_dir / "audit" / f"attempt-{retries:02d}" / "contract"
        )

        # 2: walkthrough subprocess
        walk_log_dir = session_dir / "audit" / f"attempt-{retries:02d}" / "walkthrough"
        walk_log_dir.mkdir(parents=True, exist_ok=True)
        walk_result = walk(project_dir, walk_log_dir, budget.walk_timeout_s)

        # 3: LLM judge
        agent_input = AuditAgentInput(
            spec=spec,
            project_dir=project_dir,
            integrated_worktree=project_dir,
            build_summary=_build_summary(build_result),
            merge_summary=_merge_summary(merge_result),
            cross_slice_evidence=cross_evidence,
            walkthrough_artifacts=list(walk_result.artifacts),
            log_dir=session_dir / "audit" / f"attempt-{retries:02d}" / "judge",
        )
        agent_output = await audit_agent(agent_input)
        cost_total += agent_output.cost_usd

        # The contract test result OVERRIDES the LLM verdict when a
        # test_command exists. If the contract test fails, the verdict is
        # at most PARTIAL (cannot be PASSED), regardless of what the
        # walkthrough agent thinks.
        verdict = agent_output.verdict
        narrative = agent_output.narrative
        if contract_passed is False:
            if verdict == AuditVerdict.PASSED:
                verdict = AuditVerdict.PARTIAL
            narrative = (
                f"{narrative}\n\n"
                f"[contract test FAILED]\n{contract_detail}"
            ).strip()

        # v2.2 defense D3: review the amendment chain. Broken chain or
        # spec-mutated-outside-chain → BLOCKED. Suspicious patterns
        # (missing trigger events, concentrated edits, 5+ amendments)
        # cap at PARTIAL. See docs/intent-to-product-v2.md "Safe
        # mutability" and otto/spec_amend.py.
        from otto.spec_amend import verify_amendment_chain

        chain_review = verify_amendment_chain(spec, session_dir=session_dir)
        if chain_review.verdict_cap == "blocked":
            verdict = AuditVerdict.BLOCKED
        elif chain_review.verdict_cap == "partial" and verdict == AuditVerdict.PASSED:
            verdict = AuditVerdict.PARTIAL
        if chain_review.findings:
            narrative = (
                f"{narrative}\n\n"
                f"[amendment chain review: {chain_review.verdict_cap}]\n"
                + "\n".join(f"  - {f}" for f in chain_review.findings)
            ).strip()

        last_result = AuditResult(
            verdict=verdict,
            narrative=narrative,
            slice_verdicts=list(agent_output.slice_verdicts),
            cross_slice_evidence=cross_evidence,
            walkthrough_artifacts=list(walk_result.artifacts),
            contract_test_passed=contract_passed,
            contract_test_detail=contract_detail,
            retries=retries_this_pass,
            cost_usd=cost_total,
            wall_s=time.monotonic() - t0,
        )

        # If passed or no repair available, return.
        if verdict == AuditVerdict.PASSED:
            emit(
                session_dir,
                "audit.finished",
                detail=narrative[:200],
                verdict=verdict.value,
            )
            return last_result
        if fix_agent is None or retries >= budget.audit_retries:
            emit(
                session_dir,
                "audit.finished",
                detail=narrative[:200],
                verdict=verdict.value,
            )
            return last_result

        # 4: route findings to fix loop. For each slice with a failing
        # verdict, re-engage the build agent ONCE per audit cycle.
        failing_ids = [v.slice_id for v in agent_output.slice_verdicts if not v.passed]
        if not failing_ids:
            # Verdict says partial/blocked but no specific slice flagged
            # — nothing actionable. Return as-is.
            emit(
                session_dir,
                "audit.finished",
                detail=agent_output.narrative[:200],
                verdict=agent_output.verdict.value,
            )
            return last_result

        for slice_id in failing_ids:
            slice_obj = next((s for s in spec.slices if s.id == slice_id), None)
            if slice_obj is None:
                continue
            # Find the slice's build branch from build_result.
            sresult = next(
                (r for r in build_result.slice_results if r.slice_id == slice_id),
                None,
            )
            branch = sresult.branch if sresult else f"i2p/{session_dir.name}/{slice_id}"
            worktree = sresult.worktree if sresult else project_dir
            agent_input_fix = BuildAgentInput(
                spec=spec,
                slice=slice_obj,
                project_dir=project_dir,
                worktree=worktree,
                branch=branch,
                attempt=retries + 1,
                last_failure_narrative=(
                    f"audit attempt {retries + 1} flagged slice "
                    f"{slice_id}: {next((v.detail for v in agent_output.slice_verdicts if v.slice_id == slice_id), '')}"
                ),
                log_dir=session_dir / "audit" / f"attempt-{retries:02d}" / "fix" / slice_id,
            )
            try:
                fix_output = await fix_agent(agent_input_fix)
                cost_total += fix_output.cost_usd
                emit(
                    session_dir,
                    "slice.attempt.failed" if not fix_output.succeeded else "slice.merge.eligible",
                    slice_id=slice_id,
                    attempt=retries + 1,
                    detail=fix_output.detail or "",
                )
            except Exception as exc:
                emit(
                    session_dir,
                    "slice.attempt.failed",
                    slice_id=slice_id,
                    attempt=retries + 1,
                    detail=f"audit-routed fix crashed: {type(exc).__name__}: {exc}",
                )
        retries += 1

    # Out of retries; return the latest result we have.
    if last_result is None:
        last_result = AuditResult(
            verdict=AuditVerdict.BLOCKED,
            narrative="audit produced no result",
            retries=retries,
            cost_usd=cost_total,
            wall_s=time.monotonic() - t0,
        )
    emit(
        session_dir,
        "audit.finished",
        detail=last_result.narrative[:200],
        verdict=last_result.verdict.value,
    )
    return last_result


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def _run_project_contract_test(
    project_dir: Path, *, log_dir: Path | None = None
) -> tuple[bool | None, str]:
    """Run the project's `test_command` from otto.yaml as the contract gate.

    Returns (passed, detail):
      * passed=True   — test_command exited 0
      * passed=False  — test_command exited non-zero
      * passed=None   — no test_command configured; gate is no-op

    The audit's LLM walkthrough can be fooled by an agent's own self-tests
    that don't match the project's contract. The test_command IS the
    contract; running it deterministically prevents drift between what the
    LLM sees and what a downstream consumer sees.
    """
    import shlex
    import subprocess as _sp

    try:
        from otto.config import load_config
        config = load_config(project_dir / "otto.yaml")
    except Exception as exc:
        return None, f"otto.yaml unreadable: {exc}"
    test_command = str(config.get("test_command") or "").strip()
    if not test_command:
        return None, "no test_command configured in otto.yaml"

    # Use the same PATH+venv augmentation as checks.py.
    from otto.checks import _subprocess_env

    try:
        argv = shlex.split(test_command)
    except ValueError as exc:
        return False, f"test_command shlex error: {exc}"
    if not argv:
        return None, "test_command parsed to empty argv"

    env = _subprocess_env(extra_pythonpath=[project_dir])
    try:
        completed = _sp.run(
            argv,
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except _sp.TimeoutExpired:
        return False, f"test_command timed out: {test_command}"
    except Exception as exc:  # noqa: BLE001 — surface any subprocess failure
        return False, f"test_command launch failed: {type(exc).__name__}: {exc}"

    output = (
        f"$ {test_command}\nexit_code={completed.returncode}\n\n"
        f"STDOUT:\n{completed.stdout or ''}\n\nSTDERR:\n{completed.stderr or ''}"
    )
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "test_command.log").write_text(output, encoding="utf-8")
        except OSError:
            pass

    detail = (
        f"test_command={test_command!r} exit={completed.returncode}; "
        + ((completed.stdout or "")[-400:].strip() or "(no stdout)")
    )
    return completed.returncode == 0, detail


def _build_summary(build_result: BuildResult) -> dict:
    return {
        "all_passing": build_result.all_passing,
        "passing_ids": list(build_result.passing_ids),
        "blocked_ids": list(build_result.blocked_ids),
        "total_cost_usd": build_result.total_cost_usd,
        "total_wall_s": build_result.total_wall_s,
        "slice_count": len(build_result.slice_results),
        "per_slice": [
            {
                "slice_id": r.slice_id,
                "status": r.status.value,
                "attempts": r.attempts,
                "wall_s": r.wall_s,
                "cost_usd": r.cost_usd,
                "narrative": r.failure_narrative,
            }
            for r in build_result.slice_results
        ],
    }


def _merge_summary(merge_result: MergeQueueResult) -> dict:
    return {
        "landed_ids": list(merge_result.landed_ids),
        "blocked_ids": list(merge_result.blocked_ids),
        "total_cost_usd": merge_result.total_cost_usd,
        "total_wall_s": merge_result.total_wall_s,
        "per_slice": [
            {
                "slice_id": r.slice_id,
                "status": r.status.value,
                "landed_commit": r.landed_commit,
                "repair_attempts": r.repair_attempts,
                "wall_s": r.wall_s,
                "cost_usd": r.cost_usd,
                "narrative": r.failure_narrative,
            }
            for r in merge_result.results
        ],
    }


# ---------------------------------------------------------------------------
# Default audit agent — abstract LLM call
# ---------------------------------------------------------------------------


def _audit_prompt(agent_input: AuditAgentInput) -> str:
    """Compose the audit-agent prompt.

    Walks: spec → integrated worktree state → build summary → merge
    summary → cross-slice check verdicts → walkthrough artifacts → ask
    for a per-slice verdict + a narrative.
    """
    import json as _json

    spec = agent_input.spec
    lines: list[str] = []
    lines.append("# Final audit pass")
    lines.append("")
    lines.append(
        f"You are the audit agent. Your job is to judge whether the "
        f"integrated product satisfies the user's intent: {spec.intent!r}."
    )
    lines.append(f"Project kind: {spec.project_kind}")
    lines.append(f"Integrated worktree: {agent_input.integrated_worktree}")
    lines.append("")
    lines.append("## Spec slices")
    for s in spec.slices:
        lines.append(f"- {s.id}: {s.title}")
    lines.append("")
    lines.append("## Build summary")
    lines.append("```json")
    lines.append(_json.dumps(agent_input.build_summary, indent=2, default=str))
    lines.append("```")
    lines.append("## Merge summary")
    lines.append("```json")
    lines.append(_json.dumps(agent_input.merge_summary, indent=2, default=str))
    lines.append("```")
    lines.append("## Cross-slice deterministic check evidence")
    for ev in agent_input.cross_slice_evidence:
        lines.append(f"- {'PASS' if ev.passed else 'FAIL'} — {ev.detail}")
    lines.append("")
    if agent_input.walkthrough_artifacts:
        lines.append("## Walkthrough artifacts (paths)")
        for p in agent_input.walkthrough_artifacts:
            lines.append(f"- {p}")
        lines.append("")
    lines.append("## Your task")
    lines.append("")
    lines.append(
        "Inspect the integrated worktree (you may read files), review the "
        "evidence, and output:"
    )
    lines.append(
        "  1. A short narrative of what works and what doesn't."
    )
    lines.append(
        "  2. A per-slice verdict: for each slice id, pass or fail with reason."
    )
    lines.append("  3. A final verdict: 'passed', 'partial', or 'blocked'.")
    lines.append("")
    lines.append(
        "Output as a single fenced JSON block with keys: "
        "{ verdict: passed|partial|blocked, narrative: str, "
        "slice_verdicts: [{slice_id, passed: bool, detail: str}, ...] }."
    )
    return "\n".join(lines)


def _parse_audit_output(text: str) -> AuditAgentOutput:
    """Parse the audit agent's JSON-fenced response."""
    import json as _json
    import re as _re

    match = _re.search(r"```json\s*(\{.*?\})\s*```", text, flags=_re.DOTALL)
    raw = match.group(1) if match else text.strip()
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return AuditAgentOutput(
            verdict=AuditVerdict.BLOCKED,
            narrative=f"audit agent returned non-JSON output: {text[:200]}",
        )
    verdict_str = str(data.get("verdict") or "blocked").lower()
    verdict = (
        AuditVerdict.PASSED if verdict_str == "passed"
        else AuditVerdict.PARTIAL if verdict_str == "partial"
        else AuditVerdict.BLOCKED
    )
    slice_verdicts = []
    for entry in data.get("slice_verdicts") or []:
        if not isinstance(entry, dict):
            continue
        slice_verdicts.append(
            SliceVerdict(
                slice_id=str(entry.get("slice_id") or ""),
                passed=bool(entry.get("passed")),
                detail=str(entry.get("detail") or ""),
            )
        )
    return AuditAgentOutput(
        verdict=verdict,
        narrative=str(data.get("narrative") or ""),
        slice_verdicts=slice_verdicts,
    )


async def default_audit_agent(agent_input: AuditAgentInput) -> AuditAgentOutput:
    """Default LLM-driven audit agent.

    Uses `make_agent_options(agent_type="certifier")` to inherit
    provider credentials and otto.yaml agent configuration. Constructing
    AgentOptions manually skips that auth setup.
    """
    from otto.agent import AgentCallError, make_agent_options, run_agent_with_timeout
    from otto.config import load_config

    prompt = _audit_prompt(agent_input)
    log_dir = agent_input.log_dir or (agent_input.integrated_worktree / "_otto_audit_logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    config_path = agent_input.project_dir / "otto.yaml"
    try:
        config = load_config(config_path)
    except Exception:
        config = {}
    options = make_agent_options(
        agent_input.project_dir, config, agent_type="certifier"
    )
    options.cwd = str(agent_input.integrated_worktree)
    options.permission_mode = "bypassPermissions"  # audit reads, doesn't edit

    t0 = time.monotonic()
    try:
        text, cost, _session_id, _breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=log_dir,
            phase_name="AUDIT",
            phase_label="audit",
            timeout=None,
            project_dir=agent_input.project_dir,
        )
        parsed = _parse_audit_output(text)
        parsed.cost_usd = cost or 0.0
        parsed.wall_s = time.monotonic() - t0
        return parsed
    except AgentCallError as exc:
        return AuditAgentOutput(
            verdict=AuditVerdict.BLOCKED,
            narrative=f"audit agent crashed: {exc}",
            wall_s=time.monotonic() - t0,
        )


# Suppress unused-import warning — these are part of the public flow.
_ = (Iterable, SliceStatus)


__all__ = [
    "AuditAgentCallable",
    "AuditAgentInput",
    "AuditAgentOutput",
    "AuditBudget",
    "AuditResult",
    "AuditVerdict",
    "SliceVerdict",
    "WalkthroughCallable",
    "WalkthroughResult",
    "default_audit_agent",
    "no_op_walkthrough",
    "run_audit",
]
