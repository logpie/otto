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

    journeys = normalized.get("behavior_journeys")
    if not isinstance(journeys, list):
        normalized["behavior_journeys"] = []
        return normalized

    raw_schema_version = normalized.get("schema_version")
    if raw_schema_version is None:
        schema_version = current_schema_version
    else:
        try:
            schema_version = int(raw_schema_version)
        except (TypeError, ValueError):
            schema_version = current_schema_version

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
            legacy_schema=schema_version < current_schema_version,
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

    if has_typed_level and existing_level != assigned_level:
        raise VerificationContractError(
            "verification_contract_invalid",
            f"{path}.verification_level",
            f"declares {existing_level!r}, expected {assigned_level!r}",
        )
    journey["verification_level"] = assigned_level

    if assigned_level == "api":
        existing_probe = journey.get("probe_kind")
        if isinstance(existing_probe, str) and existing_probe.strip():
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
        }
        actions.append({
            "id": f"{action_id}.effect",
            "description": str(journey.get("description") or f"Exercise {action_id}."),
            "state_changing": True,
            "covers_primary_actions": [action_id],
            "success_observables": [observable],
            "network_expectations": [],
        })
        final_assertions.append({
            "kind": "persisted_data_visible",
            "primary_action_id": action_id,
            "description": observable["description"],
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


def _well_formed_entry_route(entry_route: str) -> bool:
    return bool(entry_route and entry_route.startswith("/") and not re.search(r"\s", entry_route))
