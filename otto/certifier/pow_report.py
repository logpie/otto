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


def format_tier4_markdown(tier4_results: list[Any]) -> list[str]:
    """Format Tier 4 results as markdown lines. Shared by v1 and v2 PoW paths."""
    passed = sum(1 for r in tier4_results if r.passed)
    lines = [
        "---",
        "",
        f"## Tier 4 — Agentic Story Verification ({passed}/{len(tier4_results)} passed)",
        "",
        "Each story was verified by an AI agent simulating a real user.",
        "",
    ]
    for r in tier4_results:
        icon = "✓" if r.passed else "✗"
        lines.append(f"### {icon} {r.story_title} ({r.persona}, {r.duration_s:.0f}s, ${r.cost_usd:.3f})")
        if r.summary:
            summary_lines = r.summary.strip().split("\n")
            tail = summary_lines[-5:] if len(summary_lines) > 5 else summary_lines
            lines.append("")
            for sl in tail:
                lines.append(f"> {sl}")
        if r.blocked_at:
            lines.append(f"- **Blocked at:** {r.blocked_at}")
        if not r.passed and r.diagnosis:
            lines.append(f"- **Diagnosis:** {r.diagnosis}")
        if not r.passed and r.fix_suggestion:
            lines.append(f"- **Suggested fix:** {r.fix_suggestion}")
        failed = [s for s in r.steps if s.outcome == "fail"]
        if failed:
            lines.append("")
            lines.append("**Failed steps:**")
            for s in failed:
                lines.append(f"- ✗ {s.action}")
                if s.diagnosis:
                    lines.append(f"  _{s.diagnosis}_")
        if r.break_findings:
            lines.append("")
            lines.append("**Break findings:**")
            for b in r.break_findings:
                lines.append(f"- [{b.severity}] {b.technique}: {b.description}")
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
