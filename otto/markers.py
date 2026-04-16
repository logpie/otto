"""Parse structured markers from certifier agent output.

Both the standalone certifier (certifier/__init__.py) and the build pipeline
(pipeline.py) need to extract STORY_RESULT, VERDICT, DIAGNOSIS, etc. from
agent text. This module is the single source of truth for that parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Template placeholder story IDs that should be skipped
_PLACEHOLDER_IDS = {"", "(id)", "<story_id>", "<id>", "id"}


@dataclass
class ParsedMarkers:
    """Parsed results from certifier agent text output."""
    stories: list[dict[str, Any]] = field(default_factory=list)
    story_evidence: dict[str, str] = field(default_factory=dict)
    stories_tested: int = 0
    stories_passed: int = 0
    verdict_pass: bool = False
    diagnosis: str = ""
    certify_rounds: list[dict[str, Any]] = field(default_factory=list)


def _parse_diagnosis(raw: str) -> str:
    """Strip leading 'null' from DIAGNOSIS value."""
    diag = raw.strip()
    if diag.lower().startswith("null"):
        diag = diag[4:].strip()
    return diag


def _parse_story_result(stripped: str, evidence: dict[str, str]) -> dict[str, Any] | None:
    """Parse a single STORY_RESULT: line. Returns None if placeholder."""
    parts = stripped[len("STORY_RESULT:"):].strip().split("|", 2)
    if len(parts) < 2:
        return None
    sid = parts[0].strip()
    if sid in _PLACEHOLDER_IDS:
        return None
    passed = "PASS" in parts[1].upper()
    summary = parts[2].strip() if len(parts) > 2 else ""
    return {
        "story_id": sid,
        "passed": passed,
        "summary": summary,
        "evidence": evidence.get(sid, ""),
    }


def _is_template_verdict(verdict_text: str) -> bool:
    """Check if VERDICT line is a template placeholder like 'PASS or FAIL'."""
    return "or" in verdict_text.lower()


def _extract_evidence(text: str) -> dict[str, str]:
    """Extract STORY_EVIDENCE_START/END blocks from text."""
    evidence: dict[str, str] = {}
    current_id: str | None = None
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("STORY_EVIDENCE_START:"):
            current_id = stripped.split(":", 1)[1].strip()
            lines = []
        elif stripped.startswith("STORY_EVIDENCE_END:"):
            if current_id:
                evidence[current_id] = "\n".join(lines)
            current_id = None
            lines = []
        elif current_id is not None:
            lines.append(line)
    return evidence


def _dedup_stories(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate stories by story_id, keeping the last result per ID."""
    seen: dict[str, dict[str, Any]] = {}
    for s in stories:
        seen[s["story_id"]] = s
    return list(seen.values())


def _parse_verdict_from_end(text: str) -> tuple[bool, str]:
    """Scan from end of text for VERDICT and DIAGNOSIS lines.

    Returns (verdict_pass, diagnosis).
    """
    verdict_pass = False
    diagnosis = ""
    found_verdict = False
    for line in reversed(text.split("\n")):
        stripped = line.strip()
        if stripped.startswith("VERDICT:") and not found_verdict:
            verdict_text = stripped.split(":", 1)[1].strip()
            if _is_template_verdict(verdict_text):
                continue
            verdict_pass = "PASS" in stripped.upper()
            found_verdict = True
        elif stripped.startswith("DIAGNOSIS:") and not diagnosis:
            diag = _parse_diagnosis(stripped[len("DIAGNOSIS:"):])
            if diag:
                diagnosis = diag
        if found_verdict and diagnosis:
            break
    return verdict_pass, diagnosis


def parse_certifier_markers(text: str) -> ParsedMarkers:
    """Parse structured markers from certifier agent output.

    Handles both single-round output (standalone certifier) and multi-round
    output (build agent with CERTIFY_ROUND markers).

    Returns a ParsedMarkers with stories deduplicated and the final round's
    results as the top-level fields.
    """
    if not text:
        return ParsedMarkers()

    evidence = _extract_evidence(text)

    # Parse per-round blocks. Each CERTIFY_ROUND starts a new round.
    certify_rounds: list[dict[str, Any]] = []
    current_round: dict[str, Any] = {
        "round": 0, "stories": [], "verdict": None, "diagnosis": "",
    }

    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("CERTIFY_ROUND:"):
            if current_round["stories"] or current_round["verdict"] is not None:
                certify_rounds.append(current_round)
            try:
                rn = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                rn = len(certify_rounds) + 1
            current_round = {
                "round": rn, "stories": [], "verdict": None, "diagnosis": "",
            }

        elif stripped.startswith("STORIES_TESTED:"):
            try:
                current_round["tested"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass

        elif stripped.startswith("STORIES_PASSED:"):
            try:
                current_round["passed_count"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass

        elif stripped.startswith("STORY_RESULT:"):
            story = _parse_story_result(stripped, evidence)
            if story:
                current_round["stories"].append(story)

        elif stripped.startswith("VERDICT:"):
            verdict_text = stripped.split(":", 1)[1].strip()
            if not _is_template_verdict(verdict_text):
                current_round["verdict"] = "PASS" in stripped.upper()

        elif stripped.startswith("DIAGNOSIS:"):
            diag = _parse_diagnosis(stripped[len("DIAGNOSIS:"):])
            current_round["diagnosis"] = diag

    # Save last round
    if current_round["stories"] or current_round["verdict"] is not None:
        certify_rounds.append(current_round)

    # Determine final results from the last round with stories
    result = ParsedMarkers(story_evidence=evidence, certify_rounds=certify_rounds)

    final_round = None
    for r in reversed(certify_rounds):
        if r["stories"]:
            final_round = r
            break

    if final_round:
        result.stories = _dedup_stories(final_round["stories"])
        result.stories_tested = final_round.get(
            "tested", len(result.stories))
        result.stories_passed = final_round.get(
            "passed_count",
            sum(1 for s in result.stories if s["passed"]))
        result.verdict_pass = bool(final_round.get("verdict", False))
        result.diagnosis = final_round.get("diagnosis", "")
    else:
        # Fallback: no CERTIFY_ROUND markers — scan flat output
        result.verdict_pass, result.diagnosis = _parse_verdict_from_end(text)

        # Extract stories from flat output (dedup by story_id)
        flat_stories: list[dict[str, Any]] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("STORIES_TESTED:"):
                try:
                    result.stories_tested = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("STORIES_PASSED:"):
                try:
                    result.stories_passed = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("STORY_RESULT:"):
                story = _parse_story_result(stripped, evidence)
                if story:
                    flat_stories.append(story)
        result.stories = _dedup_stories(flat_stories)

    return result
