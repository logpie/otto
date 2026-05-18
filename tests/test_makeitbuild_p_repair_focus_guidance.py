"""Regression: the oracle-repair agent must not waste its budget
re-diagnosing already-passed oracle steps, and must treat UI-journey
failures as a single shared-session cascade (fix journey 1 first).

resume16j timed out at 1199s mid-progress; resume16k spent ~16min
re-deriving the already-fixed IPv4/IPv6 issue + reasoning about the
cascade before discovering (near timeout) that the 4 journeys run in
ONE shared sequential session where journey 1 gates 2-4.

_oracle_focus_guidance is generic + data-driven (parses the packet's
own latest_oracle_result) so it applies to ANY product, not iTracker.
"""

from __future__ import annotations

from otto.v5_preflight_repair import _oracle_focus_guidance


class _Packet:
    def __init__(self, oracle_result: dict) -> None:
        self.latest_oracle_result = oracle_result


def test_guidance_lists_passed_and_failed_and_journey_cascade() -> None:
    g = _oracle_focus_guidance(
        _Packet(
            {
                "steps": [
                    {"id": "npm_build:frontend", "status": "passed"},
                    {"id": "start", "status": "passed"},
                    {
                        "id": "ui_journeys",
                        "status": "failed",
                        "reason": (
                            "ui journeys failed: register_and_create_first_issue, "
                            "update_issue_status_via_dropdown, "
                            "comment_with_mention_appears_in_inbox, "
                            "search_with_status_and_priority_operators"
                        ),
                    },
                ]
            }
        )
    )
    # Passed steps named, with an explicit do-not-re-investigate.
    assert "ALREADY PASSING" in g
    assert "npm_build:frontend" in g and "start" in g
    assert "do NOT re-investigate" in g.lower() or "do not re-investigate" in g.lower()
    # Focus on the failed step.
    assert "ui_journeys" in g and "FAILING step" in g
    # Cascade guidance: shared sequential session, ordered journeys, fix first.
    assert "ONE shared, sequential browser" in g
    assert "register_and_create_first_issue" in g
    assert g.index("register_and_create_first_issue") < g.index(
        "search_with_status_and_priority_operators"
    ), "failing journeys must be listed in execution order"
    assert "FIRST failing journey" in g
    assert "independent" in g.lower()


def test_no_journey_step_omits_cascade_text() -> None:
    g = _oracle_focus_guidance(
        _Packet(
            {
                "steps": [
                    {"id": "npm_build:frontend", "status": "passed"},
                    {"id": "npm_ci", "status": "failed", "reason": "lockfile drift"},
                ]
            }
        )
    )
    assert "ALREADY PASSING" in g and "npm_build:frontend" in g
    assert "npm_ci" in g and "FAILING step" in g
    # Not a journey failure -> no shared-session cascade paragraph.
    assert "shared, sequential browser" not in g


def test_empty_oracle_result_is_safe_noop() -> None:
    assert _oracle_focus_guidance(_Packet({})) == ""
    assert _oracle_focus_guidance(_Packet({"steps": []})) == ""


def test_non_dict_oracle_result_is_safe_noop() -> None:
    class P:
        latest_oracle_result = None

    assert _oracle_focus_guidance(P()) == ""
