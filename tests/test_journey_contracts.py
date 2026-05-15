from __future__ import annotations

import pytest

from otto.journey_contracts import (
    VerificationContractError,
    normalize_journey_contracts,
)
from otto.spec_compile_flat import SCHEMA_VERSION


def _webapp_journey(**overrides: object) -> dict[str, object]:
    journey: dict[str, object] = {
        "id": "create_issue",
        "role": "illustrative",
        "description": "User creates an issue and sees it in the backlog.",
        "covers_primary_actions": ["issue.create"],
        "start_state": "unauthenticated",
        "entry_route": "/",
    }
    journey.update(overrides)
    return journey


def _payload(project_kind: str, journeys: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION - 1,
        "project_kind": project_kind,
        "behavior_journeys": journeys,
    }


def test_fail_closed_assignment_marks_itracker_webapp_journeys_ui() -> None:
    payload = _payload(
        "webapp",
        [_webapp_journey(id=f"journey_{idx}", entry_route=route) for idx, route in enumerate(["/", "/login", "/workspaces", "/teams", "/issues"], start=1)],
    )

    normalized = normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    journeys = normalized["behavior_journeys"]
    assert [journey["verification_level"] for journey in journeys] == ["ui"] * 5
    assert all("pass_model" in journey for journey in journeys)


def test_webapp_missing_entry_route_fails_closed() -> None:
    payload = _payload("webapp", [_webapp_journey(entry_route="")])

    with pytest.raises(VerificationContractError) as excinfo:
        normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    assert excinfo.value.code == "verification_contract_invalid"
    assert "entry_route" in excinfo.value.path


def test_webapp_api_only_api_route_assigns_http_api_probe() -> None:
    payload = _payload(
        "webapp",
        [_webapp_journey(api_only=True, entry_route="/api/issues")],
    )

    normalized = normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    journey = normalized["behavior_journeys"][0]
    assert journey["verification_level"] == "api"
    assert journey["probe_kind"] == "http_api"
    assert "pass_model" not in journey


@pytest.mark.parametrize(
    ("project_kind", "probe_kind"),
    [("cli", "cli_command"), ("library", "library_call"), ("api", "http_api"), ("service", "service_health")],
)
def test_non_webapp_journeys_are_api_with_typed_probe_kind(
    project_kind: str,
    probe_kind: str,
) -> None:
    payload = _payload(
        project_kind,
        [{"id": "main", "description": "Run the primary behavior."}],
    )

    normalized = normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    journey = normalized["behavior_journeys"][0]
    assert journey["verification_level"] == "api"
    assert journey["probe_kind"] == probe_kind


def test_typed_ui_journey_missing_pass_model_routes_to_contract_missing() -> None:
    payload = _payload(
        "webapp",
        [_webapp_journey(verification_level="ui")],
    )
    payload["schema_version"] = SCHEMA_VERSION

    with pytest.raises(VerificationContractError) as excinfo:
        normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    assert excinfo.value.code == "verification_contract_missing"


def test_weak_pass_model_routes_to_contract_invalid() -> None:
    weak_model = {
        "start_state": "unauthenticated",
        "setup": [],
        "actions": [
            {
                "id": "issue.create",
                "state_changing": True,
                "covers_primary_actions": ["issue.create"],
                "success_observables": [
                    {
                        "kind": "network_and_ui_effect",
                        "primary_action_id": "issue.create",
                        "description": "Route /workspaces loaded and HTTP 200 returned.",
                        "method": "GET",
                        "path": "/workspaces",
                        "status": 200,
                        "ui_effect": "Workspace text appears.",
                    }
                ],
            }
        ],
        "success_observables": [],
        "ready_policy": {"route": "/workspaces"},
        "settle_policy": {"after_action": "network_idle"},
        "network_expectations": [],
        "final_dom_assertions": [{"kind": "text_visible", "text": "Workspace"}],
    }
    payload = _payload(
        "webapp",
        [_webapp_journey(verification_level="ui", pass_model=weak_model)],
    )
    payload["schema_version"] = SCHEMA_VERSION

    with pytest.raises(VerificationContractError) as excinfo:
        normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    assert excinfo.value.code == "verification_contract_invalid"
    assert "success_observables" in excinfo.value.path
