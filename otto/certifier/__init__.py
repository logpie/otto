"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  run_agentic_certifier() — single agent reads, installs, tests, reports
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.certifier")


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
4. **Discover auth** (if the app has authentication):
   - Register a test user (curl the register endpoint or CLI command)
   - Login and capture the auth token/cookie
   - Save the EXACT working curl commands — you will give these to every subagent
   This is critical: do auth ONCE here, share with all subagents. Do NOT make
   each subagent figure out auth from scratch.

5. **Plan test stories:**
   If "Previous certification failures" are listed above the intent, you MUST
   re-test those specific failures FIRST (use the same story IDs). These are
   bugs that were supposedly fixed — verify they actually work now.

   Then add broader coverage from this checklist:
   - First Experience: new user registers/starts and uses the core feature
   - CRUD Lifecycle: create → read → update → delete (full cycle)
   - Data Isolation: two users' data doesn't leak between them
   - Persistence: data survives across sessions
   - Access Control: unauthenticated requests are rejected (if auth exists)
   - Search/Filter: find items by various criteria (if applicable)
   - Edge Cases: empty inputs, special characters, boundary values
   Skip stories that don't apply to this product type.

6. **Execute tests using subagents for parallelism:**

   Dispatch 3-5 subagents at once via the Agent tool. Give EACH subagent:
   - What to test (story steps + what to verify)
   - How to interact (curl commands for HTTP, CLI commands, Python for libraries)
   - Working auth commands if applicable (the exact curl from step 4)
   - Base URL / CLI entrypoint / import path
   - Ask it to report: PASS or FAIL, plus the key commands and their output

   For simple products (CLI tools), you may test inline instead.

7. **Collect results** — read each subagent's response.
8. **Report verdict** using the exact format below.

## Testing Rules
- Make REAL requests (curl for HTTP, run commands for CLI, write test scripts for libraries)
- For web apps with UI pages: use agent-browser CLI for visual verification.
  Save all screenshots to: {evidence_dir}
    agent-browser record start {evidence_dir}/recording.webm
    agent-browser open http://localhost:PORT/page
    agent-browser snapshot -i
    agent-browser screenshot {evidence_dir}/page-name.png
    agent-browser click @e3
    ... test more pages ...
    agent-browser record stop
    agent-browser close
  Take a screenshot of each key page. The screenshots are evidence for the report.
- Test the ACTUAL product, never simulate or assume
- Products can be hybrid (API + CLI + UI) — test ALL surfaces you find
- For each failure: report WHAT is wrong and WHERE (symptom + evidence). Do NOT suggest fixes.

## Verdict Format
End your final message with these EXACT markers (machine-parsed):

For EACH story, include the key evidence:

STORY_EVIDENCE_START: <story_id>
<the key commands you (or your subagent) ran and their actual output>
STORY_EVIDENCE_END: <story_id>

Then at the very end:

STORIES_TESTED: <number>
STORIES_PASSED: <number>
STORY_RESULT: <story_id> | <PASS or FAIL> | <one-line summary>
...
VERDICT: PASS or VERDICT: FAIL
DIAGNOSIS: <overall assessment or null>
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

    # Evidence directory for screenshots and video
    evidence_dir = project_dir / "otto_logs" / "certifier" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    prompt = CERTIFIER_AGENTIC_PROMPT.format(intent=intent, evidence_dir=str(evidence_dir))

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

    report_dir = project_dir / "otto_logs" / "certifier"
    report_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Running agentic certifier on %s", project_dir)

    # One LLM call — the agent does everything
    text, cost, result_msg = await run_agent_query(prompt, options)

    # Save full agent output for auditability (not truncated)
    try:
        agent_log = report_dir / "certifier-agent.log"
        agent_log.write_text(
            f"# Certifier agent output — {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# Cost: ${float(cost or 0):.3f}\n"
            f"# Text length: {len(text or '')} chars\n\n"
            f"{text or '(no output)'}\n"
        )
    except Exception as exc:
        logger.warning("Failed to write certifier agent log: %s", exc)

    # Parse results from agent output
    total_duration = round(time.monotonic() - start_time, 1)
    findings: list[Finding] = []
    stories_tested = 0
    stories_passed = 0
    story_results: list[dict[str, Any]] = []
    # Extract per-story evidence blocks
    story_evidence: dict[str, str] = {}

    if text:
        # First pass: extract STORY_EVIDENCE blocks
        current_evidence_id: str | None = None
        evidence_lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("STORY_EVIDENCE_START:"):
                current_evidence_id = stripped.split(":", 1)[1].strip()
                evidence_lines = []
            elif stripped.startswith("STORY_EVIDENCE_END:") and current_evidence_id:
                story_evidence[current_evidence_id] = "\n".join(evidence_lines)
                current_evidence_id = None
                evidence_lines = []
            elif current_evidence_id is not None:
                evidence_lines.append(line)

        # Second pass: extract verdict markers
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
                        "evidence": story_evidence.get(sid, ""),
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
    overall_diagnosis = ""
    if text:
        for line in reversed(text.split("\n")):
            stripped = line.strip()
            if stripped.startswith("VERDICT:"):
                verdict_pass = "PASS" in stripped.upper()
            elif stripped.startswith("DIAGNOSIS:"):
                diag = stripped[len("DIAGNOSIS:"):].strip()
                # Strip leading "null" (agent sometimes writes DIAGNOSIS: null<text>)
                if diag.lower().startswith("null"):
                    diag = diag[4:].strip()
                if diag:
                    overall_diagnosis = diag
            if verdict_pass or overall_diagnosis:
                # Keep scanning for both markers
                if verdict_pass and overall_diagnosis:
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
    # Stash story results for upstream extraction (CLI display)
    report._story_results = story_results  # type: ignore[attr-defined]

    # Write PoW report
    try:
        import json as _json
        pow_data = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcome": outcome.value,
            "duration_s": total_duration,
            "cost_usd": float(cost or 0),
            "stories": story_results,
        }
        (report_dir / "proof-of-work.json").write_text(
            _json.dumps(pow_data, indent=2, default=str))

        # HTML PoW
        _generate_agentic_html_pow(report_dir, story_results, outcome.value,
                                    total_duration, float(cost or 0),
                                    stories_passed, stories_tested,
                                    diagnosis=overall_diagnosis)

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
    *,
    diagnosis: str = "",
    round_history: list[dict] | None = None,
) -> None:
    """Generate HTML PoW report for the agentic certifier."""
    import html as _html

    outcome_color = "#22c55e" if outcome == "passed" else "#ef4444"
    num_rounds = len(round_history) if round_history else 1
    html = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Certification Report</title>",
        "<style>",
        "* { box-sizing: border-box; }",
        "body { font-family: system-ui, -apple-system, sans-serif; max-width: 960px; margin: 0 auto; padding: 2em 1.5em; color: #1a1a2e; background: #fafafa; }",
        "h1 { border-bottom: 3px solid #1a1a2e; padding-bottom: 0.5em; margin-bottom: 0.3em; }",
        ".outcome-banner { padding: 0.8em 1.2em; border-radius: 8px; margin-bottom: 1.5em; font-size: 1.1em; font-weight: 600; }",
        f".outcome-banner {{ background: {outcome_color}18; border: 2px solid {outcome_color}; color: {outcome_color}; }}",
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
        ".story-header { display: flex; align-items: center; gap: 0.8em; }",
        ".badge { display: inline-block; padding: 3px 10px; border-radius: 5px; font-weight: 700; font-size: 0.8em; letter-spacing: 0.03em; }",
        ".badge.pass { background: #dcfce7; color: #166534; }",
        ".badge.fail { background: #fee2e2; color: #991b1b; }",
        ".story-id { font-weight: 600; font-size: 1.05em; }",
        ".summary { margin-top: 0.5em; color: #444; line-height: 1.5; }",
        ".evidence { margin-top: 0.8em; }",
        ".evidence-toggle { background: none; border: 1px solid #ccc; border-radius: 5px; padding: 4px 12px; cursor: pointer; font-size: 0.85em; color: #555; }",
        ".evidence-toggle:hover { background: #f0f0f0; }",
        ".evidence-content { display: none; margin-top: 0.5em; background: #f7f7f9; border: 1px solid #e8e8ec; border-radius: 6px; padding: 1em; font-family: 'SF Mono', Menlo, monospace; font-size: 0.82em; white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow-y: auto; color: #333; line-height: 1.5; }",
        ".diagnosis { margin-top: 1.5em; padding: 1em; background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px; }",
        ".diagnosis h3 { margin: 0 0 0.5em; color: #9a3412; font-size: 0.95em; }",
        ".diagnosis p { margin: 0; color: #7c2d12; line-height: 1.5; }",
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
        f"<div class='outcome-banner'>{outcome.upper()} &mdash; {passed}/{total} stories passed"
        f"{f' (after {num_rounds} rounds)' if num_rounds > 1 else ''}</div>",
        "<div class='meta'>",
        f"<div class='meta-item'><span class='meta-label'>Duration</span><span class='meta-value'>{duration:.0f}s</span></div>",
        f"<div class='meta-item'><span class='meta-label'>Cost</span><span class='meta-value'>${cost:.2f}</span></div>",
        f"<div class='meta-item'><span class='meta-label'>Rounds</span><span class='meta-value'>{num_rounds}</span></div>",
        f"<div class='meta-item'><span class='meta-label'>Generated</span><span class='meta-value'>{time.strftime('%Y-%m-%d %H:%M:%S')}</span></div>",
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
        status_class = "pass" if s["passed"] else "fail"
        badge = "PASS" if s["passed"] else "FAIL"
        sid = _html.escape(s.get("story_id", ""))
        summary = _html.escape(s.get("summary", ""))
        evidence = s.get("evidence", "")

        html.append(f"<div class='story {status_class}'>")
        html.append(f"<div class='story-header'><span class='badge {status_class}'>{badge}</span><span class='story-id'>{sid}</span></div>")
        html.append(f"<div class='summary'>{summary}</div>")

        if evidence:
            eid = f"ev-{i}"
            html.append(f"<div class='evidence'>")
            html.append(f"<button class='evidence-toggle' onclick=\"toggleEvidence('{eid}')\">Show evidence</button>")
            html.append(f"<div class='evidence-content' id='evidence-{eid}'>{_html.escape(evidence)}</div>")
            html.append(f"</div>")

        html.append("</div>")

    if diagnosis:
        html.append(f"<div class='diagnosis'><h3>Overall Diagnosis</h3><p>{_html.escape(diagnosis)}</p></div>")

    # Embed screenshots from evidence directory
    evidence_dir = output_dir / "evidence"
    if evidence_dir.exists():
        screenshots = sorted(evidence_dir.glob("*.png"))
        recordings = sorted(evidence_dir.glob("*.webm"))

        if screenshots or recordings:
            html.append("<div class='evidence-section'><h2>Visual Evidence</h2>")

            if recordings:
                for vid in recordings:
                    html.append(f"<div style='margin: 1em 0;'>")
                    html.append(f"<strong>{_html.escape(vid.name)}</strong>")
                    html.append(f"<video controls width='100%' style='border-radius:8px; margin-top:0.5em;'>"
                                f"<source src='evidence/{_html.escape(vid.name)}' type='video/webm'>"
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

    html.append(f"<footer>Generated by otto certifier</footer>")
    html.append("</body></html>")
    (output_dir / "proof-of-work.html").write_text("\n".join(html))
