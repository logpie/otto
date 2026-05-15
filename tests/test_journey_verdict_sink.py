from __future__ import annotations

from otto.journey_verdict_sink import resolve_journey_verdicts


def test_executor_fail_dominates_legacy_adapter_pass() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "onboard", "verification_level": "ui"}],
        execution_scope="root_integration",
        legacy_results=[{"id": "onboard", "passed": True, "detail": "legacy passed"}],
        executor_results=[
            {
                "id": "onboard",
                "status": "fail",
                "source": "ui_executor",
                "proof_usable": True,
                "detail": "DOM effect missing",
            }
        ],
        registered_executor_levels={"ui"},
    )

    assert verdicts == [
        {
            "id": "onboard",
            "passed": False,
            "detail": "DOM effect missing",
            "source": "ui_executor",
            "proof": True,
            "status": "fail",
        }
    ]


def test_executor_unverified_dominates_legacy_adapter_pass() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "onboard", "verification_level": "ui"}],
        execution_scope="root_integration",
        legacy_results=[{"id": "onboard", "passed": True}],
        executor_results=[
            {
                "id": "onboard",
                "status": "pass",
                "source": "ui_executor",
                "proof_usable": False,
            }
        ],
        registered_executor_levels={"ui"},
    )

    assert verdicts[0]["passed"] is False
    assert verdicts[0]["source"] == "ui_executor"
    assert verdicts[0]["status"] == "unverified"


def test_legacy_adapter_is_tagged_and_not_proof() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "legacy", "verification_level": "ui"}],
        execution_scope="root_integration",
        legacy_results=[{"id": "legacy", "passed": True, "detail": "old heuristic"}],
    )

    assert verdicts[0]["passed"] is True
    assert verdicts[0]["source"] == "legacy_adapter"
    assert verdicts[0]["proof"] is False
