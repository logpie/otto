"""Typed behavior-journey contract normalization and validation."""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

VerificationLevel = Literal["ui", "api"]
ProbeKind = Literal["http_api", "cli_command", "library_call", "service_health"]

VERIFICATION_LEVELS: tuple[VerificationLevel, ...] = ("ui", "api")
PROBE_KINDS: tuple[ProbeKind, ...] = (
    "http_api",
    "cli_command",
    "library_call",
    "service_health",
)
PROJECT_KINDS_WITH_API_JOURNEYS: dict[str, ProbeKind] = {
    "cli": "cli_command",
    "library": "library_call",
    "api": "http_api",
    "service": "service_health",
}
SUPPORTED_PROJECT_KINDS = frozenset({"webapp", *PROJECT_KINDS_WITH_API_JOURNEYS})

PASS_MODEL_KEYS = (
    "start_state",
    "setup",
    "actions",
    "success_observables",
    "ready_policy",
    "settle_policy",
    "network_expectations",
    "final_dom_assertions",
)
UI_LOCATOR_KEYS = frozenset({
    "selector",
    "control_selector",
    "click_selector",
    "button_selector",
    "target_selector",
    "testid_selector",
    "testid",
    "data_testid",
    "data-testid",
    "role",
    "control_role",
    "name",
    "accessible_name",
    "control_name",
    "button_name",
    "label",
    "control_label",
    "text",
    "visible_text",
    "button_text",
})
UI_ASSERTION_KEYS = frozenset({
    "selector",
    "target_selector",
    "testid_selector",
    "testid",
    "data_testid",
    "data-testid",
    "role",
    "name",
    "accessible_name",
    "label",
    "text",
    "expected_text",
    "visible_text",
})

WEAK_OBSERVABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(route|page)\s+(?:is\s+)?loaded\b", re.IGNORECASE),
    re.compile(r"\bhttps?\s*(?:status\s*)?2\d\d\b", re.IGNORECASE),
    re.compile(r"\bhttp\s*(?:200|2xx|ok|success)\b", re.IGNORECASE),
    re.compile(r"\bbody\s+(?:is\s+)?(?:present|non[- ]?empty|visible)\b", re.IGNORECASE),
    re.compile(r"\bskeleton\b", re.IGNORECASE),
    re.compile(r"\bgeneric\s+text\b", re.IGNORECASE),
    re.compile(r"\btext\s+(?:appears|is\s+visible|present)\b", re.IGNORECASE),
)
STRONG_OBSERVABLE_KINDS = frozenset({
    "dom_state_transition",
    "persisted_data_visible",
    "persisted_data_visible_in_ui",
    "network_and_ui_effect",
})


class VerificationContractError(ValueError):
    """Raised when a behavior journey contract must fail closed."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


def normalize_journey_contracts(
    payload: dict[str, Any],
    *,
    current_schema_version: int,
) -> dict[str, Any]:
    """Return a copy with typed journey fields assigned or fail closed.

    Legacy specs missing typed fields are migrated when the journey has enough
    data to determine the level safely. Current typed specs missing a UI
    pass-model are rejected so weak or absent oracle contracts cannot pass.
    """

    normalized = copy.deepcopy(payload)
    project_kind = str(normalized.get("project_kind") or "webapp").strip()
    if project_kind not in SUPPORTED_PROJECT_KINDS:
        raise VerificationContractError(
            "verification_contract_invalid",
            "project_kind",
            f"unknown project_kind {project_kind!r}",
        )

    raw_schema_version = normalized.get("schema_version")
    if raw_schema_version is None:
        schema_version = current_schema_version
    else:
        try:
            schema_version = int(raw_schema_version)
        except (TypeError, ValueError):
            schema_version = current_schema_version
    legacy_schema = schema_version < current_schema_version

    journeys = normalized.get("behavior_journeys")
    if not isinstance(journeys, list):
        if legacy_schema:
            normalized["behavior_journeys"] = []
            return normalized
        raise VerificationContractError(
            "verification_contract_missing",
            "behavior_journeys",
            "current schema requires behavior_journeys as a non-empty list",
        )
    if not journeys and not legacy_schema:
        raise VerificationContractError(
            "verification_contract_missing",
            "behavior_journeys",
            "current schema requires at least one behavior journey",
        )

    for index, journey in enumerate(journeys):
        if not isinstance(journey, dict):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"behavior_journeys[{index}]",
                "behavior journey entry is not an object",
            )
        _normalize_one_journey(
            journey,
            project_kind=project_kind,
            path=f"behavior_journeys[{journey.get('id') or index}]",
            legacy_schema=legacy_schema,
        )

    normalized["schema_version"] = current_schema_version
    return normalized


def _normalize_one_journey(
    journey: dict[str, Any],
    *,
    project_kind: str,
    path: str,
    legacy_schema: bool,
) -> None:
    assigned_level, assigned_probe = assign_verification_level(journey, project_kind, path=path)
    existing_level = journey.get("verification_level")
    has_typed_level = isinstance(existing_level, str) and bool(existing_level.strip())

    if not has_typed_level and not legacy_schema:
        raise VerificationContractError(
            "verification_contract_missing",
            f"{path}.verification_level",
            "current schema journey is missing verification_level",
        )
    if has_typed_level and existing_level != assigned_level:
        raise VerificationContractError(
            "verification_contract_invalid",
            f"{path}.verification_level",
            f"declares {existing_level!r}, expected {assigned_level!r}",
        )
    journey["verification_level"] = assigned_level

    if assigned_level == "api":
        existing_probe = journey.get("probe_kind")
        has_typed_probe = isinstance(existing_probe, str) and bool(existing_probe.strip())
        if not has_typed_probe and not legacy_schema:
            raise VerificationContractError(
                "verification_contract_missing",
                f"{path}.probe_kind",
                "current schema api journey is missing probe_kind",
            )
        if has_typed_probe:
            if existing_probe not in PROBE_KINDS:
                raise VerificationContractError(
                    "verification_contract_invalid",
                    f"{path}.probe_kind",
                    f"unsupported probe_kind {existing_probe!r}",
                )
            if existing_probe != assigned_probe:
                raise VerificationContractError(
                    "verification_contract_invalid",
                    f"{path}.probe_kind",
                    f"declares {existing_probe!r}, expected {assigned_probe!r}",
                )
        journey["probe_kind"] = assigned_probe
        validate_api_pass_model(journey, path=path)
        return

    journey.pop("probe_kind", None)
    missing_model = "pass_model" not in journey or not isinstance(journey.get("pass_model"), dict)
    if missing_model:
        if has_typed_level and not legacy_schema:
            raise VerificationContractError(
                "verification_contract_missing",
                f"{path}.pass_model",
                "ui journey is missing declarative pass_model",
            )
        journey["pass_model"] = synthesize_ui_pass_model(journey)
    validate_ui_pass_model(journey, path=path)


def assign_verification_level(
    journey: dict[str, Any],
    project_kind: str,
    *,
    path: str,
) -> tuple[VerificationLevel, ProbeKind | None]:
    """Apply the fail-closed deterministic level assignment rule."""

    if project_kind == "webapp":
        entry_route = str(journey.get("entry_route") or "").strip()
        if not _well_formed_entry_route(entry_route):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.entry_route",
                "webapp behavior journey requires a well-formed entry_route",
            )
        api_only = bool(journey.get("api_only") is True)
        if api_only:
            if not entry_route.startswith("/api"):
                raise VerificationContractError(
                    "verification_contract_invalid",
                    f"{path}.entry_route",
                    "webapp api_only journey must use an /api entry_route",
                )
            return "api", "http_api"
        return "ui", None

    probe = PROJECT_KINDS_WITH_API_JOURNEYS.get(project_kind)
    if probe is None:
        raise VerificationContractError(
            "verification_contract_invalid",
            "project_kind",
            f"unknown project_kind {project_kind!r}",
        )
    return "api", probe


def synthesize_ui_pass_model(journey: dict[str, Any]) -> dict[str, Any]:
    """Build a conservative UI pass model for safely migratable legacy specs."""

    covered_actions = [
        str(item)
        for item in journey.get("covers_primary_actions") or journey.get("feature_ids") or []
        if str(item).strip()
    ]
    if not covered_actions:
        covered_actions = [str(journey.get("id") or "visible_effect")]
    actions: list[dict[str, Any]] = []
    final_assertions: list[dict[str, Any]] = []
    for action_id in covered_actions:
        observable = {
            "kind": "persisted_data_visible",
            "primary_action_id": action_id,
            "description": f"After {action_id}, the resulting entity/effect is visible in the UI.",
            "text": str(journey.get("expected_text") or action_id),
        }
        actions.append({
            "id": f"{action_id}.effect",
            "description": str(journey.get("description") or f"Exercise {action_id}."),
            "state_changing": True,
            "role": "button",
            "name": str(journey.get("action_name") or action_id),
            "covers_primary_actions": [action_id],
            "success_observables": [observable],
            "network_expectations": [],
        })
        final_assertions.append({
            "kind": "persisted_data_visible",
            "primary_action_id": action_id,
            "description": observable["description"],
            "text": observable["text"],
        })

    return {
        "start_state": str(journey.get("start_state") or ""),
        "setup": [],
        "actions": actions,
        "success_observables": final_assertions,
        "ready_policy": {"route": str(journey.get("entry_route") or "/"), "wait_for": "interactive"},
        "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
        "network_expectations": [],
        "final_dom_assertions": final_assertions,
    }


def validate_ui_pass_model(journey: dict[str, Any], *, path: str) -> None:
    model = journey.get("pass_model")
    if not isinstance(model, dict):
        raise VerificationContractError(
            "verification_contract_missing",
            f"{path}.pass_model",
            "ui journey is missing declarative pass_model",
        )
    missing_keys = [key for key in PASS_MODEL_KEYS if key not in model]
    if missing_keys:
        raise VerificationContractError(
            "verification_contract_invalid",
            f"{path}.pass_model",
            f"pass_model missing required keys: {', '.join(missing_keys)}",
        )

    actions = model.get("actions")
    if not isinstance(actions, list):
        raise VerificationContractError(
            "verification_contract_invalid",
            f"{path}.pass_model.actions",
            "pass_model.actions must be a list",
        )

    covered_actions = {
        str(item)
        for item in journey.get("covers_primary_actions") or journey.get("feature_ids") or []
        if str(item).strip()
    }
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.actions[{index}]",
                "pass_model action must be an object",
            )
        step_actions = {
            str(item)
            for item in action.get("covers_primary_actions") or []
            if str(item).strip()
        }
        state_changing = bool(action.get("state_changing") is True or step_actions)
        if not state_changing:
            continue
        if not _has_any_key(action, UI_LOCATOR_KEYS):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.actions[{index}]",
                "state-changing action lacks an executable role/name/label/text or selector locator",
            )
        observables = [
            item for item in action.get("success_observables") or []
            if isinstance(item, dict)
        ]
        if not any(
            _observable_is_strong(
                observable,
                covered_actions=covered_actions,
                step_actions=step_actions,
            )
            for observable in observables
        ):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.actions[{index}].success_observables",
                "state-changing action lacks a non-tautological post-action observable",
            )
        if not any(_has_any_key(observable, UI_ASSERTION_KEYS) for observable in observables):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.actions[{index}].success_observables",
                "state-changing action lacks an executable post-action DOM assertion",
            )
    final_assertions = model.get("final_dom_assertions")
    if not isinstance(final_assertions, list):
        raise VerificationContractError(
            "verification_contract_invalid",
            f"{path}.pass_model.final_dom_assertions",
            "pass_model.final_dom_assertions must be a list",
        )
    if not any(isinstance(item, dict) and _has_any_key(item, UI_ASSERTION_KEYS) for item in final_assertions):
        raise VerificationContractError(
            "verification_contract_invalid",
            f"{path}.pass_model.final_dom_assertions",
            "pass_model.final_dom_assertions lacks an executable DOM assertion",
        )


def validate_api_pass_model(journey: dict[str, Any], *, path: str) -> None:
    probe_kind = str(journey.get("probe_kind") or "").strip()
    model = journey.get("pass_model")
    if not isinstance(model, dict):
        raise VerificationContractError(
            "verification_contract_missing",
            f"{path}.pass_model",
            f"{probe_kind or 'api'} journey is missing declarative pass_model",
        )
    if probe_kind == "http_api":
        steps = model.get("steps") or model.get("api_steps")
        if not isinstance(steps, list) or not steps:
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.steps",
                "http_api pass_model requires a non-empty steps list",
            )
        strong = False
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise VerificationContractError(
                    "verification_contract_invalid",
                    f"{path}.pass_model.steps[{index}]",
                    "http_api step must be an object",
                )
            if not str(step.get("path") or "").strip():
                raise VerificationContractError(
                    "verification_contract_invalid",
                    f"{path}.pass_model.steps[{index}].path",
                    "http_api step missing path",
                )
            strong = strong or _http_step_has_strong_assertion(step)
        if not strong:
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.steps",
                "http_api pass_model requires a body assertion, extracted state, or other payload assertion",
            )
        return
    if probe_kind == "cli_command":
        if not _command_list_like(model.get("command")):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.command",
                "cli_command pass_model requires command as a non-empty string list",
            )
        if not _has_cli_success_effect(model):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model",
                "cli_command pass_model requires stdout/stderr assertion or filesystem effect",
            )
        return
    if probe_kind == "library_call":
        if not str(model.get("module") or "").strip() or not str(model.get("function") or "").strip():
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model",
                "library_call pass_model requires module and function",
            )
        if "expect_return" not in model and not str(model.get("expect_raises") or "").strip():
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model",
                "library_call pass_model requires expect_return or expect_raises",
            )
        return
    if probe_kind == "service_health":
        if not _command_list_like(model.get("start_command") or model.get("command")):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.start_command",
                "service_health pass_model requires start_command",
            )
        if not str(model.get("health_url") or "").strip():
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model.health_url",
                "service_health pass_model requires health_url",
            )
        if not _has_service_payload_assertion(model):
            raise VerificationContractError(
                "verification_contract_invalid",
                f"{path}.pass_model",
                "service_health pass_model requires health plus payload assertion",
            )
        return
    raise VerificationContractError(
        "verification_contract_invalid",
        f"{path}.probe_kind",
        f"unsupported probe_kind {probe_kind!r}",
    )


def _observable_is_strong(
    observable: dict[str, Any],
    *,
    covered_actions: set[str],
    step_actions: set[str],
) -> bool:
    kind = str(observable.get("kind") or "").strip()
    description = str(observable.get("description") or observable.get("text") or "").strip()
    if kind not in STRONG_OBSERVABLE_KINDS:
        return False
    if not description or any(pattern.search(description) for pattern in WEAK_OBSERVABLE_PATTERNS):
        return False

    action_id = str(
        observable.get("primary_action_id")
        or observable.get("covers_primary_action")
        or ""
    ).strip()
    if action_id and (action_id in covered_actions or action_id in step_actions):
        tied_to_action = True
    else:
        tied_to_action = bool(str(observable.get("entity_id") or observable.get("effect_entity") or "").strip())
    if not tied_to_action:
        return False

    if kind == "network_and_ui_effect":
        method = str(observable.get("method") or "").strip()
        path = str(observable.get("path") or "").strip()
        status = observable.get("status")
        ui_effect = str(observable.get("ui_effect") or "").strip()
        return bool(method and path.startswith("/") and status and ui_effect)

    return True


def _has_any_key(payload: dict[str, Any], keys: frozenset[str]) -> bool:
    return any(str(payload.get(key) or "").strip() for key in keys)


def _http_step_has_strong_assertion(step: dict[str, Any]) -> bool:
    return (
        "expect_json" in step
        or bool(str(step.get("expect_body_contains") or "").strip())
        or isinstance(step.get("extract"), dict) and bool(step.get("extract"))
        or bool(str(step.get("expect_json_path") or "").strip())
        or bool(str(step.get("expect_header") or "").strip())
    )


def _has_cli_success_effect(model: dict[str, Any]) -> bool:
    return (
        bool(model.get("stdout_contains"))
        or bool(model.get("stderr_contains"))
        or (isinstance(model.get("fs_effects"), list) and bool(model.get("fs_effects")))
    )


def _has_service_payload_assertion(model: dict[str, Any]) -> bool:
    return (
        bool(str(model.get("expect_body_contains") or "").strip())
        or "expect_json" in model
        or bool(str(model.get("expect_json_path") or "").strip())
    )


def _command_list_like(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value)


def _well_formed_entry_route(entry_route: str) -> bool:
    return bool(entry_route and entry_route.startswith("/") and not re.search(r"\s", entry_route))
