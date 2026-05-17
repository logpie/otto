from __future__ import annotations

import pytest

from otto.journey_contracts import (
    VerificationContractError,
    normalize_journey_contracts,
)
from otto.spec_compile_flat import SCHEMA_VERSION


def _ui_pass_model() -> dict[str, object]:
    observable = {
        "kind": "persisted_data_visible",
        "primary_action_id": "issue.create",
        "description": "The created issue title appears in the backlog after submit.",
        "text": "Fix login",
    }
    return {
        "start_state": "unauthenticated",
        "setup": [],
        "actions": [
            {
                "id": "issue.create",
                "state_changing": True,
                "role": "button",
                "name": "Create issue",
                "covers_primary_actions": ["issue.create"],
                "success_observables": [observable],
            }
        ],
        "success_observables": [observable],
        "ready_policy": {"route": "/", "wait_for": "interactive"},
        "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
        "network_expectations": [],
        "final_dom_assertions": [observable],
    }


def _api_pass_model(probe_kind: str) -> dict[str, object]:
    if probe_kind == "http_api":
        return {
            "steps": [
                {
                    "method": "GET",
                    "path": "/items/1",
                    "expect_status": 200,
                    "expect_json": {"id": "1", "name": "Item"},
                }
            ]
        }
    if probe_kind == "cli_command":
        return {
            "command": ["python3", "-c", "print('created item')"],
            "expect_exit_code": 0,
            "stdout_contains": "created item",
        }
    if probe_kind == "library_call":
        return {
            "module": "calc",
            "function": "add",
            "args": [1, 2],
            "expect_return": 3,
        }
    if probe_kind == "service_health":
        return {
            "start_command": ["python3", "service.py"],
            "health_url": "http://127.0.0.1:8000/health",
            "expect_status": 200,
            "expect_body_contains": "healthy",
        }
    raise AssertionError(probe_kind)


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


def test_old_spec_role_illustrative_is_ignored_but_still_loads() -> None:
    payload = _payload("webapp", [_webapp_journey(role="illustrative", entry_route="/")])

    normalized = normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    assert normalized["behavior_journeys"][0]["verification_level"] == "ui"


def test_webapp_missing_entry_route_fails_closed() -> None:
    payload = _payload("webapp", [_webapp_journey(entry_route="")])

    with pytest.raises(VerificationContractError) as excinfo:
        normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    assert excinfo.value.code == "verification_contract_invalid"
    assert "entry_route" in excinfo.value.path


def test_webapp_api_only_api_route_assigns_http_api_probe() -> None:
    payload = _payload(
        "webapp",
        [_webapp_journey(api_only=True, entry_route="/api/issues", pass_model=_api_pass_model("http_api"))],
    )

    normalized = normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    journey = normalized["behavior_journeys"][0]
    assert journey["verification_level"] == "api"
    assert journey["probe_kind"] == "http_api"
    assert "pass_model" in journey


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
        [{"id": "main", "description": "Run the primary behavior.", "pass_model": _api_pass_model(probe_kind)}],
    )

    normalized = normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    journey = normalized["behavior_journeys"][0]
    assert journey["verification_level"] == "api"
    assert journey["probe_kind"] == probe_kind


def test_current_schema_missing_or_empty_journeys_fails_closed() -> None:
    for payload in (
        {"schema_version": SCHEMA_VERSION, "project_kind": "webapp"},
        {"schema_version": SCHEMA_VERSION, "project_kind": "webapp", "behavior_journeys": "missing"},
        {"schema_version": SCHEMA_VERSION, "project_kind": "webapp", "behavior_journeys": []},
    ):
        with pytest.raises(VerificationContractError) as excinfo:
            normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)
        assert excinfo.value.code == "verification_contract_missing"


def test_current_schema_journey_missing_typed_fields_fails_closed() -> None:
    payload = _payload("webapp", [_webapp_journey()])
    payload["schema_version"] = SCHEMA_VERSION

    with pytest.raises(VerificationContractError) as excinfo:
        normalize_journey_contracts(payload, current_schema_version=SCHEMA_VERSION)

    assert excinfo.value.code == "verification_contract_missing"
    assert "verification_level" in excinfo.value.path


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
                "role": "button",
                "name": "Create issue",
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


@pytest.mark.parametrize(
    ("project_kind", "probe_kind", "weak_pass_model"),
    [
        ("api", "http_api", {"steps": [{"method": "GET", "path": "/items", "expect_status": 200}]}),
        ("cli", "cli_command", {"command": ["python3", "-c", "pass"], "expect_exit_code": 0}),
        ("library", "library_call", {"module": "calc", "function": "add", "args": [1, 2]}),
        (
            "service",
            "service_health",
            {
                "start_command": ["python3", "service.py"],
                "health_url": "http://127.0.0.1:8000/health",
                "expect_status": 200,
            },
        ),
    ],
)
def test_current_schema_api_journey_requires_strong_adapter_pass_model(
    project_kind: str,
    probe_kind: str,
    weak_pass_model: dict[str, object],
) -> None:
    valid_journey: dict[str, object] = {
        "id": "main",
        "description": "Run the primary behavior.",
        "covers_primary_actions": ["main.run"],
        "start_state": "empty",
        "verification_level": "api",
        "probe_kind": probe_kind,
        "pass_model": _api_pass_model(probe_kind),
    }
    valid_payload = {
        "schema_version": SCHEMA_VERSION,
        "project_kind": project_kind,
        "behavior_journeys": [valid_journey],
    }

    normalized = normalize_journey_contracts(valid_payload, current_schema_version=SCHEMA_VERSION)
    assert normalized["behavior_journeys"][0]["probe_kind"] == probe_kind

    weak_payload = {
        **valid_payload,
        "behavior_journeys": [{**valid_journey, "pass_model": weak_pass_model}],
    }
    with pytest.raises(VerificationContractError) as excinfo:
        normalize_journey_contracts(weak_payload, current_schema_version=SCHEMA_VERSION)
    assert excinfo.value.code == "verification_contract_invalid"
