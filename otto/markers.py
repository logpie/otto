"""Parse structured markers from certifier agent output.

Both the standalone certifier (certifier/__init__.py) and the build pipeline
(pipeline.py) need to extract STORY_RESULT, VERDICT, DIAGNOSIS, etc. from
agent text. This module is the single source of truth for that parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Template placeholder story IDs that should be skipped
_PLACEHOLDER_IDS = {"", "(id)", "<story_id>", "<id>", "id"}
_STORY_RESULT_RE = re.compile(
    r"^STORY_RESULT:\s*(.*?)\s*\|\s*(PASS|FAIL|WARN|SKIPPED|FLAG_FOR_HUMAN)\s*\|\s*(.*)$"
)
_VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|FAIL)\s*$")
_ALL_CAPS_MARKER_RE = re.compile(r"^[A-Z][A-Z0-9_ ]*:\s*.*$")


@dataclass
class ParsedMarkers:
    """Parsed results from certifier agent text output."""
    stories: list[dict[str, Any]] = field(default_factory=list)
    stories_tested: int = 0
    stories_passed: int = 0
    verdict_pass: bool = False
    verdict_seen: bool = False
    diagnosis: str = ""
    certify_rounds: list[dict[str, Any]] = field(default_factory=list)
    # Target mode metrics
    metric_value: str = ""
    metric_met: bool | None = None  # None = not a target run
    coverage_observed: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    coverage_observed_emitted: bool = False
    coverage_gaps_emitted: bool = False


class MalformedCertifierOutputError(RuntimeError):
    """Raised when the certifier finishes without any structured markers."""


def _parse_diagnosis(raw: str) -> str:
    """Treat placeholder-only diagnosis values as empty."""
    diag = raw.strip()
    if diag.lower() in {"null", "none", "n/a"}:
        return ""
    return diag


def _parse_observed_steps(raw: str) -> list[str]:
    """Parse semicolon/newline separated observed steps into a list."""
    if not raw:
        return []
    parts = [
        step.strip(" -")
        for step in re.split(r"[;\n]+", raw)
        if step.strip(" -")
    ]
    return parts


_STRUCTURED_STORY_FIELDS = {
    "claim",
    "observed_steps",
    "observed_result",
    "surface",
    "methodology",
    "interaction_method",
    "key_finding",
    "summary",
    "evidence",
    "failure_evidence",
}


def _parse_story_result_fields(raw: str) -> tuple[str, dict[str, str]]:
    """Parse STORY_RESULT fields from pipe-separated parts.

    Backward-compatible with the legacy 3-field format:
    ``STORY_RESULT: id | PASS | one-line summary``

    New structured fields are passed as ``key=value`` segments after verdict.
    We only split on `` | `` when the following segment starts with a known
    field name, so legacy summaries containing pipes survive unchanged.
    """
    summary = raw.strip()
    fields: dict[str, str] = {}
    if not raw:
        return "", fields
    parts = re.split(r"\s+\|\s+(?=[A-Za-z_][A-Za-z_ ]*=)", raw.strip())
    if parts:
        first = parts[0].strip()
        if "=" in first:
            key, value = first.split("=", 1)
            norm_key = key.strip().lower().replace(" ", "_")
            if norm_key in _STRUCTURED_STORY_FIELDS:
                fields[norm_key] = value.strip()
                summary = ""
        else:
            summary = first
    for part in parts[1:]:
        item = part.strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            norm_key = key.strip().lower().replace(" ", "_")
            if norm_key in _STRUCTURED_STORY_FIELDS:
                fields[norm_key] = value.strip()
                continue
        if summary:
            summary = f"{summary} | {item}"
        else:
            summary = item
    summary = fields.get("summary", "") or summary
    return summary, fields


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
    match = _STORY_RESULT_RE.match(stripped)
    if match is None:
        return None
    sid = match.group(1).strip()
    if sid in _PLACEHOLDER_IDS:
        return None
    verdict_raw = match.group(2).strip().upper()
    if verdict_raw == "SKIPPED":
        verdict = "SKIPPED"
        passed = False
        is_warn = False
    elif verdict_raw == "FLAG_FOR_HUMAN":
        verdict = "FLAG_FOR_HUMAN"
        passed = False
        is_warn = False
    else:
        verdict = verdict_raw
        is_warn = verdict_raw == "WARN"
        passed = verdict_raw in {"PASS", "WARN"}
    summary, fields = _parse_story_result_fields(match.group(3))
    claim = fields.get("claim", "") or summary
    observed_result = fields.get("observed_result", "") or summary
    observed_steps = _parse_observed_steps(fields.get("observed_steps", ""))
    surface = fields.get("surface", "")
    methodology = fields.get("methodology", "") or fields.get("interaction_method", "")
    key_finding = fields.get("key_finding", "")
    story: dict[str, Any] = {
        "story_id": sid,
        "verdict": verdict,
        "passed": passed,
        "summary": summary,
        "claim": claim,
        "observed_result": observed_result,
    }
    if observed_steps:
        story["observed_steps"] = observed_steps
    if surface:
        story["surface"] = surface
    if methodology:
        story["methodology"] = methodology
        story["interaction_method"] = methodology
    if key_finding:
        story["key_finding"] = key_finding
    if is_warn:
        story["warn"] = True
    ev = evidence.get(sid, "") or fields.get("evidence", "")
    if ev:
        story["evidence"] = ev
    failure_evidence = fields.get("failure_evidence", "")
    if failure_evidence:
        story["failure_evidence"] = failure_evidence
    return story


def compact_story_result(story: dict[str, Any]) -> dict[str, Any]:
    """Drop default-valued optional fields from serialized story payloads."""
    compact = dict(story)
    if not compact.get("warn"):
        compact.pop("warn", None)
    if not compact.get("evidence"):
        compact.pop("evidence", None)
    if not compact.get("observed_steps"):
        compact.pop("observed_steps", None)
    if not compact.get("surface"):
        compact.pop("surface", None)
    if not compact.get("methodology"):
        compact.pop("methodology", None)
    if not compact.get("interaction_method"):
        compact.pop("interaction_method", None)
    if not compact.get("key_finding"):
        compact.pop("key_finding", None)
    if not compact.get("claim"):
        compact.pop("claim", None)
    if not compact.get("observed_result"):
        compact.pop("observed_result", None)
    if not compact.get("failure_evidence"):
        compact.pop("failure_evidence", None)
    return compact


def compact_story_results(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply ``compact_story_result`` across a story list."""
    return [compact_story_result(story) for story in stories]


def _extract_evidence(text: str) -> dict[str, str]:
    """Extract STORY_EVIDENCE_START/END blocks from text."""
    evidence: dict[str, str] = {}
    current_id: str | None = None
    lines: list[str] = []

    fence_char = ""
    fence_len = 0
    in_frontmatter = False
    at_document_start = True
    for line in text.splitlines():
        stripped = line.strip()

        if current_id is not None:
            if stripped.startswith("STORY_EVIDENCE_END:"):
                evidence[current_id] = "\n".join(lines)
                current_id = None
                lines = []
            else:
                lines.append(line)
            continue

        if line.startswith("    ") or line.startswith("\t"):
            continue
        if at_document_start and stripped == "---":
            in_frontmatter = True
            at_document_start = False
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if not stripped:
            continue
        at_document_start = False
        if stripped.startswith(">"):
            continue

        match = re.match(r"^([`~]{3,})(.*)$", stripped)
        if match:
            marker = match.group(1)
            char = marker[0]
            if not fence_char:
                fence_char = char
                fence_len = len(marker)
                continue
            if char == fence_char and len(marker) >= fence_len:
                fence_char = ""
                fence_len = 0
            continue
        if fence_char:
            continue

        if stripped.startswith("STORY_EVIDENCE_START:"):
            current_id = stripped.split(":", 1)[1].strip()
            lines = []
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
    for line in reversed(list(_iter_marker_lines(text))):
        stripped = line.strip()
        verdict_match = _VERDICT_RE.match(stripped)
        if verdict_match and not found_verdict:
            verdict_pass = verdict_match.group(1) == "PASS"
            found_verdict = True
        elif stripped.startswith("DIAGNOSIS:") and not diagnosis:
            diag = _parse_diagnosis(stripped[len("DIAGNOSIS:"):])
            if diag:
                diagnosis = diag
        if found_verdict and diagnosis:
            break
    return verdict_pass, diagnosis


def _iter_marker_lines(text: str):
    """Yield lines that are eligible for marker parsing.

    Skips fenced code blocks (``` / ~~~) and Markdown indented code blocks.
    """
    fence_char = ""
    fence_len = 0
    in_frontmatter = False
    at_document_start = True
    for line in text.splitlines():
        if line.startswith("    ") or line.startswith("\t"):
            continue

        stripped = line.strip()
        if at_document_start and stripped == "---":
            in_frontmatter = True
            at_document_start = False
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if not stripped:
            if not fence_char:
                yield line
            continue
        at_document_start = False
        if stripped.startswith(">"):
            continue

        match = re.match(r"^([`~]{3,})(.*)$", stripped)
        if match:
            marker = match.group(1)
            char = marker[0]
            if not fence_char:
                fence_char = char
                fence_len = len(marker)
                continue
            if char == fence_char and len(marker) >= fence_len:
                fence_char = ""
                fence_len = 0
            continue

        if fence_char:
            continue
        yield line


def parse_certifier_markers(text: str, *, certifier_mode: str | None = None) -> ParsedMarkers:
    """Parse structured markers from certifier agent output.

    Handles both single-round output (standalone certifier) and multi-round
    output (build agent with CERTIFY_ROUND markers).

    Returns a ParsedMarkers with stories deduplicated and the final round's
    results as the top-level fields.
    """
    if not text:
        return ParsedMarkers()

    evidence = _extract_evidence(text)
    normalized_mode = str(certifier_mode or "").strip().lower()

    # Parse per-round blocks. Each CERTIFY_ROUND starts a new round.
    certify_rounds: list[dict[str, Any]] = []
    current_round: dict[str, Any] = {
        "round": 1,
        "stories": [],
        "verdict": None,
        "diagnosis": "",
        "explicit_round": False,
        "coverage_observed": [],
        "coverage_gaps": [],
        "coverage_observed_emitted": False,
        "coverage_gaps_emitted": False,
    }
    active_coverage_block: str | None = None

    for line in _iter_marker_lines(text):
        stripped = line.strip()

        if active_coverage_block is not None:
            if not stripped:
                active_coverage_block = None
                continue
            if _ALL_CAPS_MARKER_RE.match(stripped):
                active_coverage_block = None
            elif stripped.startswith("- "):
                current_round.setdefault(active_coverage_block, []).append(
                    stripped[2:].strip()
                )
                continue
            else:
                continue

        if stripped.startswith("CERTIFY_ROUND:"):
            should_append_current = (
                current_round["stories"]
                or current_round["verdict"] is not None
                or current_round.get("diagnosis")
                or current_round.get("metric_value")
                or "metric_met" in current_round
                or current_round.get("coverage_observed_emitted")
                or current_round.get("coverage_gaps_emitted")
            )
            if (
                should_append_current
                and (
                    current_round.get("explicit_round")
                    or certify_rounds
                    or current_round["stories"]
                    or current_round["verdict"] is not None
                    or current_round.get("diagnosis")
                    or current_round.get("coverage_observed_emitted")
                    or current_round.get("coverage_gaps_emitted")
                )
            ):
                certify_rounds.append(current_round)
            try:
                rn = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                rn = len(certify_rounds) + 1
            if certify_rounds and rn < int(certify_rounds[-1]["round"]):
                raise ValueError(
                    f"Non-monotonic CERTIFY_ROUND sequence: {rn} after {certify_rounds[-1]['round']}"
                )
            current_round = {
                "round": rn,
                "stories": [],
                "verdict": None,
                "diagnosis": "",
                "explicit_round": True,
                "coverage_observed": [],
                "coverage_gaps": [],
                "coverage_observed_emitted": False,
                "coverage_gaps_emitted": False,
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

        elif _STORY_RESULT_RE.match(stripped):
            story = _parse_story_result(stripped, evidence)
            if story:
                current_round["stories"].append(story)

        elif _VERDICT_RE.match(stripped):
            current_round["verdict"] = _VERDICT_RE.match(stripped).group(1) == "PASS"

        elif stripped.startswith("DIAGNOSIS:"):
            diag = _parse_diagnosis(stripped[len("DIAGNOSIS:"):])
            current_round["diagnosis"] = diag

        elif stripped == "COVERAGE_OBSERVED:":
            current_round["coverage_observed_emitted"] = True
            active_coverage_block = "coverage_observed"
        elif stripped == "COVERAGE_GAPS:":
            current_round["coverage_gaps_emitted"] = True
            active_coverage_block = "coverage_gaps"
        elif stripped.startswith("METRIC_VALUE:"):
            current_round["metric_value"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("METRIC_MET:"):
            raw_metric = stripped.split(":", 1)[1].strip().upper()
            if raw_metric == "YES":
                current_round["metric_met"] = True
            elif raw_metric == "NO":
                current_round["metric_met"] = False

    # Save last round
    if (
        current_round["stories"]
        or current_round["verdict"] is not None
        or current_round.get("diagnosis")
        or current_round.get("metric_value")
        or "metric_met" in current_round
        or current_round.get("coverage_observed_emitted")
        or current_round.get("coverage_gaps_emitted")
    ):
        certify_rounds.append(current_round)

    # Determine final results from the last round with stories
    result = ParsedMarkers(certify_rounds=certify_rounds)

    final_round = None
    round_candidates = [
        (idx, round_data) for idx, round_data in enumerate(certify_rounds)
        if round_data["stories"]
    ]
    if round_candidates:
        _, final_round = max(round_candidates, key=lambda item: (int(item[1]["round"]), item[0]))

    if final_round is None and certify_rounds and not any(
        r.get("explicit_round") for r in certify_rounds
    ):
        final_round = certify_rounds[-1]

    # Extract metric fields from the last round that has either metric marker.
    for r in reversed(certify_rounds):
        if "metric_met" in r or r.get("metric_value"):
            result.metric_value = r.get("metric_value", "") or ""
            result.metric_met = r.get("metric_met")
            break

    if final_round:
        result.stories = _dedup_stories(final_round["stories"])
        deduped_tested = len(result.stories)
        deduped_passed = sum(1 for s in result.stories if s["passed"])
        explicit_tested = final_round.get("tested")
        explicit_passed = final_round.get("passed_count")
        result.stories_tested = (
            deduped_tested
            if explicit_tested not in (None, deduped_tested)
            else explicit_tested
        )
        result.stories_passed = (
            deduped_passed
            if explicit_passed not in (None, deduped_passed)
            else explicit_passed
        )
        result.verdict_pass = bool(final_round.get("verdict", False))
        result.verdict_seen = final_round.get("verdict") is not None
        result.diagnosis = final_round.get("diagnosis", "")
        result.coverage_observed = list(final_round.get("coverage_observed", []) or [])
        result.coverage_gaps = list(final_round.get("coverage_gaps", []) or [])
        result.coverage_observed_emitted = bool(final_round.get("coverage_observed_emitted"))
        result.coverage_gaps_emitted = bool(final_round.get("coverage_gaps_emitted"))
    elif len(certify_rounds) == 0:
        # Fallback: no CERTIFY_ROUND markers — scan flat output
        result.verdict_pass, result.diagnosis = _parse_verdict_from_end(text)
        result.verdict_seen = any(_VERDICT_RE.match(line.strip()) for line in _iter_marker_lines(text))

        # Extract stories from flat output (dedup by story_id)
        flat_stories: list[dict[str, Any]] = []
        active_coverage_block = None
        for line in _iter_marker_lines(text):
            stripped = line.strip()
            if active_coverage_block is not None:
                if not stripped:
                    active_coverage_block = None
                    continue
                if _ALL_CAPS_MARKER_RE.match(stripped):
                    active_coverage_block = None
                elif stripped.startswith("- "):
                    if active_coverage_block == "coverage_observed":
                        result.coverage_observed.append(stripped[2:].strip())
                    else:
                        result.coverage_gaps.append(stripped[2:].strip())
                    continue
                else:
                    continue
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
            elif stripped == "COVERAGE_OBSERVED:":
                result.coverage_observed_emitted = True
                active_coverage_block = "coverage_observed"
            elif stripped == "COVERAGE_GAPS:":
                result.coverage_gaps_emitted = True
                active_coverage_block = "coverage_gaps"
            elif _STORY_RESULT_RE.match(stripped):
                story = _parse_story_result(stripped, evidence)
                if story:
                    flat_stories.append(story)
        result.stories = _dedup_stories(flat_stories)
        deduped_tested = len(result.stories)
        deduped_passed = sum(1 for story in result.stories if story.get("passed"))
        if result.stories:
            if result.stories_tested != deduped_tested:
                result.stories_tested = deduped_tested
            if result.stories_passed != deduped_passed:
                result.stories_passed = deduped_passed
        if result.stories or result.stories_tested or result.diagnosis or result.metric_value or result.metric_met is not None:
            result.certify_rounds = [{
                "round": 1,
                "stories": result.stories,
                "verdict": result.verdict_pass,
                "diagnosis": result.diagnosis,
                "tested": result.stories_tested or deduped_tested,
                "passed_count": result.stories_passed or deduped_passed,
                "metric_value": result.metric_value,
                "metric_met": result.metric_met,
                "coverage_observed": result.coverage_observed,
                "coverage_gaps": result.coverage_gaps,
                "coverage_observed_emitted": result.coverage_observed_emitted,
                "coverage_gaps_emitted": result.coverage_gaps_emitted,
            }]
    else:
        for round_data in reversed(certify_rounds):
            if round_data.get("verdict") is not None:
                result.verdict_seen = True
                break

    if not result.coverage_observed_emitted and not result.coverage_gaps_emitted:
        for round_data in reversed(certify_rounds):
            if (
                round_data.get("coverage_observed_emitted")
                or round_data.get("coverage_gaps_emitted")
            ):
                result.coverage_observed = list(
                    round_data.get("coverage_observed", []) or []
                )
                result.coverage_gaps = list(round_data.get("coverage_gaps", []) or [])
                result.coverage_observed_emitted = bool(
                    round_data.get("coverage_observed_emitted")
                )
                result.coverage_gaps_emitted = bool(
                    round_data.get("coverage_gaps_emitted")
                )
                break

    if normalized_mode in {"standard", "thorough"} and result.stories:
        if not result.coverage_observed_emitted or not result.coverage_gaps_emitted:
            raise MalformedCertifierOutputError(
                "Certifier omitted required COVERAGE_OBSERVED/COVERAGE_GAPS markers"
            )

    return result
