"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  discover_project() → compile_stories() → verify_all_stories() → done
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.certifier")


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

    Flow: discover_project() → compile_stories() → verify_all_stories() → done

    The journey agent handles everything: deps, app start, testing.
    Returns CertificationReport.
    """
    from otto.certifier.adapter import analyze_project
    from otto.certifier.classifier import classify
    from otto.certifier.manifest import ProductManifest, build_manifest
    from otto.certifier.report import (
        CertificationOutcome,
        CertificationReport,
        Finding,
        TierResult,
        TierStatus,
    )

    config = dict(config or {})
    start_time = time.monotonic()
    all_findings: list[Finding] = []
    tiers: list[TierResult] = []
    total_cost = 0.0
    infra_error = False

    # ── Project Discovery ──
    # LLM agent reads the project, installs deps, classifies, starts the app,
    # and reports how to test. No heuristic classifier — LLM decides everything.
    from otto.certifier.journey_agent import discover_project

    test_config = analyze_project(project_dir)

    discovery = discover_project(project_dir, config)
    total_cost += discovery.cost

    interaction = config.get("certifier_interaction") or discovery.interaction or "http"

    # Build a lightweight profile from discovery for manifest construction
    profile = classify(project_dir)
    if discovery.product_type != "unknown":
        profile.product_type = discovery.product_type
        profile.interaction = discovery.interaction

    try:
        # ── Build manifest ──
        base_url = discovery.base_url or ""
        manifest = None
        try:
            manifest = build_manifest(test_config, profile, base_url=base_url or None, interaction=interaction)
        except Exception:
            # Minimal manifest — journey agent will discover the rest
            manifest = ProductManifest(
                framework=getattr(profile, "framework", "unknown"),
                language=getattr(profile, "language", "unknown"),
                product_type=discovery.product_type or getattr(profile, "product_type", "unknown"),
                interaction=interaction,
                auth_type="unknown",
                register_endpoint="",
                login_endpoint="",
                seeded_users=[],
                routes=[],
                models=[],
                base_url=base_url,
                cli_entrypoint=discovery.cli_entrypoint,
            )
        if discovery.cli_entrypoint and not manifest.cli_entrypoint:
            manifest.cli_entrypoint = discovery.cli_entrypoint
        if discovery.test_approach:
            manifest.cli_help_text = discovery.test_approach

        # ── Journeys: agentic story verification ──
        journeys = _run_journeys(
            intent=intent,
            project_dir=project_dir,
            config=config,
            manifest=manifest,
            stories_path=stories_path,
            skip_story_ids=skip_story_ids,
        )
        tiers.append(journeys)
        all_findings.extend(journeys.findings)
        total_cost += journeys.cost_usd
        logger.info("Journeys: %s, %.1fs, $%.3f",
                    journeys.status.value, journeys.duration_s, journeys.cost_usd)
    except Exception as exc:
        logger.exception("Unified certifier failed unexpectedly")
        infra_error = True
        blocked = _blocked_tier_result(
            tier=4,
            name="journeys",
            blocked_by="certifier:internal_error",
            description="Unified certifier failed unexpectedly",
            diagnosis=str(exc),
        )
        tiers.append(blocked)
        all_findings.extend(blocked.findings)

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

    # Generate proof-of-work report
    tier4_obj = next((t for t in tiers if t.tier == 4), None)
    tier4_results = tier4_obj._cert_result.results if tier4_obj and hasattr(tier4_obj, "_cert_result") else None
    try:
        report_dir = project_dir / "otto_logs" / "certifier"
        _generate_pow(report_dir, tier4_results, report)
    except Exception as exc:
        logger.warning("Failed to generate PoW report: %s", exc)

    logger.info(
        "Unified certifier done: %s, %d findings, %.1fs, $%.3f",
        outcome.value, len(all_findings), total_duration, total_cost,
    )
    return report


def _generate_pow(
    output_dir: Path,
    tier4_results: list[Any] | None,
    report: Any,
) -> None:
    """Generate a PoW report for the unified certifier.

    Uses shared formatters from pow_report.py for journey sections.
    """
    import json as _json
    from otto.certifier.pow_report import format_tier4_json, format_tier4_markdown, generate_tier4_html

    output_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Proof-of-Work Certification Report",
        "",
        f"> **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> **Outcome:** {report.outcome.value}",
        f"> **Duration:** {report.duration_s:.0f}s",
        f"> **Cost:** ${report.cost_usd:.2f}",
        "",
    ]

    if tier4_results:
        lines.extend(format_tier4_markdown(tier4_results))

    # Findings summary
    if report.findings:
        lines.extend(["## Findings", ""])
        for f in report.findings:
            lines.append(f"- [{f.severity}] {f.description}")
            if f.diagnosis:
                lines.append(f"  _{f.diagnosis}_")
        lines.append("")

    (output_dir / "proof-of-work.md").write_text("\n".join(lines) + "\n")

    # HTML report — embedded screenshots, linked video, clean formatting
    if tier4_results:
        generate_tier4_html(tier4_results, report, output_dir)

    # Machine-readable JSON
    json_data: dict[str, Any] = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": report.outcome.value,
        "duration_s": report.duration_s,
        "cost_usd": report.cost_usd,
    }
    if tier4_results:
        json_data["tier4_stories"] = format_tier4_json(tier4_results)
    (output_dir / "proof-of-work.json").write_text(
        _json.dumps(json_data, indent=2, default=str)
    )


def _run_journeys(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    manifest: Any,
    stories_path: Path | None,
    skip_story_ids: set[str] | None,
) -> "TierResult":
    """Run journey verification (story compilation + agentic verification)."""
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
                product_type=getattr(manifest, "product_type", "") if manifest else "",
                interaction=getattr(manifest, "interaction", "") if manifest else "",
            )
            compile_cost = story_set.cost_usd
    except Exception as exc:
        logger.exception("Story loading failed")
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
    # Use new_event_loop + run_until_complete instead of asyncio.run() because
    # asyncio.run() installs signal handlers, which crashes with "signal only
    # works in main thread" when called from a thread (e.g. via run_in_executor
    # in verification.py's PER fix loop).
    base_url = getattr(manifest, "base_url", "") or ""
    loop = asyncio.new_event_loop()
    try:
        cert_result = loop.run_until_complete(
            verify_all_stories(
                stories_to_test, manifest, base_url, project_dir, config,
            )
        )
    except Exception as exc:
        logger.exception("Journey verification failed")
        return _blocked_tier_result(
            tier=4,
            name="journeys",
            blocked_by="certifier:journey_verification",
            description="Journey verification failed",
            diagnosis=str(exc),
            duration_s=round(time.monotonic() - start, 1),
            cost_usd=compile_cost,
        )
    finally:
        loop.close()

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
