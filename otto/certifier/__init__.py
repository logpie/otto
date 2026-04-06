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

    Routes to agentic v2 (single agent + subagents) when certifier_mode=v2,
    otherwise uses the existing discovery + stories + journey flow.
    """
    config = config or {}
    if config.get("certifier_mode") == "v2":
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_agentic_certifier(
                intent=intent,
                project_dir=project_dir,
                config=config,
                port_override=port_override,
            ))
        finally:
            loop.close()

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


# ---------------------------------------------------------------------------
# Agentic certifier — single agent, subagent-driven
# ---------------------------------------------------------------------------

CERTIFIER_AGENTIC_PROMPT = """\
You are a QA lead certifying a software product. Your job: verify it works
for real users by testing it thoroughly.

## Product Intent
{intent}

## Your Process

1. **Read the project** — understand what it is, what framework, what files exist.
2. **Install dependencies** if needed (npm install, pip install, etc.)
3. **Start the app** if it's a server (web app, API). For CLI/library, skip this.
4. **Plan test stories** — use this coverage checklist:
   - First Experience: new user registers/starts and uses the core feature
   - CRUD Lifecycle: create → read → update → delete (full cycle)
   - Data Isolation: two users' data doesn't leak between them
   - Persistence: data survives across sessions
   - Access Control: unauthenticated requests are rejected (if auth exists)
   - Search/Filter: find items by various criteria (if applicable)
   - Edge Cases: empty inputs, special characters, boundary values
   Skip stories that don't apply to this product type.

5. **Execute tests using subagents for parallelism:**

   You have the Agent tool. Use it to dispatch test stories in parallel:

   ```
   Agent("Test first-experience story: register a new user at http://localhost:3000/api/auth/register, then create an item. Use curl. Report PASS or FAIL with evidence.")

   Agent("Test CRUD lifecycle: create, read, update, delete an item at http://localhost:3000/api/items. Report PASS or FAIL with evidence.")
   ```

   Give each subagent:
   - Clear test instructions (what to do, what to verify)
   - How to interact: curl for HTTP, CLI commands for CLI tools, Python scripts for libraries
   - Base URL, auth credentials, or CLI entrypoint
   - Ask it to report PASS or FAIL with evidence

   Dispatch 3-5 subagents at once for parallel execution.
   For simple tests (quick CLI commands), you may test inline instead.

6. **Collect results** from all subagents.
7. **Report verdict** using the exact format below.

## Testing Rules
- Make REAL requests (curl for HTTP, run commands for CLI, write test scripts for libraries)
- For web apps with UI pages: also use agent-browser if available (screenshots, visual check)
- Test the ACTUAL product, never simulate or assume
- Products can be hybrid (API + CLI + UI) — test ALL surfaces you find
- For each failure: diagnose the root cause and suggest a fix

## Verdict Format
End your final message with these EXACT markers:

STORIES_TESTED: <number>
STORIES_PASSED: <number>
STORY_RESULT: <story_id> | <PASS or FAIL> | <one-line summary>
STORY_RESULT: <story_id> | <PASS or FAIL> | <one-line summary>
...

VERDICT: PASS or VERDICT: FAIL
DIAGNOSIS: <overall assessment or null>

Include one STORY_RESULT line per story tested. These markers are machine-parsed.
"""


async def run_agentic_certifier(
    intent: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    port_override: int | None = None,
) -> "CertificationReport":
    """Agentic certifier: one monolithic agent does everything.

    A single certifier agent reads the project, installs deps, starts the app,
    plans test stories, dispatches subagents for parallel testing, and reports.

    MUST run in the caller's process (not a subprocess) so the Agent tool
    is available for subagent dispatch. Called directly from build_agentic()
    or from run_unified_certifier() when certifier_mode=v2.
    """
    import re as _re
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
    from otto.certifier.report import (
        CertificationOutcome,
        CertificationReport,
        Finding,
        TierResult,
        TierStatus,
    )

    config = config or {}
    start_time = time.monotonic()

    prompt = CERTIFIER_AGENTIC_PROMPT.format(intent=intent)

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=["project"],
        env=_subprocess_env(),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    model = config.get("model") or config.get("planner_model")
    if model:
        options.model = str(model)

    logger.info("Running agentic certifier on %s", project_dir)

    # One LLM call — the agent does everything
    text, cost, result_msg = await run_agent_query(prompt, options)

    # Parse results from agent output
    total_duration = round(time.monotonic() - start_time, 1)
    findings: list[Finding] = []
    stories_tested = 0
    stories_passed = 0
    story_results: list[dict[str, Any]] = []

    if text:
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("STORIES_TESTED:"):
                try:
                    stories_tested = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("STORIES_PASSED:"):
                try:
                    stories_passed = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("STORY_RESULT:"):
                parts = stripped[len("STORY_RESULT:"):].strip().split("|")
                if len(parts) >= 2:
                    sid = parts[0].strip()
                    passed = "PASS" in parts[1].upper()
                    summary = parts[2].strip() if len(parts) > 2 else ""
                    story_results.append({
                        "story_id": sid,
                        "passed": passed,
                        "summary": summary,
                    })
                    if not passed:
                        findings.append(Finding(
                            tier=4,
                            severity="critical",
                            category="journey",
                            description=f"Story failed: {sid}",
                            diagnosis=summary,
                            fix_suggestion=summary,
                            story_id=sid,
                        ))

    # Determine outcome
    has_failures = any(not s["passed"] for s in story_results)
    verdict_pass = False
    if text:
        for line in reversed(text.split("\n")):
            if line.strip().startswith("VERDICT:"):
                verdict_pass = "PASS" in line.upper()
                break

    if verdict_pass and not has_failures:
        outcome = CertificationOutcome.PASSED
    elif has_failures:
        outcome = CertificationOutcome.FAILED
    else:
        outcome = CertificationOutcome.FAILED  # no verdict marker = fail

    # Build tier result for backward compat
    tier4 = TierResult(
        tier=4, name="journeys",
        status=TierStatus.PASSED if outcome == CertificationOutcome.PASSED else TierStatus.FAILED,
        findings=findings,
        cost_usd=float(cost or 0),
        duration_s=total_duration,
    )

    report = CertificationReport(
        product_type="unknown",  # agent didn't report this explicitly
        interaction="unknown",
        tiers=[tier4],
        findings=findings,
        outcome=outcome,
        cost_usd=float(cost or 0),
        duration_s=total_duration,
    )

    # Write PoW report
    try:
        report_dir = project_dir / "otto_logs" / "certifier"
        report_dir.mkdir(parents=True, exist_ok=True)

        import json as _json
        pow_data = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcome": outcome.value,
            "duration_s": total_duration,
            "cost_usd": float(cost or 0),
            "stories": story_results,
            "agent_output_tail": (text or "")[-3000:],
        }
        (report_dir / "proof-of-work.json").write_text(
            _json.dumps(pow_data, indent=2, default=str))

        # HTML PoW
        _generate_agentic_html_pow(report_dir, story_results, outcome.value,
                                    total_duration, float(cost or 0),
                                    stories_passed, stories_tested)

        # Markdown PoW
        md_lines = [
            "# Proof-of-Work Certification Report",
            "",
            f"> **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"> **Outcome:** {outcome.value}",
            f"> **Duration:** {total_duration:.0f}s",
            f"> **Cost:** ${float(cost or 0):.2f}",
            f"> **Stories:** {stories_passed}/{stories_tested}",
            "",
        ]
        for s in story_results:
            status = "PASS" if s["passed"] else "FAIL"
            md_lines.append(f"- **{status}** {s['story_id']}: {s.get('summary', '')}")
        md_lines.append("")
        (report_dir / "proof-of-work.md").write_text("\n".join(md_lines))
    except Exception as exc:
        logger.warning("Failed to write PoW report: %s", exc)

    logger.info(
        "Agentic certifier done: %s, %d/%d stories, %.1fs, $%.3f",
        outcome.value, stories_passed, stories_tested, total_duration, float(cost or 0),
    )
    return report


def _generate_agentic_html_pow(
    output_dir: Path,
    story_results: list[dict],
    outcome: str,
    duration: float,
    cost: float,
    passed: int,
    total: int,
) -> None:
    """Generate HTML PoW report for the agentic certifier."""
    html = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Certification Report</title>",
        "<style>",
        "body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; }",
        "h1 { border-bottom: 2px solid #333; padding-bottom: 0.5em; }",
        ".meta { color: #666; margin-bottom: 2em; }",
        ".story { border: 1px solid #ddd; border-radius: 8px; padding: 1em; margin: 1em 0; }",
        ".story.pass { border-left: 4px solid #22c55e; }",
        ".story.fail { border-left: 4px solid #ef4444; }",
        ".badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85em; }",
        ".badge.pass { background: #dcfce7; color: #166534; }",
        ".badge.fail { background: #fee2e2; color: #991b1b; }",
        ".summary { margin-top: 0.5em; color: #444; }",
        "</style></head><body>",
        "<h1>Certification Report</h1>",
        f"<div class='meta'>",
        f"<strong>Outcome:</strong> {outcome} &nbsp; ",
        f"<strong>Stories:</strong> {passed}/{total} &nbsp; ",
        f"<strong>Duration:</strong> {duration:.0f}s &nbsp; ",
        f"<strong>Cost:</strong> ${cost:.2f} &nbsp; ",
        f"<strong>Generated:</strong> {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"</div>",
    ]

    for s in story_results:
        status_class = "pass" if s["passed"] else "fail"
        badge = "PASS" if s["passed"] else "FAIL"
        html.append(f"<div class='story {status_class}'>")
        html.append(f"<span class='badge {status_class}'>{badge}</span> ")
        html.append(f"<strong>{s.get('story_id', '')}</strong>")
        html.append(f"<div class='summary'>{s.get('summary', '')}</div>")
        html.append("</div>")

    html.append("</body></html>")
    (output_dir / "proof-of-work.html").write_text("\n".join(html))
