from __future__ import annotations

from otto.repair_gates import (
    NEEDS_BROWSER_REPRO,
    PROOF_GAP_ONLY,
    REPAIR_NOW,
    repair_gate_for_verdict,
)


def test_repair_gate_allows_live_ui_actionable_failure() -> None:
    decision = repair_gate_for_verdict(
        {
            "feature_id": "create-card",
            "verdict": "partial",
            "detail": "After clicking Add, the card does not appear.",
            "surface": "DOM",
            "methodology": "live-ui-events",
            "evidence_refs": ["walkthrough.jsonl#L3"],
        }
    )

    assert decision.action == REPAIR_NOW
    assert decision.actionable is True


def test_repair_gate_blocks_visual_only_weak_finding() -> None:
    decision = repair_gate_for_verdict(
        {
            "feature_id": "layout",
            "verdict": "partial",
            "detail": "The screenshot looks sparse.",
            "surface": "screenshot",
            "methodology": "visual-only",
            "evidence_refs": ["home.png"],
        }
    )

    assert decision.action == NEEDS_BROWSER_REPRO
    assert decision.actionable is False


def test_repair_gate_blocks_curl_only_ui_story() -> None:
    decision = repair_gate_for_verdict(
        {
            "feature_id": "filters",
            "verdict": "partial",
            "detail": "The dashboard filter was checked by fetching HTML.",
            "surface": "DOM",
            "methodology": "http-request",
            "evidence_refs": ["curl /dashboard"],
        }
    )

    assert decision.action == NEEDS_BROWSER_REPRO
    assert decision.actionable is False


def test_repair_gate_marks_empty_verdict_as_proof_gap() -> None:
    decision = repair_gate_for_verdict(
        {"feature_id": "search", "verdict": "blocked", "detail": ""}
    )

    assert decision.action == PROOF_GAP_ONLY
    assert decision.actionable is False
