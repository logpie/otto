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
    stories_tested: int = 0
    stories_passed: int = 0
    verdict_pass: bool = False
    diagnosis: str = ""
    certify_rounds: list[dict[str, Any]] = field(default_factory=list)
    # Target mode metrics
    metric_value: str = ""
    metric_met: bool | None = None  # None = not a target run


def _parse_diagnosis(raw: str) -> str:
    """Strip leading 'null' from DIAGNOSIS value."""
    diag = raw.strip()
    if diag.lower().startswith("null"):
        diag = diag[4:].strip()
    return diag


def _parse_story_result(stripped: str, evidence: dict[str, str]) -> dict[str, Any] | None:
    """Parse a single STORY_RESULT: line. Returns None if placeholder.

    Verdict tokens: PASS, FAIL, WARN, SKIPPED, FLAG_FOR_HUMAN.
      - PASS / FAIL — standard pass/fail signal.
      - WARN — non-blocking advisory (e.g. scope-creep during build). Sets
        ``passed=True`` and surfaces ``warn=True`` so the UI can flag it.
      - SKIPPED — merge cert path when a story has no overlap with the
        merge diff.
      - FLAG_FOR_HUMAN — merge cert path when a story is genuinely
        contradicted across merged branches.
    Neither SKIPPED nor FLAG_FOR_HUMAN counts as a regression.

    `passed` stays as a backward-compat boolean (True for PASS or WARN).
    Callers that need the full picture should read `verdict` and `warn`.

    Only emits the `evidence` key when the agent actually produced a
    STORY_EVIDENCE block for this story — otherwise the field is omitted
    so consumers that check ``story.get("evidence")`` get ``None``
    (missing), not an empty string that reads like intentional absence.
    """
    parts = stripped[len("STORY_RESULT:"):].strip().split("|", 2)
    if len(parts) < 2:
        return None
    sid = parts[0].strip()
    if sid in _PLACEHOLDER_IDS:
        return None
    verdict_text = parts[1].strip().upper()
    if "FLAG_FOR_HUMAN" in verdict_text:
        verdict = "FLAG_FOR_HUMAN"
    elif "SKIPPED" in verdict_text:
        verdict = "SKIPPED"
    elif "WARN" in verdict_text:
        verdict = "WARN"
    elif "PASS" in verdict_text:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    summary = parts[2].strip() if len(parts) > 2 else ""
    # `passed` = backward-compat boolean. WARN counts as passed (non-blocking).
    story: dict[str, Any] = {
        "story_id": sid,
        "verdict": verdict,
        "passed": verdict in ("PASS", "WARN"),
        "summary": summary,
    }
    if verdict == "WARN":
        story["warn"] = True
    ev = evidence.get(sid, "")
    if ev:
        story["evidence"] = ev
    return story


def compact_story_result(story: dict[str, Any]) -> dict[str, Any]:
    """Drop default-valued optional fields from serialized story payloads."""
    compact = dict(story)
    if not compact.get("warn"):
        compact.pop("warn", None)
    if not compact.get("evidence"):
        compact.pop("evidence", None)
    return compact


def compact_story_results(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply ``compact_story_result`` across a story list."""
    return [compact_story_result(story) for story in stories]


def _is_template_verdict(verdict_text: str) -> bool:
    """Check if VERDICT line is a template placeholder like 'PASS or FAIL'."""
    return " or " in verdict_text.lower()


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

        elif stripped.startswith("METRIC_VALUE:"):
            current_round["metric_value"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("METRIC_MET:"):
            current_round["metric_met"] = stripped.split(":", 1)[1].strip().upper() == "YES"

    # Save last round
    if current_round["stories"] or current_round["verdict"] is not None:
        certify_rounds.append(current_round)

    # Determine final results from the last round with stories
    result = ParsedMarkers(certify_rounds=certify_rounds)

    final_round = None
    for r in reversed(certify_rounds):
        if r["stories"]:
            final_round = r
            break

    # Extract metric fields from the last round that has them
    for r in reversed(certify_rounds):
        if r.get("metric_value"):
            result.metric_value = r["metric_value"]
            result.metric_met = r.get("metric_met")
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
    elif len(certify_rounds) == 0:
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
            elif stripped.startswith("METRIC_VALUE:"):
                result.metric_value = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("METRIC_MET:"):
                result.metric_met = stripped.split(":", 1)[1].strip().upper() == "YES"
            elif stripped.startswith("STORY_RESULT:"):
                story = _parse_story_result(stripped, evidence)
                if story:
                    flat_stories.append(story)
        result.stories = _dedup_stories(flat_stories)

    return result
