from __future__ import annotations

from otto.repair_gates import (
    NO_REPAIR,
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


def test_repair_gate_routes_visual_only_weak_finding_to_agent() -> None:
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

    assert decision.action == REPAIR_NOW
    assert decision.actionable is True


def test_repair_gate_routes_curl_only_ui_story_to_agent() -> None:
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

    assert decision.action == REPAIR_NOW
    assert decision.actionable is True


def test_repair_gate_blocks_no_evidence_blocked_verdict() -> None:
    decision = repair_gate_for_verdict(
        {"feature_id": "search", "verdict": "blocked", "detail": ""}
    )

    assert decision.action == NO_REPAIR
    assert decision.actionable is False


def test_repair_gate_blocks_typed_provider_auth_failure() -> None:
    decision = repair_gate_for_verdict(
        {
            "feature_id": "search",
            "verdict": "blocked",
            "provider_error": {"code": "provider_auth_exhausted"},
        }
    )

    assert decision.action == NO_REPAIR
    assert decision.actionable is False
