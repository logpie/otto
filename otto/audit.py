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

import json
import logging
import time
from collections.abc import Iterable
import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from otto.build import (
    BuildAgentCallable,
    BuildAgentInput,
    BuildBudget,
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
class FeatureAudit:
    """Per-feature judgment from the audit (research §2 vocabulary).

    A feature is one atomic user-observable unit of value, typically
    derived from a `done_means` item. Replaces the single-verdict-
    for-everything model with structured per-feature judgments — lets
    the proof packet show a "feature checklist" and lets downstream
    tooling target specific gaps.

    Naming note: A0.4 renamed `CapabilityVerdict` → `FeatureAudit`. The
    new name disambiguates from the TS-layer `FeatureVerdict` Literal
    (in otto/web/client/src/types/run.ts) which means just the verdict
    outcome string. This dataclass carries name + status + detail +
    evidence_refs.

    Status:
      - "passed": feature fully works.
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
    """Aggregate audit outcome.

    A0.4: `feature_audits` is the canonical name for the per-feature
    verdict list (formerly `capability_verdicts`).
    """

    verdict: AuditVerdict
    narrative: str
    slice_verdicts: list[SliceVerdict] = field(default_factory=list)
    feature_audits: list[FeatureAudit] = field(default_factory=list)
    cross_slice_evidence: list[Evidence] = field(default_factory=list)
    walkthrough_artifacts: list[Path] = field(default_factory=list)
    contract_test_passed: bool | None = None  # None if no test_command configured
    contract_test_detail: str = ""
    quality_score: int = 0  # 1-5; 0 = not assessed
    # quality_findings: bare strings here (no severity). Per-Feature
    # severity-tagged findings live on FeatureProofBlock and use the
    # canonical `critical`/`important`/`polish` vocabulary from
    # `otto/spec_compile.py:FINDING_SEVERITIES` (research §4 ladder).
    # Legacy synonyms (`blocking`/`high`/`low`) are translated to canonical
    # at the run-view parse boundary — see
    # `otto/mission_control/run_view.py:_normalize_severity`.
    quality_findings: list[str] = field(default_factory=list)
    retries: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    # A2.1: walkthrough Feature-tagging coverage (research §A2 honesty contract).
    # `coverage_ratio < 0.90` is surfaced AND caps the audit verdict at
    # PARTIAL — see `verdict_cap_reasons` for the audit-trail of every
    # cap that fired.
    walkthrough_coverage: dict[str, Any] | None = None
    # A3.1 plumbing: parsed WalkthroughEntry objects from walkthrough.jsonl,
    # threaded into `compose_proof_packet` → `build_feature_proof_blocks` so
    # per-Feature proof blocks render their walkthrough trace. Empty list
    # when no walkthrough.jsonl exists (e.g. no-op walkthrough).
    walkthrough_entries: list[Any] = field(default_factory=list)
    # A2.1 follow-up (tick 58 deferral): human-readable record of every
    # post-judge verdict cap that fired (e.g. low walkthrough Feature-tag
    # coverage). Empty when the LLM verdict was honored as-is. Render
    # surfaces these so an operator can see WHY the final verdict differs
    # from the LLM judge's output.
    verdict_cap_reasons: list[str] = field(default_factory=list)


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
    """What an audit-agent callable returns.

    A0.4: `feature_audits` is the canonical per-Feature verdict list
    (formerly `capability_verdicts`).
    """

    verdict: AuditVerdict
    narrative: str
    slice_verdicts: list[SliceVerdict] = field(default_factory=list)
    # v2.6 per-feature verdicts: one entry per done_means item
    # (or per derived feature). Lets the proof packet show a feature
    # checklist instead of one global verdict.
    feature_audits: list[FeatureAudit] = field(default_factory=list)
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


def _validate_walkthrough_jsonl(
    walk_log_dir: Path,
    spec: Spec,
) -> tuple[list[Any], dict[str, Any] | None]:
    """A2.1 — load walkthrough.jsonl + Feature-tag coverage report.

    Looks for `walkthrough.jsonl` under `walk_log_dir`. Parses each line
    into a `WalkthroughEntry` via `parse_walkthrough_entry`, then runs
    `validate_walkthrough_coverage` against the spec.

    Returns `(entries, coverage)` where:
      - `entries` is the list of parsed `WalkthroughEntry` objects (empty
        when no walkthrough.jsonl exists, so callers can splat directly
        into `AuditResult.walkthrough_entries`).
      - `coverage` is a JSON-friendly dict suitable for
        `AuditResult.walkthrough_coverage`, or `None` if no
        walkthrough.jsonl was emitted (e.g. no-op walkthrough).

    A3.1 plumbing: `entries` is what `compose_proof_packet` feeds into
    `build_feature_proof_blocks` so per-Feature proof blocks carry their
    walkthrough trace. Single read pass — no second parser.
    """
    from otto.spec_compile import (
        parse_walkthrough_entry,
        validate_walkthrough_coverage,
    )

    jsonl_path = walk_log_dir / "walkthrough.jsonl"
    if not jsonl_path.exists():
        return [], None

    entries: list[Any] = []  # list[WalkthroughEntry]
    parse_errors: list[str] = []
    parse_warnings: list[str] = []
    for i, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {i}: {exc}")
            continue
        entry, line_warnings = parse_walkthrough_entry(payload, spec)
        if entry is None:
            parse_errors.append(f"line {i}: " + "; ".join(line_warnings))
            continue
        entries.append(entry)
        parse_warnings.extend(f"line {i}: {w}" for w in line_warnings)

    report = validate_walkthrough_coverage(entries, spec)
    coverage = {
        "total_entries": report.total_entries,
        "exploration_entries": report.exploration_entries,
        "tagged_entries": report.tagged_entries,
        "untagged_entries": report.untagged_entries,
        "non_exploration_total": report.non_exploration_total,
        "coverage_ratio": report.coverage_ratio,
        "meets_threshold": report.meets_threshold(),
        "unknown_feature_id_refs": list(report.unknown_feature_id_refs),
        "per_feature_evidence_count": dict(report.per_feature_evidence_count),
        "parse_errors": parse_errors,
        "parse_warnings": parse_warnings,
    }
    return entries, coverage


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
    for check in spec.cross_group_checks:
        if isinstance(check, BrowserJourney):
            journey = check
            break
    if journey is None:
        for slice_ in spec.groups:
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
    shared_budget: BuildBudget | None = None,
    base_branch: str = "main",
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
    audit_base_branch = base_branch
    # C4 fix: tracks audit-attempt indices where the fix loop failed.
    # When non-empty, subsequent verdicts are floored at PARTIAL —
    # otherwise the audit could re-judge the integrated state as
    # PASSED on a later attempt while real repair work never landed.
    fix_loop_failed_attempts: list[int] = []

    emit(session_dir, "audit.started")

    while retries <= budget.audit_retries:
        retries_this_pass = retries
        # 1: cross-slice checks against integrated worktree
        cross_pairs = run_checks(
            list(spec.cross_group_checks),
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

        # 2b (A2.1): Feature-tag coverage validation. The audit walkthrough
        # is contracted to emit `walkthrough.jsonl` with `feature_ids[]` per
        # action (research §A2 honesty). We compute coverage now so the
        # AuditResult surfaces tagging health — verdict cap on threshold
        # failure is a follow-up tick.
        walk_entries, walk_coverage = _validate_walkthrough_jsonl(walk_log_dir, spec)
        if walk_coverage is not None and not walk_coverage["meets_threshold"]:
            logger.warning(
                "audit walkthrough Feature-tagging coverage %.1f%% below "
                "90%% threshold (%d/%d non-exploration entries tagged)",
                walk_coverage["coverage_ratio"] * 100.0,
                walk_coverage["tagged_entries"],
                walk_coverage["non_exploration_total"],
            )

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
        # C1 fix: bail out before invoking the audit agent if the
        # shared cost pool is exhausted. Prevents an audit retry
        # loop from blowing past the global $30 ceiling. Returns
        # the last completed result (or a synthetic PARTIAL if no
        # attempt has succeeded yet) with a budget-exhausted narrative.
        if shared_budget is not None and shared_budget.remaining_total_cost_usd() <= 0:
            halt_msg = (
                f"audit halted: shared cost budget exhausted "
                f"(${shared_budget._spent_cost_usd:.2f} >= "
                f"${shared_budget.total_cost_usd:.2f})"
            )
            emit(
                session_dir, "audit.finished",
                detail=halt_msg[:200],
                verdict=AuditVerdict.PARTIAL.value,
            )
            if last_result is not None:
                return dataclasses.replace(
                    last_result,
                    narrative=last_result.narrative + "\n\n" + halt_msg,
                    cost_usd=cost_total,
                    wall_s=time.monotonic() - t0,
                )
            return AuditResult(
                verdict=AuditVerdict.PARTIAL,
                narrative=halt_msg,
                slice_verdicts=[],
                feature_audits=[],
                cross_slice_evidence=cross_evidence,
                walkthrough_artifacts=list(walk_result.artifacts),
                contract_test_passed=contract_passed,
                contract_test_detail=contract_detail,
                quality_score=0,
                quality_findings=[],
                retries=retries,
                cost_usd=cost_total,
                wall_s=time.monotonic() - t0,
                walkthrough_coverage=walk_coverage,
                walkthrough_entries=list(walk_entries),
            )
        agent_output = await audit_agent(agent_input)
        cost_total += agent_output.cost_usd
        if shared_budget is not None:
            shared_budget.charge_cost(agent_output.cost_usd)

        # Pattern B fix: caps compose order-independent. Collect ALL
        # caps as records first, then compute final verdict + narrative
        # ONCE. Previously each cap had its own `if verdict == PASSED:
        # verdict = X; narrative += Y` pattern — when one cap fired
        # first, every later cap's narrative addition was silently
        # dropped. Now narrative ALWAYS captures every active cap;
        # verdict takes the strictest of all caps.
        from otto.spec_amend import verify_amendment_chain

        chain_review = verify_amendment_chain(spec, session_dir=session_dir)

        # V4 fix: pass merge_result so the verdict reflects merge BLOCKED.
        # Slices that were BLOCKED at merge time mean the product is
        # missing their contribution; PASSED is structurally impossible
        # while merge_blocked_ids is non-empty.
        verdict, narrative = _compose_verdict(
            agent_output=agent_output,
            contract_passed=contract_passed,
            contract_detail=contract_detail,
            chain_review=chain_review,
            merge_blocked_ids=list(merge_result.blocked_ids or []),
            total_passing_slices=len(getattr(merge_result, "landed_ids", []) or [])
                + len(merge_result.blocked_ids or []),
        )
        # C4 fix: if a prior round's fix loop failed, the agent's
        # judgment on this pass cannot upgrade past PARTIAL. The
        # composer floors verdict at PARTIAL and appends a section
        # to the narrative so the operator sees WHY.
        if fix_loop_failed_attempts and verdict == AuditVerdict.PASSED:
            verdict = AuditVerdict.PARTIAL
            narrative = (
                narrative
                + "\n\n[fix-loop floor] prior audit attempts had fix-agent "
                + f"failures (rounds {fix_loop_failed_attempts}); verdict "
                + "floored at PARTIAL until repair lands cleanly."
            )

        # A2.1 follow-up (tick 58 deferral): walkthrough Feature-tag
        # coverage cap. Research §A2 honesty contract — if the walkthrough
        # didn't tag enough actions to their Features, the audit cannot
        # legitimately certify the product. Force at least PARTIAL via
        # `_strictest` (so an already-BLOCKED verdict is not downgraded).
        # Audit trail: narrate the reason AND record it in
        # `verdict_cap_reasons` so render can surface WHY the final
        # verdict differs from the LLM judge's output. The threshold
        # itself lives in `CoverageReport.meets_threshold()` —
        # `walk_coverage["meets_threshold"]` is the precomputed bool.
        verdict_cap_reasons: list[str] = []
        if walk_coverage is not None and not walk_coverage["meets_threshold"]:
            cap_reason = (
                f"walkthrough Feature-tag coverage "
                f"{walk_coverage['coverage_ratio'] * 100.0:.1f}% below "
                f"threshold "
                f"({walk_coverage['tagged_entries']}/"
                f"{walk_coverage['non_exploration_total']} non-exploration "
                f"entries tagged); verdict capped at partial — audit cannot "
                f"certify Features it did not observe"
            )
            verdict = _strictest(verdict, AuditVerdict.PARTIAL)
            narrative = narrative + "\n\n[walkthrough coverage cap]\n" + cap_reason
            verdict_cap_reasons.append(cap_reason)

        last_result = AuditResult(
            verdict=verdict,
            narrative=narrative,
            slice_verdicts=list(agent_output.slice_verdicts),
            feature_audits=list(agent_output.feature_audits),
            cross_slice_evidence=cross_evidence,
            walkthrough_artifacts=list(walk_result.artifacts),
            contract_test_passed=contract_passed,
            contract_test_detail=contract_detail,
            quality_score=agent_output.quality_score,
            quality_findings=list(agent_output.quality_findings),
            retries=retries_this_pass,
            cost_usd=cost_total,
            wall_s=time.monotonic() - t0,
            walkthrough_coverage=walk_coverage,
            walkthrough_entries=list(walk_entries),
            verdict_cap_reasons=verdict_cap_reasons,
        )

        # Pattern A: emit a per-attempt verdict event so the journal
        # records each retry's outcome. Previously only the FINAL
        # audit.finished was emitted — debugging a fix loop required
        # reading per-attempt log dirs by hand.
        # C2 fix: include the FULL feature_audits payload (not
        # just the names of blocked ones) so the journal is a complete
        # audit trail. C5 fix: also include contract_test_passed and
        # contract_test_detail so an operator can reconstruct WHY the
        # verdict was capped from the journal alone.
        emit(
            session_dir,
            "audit.attempt.finished",
            attempt=retries,
            detail=narrative[:200],
            verdict=verdict.value,
            quality_score=agent_output.quality_score,
            blocked_features=[
                c.name for c in agent_output.feature_audits
                if c.status == "blocked"
            ],
            feature_audits=[
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": (c.detail or "")[:500],
                    "evidence_refs": list(c.evidence_refs or []),
                }
                for c in agent_output.feature_audits
            ],
            contract_test_passed=contract_passed,
            contract_test_detail=(contract_detail or "")[:500],
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

        # C4 fix: track whether ALL slice fixes succeeded this round.
        # If any fix crashed or returned succeeded=False, downgrade
        # the next attempt's verdict floor to PARTIAL so the audit
        # cannot silently return PASSED on the next pass when the
        # underlying repair didn't actually land.
        any_fix_failed = False
        for slice_id in failing_ids:
            # C1 fix: check shared budget before each fix dispatch too.
            if shared_budget is not None and shared_budget.remaining_total_cost_usd() <= 0:
                any_fix_failed = True
                emit(
                    session_dir, "slice.attempt.failed",
                    slice_id=slice_id, attempt=retries + 1,
                    detail="shared cost budget exhausted; fix skipped",
                )
                break
            slice_obj = next((s for s in spec.groups if s.id == slice_id), None)
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
            # V3 fix: checkout the slice's branch before invoking the
            # fix-agent so its edits accumulate on the slice branch, NOT
            # on base_branch. After the agent runs, commit the diff to
            # the slice branch and attempt a real merge into base via
            # _merge_slice_branch — so blocked slices can be landed
            # through the same path as build-phase slices, never as
            # rogue commits on main. Without this, fix-agents bypass
            # branch isolation entirely (observed in P1: fix-agent ran
            # `git commit` directly on main, audit then declared PASSED
            # while merge_queue still recorded the slice as BLOCKED).
            from otto.build import (
                _is_git_repo as _build_is_git_repo,
                _setup_slice_branch as _build_setup_slice_branch,
                _commit_slice_work as _build_commit_slice_work,
            )
            from otto.merge_queue import _merge_slice_branch, _git as _merge_git, MergeStatus as _MergeStatus
            on_slice_branch = False
            if _build_is_git_repo(worktree):
                on_slice_branch = _build_setup_slice_branch(
                    worktree, branch=branch, parent_ref=branch,
                )
                if not on_slice_branch:
                    any_fix_failed = True
                    emit(
                        session_dir, "slice.attempt.failed",
                        slice_id=slice_id, attempt=retries + 1,
                        detail=f"could not checkout slice branch {branch} for fix",
                    )
                    continue
            try:
                fix_output = await fix_agent(agent_input_fix)
                cost_total += fix_output.cost_usd
                if shared_budget is not None:
                    shared_budget.charge_cost(fix_output.cost_usd)
                if not fix_output.succeeded:
                    any_fix_failed = True
                    if on_slice_branch:
                        _build_commit_slice_work(worktree, slice_id=slice_id, branch=branch)
                else:
                    if on_slice_branch:
                        committed = _build_commit_slice_work(
                            worktree, slice_id=slice_id, branch=branch,
                        )
                        if not committed:
                            any_fix_failed = True
                        else:
                            # Re-merge the fixed slice through the canonical
                            # merge path. If the merge succeeds the slice
                            # finally lands; if it conflicts or no diff, the
                            # next audit cycle will see it.
                            merge_outcome = _merge_slice_branch(
                                _merge_git, worktree,
                                slice_id=slice_id, branch=branch,
                                base_branch=audit_base_branch,
                            )
                            if merge_outcome.status == _MergeStatus.LANDED:
                                emit(
                                    session_dir, "slice.merge.landed",
                                    slice_id=slice_id, attempt=retries + 1,
                                    detail=merge_outcome.head_after,
                                )
                            else:
                                any_fix_failed = True
                emit(
                    session_dir,
                    "slice.attempt.failed" if not fix_output.succeeded else "slice.merge.eligible",
                    slice_id=slice_id,
                    attempt=retries + 1,
                    detail=fix_output.detail or "",
                )
                # Return to base_branch so the next audit pass judges
                # the integrated state.
                if on_slice_branch:
                    _merge_git(["checkout", audit_base_branch], worktree)
            except Exception as exc:
                any_fix_failed = True
                if on_slice_branch:
                    _merge_git(["checkout", audit_base_branch], worktree)
                emit(
                    session_dir,
                    "slice.attempt.failed",
                    slice_id=slice_id,
                    attempt=retries + 1,
                    detail=f"audit-routed fix crashed: {type(exc).__name__}: {exc}",
                )
        # C4 fix: if any fix in this cycle failed, the next audit pass
        # must NOT silently upgrade to PASSED. Track in a flag the
        # next iteration of `_compose_verdict` consults.
        if any_fix_failed:
            fix_loop_failed_attempts.append(retries + 1)
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


_AUDIT_FEATURE_TAGGING_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "audit-feature-tagging.md"
)


def _load_feature_tagging_contract() -> str:
    """Load the audit walkthrough Feature-tagging contract markdown.

    The contract (research §A2 + §4) is the single source of truth for
    what every audit agent must emit in `walkthrough.jsonl`:
    `feature_ids[]`, `action_kind`, ≥90% non-exploration coverage,
    per-project-kind examples. It lives as a sibling prompt so reviewers
    can edit one file instead of hunting for inline strings.

    Falling back to a minimal stub keeps unit-test environments that
    strip the prompts dir from breaking — the real prompt is the
    file-on-disk one in production.
    """
    try:
        return _AUDIT_FEATURE_TAGGING_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "# Audit Feature-tagging contract\n\n"
            "Every walkthrough action carries `feature_ids[]`. Untagged\n"
            "actions outside `action_kind: \"exploration\"` are rejected.\n"
            "≥90% non-exploration coverage required.\n"
        )


def _audit_prompt(agent_input: AuditAgentInput) -> str:
    """Compose the audit-agent prompt.

    Walks: spec → integrated worktree state → build summary → merge
    summary → cross-slice check verdicts → walkthrough artifacts →
    Feature-tagging contract (audit-feature-tagging.md) → ask
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
    for s in spec.groups:
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
    # ── Feature-tagging contract (research §A2 + §4) ─────────────────
    # The audit agent MUST emit walkthrough.jsonl with `feature_ids[]`
    # per action and ≥90% non-exploration coverage. Inline the contract
    # so prompt edits land in one place (otto/prompts/audit-feature-
    # tagging.md) rather than scattered string literals.
    lines.append("## Walkthrough Feature-tagging contract (REQUIRED)")
    lines.append("")
    lines.append(
        "Before recording your verdicts, you must walk the integrated "
        "product and emit `audit/attempt-NN/walkthrough.jsonl` per the "
        "contract below. Per-Feature evidence is derived from these "
        "tagged actions; untagged or weakly-tagged walkthroughs cause "
        "the audit pass to be rejected."
    )
    lines.append("")
    lines.append(_load_feature_tagging_contract().rstrip())
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
        "  feature_audits: [{name: str, status: passed|partial|blocked,\n"
        "                    detail: str, evidence_refs: [str, ...]}, ...],\n"
        "  quality_score: int (1-5),\n"
        "  quality_findings: [str, ...]\n"
        "}"
    )
    return "\n".join(lines)


_VERDICT_RANK = {
    AuditVerdict.PASSED: 0,
    AuditVerdict.PARTIAL: 1,
    AuditVerdict.BLOCKED: 2,
}


def _strictest(a: AuditVerdict, b: AuditVerdict) -> AuditVerdict:
    """Return the stricter of two verdicts (BLOCKED > PARTIAL > PASSED)."""
    return a if _VERDICT_RANK[a] >= _VERDICT_RANK[b] else b


def _compose_verdict(
    *,
    agent_output: AuditAgentOutput,
    contract_passed: bool | None,
    contract_detail: str,
    chain_review,  # ChainVerification, but spec_amend imports audit so avoid cycle
    merge_blocked_ids: list[str] | None = None,
    total_passing_slices: int = 0,
) -> tuple[AuditVerdict, str]:
    """Compose final verdict + narrative from all caps, order-independent.

    Pattern B fix. Previously each cap was an `if verdict == PASSED:
    verdict = X; narrative += Y` block; the first cap to fire silently
    dropped every subsequent cap's narrative because the guard never
    held. Now every active cap contributes a narrative section AND
    the verdict is the strictest of all cap-implied verdicts.

    Caps:
      - LLM-judge verdict (the agent's own output) is the floor.
      - Contract test failed → at least PARTIAL.
      - Chain review BLOCKED → BLOCKED. PARTIAL → at least PARTIAL.
      - Quality score < 3 → at least PARTIAL.
      - Capability has any BLOCKED → at least PARTIAL. With ALL/MOSTLY
        blocked → BLOCKED (escalation that the old code couldn't do).
      - Capability >50% partial → at least PARTIAL.
    """
    verdict = agent_output.verdict
    narrative = agent_output.narrative or ""
    sections: list[str] = []

    # Contract test cap.
    if contract_passed is False:
        verdict = _strictest(verdict, AuditVerdict.PARTIAL)
        sections.append(f"[contract test FAILED]\n{contract_detail}")

    # Amendment chain cap.
    if chain_review.verdict_cap == "blocked":
        verdict = _strictest(verdict, AuditVerdict.BLOCKED)
    elif chain_review.verdict_cap == "partial":
        verdict = _strictest(verdict, AuditVerdict.PARTIAL)
    if chain_review.findings:
        sections.append(
            f"[amendment chain review: {chain_review.verdict_cap}]\n"
            + "\n".join(f"  - {f}" for f in chain_review.findings)
        )

    # V4 fix: merge-blocked cap. If any slice was BLOCKED at merge time,
    # the integrated product is missing that slice's contribution. The
    # audit MUST NOT silently declare PASSED while merge_result.blocked_ids
    # is non-empty (the false-positive class observed in P1, where audit
    # said PASSED while home_page never landed). Cap at PARTIAL when any
    # slice blocked; cap at BLOCKED when more than half of expected
    # passing slices were blocked.
    blocked_ids = list(merge_blocked_ids or [])
    if blocked_ids:
        if total_passing_slices and len(blocked_ids) * 2 > total_passing_slices:
            verdict = _strictest(verdict, AuditVerdict.BLOCKED)
        else:
            verdict = _strictest(verdict, AuditVerdict.PARTIAL)
        sections.append(
            f"[merge: {len(blocked_ids)} slice(s) blocked at merge time]\n"
            + "\n".join(f"  - {sid} did not land via merge_queue" for sid in blocked_ids)
        )

    # Quality cap.
    qs = agent_output.quality_score
    if qs and qs < 3:
        verdict = _strictest(verdict, AuditVerdict.PARTIAL)
    # Always narrate quality if assessed and either the score is low OR
    # findings exist. Doesn't gate on verdict — every active cap surfaces.
    if qs and (qs < 3 or agent_output.quality_findings):
        sections.append(
            f"[quality assessment: {qs}/5]\n"
            + "\n".join(f"  - {f}" for f in agent_output.quality_findings[:10])
        )

    # Capability cap.
    caps = agent_output.feature_audits
    if caps:
        blocked = [c for c in caps if c.status == "blocked"]
        partial = [c for c in caps if c.status == "partial"]
        # Pattern B fix #3: capability cap CAN escalate to BLOCKED when
        # MORE THAN HALF of capabilities are blocked (catastrophic).
        # 50/50 stays at PARTIAL — partial damage, not catastrophic.
        # Previously the cap could only downgrade PASSED→PARTIAL.
        if len(blocked) * 2 > len(caps):
            verdict = _strictest(verdict, AuditVerdict.BLOCKED)
        elif blocked:
            verdict = _strictest(verdict, AuditVerdict.PARTIAL)
        elif len(partial) * 2 > len(caps):
            verdict = _strictest(verdict, AuditVerdict.PARTIAL)
        # Always narrate any blocked or majority-partial pattern.
        if blocked or (len(partial) * 2 > len(caps) and partial):
            section_lines = []
            if blocked:
                section_lines.append(f"[capability cap: {len(blocked)}/{len(caps)} blocked]")
                for c in blocked[:10]:
                    section_lines.append(f"  - {c.name}: {(c.detail or '')[:120]}")
            if partial and (len(partial) * 2 > len(caps)):
                section_lines.append(f"[capability cap: {len(partial)}/{len(caps)} partial]")
            sections.append("\n".join(section_lines))

    if sections:
        narrative = (narrative + "\n\n" + "\n\n".join(sections)).strip()
    return verdict, narrative


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

    # A0.4: per-Feature audits. Canonical wire key is `feature_audits`.
    # Permissive — invalid status → "blocked" (defensive default),
    # missing fields → empty.
    feature_audits: list[FeatureAudit] = []
    raw_feats = data.get("feature_audits") or []
    if isinstance(raw_feats, list):
        for entry in raw_feats:
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
            feature_audits.append(FeatureAudit(
                name=name,
                status=status,
                detail=str(entry.get("detail") or ""),
                evidence_refs=evidence_refs,
            ))

    return AuditAgentOutput(
        verdict=verdict,
        narrative=str(data.get("narrative") or ""),
        slice_verdicts=slice_verdicts,
        feature_audits=feature_audits,
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
    # Pattern F: distinguish missing (fine) from unreadable (fail).
    config: dict = {}
    if config_path.exists():
        try:
            config = load_config(config_path)
        except Exception as exc:
            raise RuntimeError(
                f"otto.yaml at {config_path} is unreadable: {exc}"
            ) from exc
    options = make_agent_options(
        agent_input.project_dir, config, agent_type="certifier"
    )
    options.cwd = str(agent_input.integrated_worktree)
    options.permission_mode = "bypassPermissions"  # audit reads, doesn't edit
    # C3 fix: hard-assert the read-only invariant. The certifier
    # reports symptoms, not fixes; if a refactor flips this to
    # "acceptEdits", the audit silently starts patching the integrated
    # worktree and the fix loop's slice-targeted dispatch is bypassed.
    assert options.permission_mode == "bypassPermissions", (
        "audit agent must be read-only (permission_mode='bypassPermissions'); "
        f"got {options.permission_mode!r}. Audit reports symptoms, not fixes — "
        "patches must go through the fix_agent dispatch."
    )

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
    "FeatureAudit",
    "SliceVerdict",
    "WalkthroughCallable",
    "WalkthroughResult",
    "default_audit_agent",
    "no_op_walkthrough",
    "run_audit",
]
