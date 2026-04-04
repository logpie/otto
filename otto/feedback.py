"""Format certifier findings as agent feedback.

Translates CertificationReport into natural language that looks like
user feedback to the coding agent. Non-actionable outcomes (BLOCKED,
INFRA_ERROR) return None — they should not be sent to the agent.
"""

from __future__ import annotations

from typing import Any


def format_certifier_as_feedback(report: Any) -> str | None:
    """Format certifier report as natural user feedback for the coding agent.

    Returns None for non-actionable outcomes (PASSED, BLOCKED, INFRA_ERROR).
    The agent only sees this when there are actionable product bugs to fix.
    """
    from otto.certifier.report import CertificationOutcome

    if report.outcome == CertificationOutcome.PASSED:
        return None
    if report.outcome == CertificationOutcome.BLOCKED:
        return None
    if hasattr(CertificationOutcome, "INFRA_ERROR") and report.outcome == CertificationOutcome.INFRA_ERROR:
        return None

    critical = report.critical_findings()
    if not critical:
        return None

    lines = ["A user tested your product and found these issues:\n"]
    for i, f in enumerate(critical, 1):
        lines.append(f"{i}. {f.description}")
        if f.diagnosis:
            lines.append(f"   What happened: {f.diagnosis}")
        if f.fix_suggestion:
            lines.append(f"   Suggested fix: {f.fix_suggestion}")
        lines.append("")

    warnings = report.break_findings()
    if warnings:
        lines.append("Quality warnings (edge cases found during testing):")
        for f in warnings:
            lines.append(f"- [{f.severity}] {f.description}")
        lines.append("")

    lines.append("Please fix these issues and let me know when you're done.")
    return "\n".join(lines)


def format_passed_with_warnings(report: Any) -> str | None:
    """Format a passing report that has warnings. Returns None if no warnings."""
    warnings = report.break_findings()
    if not warnings:
        return None

    lines = ["Your product passed all tests. However, some quality warnings were found:\n"]
    for f in warnings:
        lines.append(f"- [{f.severity}] {f.description}")
        if f.fix_suggestion:
            lines.append(f"  Suggested fix: {f.fix_suggestion}")
    lines.append("")
    lines.append("These are optional improvements. Fix them if you think they matter, or you can stop here.")
    return "\n".join(lines)


def finding_fingerprints(findings: list[Any]) -> set[str]:
    """Compute normalized root-cause fingerprints for no-progress detection.

    Same fingerprints across rounds = no progress.
    """
    fps = set()
    for f in findings:
        # Normalize: category + first 50 chars of description + story_id
        key = f"{f.category}:{f.description[:50]}:{f.story_id or ''}"
        fps.add(key)
    return fps
