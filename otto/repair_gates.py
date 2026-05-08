"""Repair-gate policy for audit findings.

Layer 2 repair should spend agent retries only on reproducible product
failures. Proof gaps, weak visual-only observations, and curl-only checks for
browser UI surfaces should stay visible to the user without automatically
dispatching code repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RepairGateDecision:
    action: str
    actionable: bool
    reason: str


REPAIR_NOW = "repair_now"
PROOF_GAP_ONLY = "proof_gap_only"
NEEDS_BROWSER_REPRO = "needs_browser_repro"
NO_REPAIR = "no_repair"

_UI_SURFACE_TERMS = {
    "page",
    "dom",
    "dashboard",
    "filter",
    "link",
    "form",
    "button",
    "modal",
    "browser",
    "navigation",
    "screenshot",
    "video",
}

_HARD_FAILURE_TERMS = {
    "crash",
    "exception",
    "traceback",
    "500",
    "404",
    "cannot",
    "can't",
    "does not",
    "doesn't",
    "missing",
    "not implemented",
    "broken",
    "fails",
    "failed",
    "data loss",
    "lost",
    "corrupt",
    "clipped",
    "overlap",
    "overflow",
    "unreachable",
    "hidden",
    "disabled",
}

_VISUAL_MEASURABLE_FAILURE_TERMS = {
    "clipped",
    "overlap",
    "overflow",
    "unreachable",
    "hidden",
    "disabled",
}


def repair_gate_for_verdict(verdict_payload: dict[str, Any]) -> RepairGateDecision:
    """Classify whether an audit feature verdict should trigger repair."""
    verdict = _clean(verdict_payload.get("verdict"))
    if verdict in {"", "passed"}:
        return RepairGateDecision(NO_REPAIR, False, "feature already passed")

    detail = _clean(verdict_payload.get("detail"))
    methodology = _clean(verdict_payload.get("methodology"))
    surface = _clean(verdict_payload.get("surface"))
    evidence_completeness = _clean(verdict_payload.get("evidence_completeness"))
    coverage_confidence = _clean(verdict_payload.get("coverage_confidence"))
    refs = " ".join(str(ref).casefold() for ref in verdict_payload.get("evidence_refs") or [])

    if not detail and not refs:
        return RepairGateDecision(PROOF_GAP_ONLY, False, "audit verdict has no concrete evidence")

    if evidence_completeness in {"partial", "proxy_only"}:
        return RepairGateDecision(
            NEEDS_BROWSER_REPRO,
            False,
            f"evidence completeness is {evidence_completeness}; collect stronger proof before repair",
        )
    if coverage_confidence == "low":
        return RepairGateDecision(
            NEEDS_BROWSER_REPRO,
            False,
            "coverage confidence is low; collect reproducible evidence before repair",
        )

    if methodology == "visual-only" and not _has_visual_measurable_failure(detail, refs):
        return RepairGateDecision(
            NEEDS_BROWSER_REPRO,
            False,
            "visual-only evidence is not enough to trigger product repair",
        )

    if methodology == "http-request" and _looks_like_browser_ui_surface(surface, detail):
        return RepairGateDecision(
            NEEDS_BROWSER_REPRO,
            False,
            "curl/http evidence is not enough for a browser UI story",
        )

    if methodology in {"source-review", "other"} and not _has_hard_failure_signal(detail, refs):
        return RepairGateDecision(
            PROOF_GAP_ONLY,
            False,
            f"{methodology} evidence lacks a reproducible product failure",
        )

    return RepairGateDecision(REPAIR_NOW, True, "audit evidence is actionable")


def _clean(value: Any) -> str:
    return str(value or "").strip().casefold()


def _has_hard_failure_signal(detail: str, refs: str) -> bool:
    haystack = f"{detail} {refs}"
    return any(term in haystack for term in _HARD_FAILURE_TERMS)


def _has_visual_measurable_failure(detail: str, refs: str) -> bool:
    haystack = f"{detail} {refs}"
    return any(term in haystack for term in _VISUAL_MEASURABLE_FAILURE_TERMS)


def _looks_like_browser_ui_surface(surface: str, detail: str) -> bool:
    haystack = f"{surface} {detail}"
    return any(term in haystack for term in _UI_SURFACE_TERMS)
