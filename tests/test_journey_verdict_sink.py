from __future__ import annotations

from otto.journey_verdict_sink import failed_journey_ids, resolve_journey_verdicts


def test_executor_fail_sets_controller_verdict() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "onboard", "verification_level": "ui"}],
        execution_scope="root_integration",
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


def test_executor_unverified_cannot_pass_without_usable_proof() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "onboard", "verification_level": "ui"}],
        execution_scope="root_integration",
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


def test_registered_executor_missing_result_fails_closed() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "missing", "verification_level": "api"}],
        execution_scope="leaf",
        executor_results=[],
        registered_executor_levels={"ui", "api"},
    )

    assert verdicts[0]["passed"] is False
    assert verdicts[0]["source"] == "journey_verdict_sink"
    assert verdicts[0]["status"] == "unverified"


def test_deferred_and_skipped_journeys_do_not_count_as_failures() -> None:
    deferred = resolve_journey_verdicts(
        journeys=[{"id": "ui_leaf", "verification_level": "ui"}],
        execution_scope="leaf",
        executor_results=[],
        registered_executor_levels={"ui", "api"},
    )
    skipped = resolve_journey_verdicts(
        journeys=[
            {"id": "api_root", "verification_level": "api"},
        ],
        execution_scope="root_integration",
        executor_results=[],
        registered_executor_levels={"ui", "api"},
    )

    assert deferred[0]["status"] == "defer"
    assert skipped[0]["status"] == "skip"
    assert failed_journey_ids([*deferred, *skipped]) == []
