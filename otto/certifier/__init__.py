"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  Intent → Intent Compiler → Requirement Matrix
                                    ↓
                          Product Classifier
                                    ↓
                          Deterministic Baseline (Tier 1)
                                    ↓
                          Sequential Journeys (Tier 2)
                                    ↓
                          Judge → Proof-of-Work Report
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.certifier")


def run_certifier_for_verification(
    intent: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    port_override: int | None = None,
) -> dict[str, Any]:
    """Run the full certifier pipeline and return verification-compatible results.

    Runs Tier 1 (endpoint probes) + Tier 2 (user journeys) and returns a dict
    matching the interface verification expects from product QA.

    Returns dict with:
        product_passed (bool), journeys (list with step-level proof),
        cost_usd (float), duration_s (float),
        tier1_result (BaselineResult), tier2_result (Tier2Result).
    """
    from otto.certifier.adapter import analyze_project
    from otto.certifier.binder import bind, save_bound_plan
    from otto.certifier.baseline import (
        AppRunner,
        judge,
        load_or_compile_journeys,
        load_or_compile_matrix,
        run_baseline_from_bound_plan,
    )
    from otto.certifier.classifier import classify
    from otto.certifier.pow_report import generate_pow_report
    from otto.certifier.tier2 import run_tier2_from_bound_plan

    config = dict(config or {})
    start_time = time.monotonic()

    # 1. Classify and analyze the project
    profile = classify(project_dir)
    effective_port = port_override or config.get("port_override")
    if effective_port is not None:
        profile.port = int(effective_port)
        profile.extra["reuse_existing_app"] = True
    test_config = analyze_project(project_dir)

    # 2. Compile/load matrices (cached after first call)
    matrix, matrix_source, matrix_path, compile_duration = load_or_compile_matrix(
        project_dir, intent, config=config, test_config=test_config,
    )
    journey_matrix, j_source, j_path, j_compile_duration = load_or_compile_journeys(
        project_dir, intent, config=config,
    )
    bound_plan = bind(matrix, journey_matrix, test_config, profile)
    report_dir = project_dir / "otto_logs" / "certifier"
    save_bound_plan(bound_plan, report_dir / "bound-plan.json")

    # 3. Start app once, share across both tiers
    runner = AppRunner(project_dir, profile)
    app_evidence = runner.start()

    if not app_evidence.passed:
        logger.error("App failed to start: %s", app_evidence.actual)
        return {
            "product_passed": False,
            "journeys": [],
            "cost_usd": matrix.cost_usd if matrix_source != "cache" else 0.0,
            "duration_s": round(time.monotonic() - start_time, 1),
            "error": f"App failed to start: {app_evidence.actual}",
        }

    try:
        # 4. Tier 1 — endpoint probes
        tier1_result = run_baseline_from_bound_plan(
            bound_plan,
            project_dir,
            profile,
            app_runner=runner,
        )
        tier1_result.compile_cost_usd = matrix.cost_usd if matrix_source != "cache" else 0.0
        tier1_result.compile_duration_s = compile_duration
        tier1_result.compiled_at = matrix.compiled_at
        tier1_result.matrix_source = matrix_source
        tier1_result.matrix_path = str(matrix_path)
        tier1_result.app_start_evidence = app_evidence
        tier1_result.verdict = judge(tier1_result)
        tier1_result.certified = tier1_result.verdict.certified

        # 5. Tier 2 — user journeys
        tier2_result = run_tier2_from_bound_plan(
            bound_plan, runner.base_url, project_dir,
        )
    finally:
        runner.stop()

    total_duration = round(time.monotonic() - start_time, 1)
    compile_cost = matrix.cost_usd if matrix_source != "cache" else 0.0
    j_compile_cost = journey_matrix.cost_usd if j_source != "cache" else 0.0

    # 6. Generate proof-of-work report
    try:
        generate_pow_report(tier1_result, tier2_result, report_dir)
    except Exception as exc:
        logger.warning("Failed to generate PoW report: %s", exc)

    # 7. Translate to verification format
    journeys = []
    for j in tier2_result.journeys:
        failed_steps = [s for s in j.steps if not s.passed]
        first_fail = failed_steps[0] if failed_steps else None

        journeys.append({
            "name": j.name,
            "passed": j.passed,
            "error": j.stopped_at or (first_fail.error if first_fail else None),
            "evidence": _format_journey_evidence(j),
            "steps": [
                {
                    "action": s.action,
                    "detail": s.detail,
                    "passed": s.passed,
                    "error": s.error,
                    "proof": {
                        "request": s.proof.request,
                        "response": s.proof.response,
                        "timestamp": s.proof.timestamp,
                    } if s.proof and s.proof.timestamp else None,
                }
                for s in j.steps
            ],
        })

    product_passed = (
        tier1_result.certified
        and tier2_result.journeys_tested > 0
        and tier2_result.journeys_failed == 0
    )

    logger.info(
        "Certifier done: Tier1=%s/%s, Tier2=%s, duration=%.1fs, cost=$%.3f",
        tier1_result.claims_passed, tier1_result.claims_tested,
        tier2_result.journey_score(), total_duration,
        compile_cost + j_compile_cost,
    )

    return {
        "product_passed": product_passed,
        "journeys": journeys,
        "cost_usd": compile_cost + j_compile_cost,
        "duration_s": total_duration,
        "tier1_result": tier1_result,
        "tier2_result": tier2_result,
    }


def run_certifier_v2(
    intent: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    port_override: int | None = None,
    stories_path: Path | None = None,
    skip_story_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Certifier v2: agentic journey verification.

    Compiles user stories from intent, builds a product manifest from code analysis,
    runs a journey agent per story to simulate real users. Produces actionable
    fix tasks for the verification loop.

    Returns dict with:
        product_passed, stories (list with step-level evidence + diagnosis),
        cost_usd, duration_s, certification_result.
    """
    import asyncio

    from otto.certifier.adapter import analyze_project
    from otto.certifier.baseline import AppRunner
    from otto.certifier.classifier import classify
    from otto.certifier.journey_agent import verify_all_stories, CertificationResult
    from otto.certifier.manifest import build_manifest
    from otto.certifier.preflight import preflight_check
    from otto.certifier.stories import load_or_compile_stories, load_stories

    config = dict(config or {})
    start_time = time.monotonic()

    # 1. Classify and analyze
    profile = classify(project_dir)
    effective_port = port_override or config.get("port_override")
    if effective_port is not None:
        profile.port = int(effective_port)
        profile.extra["reuse_existing_app"] = True
    test_config = analyze_project(project_dir)

    # 2. Compile/load stories (shared for fair comparison)
    if stories_path:
        story_set = load_stories(stories_path)
        story_source = "shared"
        compile_cost = 0.0
    else:
        story_set, story_source, _, _ = load_or_compile_stories(
            project_dir, intent, config=config,
        )
        compile_cost = story_set.cost_usd

    # 3. Start app
    runner = AppRunner(project_dir, profile)
    app_evidence = runner.start()

    if not app_evidence.passed:
        logger.error("App failed to start: %s", app_evidence.actual)
        return {
            "product_passed": False,
            "stories": [],
            "journeys": [],
            "stories_passed": 0,
            "stories_tested": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "break_findings": [],
            "cost_usd": compile_cost,
            "duration_s": round(time.monotonic() - start_time, 1),
            "error": f"App failed to start: {app_evidence.actual}",
        }

    try:
        # 4. Build manifest (static + runtime probes)
        manifest = build_manifest(test_config, profile, runner.base_url)

        # 5. Pre-flight check
        pf = preflight_check(manifest, runner.base_url)
        if not pf.ready:
            logger.warning("Pre-flight failed: %s", pf.reason)
            return {
                "product_passed": False,
                "stories": [],
                "journeys": [],
                "stories_passed": 0,
                "stories_tested": 0,
                "critical_passed": 0,
                "critical_total": 0,
                "break_findings": [],
                "cost_usd": compile_cost,
                "duration_s": round(time.monotonic() - start_time, 1),
                "error": f"Pre-flight failed: {pf.reason}",
                "preflight": {
                    "ready": False,
                    "summary": pf.summary,
                    "checks": [
                        {"name": c.name, "passed": c.passed, "detail": c.detail}
                        for c in pf.checks
                    ],
                },
            }

        # 6. Verify stories (THE MAIN EVENT)
        # On re-verify, skip stories that already passed
        stories_to_test = story_set.stories
        if skip_story_ids:
            stories_to_test = [s for s in stories_to_test if s.id not in skip_story_ids]
            logger.info("Targeted re-verify: testing %d of %d stories", len(stories_to_test), len(story_set.stories))

        cert_result = asyncio.run(
            verify_all_stories(
                stories_to_test, manifest, runner.base_url, project_dir, config,
                on_between_stories=runner.ensure_alive,
            )
        )
    finally:
        runner.stop()

    total_duration = round(time.monotonic() - start_time, 1)

    # 7. Format for verification
    stories_output = []
    for r in cert_result.results:
        failed_steps = [s for s in r.steps if s.outcome == "fail"]
        stories_output.append({
            "name": r.story_title,
            "story_id": r.story_id,
            "persona": r.persona,
            "passed": r.passed,
            "blocked_at": r.blocked_at,
            "summary": r.summary,
            "diagnosis": r.diagnosis,
            "fix_suggestion": r.fix_suggestion,
            "steps": [
                {
                    "action": s.action,
                    "outcome": s.outcome,
                    "verification": s.verification,
                    "diagnosis": s.diagnosis,
                    "fix_suggestion": s.fix_suggestion,
                }
                for s in r.steps
            ],
            "break_findings": [
                {
                    "technique": b.technique,
                    "description": b.description,
                    "result": b.result,
                    "severity": b.severity,
                    "fix_suggestion": b.fix_suggestion,
                }
                for b in r.break_findings
            ],
        })

    logger.info(
        "Certifier v2 done: %d/%d stories passed, duration=%.1fs, cost=$%.3f",
        cert_result.stories_passed, cert_result.stories_tested,
        total_duration, compile_cost + cert_result.total_cost_usd,
    )

    return {
        "product_passed": cert_result.certified,
        "stories": stories_output,
        "stories_passed": cert_result.stories_passed,
        "stories_tested": cert_result.stories_tested,
        "critical_passed": cert_result.critical_passed,
        "critical_total": cert_result.critical_total,
        "break_findings": [
            {
                "technique": b.technique,
                "description": b.description,
                "result": b.result,
                "severity": b.severity,
                "fix_suggestion": b.fix_suggestion,
            }
            for b in cert_result.break_findings
        ],
        "cost_usd": compile_cost + cert_result.total_cost_usd,
        "duration_s": total_duration,
        "certification_result": cert_result,
        # Backward compat: verification checks "journeys" key
        "journeys": stories_output,
    }


def run_unified_certifier(
    intent: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    port_override: int | None = None,
    stories_path: Path | None = None,
    skip_story_ids: set[str] | None = None,
) -> "CertificationReport":
    """Unified certifier: single source of product truth.

    Runs 4 tiers sequentially:
      Tier 1 — Structural: files, build, tests, app start (seconds, no LLM)
      Tier 2 — Probes: HTTP route checks (seconds, no LLM)
      Tier 3 — Regression: graduated tests from prior runs (seconds, no LLM)
      Tier 4 — Journeys: agentic story verification (minutes, LLM)

    No early exit — every applicable tier runs. Returns CertificationReport.
    """
    from otto.certifier.adapter import analyze_project
    from otto.certifier.baseline import AppRunner
    from otto.certifier.classifier import classify
    from otto.certifier.manifest import build_manifest
    from otto.certifier.report import (
        CertificationOutcome,
        CertificationReport,
        Finding,
        TierResult,
        TierStatus,
    )
    from otto.certifier.tiers import run_tier1_structural, run_tier2_probes

    config = dict(config or {})
    start_time = time.monotonic()
    all_findings: list[Finding] = []
    tiers: list[TierResult] = []
    total_cost = 0.0
    infra_error = False

    # Classify and analyze
    profile = classify(project_dir)
    effective_port = port_override or config.get("port_override")
    if effective_port is not None:
        profile.port = int(effective_port)
        profile.extra["reuse_existing_app"] = True

    interaction = config.get("certifier_interaction") or profile.interaction or "http"
    test_config = analyze_project(project_dir)
    test_command = config.get("test_command")

    # For non-HTTP products, we can't run probes or HTTP journeys
    # Phase 1: only HTTP executor is implemented
    if interaction not in ("http", "browser"):
        total_duration = round(time.monotonic() - start_time, 1)
        report = CertificationReport(
            product_type=profile.product_type or "unknown",
            interaction=interaction,
            outcome=CertificationOutcome.BLOCKED,
            cost_usd=0.0,
            duration_s=total_duration,
        )
        report.tiers.append(TierResult(
            tier=1, name="structural", status=TierStatus.SKIPPED,
            skip_reason=f"Interaction type '{interaction}' not yet supported (Phase 1 = HTTP only)",
        ))
        return report

    # Create AppRunner for web apps
    runner = AppRunner(project_dir, profile)

    try:
        # ── Tier 1: Structural ──
        tier1 = run_tier1_structural(
            project_dir, profile,
            test_command=test_command,
            app_runner=runner,
        )
        tiers.append(tier1)
        all_findings.extend(tier1.findings)
        logger.info("Tier 1 (structural): %s, %.1fs", tier1.status.value, tier1.duration_s)

        # ── Tier 2: Probes ──
        app_started = getattr(tier1, "_app_started", False)
        manifest = None
        if app_started:
            try:
                manifest = build_manifest(test_config, profile, runner.base_url)
            except Exception as exc:
                logger.exception("Tier 2/4 manifest build failed")
                tier2 = _blocked_tier_result(
                    tier=2,
                    name="probes",
                    blocked_by="certifier:manifest_build",
                    description="Certifier manifest build failed",
                    diagnosis=str(exc),
                )
            else:
                tier2 = run_tier2_probes(project_dir, manifest, runner.base_url, tier1)
        else:
            tier2 = TierResult(
                tier=2, name="probes", status=TierStatus.BLOCKED,
                blocked_by="tier_1:app_start",
            )
        tiers.append(tier2)
        all_findings.extend(tier2.findings)
        logger.info("Tier 2 (probes): %s, %.1fs", tier2.status.value, tier2.duration_s)

        # ── Tier 3: Regression (graduated tests from prior runs) ──
        # Phase 2 — not yet implemented, skip for now
        tier3 = TierResult(
            tier=3, name="regression", status=TierStatus.SKIPPED,
            skip_reason="Regression tests not yet implemented (Phase 2)",
        )
        tiers.append(tier3)

        # ── Tier 4: Journeys (agentic story verification) ──
        if not app_started:
            tier4 = TierResult(
                tier=4, name="journeys", status=TierStatus.BLOCKED,
                blocked_by="tier_1:app_start",
            )
        elif manifest is None:
            tier4 = _blocked_tier_result(
                tier=4,
                name="journeys",
                blocked_by="certifier:manifest_build",
                description="Journey verification blocked by manifest build failure",
            )
        else:
            tier4 = _run_tier4_journeys(
                intent=intent,
                project_dir=project_dir,
                config=config,
                runner=runner,
                manifest=manifest,
                stories_path=stories_path,
                skip_story_ids=skip_story_ids,
            )
        tiers.append(tier4)
        all_findings.extend(tier4.findings)
        total_cost += tier4.cost_usd
        logger.info("Tier 4 (journeys): %s, %.1fs, $%.3f",
                    tier4.status.value, tier4.duration_s, tier4.cost_usd)
    except Exception as exc:
        logger.exception("Unified certifier failed unexpectedly")
        infra_error = True
        blocked = _blocked_tier_result(
            tier=0,
            name="certifier",
            blocked_by="certifier:internal_error",
            description="Unified certifier failed unexpectedly",
            diagnosis=str(exc),
        )
        tiers.append(blocked)
        all_findings.extend(blocked.findings)
    finally:
        try:
            runner.stop()
        except Exception:
            pass

    # Determine outcome
    total_duration = round(time.monotonic() - start_time, 1)
    has_critical = any(f.severity in ("critical", "important") for f in all_findings)
    any_blocked = any(t.status == TierStatus.BLOCKED for t in tiers)

    if has_critical:
        outcome = CertificationOutcome.FAILED
    elif infra_error:
        outcome = CertificationOutcome.INFRA_ERROR
    elif any_blocked:
        outcome = CertificationOutcome.BLOCKED
    else:
        outcome = CertificationOutcome.PASSED

    report = CertificationReport(
        product_type=profile.product_type or "unknown",
        interaction=interaction,
        tiers=tiers,
        findings=all_findings,
        outcome=outcome,
        cost_usd=total_cost,
        duration_s=total_duration,
    )

    logger.info(
        "Unified certifier done: %s, %d findings, %.1fs, $%.3f",
        outcome.value, len(all_findings), total_duration, total_cost,
    )
    return report


def _run_tier4_journeys(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    runner: Any,
    manifest: Any,
    stories_path: Path | None,
    skip_story_ids: set[str] | None,
) -> "TierResult":
    """Run tier 4 journey verification. Wraps existing run_certifier_v2 logic."""
    import asyncio
    from otto.certifier.journey_agent import verify_all_stories
    from otto.certifier.report import Finding, TierResult, TierStatus
    from otto.certifier.stories import load_or_compile_stories, load_stories

    start = time.monotonic()
    compile_cost = 0.0

    # Compile/load stories
    try:
        if stories_path:
            story_set = load_stories(stories_path)
        else:
            story_set, _, _, _ = load_or_compile_stories(
                project_dir, intent, config=config,
            )
            compile_cost = story_set.cost_usd
    except Exception as exc:
        logger.exception("Tier 4 story loading failed")
        return _blocked_tier_result(
            tier=4,
            name="journeys",
            blocked_by="certifier:story_loading",
            description="Journey story loading failed",
            diagnosis=str(exc),
            duration_s=round(time.monotonic() - start, 1),
            cost_usd=compile_cost,
        )

    # Filter stories for targeted re-verify
    stories_to_test = story_set.stories
    skipped_any = False
    if skip_story_ids:
        stories_to_test = [s for s in stories_to_test if s.id not in skip_story_ids]
        skipped_any = len(stories_to_test) < len(story_set.stories)
        logger.info("Targeted re-verify: testing %d of %d stories",
                     len(stories_to_test), len(story_set.stories))

    if not stories_to_test:
        return TierResult(
            tier=4, name="journeys",
            status=TierStatus.PASSED if skipped_any else TierStatus.SKIPPED,
            skip_reason=None if skipped_any else "no stories to test",
            duration_s=round(time.monotonic() - start, 1),
            cost_usd=compile_cost,
        )

    # Run journey agents
    ensure_alive = getattr(runner, "ensure_alive", None)
    try:
        cert_result = asyncio.run(
            verify_all_stories(
                stories_to_test, manifest, runner.base_url, project_dir, config,
                on_between_stories=ensure_alive,
            )
        )
    except Exception as exc:
        logger.exception("Tier 4 journey verification failed")
        return _blocked_tier_result(
            tier=4,
            name="journeys",
            blocked_by="certifier:journey_verification",
            description="Journey verification failed",
            diagnosis=str(exc),
            duration_s=round(time.monotonic() - start, 1),
            cost_usd=compile_cost,
        )

    # Convert to findings
    findings: list[Finding] = []
    paired_results = list(zip(stories_to_test, cert_result.results))
    result_count_mismatch = len(cert_result.results) != len(stories_to_test)
    if result_count_mismatch:
        logger.warning(
            "Tier 4 result count mismatch: %d stories, %d results",
            len(stories_to_test), len(cert_result.results),
        )
        findings.append(Finding(
            tier=4,
            severity="warning",
            category="harness",
            description=(
                "Journey verification returned a different number of results than "
                "stories requested; some stories may be unverified"
            ),
            diagnosis=(
                f"Requested {len(stories_to_test)} stories but received "
                f"{len(cert_result.results)} results"
            ),
            fix_suggestion=(
                "Treat this certification run as invalid and investigate the "
                "journey verifier before trusting the outcome"
            ),
            evidence={
                "stories_requested": len(stories_to_test),
                "results_received": len(cert_result.results),
                "requested_story_ids": [story.id for story in stories_to_test],
                "result_story_ids": [result.story_id for result in cert_result.results],
            },
        ))

    for story, r in paired_results:
        if not r.passed:
            findings.append(Finding(
                tier=4,
                severity="critical" if story.critical else "important",
                category="journey",
                description=f"Story failed: {r.story_title}",
                diagnosis=r.diagnosis or "",
                fix_suggestion=r.fix_suggestion or "",
                story_id=r.story_id,
                evidence={
                    "persona": r.persona,
                    "blocked_at": r.blocked_at,
                    "summary": r.summary,
                    "steps": [
                        {"action": s.action, "outcome": s.outcome,
                         "diagnosis": s.diagnosis, "fix_suggestion": s.fix_suggestion}
                        for s in r.steps if s.outcome == "fail"
                    ],
                },
            ))
        # Break findings
        for b in r.break_findings:
            # All break findings are warnings — surfaced loudly but don't fail certification.
            # Break testing is adversarial edge-case probing; severity is stochastic
            # (same XSS can be "high" or "medium" across runs). Using warnings ensures
            # consistent pass/fail across runs and fair comparison with bare CC.
            finding_severity = "warning"
            findings.append(Finding(
                tier=4,
                severity=finding_severity,
                category="edge-case",
                description=f"Break finding ({b.severity}): {b.description}",
                diagnosis=b.result,
                fix_suggestion=b.fix_suggestion,
                story_id=r.story_id,
            ))

    for r in cert_result.results[len(paired_results):]:
        if not r.passed:
            findings.append(Finding(
                tier=4,
                severity="important",
                category="journey",
                description=f"Story failed: {r.story_title}",
                diagnosis=r.diagnosis or "",
                fix_suggestion=r.fix_suggestion or "",
                story_id=r.story_id,
                evidence={
                    "persona": r.persona,
                    "blocked_at": r.blocked_at,
                    "summary": r.summary,
                    "steps": [
                        {"action": s.action, "outcome": s.outcome,
                         "diagnosis": s.diagnosis, "fix_suggestion": s.fix_suggestion}
                        for s in r.steps if s.outcome == "fail"
                    ],
                },
            ))
        for b in r.break_findings:
            # All break findings are warnings — surfaced loudly but don't fail certification.
            # Break testing is adversarial edge-case probing; severity is stochastic
            # (same XSS can be "high" or "medium" across runs). Using warnings ensures
            # consistent pass/fail across runs and fair comparison with bare CC.
            finding_severity = "warning"
            findings.append(Finding(
                tier=4,
                severity=finding_severity,
                category="edge-case",
                description=f"Break finding ({b.severity}): {b.description}",
                diagnosis=b.result,
                fix_suggestion=b.fix_suggestion,
                story_id=r.story_id,
            ))

    duration = round(time.monotonic() - start, 1)
    total_cost = compile_cost + cert_result.total_cost_usd

    # Tier passes if all tested stories pass (and at least one was tested)
    status = (
        TierStatus.PASSED
        if cert_result.certified and not result_count_mismatch
        else TierStatus.FAILED
    )

    result = TierResult(
        tier=4, name="journeys", status=status,
        findings=findings, duration_s=duration, cost_usd=total_cost,
    )
    # Stash raw certification result for legacy compat
    result._cert_result = cert_result  # type: ignore[attr-defined]
    result._stories_output = _format_stories_output(cert_result)  # type: ignore[attr-defined]
    return result


def _blocked_tier_result(
    *,
    tier: int,
    name: str,
    blocked_by: str,
    description: str,
    diagnosis: str = "",
    duration_s: float = 0.0,
    cost_usd: float = 0.0,
) -> "TierResult":
    """Return a BLOCKED tier result with a non-actionable harness finding."""
    from otto.certifier.report import Finding, TierResult, TierStatus

    finding = Finding(
        tier=tier,
        severity="warning",
        category="harness",
        description=description,
        diagnosis=diagnosis[:500],
        fix_suggestion="Inspect certifier infrastructure and retry",
    )
    return TierResult(
        tier=tier,
        name=name,
        status=TierStatus.BLOCKED,
        findings=[finding],
        blocked_by=blocked_by,
        duration_s=duration_s,
        cost_usd=cost_usd,
    )


def _format_stories_output(cert_result: Any) -> list[dict[str, Any]]:
    """Format certification results as legacy journey dicts."""
    stories = []
    for r in cert_result.results:
        stories.append({
            "name": r.story_title,
            "story_id": r.story_id,
            "persona": r.persona,
            "passed": r.passed,
            "blocked_at": r.blocked_at,
            "summary": r.summary,
            "diagnosis": r.diagnosis,
            "fix_suggestion": r.fix_suggestion,
            "steps": [
                {"action": s.action, "outcome": s.outcome,
                 "verification": s.verification,
                 "diagnosis": s.diagnosis,
                 "fix_suggestion": s.fix_suggestion}
                for s in r.steps
            ],
        })
    return stories


def _format_journey_evidence(j: Any) -> str:
    """Format journey results as a concise evidence string for fix tasks."""
    passed = sum(1 for s in j.steps if s.passed)
    total = len(j.steps)
    lines = [f"{passed}/{total} steps passed"]

    for s in j.steps:
        if not s.passed:
            line = f"FAIL: {s.action}"
            if s.proof and s.proof.request:
                req = s.proof.request
                line += f" {req.get('method', '?')} {req.get('url', '?')}"
            if s.proof and s.proof.response:
                line += f" → HTTP {s.proof.response.get('status', '?')}"
            if s.error:
                line += f" ({s.error[:100]})"
            lines.append(line)

    return "\n".join(lines)
