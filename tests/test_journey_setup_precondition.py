"""Regression: non-cold behavior journeys must declare EXECUTABLE setup.

Root cause (resume16o, 2026-05-18, terminal merge_blocked): the clean_deploy
UI-journey oracle (`journey_ui_executor._run_one_journey`) navigates straight
to `entry_route` and never executes `pass_model.setup`, so every journey whose
`start_state` is non-cold (a seeded `workspace_with_issue` etc.) spuriously
fails "control absent" on a precondition the oracle never established — while
only the one cold/self-bootstrapping journey passes. The product was correct
the whole time; this is a systemic ORACLE false-negative.

Fix is fail-early (per fail-early-compile-time): a journey whose `start_state`
is NOT one of the spec's own `cold_start_states[].id` MUST carry a non-empty
`pass_model.setup` whose every step is executable (a navigation `route` and/or
an executable UI locator — the same primitives `pass_model.actions` use). An
abstract `{action:"seed",entity:...,fields:...}` declaration with no executable
recipe is rejected at compile time, not silently skipped at clean_deploy.

Generic: any product with non-cold journeys (auth-gated detail/search/comment
flows) hits this. Cold journeys are unaffected (empty setup stays valid) so no
currently-passing journey regresses.
"""

from __future__ import annotations

import pytest

from otto.journey_contracts import (
    VerificationContractError,
    normalize_journey_contracts,
)
from otto.spec_compile_flat import SCHEMA_VERSION


def _observable() -> dict[str, object]:
    return {
        "kind": "persisted_data_visible",
        "primary_action_id": "issue.update_status",
        "description": "The status badge reflects the new status after the change.",
        "text": "In Progress",
    }


def _state_changing_action() -> dict[str, object]:
    obs = _observable()
    return {
        "id": "issue.update_status",
        "state_changing": True,
        "role": "button",
        "name": "Todo",
        "covers_primary_actions": ["issue.update_status"],
        "success_observables": [obs],
    }


def _journey(*, jid: str, start_state: str, setup: list[dict[str, object]]) -> dict[str, object]:
    obs = _observable()
    return {
        "id": jid,
        "verification_level": "ui",
        "start_state": start_state,
        "entry_route": "/issues/ENG-1",
        "covers_primary_actions": ["issue.update_status"],
        "pass_model": {
            "start_state": start_state,
            "setup": setup,
            "actions": [_state_changing_action()],
            "success_observables": [obs],
            "ready_policy": {"route": "/issues/ENG-1", "wait_for": "interactive"},
            "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
            "network_expectations": [],
            "final_dom_assertions": [obs],
        },
    }


def _cold_journey() -> dict[str, object]:
    obs = {
        "kind": "persisted_data_visible",
        "primary_action_id": "issue.create",
        "description": "The created issue appears in the backlog after submit.",
        "text": "Set up CI",
    }
    return {
        "id": "register_and_create_first_issue",
        "verification_level": "ui",
        "start_state": "unauthenticated",
        "entry_route": "/register",
        "covers_primary_actions": ["issue.create"],
        "pass_model": {
            "start_state": "unauthenticated",
            "setup": [],
            "actions": [
                {
                    "id": "issue.create",
                    "state_changing": True,
                    "role": "button",
                    "name": "Create issue",
                    "covers_primary_actions": ["issue.create"],
                    "success_observables": [obs],
                }
            ],
            "success_observables": [obs],
            "ready_policy": {"route": "/register", "wait_for": "interactive"},
            "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
            "network_expectations": [],
            "final_dom_assertions": [obs],
        },
    }


def _payload(journeys: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_kind": "webapp",
        "cold_start_states": [
            {"id": "unauthenticated", "name": "Unauthenticated visitor", "description": "No session."},
        ],
        "behavior_journeys": journeys,
    }


def _executable_setup() -> list[dict[str, object]]:
    """The shape the compiler MUST emit: navigation + an executable action,
    same primitives as pass_model.actions."""
    return [
        {
            "id": "setup.register",
            "route": "/register",
            "role": "button",
            "name": "Create account",
            "inputs": [{"label": "Email", "value": "a@b.com"}],
        },
        {
            "id": "setup.create_issue",
            "route": "/eng/issues",
            "role": "button",
            "name": "Create issue",
            "inputs": [{"label": "Title", "value": "Set up CI pipeline"}],
        },
    ]


def test_cold_journey_with_empty_setup_still_valid() -> None:
    # Zero behavior change for cold/self-bootstrapping journeys — the exact
    # journey that already passes (register_and_create_first_issue) and every
    # other product's cold journey must remain valid.
    normalize_journey_contracts(_payload([_cold_journey()]), current_schema_version=SCHEMA_VERSION)


def test_noncold_journey_with_empty_setup_is_rejected() -> None:
    # THE BUG: start_state 'workspace_with_issue' is NOT in cold_start_states,
    # yet setup is empty. Today this is accepted and the oracle then navigates
    # /issues/ENG-1 against an empty DB -> false "control absent". Must fail
    # CLOSED at compile, not silently at clean_deploy.
    with pytest.raises(VerificationContractError) as ei:
        normalize_journey_contracts(
            _payload([_journey(jid="update_issue_status", start_state="workspace_with_issue", setup=[])]),
            current_schema_version=SCHEMA_VERSION,
        )
    assert "setup" in str(ei.value).lower()


def test_noncold_journey_with_abstract_seed_setup_is_rejected() -> None:
    # Abstract declaration with no executable recipe — the oracle cannot run
    # this against a black-box clean deploy. Must be rejected so the compiler
    # is forced to emit concrete steps.
    abstract = [{"action": "seed", "entity": "issue", "fields": {"identifier": "ENG-1", "status": "todo"}}]
    with pytest.raises(VerificationContractError):
        normalize_journey_contracts(
            _payload(
                [_journey(jid="update_issue_status", start_state="workspace_with_issue", setup=abstract)]
            ),
            current_schema_version=SCHEMA_VERSION,
        )


def test_noncold_journey_with_executable_setup_is_valid() -> None:
    # The correct compiled shape: navigation + actions that bootstrap the
    # start_state from a cold clean deploy. Must pass.
    normalize_journey_contracts(
        _payload(
            [
                _journey(
                    jid="update_issue_status",
                    start_state="workspace_with_issue",
                    setup=_executable_setup(),
                )
            ]
        ),
        current_schema_version=SCHEMA_VERSION,
    )
