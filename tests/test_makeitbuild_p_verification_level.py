"""Run #3 regression: API-route journeys must verify as `api`, not crash.

Run #3 died at flat-compile: a journey `rest_api_pat_issue_crud` (an /api
REST+PAT journey the iTracker product explicitly requires) was force-assigned
`ui` because the validator demanded a redundant `api_only:true` flag, then the
mismatch propagated `catastrophic` instead of bounded spec-repair. Two fixes:

1. assign_verification_level: an /api-route webapp journey IS an api journey
   (structural truth), no redundant flag required. Fail-closed preserved: a
   non-/api (UI) route still → `ui` (a broken UI can never be api-verified).
2. verification_contract_invalid is bounded-repairable → honest terminal,
   never catastrophic.
"""

from __future__ import annotations

import pytest

from otto.journey_contracts import VerificationContractError, assign_verification_level
from otto.spec_compile_flat import _contract_error_is_pass_model_repairable


def _journey(**kw):
    base = {"id": "j", "entry_route": "/", "covers_primary_actions": ["a"]}
    base.update(kw)
    return base


def test_api_route_webapp_journey_is_api_without_redundant_flag() -> None:
    # The run#3 bug: /api journey, NO api_only flag → must be api (not forced ui).
    level, probe = assign_verification_level(
        _journey(id="rest_api_pat_issue_crud", entry_route="/api/issues"),
        "webapp",
        path="behavior_journeys[rest_api_pat_issue_crud]",
    )
    assert (level, probe) == ("api", "http_api")


def test_ui_route_webapp_journey_stays_ui_failclosed() -> None:
    # Fail-closed preserved: a UI-page route is ui — no false-pass reopened.
    level, probe = assign_verification_level(
        _journey(id="create_issue_ui", entry_route="/issues"),
        "webapp",
        path="behavior_journeys[create_issue_ui]",
    )
    assert (level, probe) == ("ui", None)


def test_api_only_flag_still_works_and_still_validated() -> None:
    level, probe = assign_verification_level(
        _journey(entry_route="/api/x", api_only=True), "webapp", path="p"
    )
    assert (level, probe) == ("api", "http_api")
    with pytest.raises(VerificationContractError):
        assign_verification_level(
            _journey(entry_route="/issues", api_only=True), "webapp", path="p"
        )


def test_verification_contract_invalid_is_bounded_repairable_not_catastrophic() -> None:
    exc = VerificationContractError(
        "verification_contract_invalid",
        "behavior_journeys[x].verification_level",
        "declares 'api', expected 'ui'",
    )
    assert _contract_error_is_pass_model_repairable(exc) is True
