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
from typing import Callable, Literal, Protocol

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
class CapabilityVerdict:
    """Per-capability judgment from the audit (v2.6).

    A capability is one user-observable feature, typically derived
    from a `done_means` item. Replaces the single-verdict-for-everything
    model with structured per-feature judgments — lets the proof
    packet show a "feature checklist" and lets downstream tooling
    target specific gaps.

    Status:
      - "passed": capability fully works.
      - "partial": works but with caveats (specific in `narrative`).
      - "blocked": doesn't work or unverified.

    `evidence_refs` are paths or URLs the audit consulted (rendered
    HTML, screenshots, contract-test logs) — empty list is allowed
    when the verdict is from code-reading alone, but paths anchor the
    judgment.
    """

    name: str  # short label (e.g. "user signup", "RSS discoverability")
    status: Literal["passed", "partial", "blocked"]
    detail: str = ""  # 1-2 sentence rationale
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class AuditResult:
    """Aggregate audit outcome."""

    verdict: AuditVerdict
    narrative: str
    slice_verdicts: list[SliceVerdict] = field(default_factory=list)
    capability_verdicts: list[CapabilityVerdict] = field(default_factory=list)
    cross_slice_evidence: list[Evidence] = field(default_factory=list)
    walkthrough_artifacts: list[Path] = field(default_factory=list)
    contract_test_passed: bool | None = None  # None if no test_command configured
    contract_test_detail: str = ""
    quality_score: int = 0  # 1-5; 0 = not assessed
    quality_findings: list[str] = field(default_factory=list)
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
    # v2.6 per-capability verdicts: one entry per done_means item
    # (or per derived feature). Lets the proof packet show a feature
    # checklist instead of one global verdict.
    capability_verdicts: list[CapabilityVerdict] = field(default_factory=list)
    # Quality dimension (added when "audit final app quality" check
    # surfaced that bare-bones-but-functional products were passing).
    # Score 1-5 where 1 = unusable, 3 = MVP, 5 = polished. Findings
    # are concrete UX/visual issues. Functional verdict and quality
    # are independent — a product can be functional but rate quality
    # 2 (passes contract test, but UX is rough). Quality < 3 caps the
    # final verdict at PARTIAL.
    quality_score: int = 0  # 0 = not assessed
    quality_findings: list[str] = field(default_factory=list)
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


def default_walkthrough_from_spec(spec: Spec) -> WalkthroughCallable:
    """Build a production walkthrough callable from the spec.

    Strategy: find a `BrowserJourney` check anywhere in the spec
    (cross_slice_checks first, then any slice's checks), run its
    command in `project_dir`, glob the configured `evidence_globs`,
    and return them as audit artifacts. The audit's walkthrough dir
    receives the raw stdout/stderr log; the artifacts themselves stay
    where the runner wrote them (typically `otto_artifacts/browser/`)
    so the proof packet can link directly.

    If no BrowserJourney is declared anywhere in the spec, falls back
    to no-op with a clear diagnostic so audit can still proceed but
    flags the absence in `detail`.

    This was the missing wiring that left `audit/<attempt>/walkthrough/`
    empty in v1 — the LLM judge was reading code without any
    interactive verification.
    """
    from otto.checks import run_check
    from otto.spec_compile import BrowserJourney

    journey: BrowserJourney | None = None
    for check in spec.cross_slice_checks:
        if isinstance(check, BrowserJourney):
            journey = check
            break
    if journey is None:
        for slice_ in spec.slices:
            for check in slice_.checks:
                if isinstance(check, BrowserJourney):
                    journey = check
                    break
            if journey is not None:
                break

    if journey is None:
        # No BrowserJourney declared. For webapp project_kind, synthesize
        # a minimal one — boot the app, hit `/`, screenshot, capture
        # console errors. Audit verdict should NEVER come from "LLM
        # read code" alone for webapps. For non-webapp kinds (cli,
        # library), legitimately no-op.
        project_kind = (spec.project_kind or "").lower()
        if project_kind == "webapp":
            return _synthesized_webapp_walkthrough()

        def _no_journey(_pd: Path, _ld: Path, _ts: int) -> WalkthroughResult:
            return WalkthroughResult(
                succeeded=True,
                detail=(
                    f"no BrowserJourney declared and project_kind={project_kind!r} "
                    "has no synthesized fallback; LLM judge reads code only"
                ),
                artifacts=[],
            )
        return _no_journey

    journey_check = journey  # capture for closure

    def _run_journey(project_dir: Path, log_dir: Path, _timeout_s: int) -> WalkthroughResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        evidence = run_check(
            journey_check,
            project_dir=project_dir,
            cwd=project_dir,
            raw_log_path=log_dir / "browser-journey.log",
        )
        return WalkthroughResult(
            succeeded=evidence.passed,
            detail=evidence.detail,
            artifacts=list(evidence.artifacts),
        )

    return _run_journey


def _synthesized_webapp_walkthrough() -> WalkthroughCallable:
    """Default walkthrough for webapp project_kind when none was declared.

    Boots the app via `create_app`, walks the home page using Flask's
    test_client (no Playwright dependency, no real server), captures
    response status/length, and saves the rendered HTML body as an
    audit artifact. This is a best-effort default — projects that
    don't expose `create_app()` (or aren't Flask-shaped) get a clear
    diagnostic instead of silent no-op.

    For richer interactive verification, projects should declare a
    BrowserJourney check in cross_slice_checks (Playwright runner,
    Cypress harness, etc.).
    """
    def _walk(project_dir: Path, log_dir: Path, _timeout_s: int) -> WalkthroughResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "synthesized-webapp.log"

        # Try to boot via create_app.
        import subprocess
        from otto.checks import _subprocess_env

        # Generalization: a "webapp" can be many shapes (Flask,
        # FastAPI, SSG that emits HTML, etc.). Try create_app first;
        # if not available, fall back to detecting that the project
        # produced static HTML output (output/ dir or similar). Either
        # way, missing infrastructure is "walkthrough not applicable",
        # not "audit failure".
        boot_script = (
            "import json, sys, traceback, os\n"
            "from pathlib import Path\n"
            "ROOT = Path(os.getcwd())\n"
            "result = {}\n"
            "# Attempt 1: Flask/FastAPI-style create_app.\n"
            "try:\n"
            "    from app import create_app  # type: ignore[import-not-found]\n"
            "    app = create_app({'TESTING': True})\n"
            "    client = app.test_client()\n"
            "    r = client.get('/')\n"
            "    body = r.get_data(as_text=True) or ''\n"
            "    result = {'shape': 'flask-create_app', 'status': r.status_code,\n"
            "              'body_len': len(body), 'body_preview': body[:500]}\n"
            "    with open('__audit_home_body__.html', 'w') as f:\n"
            "        f.write(body)\n"
            "    print(json.dumps(result))\n"
            "    sys.exit(0)\n"
            "except (ImportError, ModuleNotFoundError):\n"
            "    pass  # not Flask-shaped\n"
            "except Exception as exc:\n"
            "    # create_app exists but boot failed → real audit signal.\n"
            "    result = {'shape': 'flask-create_app', 'error': f'{type(exc).__name__}: {exc}',\n"
            "              'traceback': traceback.format_exc()}\n"
            "    print(json.dumps(result))\n"
            "    sys.exit(2)\n"
            "# Attempt 2: static-site or CLI shape — look for produced output.\n"
            "for candidate in ('output/index.html', 'dist/index.html', 'build/index.html', 'site/index.html'):\n"
            "    p = ROOT / candidate\n"
            "    if p.is_file():\n"
            "        body = p.read_text(encoding='utf-8', errors='replace')\n"
            "        result = {'shape': 'static-site', 'index_path': str(candidate),\n"
            "                  'body_len': len(body), 'body_preview': body[:500]}\n"
            "        (ROOT / '__audit_home_body__.html').write_text(body)\n"
            "        print(json.dumps(result))\n"
            "        sys.exit(0)\n"
            "# No webapp shape detected. Not a failure — declare not-applicable.\n"
            "print(json.dumps({'shape': 'not-applicable',\n"
            "                  'note': 'no Flask create_app and no static index.html; '\n"
            "                          'project may be a CLI/library/lib — walkthrough skipped'}))\n"
            "sys.exit(0)\n"
        )

        env = _subprocess_env(extra_pythonpath=[project_dir])
        # Use sys.executable for portability — `python` isn't on PATH
        # on macOS by default; `_subprocess_env` adds the interpreter
        # bin dir but the basename varies (python3 vs python). Direct
        # sys.executable bypasses the lookup entirely.
        import sys as _sys

        try:
            completed = subprocess.run(
                [_sys.executable, "-c", boot_script],
                cwd=project_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log_path.write_text("synthesized webapp walkthrough timed out after 60s\n")
            return WalkthroughResult(
                succeeded=False, detail="synthesized webapp walkthrough timed out", artifacts=[]
            )
        except FileNotFoundError as exc:
            log_path.write_text(f"python not found: {exc}\n")
            return WalkthroughResult(
                succeeded=False, detail=f"python interpreter not available: {exc}", artifacts=[]
            )

        log_text = (
            f"$ python -c <synthesized-webapp-walkthrough>\n"
            f"exit_code={completed.returncode}\n\n"
            f"STDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}"
        )
        log_path.write_text(log_text)

        artifacts: list[Path] = [log_path]
        body_path = project_dir / "__audit_home_body__.html"
        if body_path.exists():
            artifacts.append(body_path)

        if completed.returncode == 0:
            return WalkthroughResult(
                succeeded=True,
                detail=f"synthesized GET / succeeded ({completed.stdout.strip()[:200]})",
                artifacts=artifacts,
            )
        return WalkthroughResult(
            succeeded=False,
            detail=(
                f"synthesized webapp walkthrough failed "
                f"(exit={completed.returncode}); see {log_path.name}"
            ),
            artifacts=artifacts,
        )

    return _walk


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

        # Audit-final-quality cap: a functionally-correct but
        # bare-bones product (forms stacked vertically, no nav, raw
        # browser styling) shouldn't claim PASSED. Quality < 3 (worse
        # than MVP) caps at PARTIAL. The intent is to surface "looks
        # broken" UX without re-litigating "does the contract test
        # pass" — those are independent dimensions.
        if (
            agent_output.quality_score
            and agent_output.quality_score < 3
            and verdict == AuditVerdict.PASSED
        ):
            verdict = AuditVerdict.PARTIAL
            narrative = (
                f"{narrative}\n\n"
                f"[quality assessment: {agent_output.quality_score}/5]\n"
                + "\n".join(f"  - {f}" for f in agent_output.quality_findings)
            ).strip()

        # v2.6 capability cap: any blocked capability prevents PASSED.
        # >50% of capabilities partial OR mixed → cap at PARTIAL.
        # This makes the per-feature signal load-bearing: a product
        # where "RSS feed" is blocked can't pass even if every other
        # capability works.
        caps = agent_output.capability_verdicts
        if caps:
            blocked_count = sum(1 for c in caps if c.status == "blocked")
            partial_count = sum(1 for c in caps if c.status == "partial")
            if blocked_count and verdict == AuditVerdict.PASSED:
                verdict = AuditVerdict.PARTIAL
                narrative = (
                    f"{narrative}\n\n"
                    f"[capability cap: {blocked_count} blocked]\n"
                    + "\n".join(
                        f"  - {c.name}: {c.detail[:120]}"
                        for c in caps if c.status == "blocked"
                    )
                ).strip()
            elif (
                partial_count > len(caps) / 2
                and verdict == AuditVerdict.PASSED
            ):
                verdict = AuditVerdict.PARTIAL
                narrative = (
                    f"{narrative}\n\n"
                    f"[capability cap: {partial_count}/{len(caps)} partial]"
                ).strip()

        last_result = AuditResult(
            verdict=verdict,
            narrative=narrative,
            slice_verdicts=list(agent_output.slice_verdicts),
            capability_verdicts=list(agent_output.capability_verdicts),
            cross_slice_evidence=cross_evidence,
            walkthrough_artifacts=list(walk_result.artifacts),
            contract_test_passed=contract_passed,
            contract_test_detail=contract_detail,
            quality_score=agent_output.quality_score,
            quality_findings=list(agent_output.quality_findings),
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
            # Bug fix (2026-05-03 amendment-attack bench): emit the
            # POST-OVERRIDE verdict, not agent_output.verdict. Otherwise
            # the journal records "passed" while audit_result.verdict is
            # PARTIAL (because contract gate or chain review capped it),
            # and downstream consumers see the inconsistency.
            emit(
                session_dir,
                "audit.finished",
                detail=narrative[:200],
                verdict=verdict.value,
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
        lines.append("## Walkthrough artifacts (paths — READ these)")
        for p in agent_input.walkthrough_artifacts:
            lines.append(f"- {p}")
        lines.append(
            "These are the rendered home page, screenshots, or "
            "browser-journey logs from the integrated product. Read them "
            "to assess what a user actually sees."
        )
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
    lines.append(
        "  3. **Per-capability verdicts (REQUIRED, v2.6)**: one verdict per "
        "user-observable feature listed in `done_means` above. Each entry "
        "MUST cite specific evidence — what file/page/log you inspected to "
        "reach the verdict. Format:"
    )
    lines.append(
        "       {name: \"<capability name, ideally matching a done_means line>\", "
        "status: \"passed\"|\"partial\"|\"blocked\", "
        "detail: \"1-2 sentence rationale\", "
        "evidence_refs: [\"path/to/file:line\" or URL or screenshot path]}"
    )
    lines.append(
        "       Aim for one capability per `done_means` item. If a "
        "capability is implemented but with caveats, mark partial and "
        "explain. Empty list is NOT acceptable when done_means has items."
    )
    lines.append("  4. A final verdict: 'passed', 'partial', or 'blocked'.")
    lines.append(
        "  5. A quality assessment of the user-facing experience (REQUIRED, "
        "independent of the functional verdict):"
    )
    lines.append("")
    lines.append(
        "     **Calibration — what each score MEANS** (be honest, don't grade-inflate):"
    )
    lines.append(
        "     - **1/5 = unusable**: errors visible, broken layout, can't "
        "complete primary action."
    )
    lines.append(
        "     - **2/5 = broken UX**: things work but UX is wrong — "
        "missing labels, no error states, controls hidden where users "
        "won't find them."
    )
    lines.append(
        "     - **3/5 = MVP**: this is the DEFAULT for a project that "
        "passed acceptance tests with no extra design effort. "
        "Browser-default form styling, vertical-stacked sections, "
        "minimal CSS, plain typography, basic nav. Functional, but "
        "looks like a code sample, not a product. **Most projects that "
        "shipped for the first time will land here.**"
    )
    lines.append(
        "     - **4/5 = thoughtful**: clear design language — consistent "
        "spacing, typography, color, hover/focus states, responsive at "
        "narrow widths, error states styled, visual hierarchy beyond "
        "<h1>/<h2>. Goes beyond MVP."
    )
    lines.append(
        "     - **5/5 = polished**: production-ready feel — accessibility "
        "(aria-labels, keyboard nav), loading states, animations, "
        "branded look-and-feel, consistent design system across all "
        "surfaces."
    )
    lines.append("")
    lines.append(
        "     **Anti-grade-inflation rule**: if you find yourself wanting "
        "to give 4/5 to a product whose home page is just stacked forms "
        "with browser-default styling, that's a 3/5. Reserve 4 for "
        "products that show evidence of design thinking, not just "
        "label/nav presence."
    )
    lines.append("")
    lines.append(
        "     - quality_findings: list of CONCRETE observations about the "
        "user-facing experience. **Required: list at least 2 specific "
        "findings, even if the product is good** — name the WEAKEST thing "
        "you see and the next-most-actionable improvement. Findings can "
        "be issues (\"home page has no responsive styling — overflows on "
        "mobile\") OR opportunities (\"could group account-related forms "
        "into a single section instead of three sections at top of "
        "home\"). Empty list is NOT acceptable for a real product — if "
        "you can't find ANY improvement, you're not looking hard enough."
    )
    lines.append("")
    lines.append("Quality criteria by project_kind (use as a checklist):")
    lines.append(
        "  - **webapp**: nav present and consistent; primary actions "
        "discoverable from /; forms labelled; error states visible; "
        "responsive at narrow widths; visual hierarchy (not raw browser "
        "default styling); each page has the same design language."
    )
    lines.append(
        "  - **static-site / blog**: navigation between pages works; post "
        "list ordered properly; dates formatted; tag links clickable; "
        "**RSS feed has both a discovery <link> in head AND a visible "
        "footer/header link** (artifact existing isn't enough); "
        "readable typography (not raw browser default)."
    )
    lines.append(
        "  - **cli / library**: --help text complete; error messages "
        "actionable; exit codes meaningful; usage-friendly defaults."
    )
    lines.append("")
    lines.append(
        "**Be specific in findings.** \"Could be better\" is not useful. "
        "\"Home page has 6 forms stacked vertically with no styling, no "
        "labels, no nav bar — feels like 1998\" is useful."
    )
    lines.append("")
    lines.append(
        "Output as a single fenced JSON block with keys:\n"
        "{\n"
        "  verdict: passed|partial|blocked,\n"
        "  narrative: str,\n"
        "  slice_verdicts: [{slice_id, passed: bool, detail: str}, ...],\n"
        "  capability_verdicts: [{name: str, status: passed|partial|blocked,\n"
        "                         detail: str, evidence_refs: [str, ...]}, ...],\n"
        "  quality_score: int (1-5),\n"
        "  quality_findings: [str, ...]\n"
        "}"
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
    # Quality assessment (added with audit-final-quality check). Permissive
    # parsing — score absent / non-int → 0 (not assessed). Findings absent
    # → []. Score outside 1-5 → clamped.
    raw_score = data.get("quality_score") or 0
    try:
        quality_score = max(0, min(5, int(raw_score)))
    except (TypeError, ValueError):
        quality_score = 0
    raw_findings = data.get("quality_findings") or []
    quality_findings: list[str] = []
    if isinstance(raw_findings, list):
        quality_findings = [str(f) for f in raw_findings if f]

    # v2.6: per-capability verdicts. Permissive — invalid status →
    # "blocked" (defensive default), missing fields → empty.
    capability_verdicts: list[CapabilityVerdict] = []
    raw_caps = data.get("capability_verdicts") or []
    if isinstance(raw_caps, list):
        for entry in raw_caps:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            status_raw = str(entry.get("status") or "").strip().lower()
            status: Literal["passed", "partial", "blocked"]
            if status_raw == "passed":
                status = "passed"
            elif status_raw == "partial":
                status = "partial"
            else:
                status = "blocked"
            evidence_raw = entry.get("evidence_refs") or []
            evidence_refs = (
                [str(e) for e in evidence_raw if e]
                if isinstance(evidence_raw, list) else []
            )
            capability_verdicts.append(CapabilityVerdict(
                name=name,
                status=status,
                detail=str(entry.get("detail") or ""),
                evidence_refs=evidence_refs,
            ))

    return AuditAgentOutput(
        verdict=verdict,
        narrative=str(data.get("narrative") or ""),
        slice_verdicts=slice_verdicts,
        capability_verdicts=capability_verdicts,
        quality_score=quality_score,
        quality_findings=quality_findings,
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
    "CapabilityVerdict",
    "SliceVerdict",
    "WalkthroughCallable",
    "WalkthroughResult",
    "default_audit_agent",
    "no_op_walkthrough",
    "run_audit",
]
