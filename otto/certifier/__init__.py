"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  run_agentic_certifier() — single agent reads, installs, tests, reports
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from otto.budget import RunBudget

logger = logging.getLogger("otto.certifier")


def _render_certifier_prompt(
    *,
    mode: str,
    intent: str,
    evidence_dir: Path,
    focus: str | None = None,
    target: str | None = None,
) -> str:
    """Render a standalone certifier prompt with safe placeholder defaults."""
    from otto.prompts import render_prompt

    focus_section = f"## Improvement Focus\n{focus}" if focus else ""
    prompt_name = {
        "standard": "certifier.md",
        "fast": "certifier-fast.md",
        "thorough": "certifier-thorough.md",
        "hillclimb": "certifier-hillclimb.md",
        "target": "certifier-target.md",
    }.get(mode, "certifier.md")
    return render_prompt(
        prompt_name,
        intent=intent,
        evidence_dir=str(evidence_dir),
        focus_section=focus_section,
        spec_section="",
        target=target or "",
    )


# ---------------------------------------------------------------------------
# Agentic certifier — single agent, subagent-driven
# ---------------------------------------------------------------------------

async def run_agentic_certifier(
    intent: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    mode: str = "standard",
    focus: str | None = None,
    target: str | None = None,
    budget: "RunBudget | None" = None,
    session_id: str | None = None,
    write_session_summary: bool = True,
) -> "CertificationReport":
    """Agentic certifier: one monolithic agent does everything.

    A single certifier agent reads the project, installs deps, starts the app,
    plans test stories, dispatches subagents for parallel testing, and reports.

    MUST run in the caller's process (not a subprocess) so the Agent tool
    is available for subagent dispatch.

    CONTRACT: `AgentCallError` propagates — callers own the retry/pause
    decision. Non-timeout errors still return `INFRA_ERROR` (existing
    behavior preserved for split-mode retry logic).
    """
    from otto.agent import make_agent_options, run_agent_with_timeout
    from otto.certifier.report import (
        CertificationOutcome,
        CertificationReport,
    )

    config = config or {}
    start_time = time.monotonic()

    # Each certifier run goes under a session dir. When called standalone
    # (no session_id), allocate one.
    from otto import paths
    if session_id is None:
        session_id = paths.new_session_id(project_dir)
    paths.ensure_session_scaffold(project_dir, session_id)
    paths.set_pointer(project_dir, paths.LATEST_POINTER, session_id)
    run_id = session_id  # kept for downstream log mentions
    report_dir = paths.certify_dir(project_dir, session_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    latest_dir: Path | None = None  # `latest` pointer is now managed by paths.set_pointer

    # Evidence stays inside the run-specific directory so concurrent runs do
    # not clobber each other's screenshots or recordings.
    evidence_dir = report_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    prompt = _render_certifier_prompt(
        mode=mode,
        intent=intent,
        evidence_dir=evidence_dir,
        focus=focus,
        target=target,
    )

    # Inject cross-run memory (opt-in via config)
    from otto.memory import inject_memory
    prompt = inject_memory(prompt, project_dir, config)

    options = make_agent_options(project_dir, config, agent_type="certifier")

    logger.info("Running agentic certifier on %s", project_dir)

    # Timeout derives from the run budget (None = no timeout).
    timeout = budget.for_call() if budget is not None else None

    text, cost, _session_id, _breakdown = await run_agent_with_timeout(
        prompt, options,
        log_dir=report_dir,
        phase_name="CERTIFY",
        timeout=timeout,
        project_dir=project_dir,
    )

    # Parse results from agent output
    total_duration = round(time.monotonic() - start_time, 1)
    from otto.markers import parse_certifier_markers
    parsed = parse_certifier_markers(text or "")
    from otto.markers import compact_story_results
    story_results = compact_story_results(parsed.stories)

    # Determine outcome
    has_failures = any(not s["passed"] for s in story_results)
    passed = parsed.verdict_pass and not has_failures and bool(story_results)
    target_mode = mode == "target" or bool(target) or bool(config.get("_target"))
    if target_mode:
        passed = passed and parsed.metric_met is True
    outcome = CertificationOutcome.PASSED if passed else CertificationOutcome.FAILED

    report = CertificationReport(
        outcome=outcome,
        cost_usd=float(cost or 0),
        duration_s=total_duration,
        story_results=story_results,
        metric_value=parsed.metric_value,
        metric_met=parsed.metric_met,
    )

    # Write PoW report
    try:
        from otto.observability import write_json_file as _write_json
        # Standalone certify: "total" equals certifier cost (no build agent).
        certifier_cost = float(cost or 0)
        pow_data = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": outcome.value,
            "duration_s": total_duration,
            "certifier_cost_usd": certifier_cost,
            "total_cost_usd": certifier_cost,
            "stories": story_results,
        }
        _write_json(report_dir / "proof-of-work.json", pow_data)

        # HTML PoW
        _generate_agentic_html_pow(report_dir, story_results, outcome.value,
                                    total_duration, certifier_cost,
                                    parsed.stories_passed, parsed.stories_tested,
                                    diagnosis=parsed.diagnosis,
                                    certifier_cost=certifier_cost)

        # Markdown PoW — certifier-only here, so total == certifier.
        md_lines = [
            "# Proof-of-Work Certification Report",
            "",
            f"> **Generated:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"> **Outcome:** {outcome.value}",
            f"> **Duration:** {total_duration:.0f}s",
            f"> **Cost:** ${certifier_cost:.2f}",
            f"> **Stories:** {parsed.stories_passed}/{parsed.stories_tested}",
            "",
        ]
        for s in story_results:
            status = "WARN" if s.get("warn") else ("PASS" if s["passed"] else "FAIL")
            md_lines.append(f"- **{status}** {s['story_id']}: {s.get('summary', '')}")
        md_lines.append("")
        (report_dir / "proof-of-work.md").write_text("\n".join(md_lines))
    except Exception as exc:
        logger.warning("Failed to write PoW report: %s", exc)

    # `latest` pointer (session-scoped) is set by paths.set_pointer above;
    # the old top-level certifier/latest symlink is retired. Callers that
    # want the newest PoW should use paths.resolve_pointer("latest").

    logger.info(
        "Agentic certifier done: %s, %d/%d stories, %.1fs, $%.3f",
        outcome.value, parsed.stories_passed, parsed.stories_tested, total_duration, float(cost or 0),
    )

    # Record cross-run memory
    from otto.memory import record_run
    record_run(
        project_dir,
        run_id=run_id,
        command="certify",
        certifier_mode=mode,
        stories=story_results,
        cost=float(cost or 0),
    )

    if write_session_summary:
        try:
            from otto.pipeline import _write_session_summary
            certify_rounds_count = len(parsed.certify_rounds) or 1
            breakdown = {
                "certify": {
                    "duration_s": total_duration,
                    "cost_usd": float(cost or 0.0),
                    "rounds": certify_rounds_count,
                }
            }
            _write_session_summary(
                project_dir,
                run_id,
                verdict=outcome.value,
                passed=passed,
                cost=float(cost or 0),
                duration=total_duration,
                stories_passed=parsed.stories_passed,
                stories_tested=parsed.stories_tested,
                rounds=certify_rounds_count,
                intent=intent,
                command="certify",
                breakdown=breakdown,
            )
        except Exception as exc:
            logger.warning("Failed to write session summary: %s", exc)

    return report


def _generate_agentic_html_pow(
    output_dir: Path,
    story_results: list[dict],
    outcome: str,
    duration: float,
    cost: float,
    passed: int,
    total: int,
    *,
    diagnosis: str = "",
    round_history: list[dict] | None = None,
    evidence_dir: Path | None = None,
    certifier_cost: float | None = None,
) -> None:
    """Generate HTML PoW report for the agentic certifier.

    `cost` is the authoritative total cost (spec + agent + certifier where
    applicable) — displayed to the user. `certifier_cost`, when different
    from `cost`, is shown as an additional breakdown.
    """
    import html as _html

    num_rounds = len(round_history) if round_history else 1
    # Qualified selectors (.outcome-banner.pass / .outcome-banner.fail) avoid
    # the previous duplicate-selector CSS-cascade bug where the FAIL variant
    # silently inherited PASS colors.
    banner_class = "pass" if outcome == "passed" else "fail"
    html = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Certification Report</title>",
        "<style>",
        "* { box-sizing: border-box; }",
        "body { font-family: system-ui, -apple-system, sans-serif; max-width: 960px; margin: 0 auto; padding: 2em 1.5em; color: #1a1a2e; background: #fafafa; }",
        "h1 { border-bottom: 3px solid #1a1a2e; padding-bottom: 0.5em; margin-bottom: 0.3em; }",
        ".outcome-banner { padding: 0.8em 1.2em; border-radius: 8px; margin-bottom: 1.5em; font-size: 1.1em; font-weight: 600; }",
        ".outcome-banner.pass { background: #22c55e18; border: 2px solid #22c55e; color: #22c55e; }",
        ".outcome-banner.fail { background: #ef444418; border: 2px solid #ef4444; color: #ef4444; }",
        ".meta { display: flex; gap: 2em; flex-wrap: wrap; color: #555; margin-bottom: 2em; font-size: 0.95em; }",
        ".meta-item { display: flex; flex-direction: column; }",
        ".meta-label { font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.05em; color: #888; }",
        ".meta-value { font-weight: 600; font-size: 1.1em; }",
        ".rounds { margin-bottom: 1.5em; padding: 1em; background: #f0f4ff; border: 1px solid #c7d2fe; border-radius: 8px; }",
        ".rounds h3 { margin: 0 0 0.8em; font-size: 0.95em; color: #3730a3; }",
        ".round-item { display: flex; align-items: center; gap: 0.8em; padding: 0.4em 0; }",
        ".round-num { font-weight: 700; color: #4338ca; min-width: 5em; }",
        ".round-verdict { font-weight: 600; }",
        ".round-verdict.pass { color: #166534; }",
        ".round-verdict.fail { color: #991b1b; }",
        ".round-detail { color: #555; font-size: 0.9em; }",
        ".story { border: 1px solid #e0e0e0; border-radius: 10px; padding: 1.2em; margin: 1em 0; background: #fff; }",
        ".story.pass { border-left: 5px solid #22c55e; }",
        ".story.fail { border-left: 5px solid #ef4444; }",
        ".story.warn { border-left: 5px solid #eab308; }",
        ".story-header { display: flex; align-items: center; gap: 0.8em; }",
        ".badge { display: inline-block; padding: 3px 10px; border-radius: 5px; font-weight: 700; font-size: 0.8em; letter-spacing: 0.03em; }",
        ".badge.pass { background: #dcfce7; color: #166534; }",
        ".badge.fail { background: #fee2e2; color: #991b1b; }",
        ".badge.warn { background: #fef3c7; color: #92400e; }",
        ".story-id { font-weight: 600; font-size: 1.05em; }",
        ".summary { margin-top: 0.5em; color: #444; line-height: 1.5; }",
        ".evidence { margin-top: 0.8em; }",
        ".evidence-toggle { background: none; border: 1px solid #ccc; border-radius: 5px; padding: 4px 12px; cursor: pointer; font-size: 0.85em; color: #555; }",
        ".evidence-toggle:hover { background: #f0f0f0; }",
        ".evidence-content { display: none; margin-top: 0.5em; background: #f7f7f9; border: 1px solid #e8e8ec; border-radius: 6px; padding: 1em; font-family: 'SF Mono', Menlo, monospace; font-size: 0.82em; white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow-y: auto; color: #333; line-height: 1.5; }",
        ".diagnosis { margin-top: 1.5em; padding: 1em; background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px; }",
        ".diagnosis h3 { margin: 0 0 0.5em; color: #9a3412; font-size: 0.95em; }",
        ".diagnosis p { margin: 0; color: #7c2d12; line-height: 1.5; }",
        ".note { margin: 0.5em 0; padding: 0.6em 1em; background: #f7f7f9; border: 1px dashed #c7c7cc; border-radius: 6px; color: #555; font-size: 0.9em; }",
        "footer { margin-top: 2em; padding-top: 1em; border-top: 1px solid #e0e0e0; color: #999; font-size: 0.8em; }",
        "</style>",
        "<script>",
        "function toggleEvidence(id) {",
        "  var el = document.getElementById('evidence-' + id);",
        "  el.style.display = el.style.display === 'block' ? 'none' : 'block';",
        "}",
        "</script>",
        "</head><body>",
        "<h1>Certification Report</h1>",
        f"<div class='outcome-banner {banner_class}'>{outcome.upper()} &mdash; {passed}/{total} stories passed"
        f"{f' (after {num_rounds} rounds)' if num_rounds > 1 else ''}</div>",
        "<div class='meta'>",
        f"<div class='meta-item'><span class='meta-label'>Duration</span><span class='meta-value'>{duration:.0f}s</span></div>",
    ]
    # Cost display: if we have a distinct certifier cost, show both lines for transparency.
    if certifier_cost is not None and abs(certifier_cost - cost) > 1e-9:
        html.append(
            "<div class='meta-item'><span class='meta-label'>Cost</span>"
            f"<span class='meta-value'>${cost:.2f} total "
            f"(certifier ${certifier_cost:.2f})</span></div>"
        )
    else:
        html.append(
            "<div class='meta-item'><span class='meta-label'>Cost</span>"
            f"<span class='meta-value'>${cost:.2f}</span></div>"
        )
    html += [
        f"<div class='meta-item'><span class='meta-label'>Rounds</span><span class='meta-value'>{num_rounds}</span></div>",
        f"<div class='meta-item'><span class='meta-label'>Generated</span><span class='meta-value'>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</span></div>",
        "</div>",
    ]

    # Round history (only shown if multiple rounds — fix loop was triggered)
    if round_history and len(round_history) > 1:
        html.append("<div class='rounds'><h3>Certification Rounds</h3>")
        for r in round_history:
            rn = r.get("round", "?")
            v = r.get("verdict")
            sc = r.get("stories_count", 0)
            pc = r.get("passed_count", 0)
            if v is None or sc == 0:
                continue  # skip empty rounds
            v_class = "pass" if v else "fail"
            v_text = "PASS" if v else "FAIL"
            html.append(
                f"<div class='round-item'>"
                f"<span class='round-num'>Round {rn}</span>"
                f"<span class='round-verdict {v_class}'>{v_text}</span>"
                f"<span class='round-detail'>{pc}/{sc} stories</span>"
                f"</div>"
            )
        html.append("</div>")

    for i, s in enumerate(story_results):
        if s.get("warn"):
            status_class = "warn"
            badge = "WARN"
        elif s["passed"]:
            status_class = "pass"
            badge = "PASS"
        else:
            status_class = "fail"
            badge = "FAIL"
        sid = _html.escape(s.get("story_id", ""))
        summary = _html.escape(s.get("summary", ""))
        evidence = s.get("evidence", "")

        html.append(f"<div class='story {status_class}'>")
        html.append(f"<div class='story-header'><span class='badge {status_class}'>{badge}</span><span class='story-id'>{sid}</span></div>")
        html.append(f"<div class='summary'>{summary}</div>")

        if evidence:
            eid = f"ev-{i}"
            html.append("<div class='evidence'>")
            html.append(f"<button class='evidence-toggle' onclick=\"toggleEvidence('{eid}')\">Show evidence</button>")
            html.append(f"<div class='evidence-content' id='evidence-{eid}'>{_html.escape(evidence)}</div>")
            html.append("</div>")

        html.append("</div>")

    diagnosis_text = (diagnosis or "").strip()
    if diagnosis_text:
        html.append(
            f"<div class='diagnosis'><h3>Overall Diagnosis</h3><p>{_html.escape(diagnosis_text)}</p></div>"
        )

    # Embed screenshots from evidence directory
    evidence_dir = evidence_dir or (output_dir / "evidence")
    if evidence_dir.exists():
        screenshots = sorted(evidence_dir.glob("*.png"))
        recordings = sorted(evidence_dir.glob("*.webm"))

        if screenshots or recordings:
            html.append("<div class='evidence-section'><h2>Visual Evidence</h2>")

            if recordings:
                for vid in recordings:
                    rel_video = os.path.relpath(vid, output_dir).replace(os.sep, "/")
                    html.append("<div style='margin: 1em 0;'>")
                    html.append(f"<strong>{_html.escape(vid.name)}</strong>")
                    html.append(f"<video controls width='100%' style='border-radius:8px; margin-top:0.5em;'>"
                                f"<source src='{_html.escape(rel_video)}' type='video/webm'>"
                                f"</video></div>")

            if screenshots:
                html.append("<div style='display:flex; flex-wrap:wrap; gap:1em; margin-top:1em;'>")
                for img in screenshots:
                    import base64 as _b64
                    try:
                        img_data = _b64.b64encode(img.read_bytes()).decode()
                        html.append(
                            f"<div style='flex:1; min-width:300px;'>"
                            f"<div style='font-size:0.8em; color:#666; margin-bottom:0.3em;'>{_html.escape(img.name)}</div>"
                            f"<img src='data:image/png;base64,{img_data}' style='width:100%; border-radius:8px; border:1px solid #ddd;' />"
                            f"</div>"
                        )
                    except Exception:
                        html.append(f"<div><em>Screenshot: {_html.escape(img.name)} (failed to embed)</em></div>")
                html.append("</div>")

            html.append("</div>")

    html.append("<footer>Generated by otto certifier</footer>")
    html.append("</body></html>")
    (output_dir / "proof-of-work.html").write_text("\n".join(html))
