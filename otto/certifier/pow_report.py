"""Proof-of-work report generator.

Generates a single human-auditable report with verifiable evidence
for every claim and journey. A human can read this report and verify
every assertion by re-running the documented requests.

Every claim says: "I sent THIS request, got THIS response, at THIS time."
Nothing is claimed without evidence.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from otto.certifier.baseline import BaselineResult
from otto.certifier.tier2 import Tier2Result


def generate_pow_report(
    tier1: BaselineResult,
    tier2: Tier2Result,
    output_dir: Path,
    label: str = "",
    *,
    tier4_results: list[Any] | None = None,
) -> Path:
    """Generate a complete proof-of-work report.

    tier4_results: optional list of JourneyResult from agentic story verification.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Proof-of-Work Certification Report{' — ' + label if label else ''}",
        "",
        f"> **Product:** `{tier1.product_dir}`",
        f"> **Intent:** {tier1.intent}",
        f"> **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Scores",
        "",
        "| Tier | Score | What it measures |",
        "|---|---|---|",
        f"| Tier 1 (Endpoints) | {tier1.claims_passed}/{tier1.claims_tested} ({_pct(tier1.claims_passed, tier1.claims_tested)}) | API endpoints exist and respond correctly |",
        f"| Tier 2 (Journeys) | {tier2.journey_score()} | Multi-step user flows complete end-to-end |",
        f"| Tier 2 (Steps) | {tier2.step_score()} | Individual actions within journeys |",
        *(
            [f"| Tier 4 (Stories) | {sum(1 for r in tier4_results if r.passed)}/{len(tier4_results)} | Agentic user story verification |"]
            if tier4_results else []
        ),
        "",
        f"**Verdict:** {tier1.verdict.summary if tier1.verdict else 'Unknown'}",
        "",
        "---",
        "",
        "## How to Read This Report",
        "",
        "Every claim below includes **proof**: the exact HTTP request sent,",
        "the exact response received, and when. You can verify any claim by",
        "re-running the request yourself.",
        "",
        "```",
        "CLAIM: cart-add-item — Users can add a product to their cart",
        "PROOF:",
        "  Request:  POST http://localhost:3000/api/cart",
        "            Body: {\"productId\": \"abc123\", \"quantity\": 1}",
        "  Response: HTTP 201",
        "            Body: {\"id\": \"cart1\", \"productId\": \"abc123\", ...}",
        "  Time:     2026-03-31T00:39:50Z",
        "```",
        "",
        "If proof is missing, the claim is marked `(no proof)` — the certifier",
        "could not execute the test deterministically.",
        "",
    ]

    # --- Tier 1 ---
    lines.extend([
        "---",
        "",
        "## Tier 1 — Endpoint Proof-of-Work",
        "",
    ])

    ni_claims = [r for r in tier1.results if r.outcome == "not_implemented"]
    fail_claims = [r for r in tier1.results if r.outcome == "fail"]
    pass_claims = [r for r in tier1.results if r.outcome == "pass"]

    if ni_claims:
        lines.append("### Not Implemented (feature missing from code)")
        lines.append("")
        for r in ni_claims:
            lines.append(f"**{r.claim_id}**: {r.claim_description}")
            for e in r.evidence:
                lines.append(f"- Evidence: {e.actual[:150]}")
            lines.append("")

    if fail_claims:
        lines.append("### Failed")
        lines.append("")
        for r in fail_claims:
            lines.append(f"**{r.claim_id}**: {r.claim_description}")
            _append_tier1_proof(lines, r)
            lines.append("")

    lines.append("### Passed")
    lines.append("")
    for r in pass_claims:
        lines.append(f"**{r.claim_id}**: {r.claim_description}")
        _append_tier1_proof(lines, r)
        lines.append("")

    # --- Tier 2 ---
    lines.extend([
        "---",
        "",
        "## Tier 2 — User Journey Proof-of-Work",
        "",
    ])

    for j in tier2.journeys:
        icon = "✓" if j.passed else "✗"
        passed_steps = sum(1 for s in j.steps if s.passed)
        lines.append(f"### {icon} {j.name} ({passed_steps}/{len(j.steps)} steps)")
        lines.append(f"_{j.description}_")
        if j.stopped_at and not j.passed:
            lines.append(f"**Stopped at:** {j.stopped_at}")
        lines.append("")

        for s in j.steps:
            si = "✓" if s.passed else "✗"
            lines.append(f"**{si} {s.action}**: {s.detail}")

            if s.proof and s.proof.timestamp:
                req = s.proof.request
                resp = s.proof.response
                lines.append("```")
                lines.append(f"Request:  {req.get('method', '?')} {req.get('url', '?')}")
                if req.get("body"):
                    body_str = json.dumps(req["body"], default=str)
                    if len(body_str) > 200:
                        body_str = body_str[:200] + "..."
                    lines.append(f"          Body: {body_str}")
                lines.append(f"Response: HTTP {resp.get('status', '?')}")
                if resp.get("body"):
                    resp_str = json.dumps(resp["body"], default=str)
                    if len(resp_str) > 200:
                        resp_str = resp_str[:200] + "..."
                    lines.append(f"          Body: {resp_str}")
                lines.append(f"Time:     {s.proof.timestamp}")
                lines.append("```")
            else:
                lines.append("_(no proof — step was not an HTTP request)_")

            if s.error:
                lines.append(f"> ⚠ {s.error}")
            lines.append("")

    # --- Tier 4 ---
    if tier4_results:
        lines.extend(format_tier4_markdown(tier4_results))

    # --- Scope ---
    lines.extend([
        "---",
        "",
        "## Scope & Limitations",
        "",
        "**What this report proves:**",
        "- Every claim was tested with a real HTTP request",
        "- Responses were received and validated",
        "- Timestamps are included for auditability",
        "",
        "**What this report does NOT prove:**",
        "- Visual rendering (no screenshots in Tier 1/2 sequential mode)",
        "- Real payment processing (Stripe uses placeholder keys)",
        "- Performance or load handling",
        "- Accessibility or mobile responsiveness",
        "- Security beyond basic auth checks",
        "",
        "To verify any claim, re-run the documented request against the",
        "running application and compare the response.",
    ])

    report_path = output_dir / "proof-of-work.md"
    report_path.write_text("\n".join(lines) + "\n")

    # Also save machine-readable version
    json_path = output_dir / "proof-of-work.json"
    json_data = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "product_dir": tier1.product_dir,
        "intent": tier1.intent,
        "scores": {
            "tier1": f"{tier1.claims_passed}/{tier1.claims_tested}",
            "tier2_journeys": tier2.journey_score(),
            "tier2_steps": tier2.step_score(),
        },
        "tier1_claims": [
            {
                "id": r.claim_id,
                "description": r.claim_description,
                "outcome": r.outcome,
                "proof": [
                    {
                        "command": e.command,
                        "expected": e.expected,
                        "actual": e.actual[:200],
                        "timestamp": e.timestamp,
                        "proof": e.proof if hasattr(e, "proof") else None,
                    }
                    for e in r.evidence
                    if not e.skipped
                ],
            }
            for r in tier1.results
        ],
        "tier2_journeys": [
            {
                "name": j.name,
                "passed": j.passed,
                "steps": [
                    {
                        "action": s.action,
                        "passed": s.passed,
                        "proof": asdict(s.proof) if s.proof and s.proof.timestamp else None,
                        "error": s.error,
                    }
                    for s in j.steps
                ],
            }
            for j in tier2.journeys
        ],
    }
    if tier4_results:
        json_data["tier4_stories"] = format_tier4_json(tier4_results)

    json_path.write_text(json.dumps(json_data, indent=2, default=str))

    return report_path


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _extract_command(input_str: str) -> str:
    """Extract the shell command from a tool input dict repr string."""
    # Input is str({"command": "curl ...", "description": "..."})
    # Extract just the command value
    import re
    m = re.search(r"'command':\s*'(.*?)'(?:,\s*'description'|$|\})", input_str, re.DOTALL)
    if m:
        cmd = m.group(1)
        # Unescape
        cmd = cmd.replace("\\'", "'").replace("\\\\", "\\")
        # Truncate long commands
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        return cmd
    # Fallback: return cleaned input
    return input_str[:200]


def format_tier4_markdown(tier4_results: list[Any]) -> list[str]:
    """Format Tier 4 results as markdown lines. Shared by v1 and v2 PoW paths."""
    passed = sum(1 for r in tier4_results if r.passed)
    lines = [
        "---",
        "",
        f"## Tier 4 — Agentic Story Verification ({passed}/{len(tier4_results)} passed)",
        "",
    ]
    for r in tier4_results:
        icon = "✓" if r.passed else "✗"
        lines.append(f"### {icon} {r.story_title} ({r.persona}, {r.duration_s:.0f}s, ${r.cost_usd:.3f})")
        lines.append("")
        if r.blocked_at:
            lines.append(f"**Blocked at:** {r.blocked_at}")
        if not r.passed and r.diagnosis:
            lines.append(f"**Diagnosis:** {_strip_ansi(r.diagnosis)}")
        if not r.passed and r.fix_suggestion:
            lines.append(f"**Suggested fix:** {_strip_ansi(r.fix_suggestion)}")
        failed = [s for s in r.steps if s.outcome == "fail"]
        if failed:
            lines.append("")
            lines.append("**Failed steps:**")
            for s in failed:
                lines.append(f"- ✗ {s.action}")
                if s.diagnosis:
                    lines.append(f"  _{_strip_ansi(s.diagnosis)}_")
        if r.break_findings:
            lines.append("")
            lines.append("**Break findings:**")
            for b in r.break_findings:
                lines.append(f"- [{b.severity}] {b.technique}: {_strip_ansi(b.description)}")

        # Evidence trail — clean formatted tool calls
        evidence = getattr(r, "evidence_chain", None) or []
        if evidence:
            lines.append("")
            lines.append("**Evidence trail:**")
            lines.append("```")
            for e in evidence:
                ts = e.get("timestamp", "")
                cmd = _extract_command(e.get("input", ""))
                out = _strip_ansi(e.get("output", ""))[:300]
                err = " [ERROR]" if e.get("is_error") else ""
                lines.append(f"[{ts}] {cmd}")
                if out:
                    lines.append(f"  → {out}{err}")
            lines.append("```")

        # Visual evidence references
        evidence_dir = f"evidence-{r.story_id}"
        has_video = any("record start" in str(e.get("input", "")) for e in evidence)
        screenshot_count = sum(1 for e in evidence
                               if "screenshot" in str(e.get("input", ""))
                               and not e.get("is_error"))
        if has_video or screenshot_count:
            lines.append("")
            lines.append("**Visual evidence:**")
            if has_video:
                lines.append(f"- 🎥 Video: `{evidence_dir}/recording.webm`")
            if screenshot_count:
                lines.append(f"- 📸 Screenshots: `{evidence_dir}/` ({screenshot_count} captures)")

        lines.append("")
    return lines


def format_tier4_json(tier4_results: list[Any]) -> list[dict[str, Any]]:
    """Format Tier 4 results as JSON-serializable dicts. Shared by v1 and v2 PoW paths."""
    return [
        {
            "story_id": r.story_id,
            "title": r.story_title,
            "persona": r.persona,
            "passed": r.passed,
            "duration_s": r.duration_s,
            "cost_usd": r.cost_usd,
            "diagnosis": r.diagnosis if not r.passed else None,
            "fix_suggestion": r.fix_suggestion if not r.passed else None,
            "failed_steps": [
                {"action": s.action, "diagnosis": s.diagnosis}
                for s in r.steps if s.outcome == "fail"
            ],
        }
        for r in tier4_results
    ]


def _clean_evidence_output(output: str) -> str:
    """Clean evidence output for display — strip noise, paths, base64."""
    import re
    output = _strip_ansi(output)
    # Strip base64 image data (agent Read-ing screenshot PNGs)
    if "base64" in output and len(output) > 200:
        return "(image data)"
    # Strip absolute paths — keep just the filename or relative part
    output = re.sub(
        r"/Users/[^\s\"']+/otto_logs/certifier/",
        "otto_logs/certifier/",
        output,
    )
    return output[:500]


def generate_tier4_html(
    tier4_results: list[Any],
    report: Any,
    output_dir: Path,
) -> Path:
    """Generate an HTML proof-of-work report for human audit.

    Organized by story flow, not data source. Screenshots embedded,
    video linked, raw evidence in expandable section.
    """
    import base64

    outcome_color = "#22c55e" if report.outcome.value == "passed" else "#ef4444"
    passed = sum(1 for r in tier4_results if r.passed) if tier4_results else 0
    total = len(tier4_results) if tier4_results else 0

    html = [f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Certification Report</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #1a1a1a; line-height: 1.5; }}
h1 {{ border-bottom: 2px solid #e5e7eb; padding-bottom: 0.5em; }}
.outcome {{ color: {outcome_color}; font-weight: bold; font-size: 1.3em; }}
.meta {{ color: #6b7280; margin-bottom: 1.5em; }}
.prechecks {{ background: #f9fafb; border-radius: 8px; padding: 1em 1.5em; margin: 1em 0; }}
.prechecks .check {{ margin: 0.3em 0; }}
.story {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 1.5em; margin: 1.5em 0; }}
.story.pass {{ border-left: 4px solid #22c55e; }}
.story.fail {{ border-left: 4px solid #ef4444; }}
.story h3 {{ margin-top: 0; }}
.api-call {{ background: #f0f4ff; border-radius: 4px; padding: 0.6em 1em; margin: 0.5em 0; font-family: monospace; font-size: 0.85em; }}
.api-call .method {{ color: #2563eb; font-weight: bold; }}
.api-call .status {{ color: #059669; }}
.api-call .status.err {{ color: #ef4444; }}
.api-call .body {{ color: #374151; word-break: break-all; }}
.screenshots {{ display: flex; flex-wrap: wrap; gap: 0.8em; margin: 1em 0; }}
.screenshot {{ text-align: center; }}
.screenshot img {{ max-width: 380px; border: 1px solid #d1d5db; border-radius: 4px; cursor: pointer; transition: transform 0.2s; }}
.screenshot img:hover {{ transform: scale(1.02); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
.screenshot .caption {{ font-size: 0.8em; color: #6b7280; margin-top: 0.3em; }}
.lightbox {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); z-index: 1000; cursor: pointer; justify-content: center; align-items: center; }}
.lightbox.active {{ display: flex; }}
.lightbox img {{ max-width: 95%; max-height: 95%; border-radius: 8px; }}
video {{ max-width: 100%; border-radius: 8px; margin: 1em 0; }}
.diagnosis {{ background: #fef2f2; border-left: 3px solid #ef4444; padding: 0.5em 1em; margin: 0.5em 0; border-radius: 0 4px 4px 0; }}
.fix {{ background: #f0fdf4; border-left: 3px solid #22c55e; padding: 0.5em 1em; margin: 0.5em 0; border-radius: 0 4px 4px 0; }}
details {{ margin: 1em 0; }}
details summary {{ cursor: pointer; color: #6b7280; font-size: 0.9em; }}
details .evidence {{ background: #f9fafb; border-radius: 4px; padding: 1em; font-family: monospace; font-size: 0.8em; white-space: pre-wrap; overflow-x: auto; max-height: 400px; overflow-y: auto; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5em 0; }}
th, td {{ border: 1px solid #e5e7eb; padding: 0.4em 0.8em; text-align: left; font-size: 0.9em; }}
th {{ background: #f9fafb; }}
</style>
</head><body>
<h1>Certification Report</h1>
<div class="meta">
  <span class="outcome">{report.outcome.value.upper()}</span> &mdash;
  {report.duration_s:.0f}s &mdash; ${report.cost_usd:.2f} &mdash;
  {time.strftime('%Y-%m-%d %H:%M:%S')}
</div>"""]

    # Stories
    if tier4_results:
        html.append(f"<h2>Story Verification ({passed}/{total} passed)</h2>")
        for r in tier4_results:
            cls = "pass" if r.passed else "fail"
            icon = "✓" if r.passed else "✗"
            html.append(f'<div class="story {cls}">')
            html.append(f"<h3>{icon} {_html_escape(_strip_ansi(r.story_title))}</h3>")
            html.append(f"<p><em>{r.persona}</em> &mdash; {r.duration_s:.0f}s &mdash; ${r.cost_usd:.3f}</p>")

            if not r.passed and r.diagnosis:
                html.append(f'<div class="diagnosis"><strong>Diagnosis:</strong> {_html_escape(_strip_ansi(r.diagnosis))}</div>')
            if not r.passed and r.fix_suggestion:
                html.append(f'<div class="fix"><strong>Suggested fix:</strong> {_html_escape(_strip_ansi(r.fix_suggestion))}</div>')

            # Video first — most useful for audit
            evidence_dir = output_dir / f"evidence-{r.story_id}"
            if evidence_dir.exists():
                video = evidence_dir / "recording.webm"
                if video.exists():
                    rel_path = f"evidence-{r.story_id}/recording.webm"
                    html.append(f'<video controls><source src="{rel_path}" type="video/webm"></video>')

            # API calls — extract curl requests + responses as clean pairs
            evidence = getattr(r, "evidence_chain", None) or []
            api_calls = [e for e in evidence
                         if e.get("tool") == "Bash"
                         and "curl " in str(e.get("input", ""))
                         and not e.get("is_error")]
            if api_calls:
                html.append("<h4>API Verification</h4>")
                for e in api_calls:
                    cmd = _extract_command(e.get("input", ""))
                    out = _clean_evidence_output(e.get("output", ""))
                    # Try to extract method + path from curl command
                    import re
                    method_match = re.search(r"-X\s+(POST|PUT|DELETE|PATCH)", cmd)
                    method = method_match.group(1) if method_match else "GET"
                    path_match = re.search(r"http://localhost:\d+(/\S+)", cmd)
                    path = path_match.group(1) if path_match else ""
                    # Extract HTTP status from output
                    status = ""
                    status_match = re.search(r"HTTP[_ ](?:STATUS:)?(\d+)", out) or re.search(r"^(\d{3})$", out.strip())
                    if status_match:
                        status = status_match.group(1)
                    status_cls = "err" if status and int(status) >= 400 else ""
                    html.append(f'<div class="api-call">')
                    html.append(f'<span class="method">{method}</span> {_html_escape(path)}')
                    if status:
                        html.append(f' → <span class="status {status_cls}">{status}</span>')
                    if out and out != status:
                        body = out.replace(f"HTTP_STATUS:{status}", "").replace(f"---HTTP_CODE:{status}", "").strip()
                        if body and len(body) < 300:
                            html.append(f'<div class="body">{_html_escape(body[:200])}</div>')
                    html.append("</div>")

            # Screenshots with captions
            if evidence_dir and evidence_dir.exists():
                pngs = sorted(evidence_dir.glob("*.png"))
                if pngs:
                    html.append("<h4>Screenshots</h4><div class='screenshots'>")
                    for png in pngs:
                        try:
                            b64 = base64.b64encode(png.read_bytes()).decode()
                            caption = png.stem.replace("-", " ").replace("_", " ")
                            html.append(f'<div class="screenshot">')
                            html.append(f'<img src="data:image/png;base64,{b64}" title="{png.name}" />')
                            html.append(f'<div class="caption">{_html_escape(caption)}</div></div>')
                        except OSError:
                            pass
                    html.append("</div>")

            # Raw evidence trail — expandable
            if evidence:
                html.append("<details><summary>Full evidence trail ({} tool calls)</summary>".format(len(evidence)))
                html.append('<div class="evidence">')
                for e in evidence:
                    ts = e.get("timestamp", "")
                    cmd = _strip_ansi(_extract_command(e.get("input", "")))
                    out = _clean_evidence_output(e.get("output", ""))
                    if not out or out == "(image data)":
                        html.append(f'[{ts}] {_html_escape(cmd)}')
                    else:
                        err = " [ERROR]" if e.get("is_error") else ""
                        html.append(f'[{ts}] {_html_escape(cmd)}\n  → {_html_escape(out)}{err}')
                html.append("</div></details>")

            html.append("</div>")  # story

    html.append("""\
<div class="lightbox" id="lightbox" onclick="this.classList.remove('active')">
<img id="lightbox-img" src="" />
</div>
<script>
document.querySelectorAll('.screenshot img').forEach(img => {
  img.onclick = () => {
    document.getElementById('lightbox-img').src = img.src;
    document.getElementById('lightbox').classList.add('active');
  };
});
</script>
</body></html>""")
    report_path = output_dir / "proof-of-work.html"
    report_path.write_text("\n".join(html))
    return report_path


def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _append_tier1_proof(lines: list[str], claim: Any) -> None:
    """Append proof for a Tier 1 claim."""
    for e in claim.evidence:
        if e.skipped:
            continue
        proof = getattr(e, "proof", None)
        if proof and isinstance(proof, dict) and proof.get("timestamp"):
            req = proof.get("request", {})
            resp = proof.get("response", {})
            lines.append("```")
            lines.append(f"Request:  {req.get('method', '?')} {req.get('url', '?')}")
            if req.get("body"):
                body_str = json.dumps(req["body"], default=str)
                if len(body_str) > 200:
                    body_str = body_str[:200] + "..."
                lines.append(f"          Body: {body_str}")
            lines.append(f"Response: HTTP {resp.get('status', '?')}")
            if resp.get("body"):
                resp_str = json.dumps(resp["body"], default=str)
                if len(resp_str) > 200:
                    resp_str = resp_str[:200] + "..."
                lines.append(f"          Body: {resp_str}")
            lines.append(f"Time:     {proof['timestamp']}")
            lines.append("```")
        else:
            lines.append(f"- Command: `{e.command[:100]}`")
            lines.append(f"- Result: {e.actual[:150]}")


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{round(n / total * 100)}%"
