"""Bind compiled certifier plans to concrete adapter-discovered runtime details.

The compilers decide what to test. The binder resolves how those steps should
run against the specific project under test without performing any network I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from otto.certifier.adapter import RouteInfo, SeededUser, TestConfig
from otto.certifier.classifier import ProductProfile
from otto.certifier.intent_compiler import Claim, RequirementMatrix
from otto.certifier.journey_compiler import JourneyMatrix, JourneySpec

_PARTIAL_TEMPLATE_RE = re.compile(r"\{\{([a-zA-Z0-9_.-]+)\}\}")


@dataclass
class BoundAuth:
    mechanism: str = ""
    email: str = ""
    password: str = ""
    name: str = ""
    role: str = "user"
    source: str = ""
    login_route: str = ""
    register_route: str = ""
    csrf_route: str = ""


@dataclass
class BoundStep:
    action: str
    method: str = ""
    step: dict[str, Any] = field(default_factory=dict)
    resolved_route: str = ""
    fallback_routes: list[str] = field(default_factory=list)
    resolved_body: Any = None
    auth: BoundAuth | None = None
    requires_auth: bool = False
    requires_admin: bool = False
    entity_discovery_paths: list[str] = field(default_factory=list)
    binding_notes: list[str] = field(default_factory=list)


@dataclass
class BoundClaim:
    id: str
    description: str
    priority: str
    category: str
    test_approach: str
    hard_fail: bool
    steps: list[BoundStep] = field(default_factory=list)
    structural_outcome: str | None = None
    structural_reason: str = ""
    structural_proof: dict[str, Any] = field(default_factory=dict)
    binding_notes: list[str] = field(default_factory=list)


@dataclass
class BoundJourney:
    name: str
    description: str
    persona: str
    critical: bool
    steps: list[BoundStep] = field(default_factory=list)
    binding_notes: list[str] = field(default_factory=list)


@dataclass
class BoundPlan:
    intent: str
    claims: list[BoundClaim] = field(default_factory=list)
    journeys: list[BoundJourney] = field(default_factory=list)
    product_type_hint: str = ""
    compiled_at: str = ""
    journeys_compiled_at: str = ""
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def bind(
    matrix: RequirementMatrix,
    journey_matrix: JourneyMatrix,
    test_config: TestConfig,
    profile: ProductProfile,
) -> BoundPlan:
    """Bind compiled claims and journeys to adapter-discovered runtime details."""

    bound_claims: list[BoundClaim] = []
    for claim in matrix.claims:
        scope_id = f"claim:{claim.id}"
        scope_identity: dict[str, BoundAuth] = {}
        steps: list[BoundStep] = []
        binding_notes: list[str] = []

        for step in claim.test_steps:
            bound_step = _bind_step(
                step,
                test_config=test_config,
                claim=claim,
                journey=None,
                scope_id=scope_id,
                scope_identity=scope_identity,
            )
            steps.append(bound_step)
            binding_notes.extend(bound_step.binding_notes)

        structural_outcome, structural_reason = _classify_structural_claim_outcome(claim, profile, test_config)
        structural_proof = _structural_claim_proof(claim, structural_outcome, structural_reason, test_config)
        bound_claims.append(
            BoundClaim(
                id=claim.id,
                description=claim.description,
                priority=claim.priority,
                category=claim.category,
                test_approach=claim.test_approach,
                hard_fail=claim.hard_fail,
                steps=steps,
                structural_outcome=structural_outcome,
                structural_reason=structural_reason,
                structural_proof=structural_proof,
                binding_notes=_dedupe_preserving_order(binding_notes),
            )
        )

    bound_journeys: list[BoundJourney] = []
    for journey in (journey_matrix.journeys if journey_matrix else []):
        scope_id = f"journey:{journey.name}"
        scope_identity: dict[str, BoundAuth] = {}
        steps: list[BoundStep] = []
        binding_notes: list[str] = []

        for step in journey.steps:
            bound_step = _bind_step(
                step,
                test_config=test_config,
                claim=None,
                journey=journey,
                scope_id=scope_id,
                scope_identity=scope_identity,
            )
            steps.append(bound_step)
            binding_notes.extend(bound_step.binding_notes)

        bound_journeys.append(
            BoundJourney(
                name=journey.name,
                description=journey.description,
                persona=journey.persona,
                critical=journey.critical,
                steps=steps,
                binding_notes=_dedupe_preserving_order(binding_notes),
            )
        )

    return BoundPlan(
        intent=matrix.intent or (journey_matrix.intent if journey_matrix else ""),
        claims=bound_claims,
        journeys=bound_journeys,
        product_type_hint=matrix.product_type_hint,
        compiled_at=matrix.compiled_at,
        journeys_compiled_at=journey_matrix.compiled_at if journey_matrix else "",
        cost_usd=float(matrix.cost_usd or 0.0) + float(journey_matrix.cost_usd if journey_matrix else 0.0),
        metadata={
            "auth_type": test_config.auth_type,
            "route_count": len(test_config.routes),
            "seeded_user_count": len(test_config.seeded_users),
        },
    )


def save_bound_plan(plan: BoundPlan, path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(plan), indent=2, default=str))


def load_bound_plan(path: Path | str) -> BoundPlan:
    data = json.loads(Path(path).read_text())
    return BoundPlan(
        intent=data.get("intent", ""),
        claims=[_bound_claim_from_dict(item) for item in data.get("claims", [])],
        journeys=[_bound_journey_from_dict(item) for item in data.get("journeys", [])],
        product_type_hint=data.get("product_type_hint", ""),
        compiled_at=data.get("compiled_at", ""),
        journeys_compiled_at=data.get("journeys_compiled_at", ""),
        cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
        metadata=dict(data.get("metadata", {})),
    )


def _bound_claim_from_dict(data: dict[str, Any]) -> BoundClaim:
    return BoundClaim(
        id=data.get("id", ""),
        description=data.get("description", ""),
        priority=data.get("priority", "important"),
        category=data.get("category", "feature"),
        test_approach=data.get("test_approach", "api"),
        hard_fail=bool(data.get("hard_fail", False)),
        steps=[_bound_step_from_dict(item) for item in data.get("steps", [])],
        structural_outcome=data.get("structural_outcome"),
        structural_reason=data.get("structural_reason", ""),
        structural_proof=dict(data.get("structural_proof", {})),
        binding_notes=list(data.get("binding_notes", [])),
    )


def _bound_journey_from_dict(data: dict[str, Any]) -> BoundJourney:
    return BoundJourney(
        name=data.get("name", ""),
        description=data.get("description", ""),
        persona=data.get("persona", "unknown"),
        critical=bool(data.get("critical", False)),
        steps=[_bound_step_from_dict(item) for item in data.get("steps", [])],
        binding_notes=list(data.get("binding_notes", [])),
    )


def _bound_step_from_dict(data: dict[str, Any]) -> BoundStep:
    auth_payload = data.get("auth")
    return BoundStep(
        action=data.get("action", ""),
        method=data.get("method", ""),
        step=dict(data.get("step", {})),
        resolved_route=data.get("resolved_route", ""),
        fallback_routes=list(data.get("fallback_routes", [])),
        resolved_body=data.get("resolved_body"),
        auth=BoundAuth(**auth_payload) if isinstance(auth_payload, dict) else None,
        requires_auth=bool(data.get("requires_auth", False)),
        requires_admin=bool(data.get("requires_admin", False)),
        entity_discovery_paths=list(data.get("entity_discovery_paths", [])),
        binding_notes=list(data.get("binding_notes", [])),
    )


def _bind_step(
    step: dict[str, Any],
    *,
    test_config: TestConfig,
    claim: Claim | None,
    journey: JourneySpec | None,
    scope_id: str,
    scope_identity: dict[str, BoundAuth],
) -> BoundStep:
    action = str(step.get("action", "")).lower()
    method = _step_method(action, step)
    resolved_route, fallback_routes, route_notes = _resolve_route(
        step,
        method=method,
        test_config=test_config,
        claim=claim,
        journey=journey,
    )
    route_info = _find_route_info(test_config, resolved_route, method)

    # Detect "negative test" steps that intentionally send bad data
    # Don't correct their bodies — the bad data is the test
    is_negative_test = _is_negative_test_step(step, claim)

    if is_negative_test:
        corrected_body = step.get("body")
        body_notes = ["negative test step — body preserved as-is (intentionally invalid)"]
    else:
        corrected_body, body_notes = _correct_body(
            step.get("body"),
            step=step,
            method=method,
            resolved_route=resolved_route,
            test_config=test_config,
            claim=claim,
            journey=journey,
        )
    auth = _resolve_auth(
        step,
        method=method,
        resolved_route=resolved_route,
        route_info=route_info,
        test_config=test_config,
        claim=claim,
        journey=journey,
        scope_id=scope_id,
        scope_identity=scope_identity,
    )
    corrected_body = _apply_auth_to_body(corrected_body, step, auth)
    entity_paths = _prebind_entity_discovery_paths(
        resolved_route=resolved_route,
        fallback_routes=fallback_routes,
        resolved_body=corrected_body,
        test_config=test_config,
    )
    return BoundStep(
        action=action,
        method=method,
        step=dict(step),
        resolved_route=resolved_route,
        fallback_routes=fallback_routes,
        resolved_body=corrected_body,
        auth=auth,
        requires_auth=bool(route_info and route_info.requires_auth),
        requires_admin=bool(route_info and route_info.requires_admin),
        entity_discovery_paths=entity_paths,
        binding_notes=_dedupe_preserving_order(route_notes + body_notes + _auth_notes(auth)),
    )


def _step_method(action: str, step: dict[str, Any]) -> str:
    if action == "navigate":
        return "GET"
    if action in {"get", "post", "put", "patch", "delete"}:
        return action.upper()
    return str(step.get("method", "GET")).upper()


def _resolve_route(
    step: dict[str, Any],
    *,
    method: str,
    test_config: TestConfig,
    claim: Claim | None,
    journey: JourneySpec | None,
) -> tuple[str, list[str], list[str]]:
    notes: list[str] = []
    candidates: list[str] = []
    raw_paths = _step_candidate_paths(step)
    if raw_paths:
        candidates.extend(raw_paths)

    if _is_registration_step(step):
        preferred = _register_paths(test_config)
        if preferred:
            notes.append("bound registration step to adapter-discovered register routes")
            candidates = preferred + candidates
    elif _is_login_step(step):
        preferred = _login_paths(test_config)
        if preferred:
            notes.append("bound login step to adapter-discovered login routes")
            candidates = preferred + candidates

    claim_key = (claim.id if claim else "").lower()
    if claim_key in {"auth-protected-route", "auth-protected-routes"}:
        protected = [
            route.path
            for route in test_config.routes
            if method in route.methods and route.requires_auth and not route.requires_admin
        ]
        if protected:
            notes.append("preferring adapter-protected routes for protected-route claim")
            candidates = protected + candidates

    if claim_key.startswith("admin-") and any(token in claim_key for token in ("public", "authorization", "access", "login")):
        admin_paths = [
            route.path
            for route in test_config.routes
            if method in route.methods and route.requires_admin
        ]
        if admin_paths:
            notes.append("preferring adapter admin-only routes for admin access claim")
            candidates = admin_paths + candidates

    candidates = _dedupe_preserving_order(_expand_auth_aliases(candidates))
    filtered = _filter_candidate_paths(candidates, method, test_config)

    keywords = _binding_keywords(claim, journey, step, candidates)
    adapter_matches = _adapter_candidate_paths(keywords, method, test_config, filtered)
    if adapter_matches:
        notes.append("added adapter-discovered route matches by keyword")

    combined = _dedupe_preserving_order(filtered + adapter_matches)
    if not combined and candidates:
        notes.append("no adapter route match; keeping compiled candidate path as fallback")
        combined = _dedupe_preserving_order(candidates)

    resolved_route = combined[0] if combined else ""
    fallback_routes = combined[1:] if len(combined) > 1 else []
    return resolved_route, fallback_routes, notes


def _filter_candidate_paths(candidate_paths: list[str], method: str, test_config: TestConfig) -> list[str]:
    if not test_config.routes:
        return _dedupe_preserving_order(candidate_paths)

    filtered: list[str] = []
    for path in candidate_paths:
        if not _is_api_path(path):
            filtered.append(path)
            continue
        if _is_nextauth_internal_path(path, test_config):
            filtered.append(path)
            continue
        if _route_exists_in_config(test_config, path, method):
            filtered.append(path)
    return _dedupe_preserving_order(filtered)


def _adapter_candidate_paths(
    keywords: set[str],
    method: str,
    test_config: TestConfig,
    existing_paths: list[str],
) -> list[str]:
    if not keywords:
        return []

    existing_normalized = {_normalize_path(path) for path in existing_paths if path}
    matches: list[str] = []
    for route in test_config.routes:
        if method.upper() not in route.methods:
            continue
        candidate = _route_path_to_candidate(route.path)
        if _normalize_path(candidate) in existing_normalized:
            continue
        route_keywords = _route_keywords(route.path)
        if route_keywords.intersection(keywords):
            matches.append(candidate)
    return _dedupe_preserving_order(matches)


def _correct_body(
    body: Any,
    *,
    step: dict[str, Any],
    method: str,
    resolved_route: str,
    test_config: TestConfig,
    claim: Claim | None,
    journey: JourneySpec | None,
) -> tuple[Any, list[str]]:
    if not isinstance(body, dict):
        return body, []

    notes: list[str] = []
    corrected = dict(body)
    target_model = _target_model_for_route(resolved_route, test_config)
    if not target_model:
        target_model = _target_model_from_text(claim, journey, resolved_route, test_config)

    model_fields = test_config.model_fields.get(target_model, {}) if target_model else {}
    all_schema_fields = {
        field_name
        for fields in test_config.model_fields.values()
        for field_name in fields
    }

    for original_key in list(corrected):
        if target_model:
            renamed = _rename_key_to_model_field(original_key, model_fields)
        else:
            renamed = _rename_key_to_known_field(original_key, all_schema_fields)
        if renamed and renamed != original_key and renamed not in corrected:
            corrected[renamed] = corrected.pop(original_key)
            notes.append(f"corrected field casing: {original_key} -> {renamed}")

    for key, value in list(corrected.items()):
        if not isinstance(value, str):
            continue
        enum_values = _field_enum_values(test_config, target_model, key)
        if not enum_values:
            continue
        corrected_value = _case_correct_enum_value(value, enum_values)
        if corrected_value != value:
            corrected[key] = corrected_value
            notes.append(f"corrected enum casing: {key} {value} -> {corrected_value}")

    if method in {"POST", "PUT", "PATCH"} and target_model:
        required_fields = test_config.creatable_fields.get(target_model, [])
        for field_name in required_fields:
            if field_name in corrected:
                continue
            # Skip foreign key fields (server-managed, not client-supplied)
            # e.g., userId, authorId, createdById — these are set from the session
            if field_name.endswith("Id") and field_name != "id":
                continue
            default_value = _default_value_for_field(
                field_name,
                field_type=model_fields.get(field_name, ""),
                route=resolved_route,
                test_config=test_config,
            )
            if default_value is _MISSING:
                continue
            corrected[field_name] = default_value
            notes.append(f"added missing creatable field: {field_name}")

    return corrected, notes


def _is_negative_test_step(step: dict[str, Any], claim: Claim | None) -> bool:
    """Check if this step intentionally sends bad/missing data to test error handling.

    These steps should NOT have their bodies corrected — the bad data IS the test.
    """
    expect_status = step.get("expect_status", [])
    if isinstance(expect_status, int):
        expect_status = [expect_status]
    # Step expects client error codes (validation failure, auth rejection)
    if any(s in expect_status for s in (400, 422)):
        return True
    # Claim ID suggests error/validation testing
    if claim:
        claim_lower = claim.id.lower()
        if any(kw in claim_lower for kw in ("invalid", "validation", "missing", "duplicate", "nonexistent", "not-found", "reject")):
            return True
    return False


def _step_tests_unauthenticated_access(step: dict[str, Any], claim: Claim | None) -> bool:
    """Check if this step intentionally tests unauthenticated/unauthorized access.

    These steps MUST NOT be authenticated — the whole point is to verify rejection.
    Note: login steps that expect 401 are testing INVALID credentials, not
    unauthenticated access. Those still need auth binding (with wrong password).
    """
    action = str(step.get("action", "")).lower()
    # Login/register steps are NEVER "unauthenticated access" tests —
    # they test the auth mechanism itself (possibly with wrong creds)
    if action in ("login", "login_admin", "register") or _is_login_step(step) or _is_registration_step(step):
        return False

    expect_status = step.get("expect_status", [])
    if isinstance(expect_status, int):
        expect_status = [expect_status]
    # Non-login step explicitly expects 401 or 403
    if any(s in expect_status for s in (401, 403)):
        return True
    # Claim ID suggests auth protection testing
    if claim:
        claim_lower = claim.id.lower()
        if any(kw in claim_lower for kw in ("protected", "unauthenticated", "unauthorized", "no-public")):
            return True
    return False


def _resolve_auth(
    step: dict[str, Any],
    *,
    method: str,
    resolved_route: str,
    route_info: RouteInfo | None,
    test_config: TestConfig,
    claim: Claim | None,
    journey: JourneySpec | None,
    scope_id: str,
    scope_identity: dict[str, BoundAuth],
) -> BoundAuth | None:
    # Steps testing unauthenticated access must NOT be authenticated
    if _step_tests_unauthenticated_access(step, claim):
        return None

    mechanism = _auth_mechanism(test_config)
    action = str(step.get("action", "")).lower()
    claim_key = (claim.id if claim else "").lower()
    persona = (journey.persona if journey else "").lower()

    # Login steps in negative tests (expect 401) need WRONG credentials
    if _is_negative_test_step(step, claim) and (action in ("login", "login_admin") or _is_login_step(step)):
        user = _preferred_seeded_user(test_config, claim_key, persona)
        if user:
            auth = BoundAuth(
                email=user.email,
                password="WRONG_PASSWORD_FOR_NEGATIVE_TEST",
                name=user.name if hasattr(user, "name") else "",
                role=user.role,
                mechanism=mechanism,
                login_route=test_config.login_endpoint or _first_non_empty(test_config.login_candidates) or "",
                csrf_route="/api/auth/csrf" if mechanism == "nextauth" else "",
                register_route="",
            )
            return auth

    if action == "login_admin":
        return _seeded_auth(
            test_config.admin_user() or test_config.any_user(),
            mechanism=mechanism,
            role_hint="admin",
            test_config=test_config,
        )

    if _is_registration_step(step) or action == "register":
        auth = scope_identity.get("generated")
        if auth is None:
            auth = _generated_auth(scope_id)
            auth.mechanism = mechanism
            auth.register_route = test_config.register_endpoint or _first_non_empty(test_config.register_candidates)
            auth.login_route = test_config.login_endpoint or _first_non_empty(test_config.login_candidates)
            auth.csrf_route = "/api/auth/csrf" if mechanism == "nextauth" else ""
            scope_identity["generated"] = auth
        return auth

    if _is_login_step(step) or action == "login":
        if persona.startswith("new_") and scope_identity.get("generated"):
            auth = scope_identity["generated"]
        elif claim_key.startswith("admin-") or persona == "admin":
            auth = _seeded_auth(
                test_config.admin_user() or test_config.any_user(),
                mechanism=mechanism,
                role_hint="admin",
                test_config=test_config,
            )
        else:
            auth = _seeded_auth(
                _preferred_seeded_user(test_config, claim_key, persona),
                mechanism=mechanism,
                role_hint="user",
                test_config=test_config,
            )
        return auth

    if route_info and (route_info.requires_auth or route_info.requires_admin):
        user = (
            test_config.admin_user()
            if route_info.requires_admin
            else _preferred_seeded_user(test_config, claim_key, persona)
        )
        auth = _seeded_auth(
            user,
            mechanism=mechanism,
            role_hint="admin" if route_info.requires_admin else "user",
            test_config=test_config,
        )
        if auth is not None:
            return auth
        return scope_identity.get("generated")

    if claim_key == "auth-login":
        return _seeded_auth(
            test_config.admin_user() or test_config.any_user(),
            mechanism=mechanism,
            role_hint="admin",
            test_config=test_config,
        )

    return None


def _prebind_entity_discovery_paths(
    *,
    resolved_route: str,
    fallback_routes: list[str],
    resolved_body: Any,
    test_config: TestConfig,
) -> list[str]:
    entity_names: set[str] = set()
    for variable_name in _template_variables_in_value(
        {
            "route": resolved_route,
            "fallback_routes": fallback_routes,
            "body": resolved_body,
        }
    ):
        entity_name = _entity_name_from_template_var(variable_name)
        if entity_name:
            entity_names.add(entity_name)

    for path in [resolved_route, *fallback_routes]:
        entity_name = _entity_name_from_api_path(path)
        collection_path = _collection_path_from_api_path(path)
        if entity_name and collection_path and _normalize_path(path) != collection_path:
            entity_names.add(entity_name)

    collection_paths: list[str] = []
    for entity_name in sorted(entity_names):
        collection_paths.extend(_collection_routes_for_entity(entity_name, test_config))
    return _dedupe_preserving_order(collection_paths)


def _collection_routes_for_entity(entity_name: str, test_config: TestConfig) -> list[str]:
    plural_name = _pluralize_entity_name(entity_name)
    candidates = [f"/api/{plural_name}", f"/api/{entity_name}"]
    for route in test_config.routes:
        if "GET" not in route.methods:
            continue
        collection_path = _collection_path_from_api_path(route.path)
        if not collection_path:
            continue
        if _entity_name_from_api_path(collection_path) == entity_name:
            candidates.append(collection_path)
    return _dedupe_preserving_order(candidates)


def _classify_structural_claim_outcome(
    claim: Claim,
    profile: ProductProfile,
    test_config: TestConfig,
) -> tuple[str | None, str]:
    if claim.test_approach in {"api", "browser"} and profile.product_type in {"cli", "desktop", "library"}:
        return "not_applicable", f"{profile.product_type} product does not expose {claim.test_approach} probes"
    if claim.test_approach == "cli" and profile.interaction not in {"cli", "unknown"}:
        return "not_applicable", f"{profile.product_type} product does not expose a CLI surface"

    claim_key = claim.id.lower()
    if ("cart" in claim_key or claim_key == "ux-empty-cart") and not _config_has_resource_model(test_config, "cart"):
        return "not_implemented", "adapter found no cart model in the codebase"
    if claim_key == "auth-register" and not test_config.register_endpoint:
        return "not_implemented", "adapter found no registration endpoint in the codebase"
    if _claim_uses_seed_auth_steps(claim) and not test_config.seeded_users and not test_config.register_endpoint:
        return "blocked_by_harness", "claim needs an authenticated user but adapter found neither seeded credentials nor a register endpoint"
    return None, ""


def _structural_claim_proof(
    claim: Claim,
    structural_outcome: str | None,
    structural_reason: str,
    test_config: TestConfig,
) -> dict[str, Any]:
    if not structural_outcome:
        return {}
    if structural_outcome == "not_implemented":
        claim_key = claim.id.lower()
        if "cart" in claim_key:
            return {
                "adapter_check": "searched adapter resource inventory for a cart model",
                "models_found": test_config.resource_models,
                "missing": "cart-related resource model",
                "source_file": test_config.schema_source_file or "prisma/schema.prisma",
            }
        if claim_key == "auth-register":
            return {
                "adapter_check": "searched for registration endpoint in route inventory",
                "routes_found": [route.path for route in test_config.routes[:20]],
                "missing": "registration endpoint",
            }
    return {
        "adapter_check": "structural claim precheck",
        "reason": structural_reason,
    }


def _step_candidate_paths(step: dict[str, Any]) -> list[str]:
    candidate_paths = step.get("candidate_paths")
    if isinstance(candidate_paths, list) and candidate_paths:
        return [str(item) for item in candidate_paths]
    path = step.get("path")
    if path:
        return [str(path)]
    return []


def _expand_auth_aliases(paths: list[str]) -> list[str]:
    expanded: list[str] = []
    for raw_path in paths:
        if not raw_path:
            continue
        expanded.append(raw_path)
        lower = raw_path.lower()
        if "register" in lower or "signup" in lower:
            expanded.extend(["/api/auth/register", "/api/register", "/api/signup", "/api/auth/signup", "/register", "/signup"])
        elif "login" in lower or "signin" in lower:
            expanded.extend(["/api/auth/login", "/api/login", "/api/signin", "/api/auth/signin", "/login", "/signin"])
    return expanded


def _binding_keywords(
    claim: Claim | None,
    journey: JourneySpec | None,
    step: dict[str, Any],
    candidate_paths: list[str],
) -> set[str]:
    text_parts = [
        claim.id if claim else "",
        claim.description if claim else "",
        claim.category if claim else "",
        journey.name if journey else "",
        journey.description if journey else "",
        journey.persona if journey else "",
    ]
    keywords: set[str] = set()
    for text in text_parts:
        for token in re.split(r"[^a-zA-Z0-9]+", text.lower()):
            if token and token not in {"auth", "user", "users", "the", "a", "an", "can", "and"}:
                keywords.add(token)
    for path in candidate_paths + _step_candidate_paths(step):
        keywords.update(_route_keywords(path))
    return {token for token in keywords if token}


def _register_paths(test_config: TestConfig) -> list[str]:
    paths = []
    if test_config.register_endpoint:
        paths.append(test_config.register_endpoint)
    paths.extend(test_config.register_candidates)
    for route in test_config.routes:
        if "POST" in route.methods and any(token in route.path.lower() for token in ("register", "signup")):
            paths.append(route.path)
    return _dedupe_preserving_order(paths)


def _login_paths(test_config: TestConfig) -> list[str]:
    paths = []
    if test_config.login_endpoint:
        paths.append(test_config.login_endpoint)
    paths.extend(test_config.login_candidates)
    if test_config.auth_type == "nextauth":
        paths.append("/api/auth/callback/credentials")
    for route in test_config.routes:
        if "POST" in route.methods and any(token in route.path.lower() for token in ("login", "signin")):
            paths.append(route.path)
    return _dedupe_preserving_order(paths)


def _find_route_info(test_config: TestConfig, path: str, method: str) -> RouteInfo | None:
    if not path:
        return None
    normalized = _normalize_path(path)
    for route in test_config.routes:
        if method.upper() not in route.methods:
            continue
        if re.fullmatch(_route_match_pattern(route.path), normalized):
            return route
    return None


def _route_exists_in_config(test_config: TestConfig, path: str, method: str) -> bool:
    return _find_route_info(test_config, path, method) is not None


def _route_match_pattern(path: str) -> str:
    normalized = _normalize_path(path)
    escaped = re.escape(normalized)
    escaped = re.sub(r"\\\{\\\{[^}]+\\\}\\\}", r"[^/]+", escaped)
    escaped = re.sub(r":[^/]+", r"[^/]+", escaped)
    return escaped


def _is_nextauth_internal_path(path: str, test_config: TestConfig) -> bool:
    normalized = _normalize_path(path)
    return test_config.auth_type == "nextauth" and normalized in {
        "/api/auth/csrf",
        "/api/auth/callback/credentials",
        "/api/auth/session",
    }


def _is_api_path(path: str) -> bool:
    return _normalize_path(path).startswith("/api/")


def _normalize_path(path: str) -> str:
    trimmed = str(path).split("?", 1)[0].strip()
    if not trimmed:
        return ""
    if not trimmed.startswith("/"):
        trimmed = f"/{trimmed}"
    if trimmed != "/" and trimmed.endswith("/"):
        trimmed = trimmed[:-1]
    return trimmed


def _route_keywords(path: str) -> set[str]:
    keywords: set[str] = set()
    normalized = _normalize_path(path)
    for segment in normalized.strip("/").split("/"):
        clean = _clean_path_segment(segment)
        if not clean or clean in {"api", "v1", "v2", "admin", "auth", "internal"}:
            continue
        keywords.add(_normalize_entity_name(clean))
    entity_name = _entity_name_from_api_path(path)
    if entity_name:
        keywords.add(entity_name)
        keywords.add(_pluralize_entity_name(entity_name))
    return {token for token in keywords if token}


def _route_path_to_candidate(path: str) -> str:
    entity_name = _entity_name_from_api_path(path)
    segments: list[str] = []
    for segment in _normalize_path(path).strip("/").split("/"):
        if segment.startswith(":"):
            param_name = segment[1:]
            if param_name == "id" and entity_name:
                param_name = f"{entity_name}_id"
            segments.append(f"{{{{{_normalize_template_name(param_name)}}}}}")
            continue
        segments.append(segment)
    return "/" + "/".join(segment for segment in segments if segment)


def _target_model_for_route(route: str, test_config: TestConfig) -> str:
    entity_name = _entity_name_from_api_path(route)
    if not entity_name:
        return ""
    for model_name in test_config.models:
        normalized = _normalize_entity_name(model_name)
        if normalized == entity_name or _pluralize_entity_name(normalized) == entity_name:
            return model_name
    return ""


def _target_model_from_text(
    claim: Claim | None,
    journey: JourneySpec | None,
    resolved_route: str,
    test_config: TestConfig,
) -> str:
    candidates = " ".join(
        [
            claim.id if claim else "",
            claim.description if claim else "",
            journey.name if journey else "",
            journey.description if journey else "",
            resolved_route,
        ]
    ).lower()
    for model_name in test_config.models:
        token = _normalize_entity_name(model_name)
        if token and (token in candidates or _pluralize_entity_name(token) in candidates):
            return model_name
    return ""


def _rename_key_to_model_field(key: str, model_fields: dict[str, str]) -> str:
    if key in model_fields:
        return key
    key_snake = _camel_to_snake(key)
    key_camel = _snake_to_camel(key)
    for field_name in model_fields:
        if field_name in {key_snake, key_camel}:
            return field_name
    return key


def _rename_key_to_known_field(key: str, known_fields: set[str]) -> str:
    if key in known_fields:
        return key
    for candidate in (_camel_to_snake(key), _snake_to_camel(key)):
        if candidate in known_fields:
            return candidate
    return key


def _field_enum_values(test_config: TestConfig, target_model: str, field_name: str) -> list[str]:
    if target_model and f"{target_model}.{field_name}" in test_config.enum_values:
        return list(test_config.enum_values[f"{target_model}.{field_name}"])
    if field_name in test_config.enum_values:
        return list(test_config.enum_values[field_name])
    if target_model:
        field_type = test_config.model_fields.get(target_model, {}).get(field_name, "")
        if field_type in test_config.enum_values:
            return list(test_config.enum_values[field_type])
    return []


def _case_correct_enum_value(value: str, valid_values: list[str]) -> str:
    for candidate in valid_values:
        if value.lower() == candidate.lower():
            return candidate
    return value


def _default_value_for_field(
    field_name: str,
    *,
    field_type: str,
    route: str,
    test_config: TestConfig,
) -> Any:
    field_lower = field_name.lower()
    enum_values = test_config.enum_values.get(field_type) or test_config.enum_values.get(field_name)
    if enum_values:
        return enum_values[0]

    defaults: dict[str, Any] = {
        "name": "Bound Test User",
        "title": "Bound Test Item",
        "description": "Generated by certifier binder",
        "email": "bound-user@eval.local",
        "password": "BoundTest123!",
        "price": 29.99,
        "amount": 29.99,
        "category": "General",
        "stock": 100,
        "inventory": 100,
        "quantity": 1,
        "status": "TODO",
        "imageurl": "https://placehold.co/400x300",
        "image_url": "https://placehold.co/400x300",
    }
    if field_lower in defaults:
        return defaults[field_lower]
    if field_lower.endswith("id"):
        entity_name = _normalize_entity_name(field_name[:-2])
        if entity_name:
            return f"{{{{{entity_name}_id}}}}"
    if field_type in {"Int", "BigInt"}:
        return 1
    if field_type in {"Float", "Decimal"}:
        return 1.0
    if field_type == "Boolean":
        return False
    if field_type == "DateTime":
        return "2026-01-01T00:00:00Z"
    if "checkout" in route.lower() and field_lower in {"address", "city", "state", "zip", "country"}:
        shipping = _default_shipping_address()
        return shipping.get(field_name) or shipping.get(field_lower)
    return _MISSING


def _default_shipping_address() -> dict[str, str]:
    return {
        "name": "Bound Test User",
        "address": "123 Market Street",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "country": "US",
    }


def _apply_auth_to_body(body: Any, step: dict[str, Any], auth: BoundAuth | None) -> Any:
    if auth is None:
        return body
    action = str(step.get("action", "")).lower()
    if action not in {"register", "login", "login_admin"} and not (_is_registration_step(step) or _is_login_step(step)):
        return body

    resolved = dict(body) if isinstance(body, dict) else {}
    if auth.email:
        resolved.setdefault("email", auth.email)
        if "username" in resolved and not resolved.get("username"):
            resolved["username"] = auth.email
    if auth.password:
        resolved.setdefault("password", auth.password)
    if auth.name:
        for name_key in ("name", "username"):
            if name_key in resolved or action == "register" or _is_registration_step(step):
                resolved.setdefault(name_key, auth.name)
                break
    return resolved or body


def _auth_notes(auth: BoundAuth | None) -> list[str]:
    if auth is None:
        return []
    if auth.source == "generated":
        return [f"bound auth to generated {auth.role} credentials"]
    if auth.source == "seeded":
        return [f"bound auth to seeded {auth.role} credentials ({auth.email})"]
    if auth.source == "missing":
        return [f"auth requires {auth.role} credentials but adapter did not recover usable ones"]
    return []


def _seeded_auth(
    user: SeededUser | None,
    *,
    mechanism: str,
    role_hint: str,
    test_config: TestConfig,
) -> BoundAuth | None:
    if user is None:
        return None
    source = "seeded" if user.password else "missing"
    return BoundAuth(
        mechanism=mechanism,
        email=user.email,
        password=user.password,
        name=user.email.split("@", 1)[0],
        role=user.role or role_hint,
        source=source,
        login_route=test_config.login_endpoint or _first_non_empty(test_config.login_candidates),
        register_route=test_config.register_endpoint or _first_non_empty(test_config.register_candidates),
        csrf_route="/api/auth/csrf" if mechanism == "nextauth" else "",
    )


def _generated_auth(scope_id: str) -> BoundAuth:
    digest = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:10]
    return BoundAuth(
        mechanism="unknown",
        email=f"bound-{digest}@eval.local",
        password="BoundTest123!",
        name="Bound Test User",
        role="user",
        source="generated",
    )


def _preferred_seeded_user(test_config: TestConfig, claim_key: str, persona: str) -> SeededUser | None:
    if claim_key == "admin-auth":
        for user in test_config.seeded_users:
            if user.role.lower() not in {"admin", "administrator"}:
                return user
    if claim_key.startswith("admin-") or claim_key in {"auth-login", "auth-logout"} or persona == "admin":
        return test_config.admin_user() or test_config.any_user()
    for user in test_config.seeded_users:
        if user.role.lower() not in {"admin", "administrator"}:
            return user
    return test_config.any_user()


def _auth_mechanism(test_config: TestConfig) -> str:
    return test_config.auth_type if test_config.auth_type and test_config.auth_type != "unknown" else "session"


def _is_registration_step(step: dict[str, Any]) -> bool:
    action = str(step.get("action", "")).lower()
    if action == "register":
        return True
    if action != "http":
        return False
    if str(step.get("method", "GET")).upper() != "POST":
        return False
    return any(token in path.lower() for path in _step_candidate_paths(step) for token in ("register", "signup"))


def _is_login_step(step: dict[str, Any]) -> bool:
    action = str(step.get("action", "")).lower()
    if action in {"login", "login_admin"}:
        return True
    if action != "http":
        return False
    if str(step.get("method", "GET")).upper() != "POST":
        return False
    return any(token in path.lower() for path in _step_candidate_paths(step) for token in ("login", "signin"))


def _config_has_resource_model(test_config: TestConfig, resource_name: str) -> bool:
    token = resource_name.lower()
    return any(token in model.lower() for model in test_config.resource_models)


def _claim_uses_seed_auth_steps(claim: Claim) -> bool:
    return any(
        isinstance(step, dict) and (_is_registration_step(step) or _is_login_step(step))
        for step in claim.test_steps
    )


def _template_variables_in_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [match.group(1) for match in _PARTIAL_TEMPLATE_RE.finditer(value)]
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_template_variables_in_value(item))
        return found
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_template_variables_in_value(item))
        return found
    return []


def _entity_name_from_template_var(variable_name: str) -> str:
    token = variable_name.rsplit(".", 1)[-1]
    if not token.endswith("_id"):
        return ""
    entity_name = token[:-3].rstrip("_")
    if entity_name in {"last", "last_created", "first_item"}:
        return ""
    return _normalize_entity_name(entity_name)


def _entity_name_from_api_path(path: str) -> str:
    normalized = _normalize_path(path)
    segments = [segment for segment in normalized.strip("/").split("/") if segment]
    if not segments or segments[0] != "api":
        return ""
    candidates: list[str] = []
    for segment in segments[1:]:
        clean = _clean_path_segment(segment)
        if not clean or clean in {"api", "v1", "v2", "admin", "auth", "internal"}:
            continue
        candidates.append(clean)
    if not candidates:
        return ""
    return _normalize_entity_name(candidates[-1])


def _collection_path_from_api_path(path: str) -> str:
    normalized = _normalize_path(path)
    segments = [segment for segment in normalized.strip("/").split("/") if segment]
    if not segments or segments[0] != "api":
        return ""
    last_collection_index: int | None = None
    for index, segment in enumerate(segments[1:], start=1):
        if segment.startswith(":") or segment.startswith("{{"):
            break
        clean = _clean_path_segment(segment)
        if not clean or clean in {"api", "v1", "v2", "admin", "auth", "internal"}:
            continue
        last_collection_index = index
    if last_collection_index is None:
        return ""
    return "/" + "/".join(segments[:last_collection_index + 1])


def _clean_path_segment(segment: str) -> str:
    if segment.startswith("{{") and segment.endswith("}}"):
        return _entity_name_from_template_var(segment[2:-2])
    if segment.startswith(":"):
        param_name = _normalize_template_name(segment[1:])
        if param_name == "id":
            return ""
        return _entity_name_from_template_var(param_name)
    return _normalize_entity_name(segment)


def _normalize_entity_name(value: str) -> str:
    normalized = _normalize_template_name(value)
    if normalized.endswith("_id"):
        normalized = normalized[:-3].rstrip("_")
    return _singularize_entity_name(normalized)


def _normalize_template_name(value: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    snake = re.sub(r"[^a-zA-Z0-9_]+", "_", snake)
    return snake.strip("_").lower()


def _singularize_entity_name(entity_name: str) -> str:
    if entity_name.endswith("ies") and len(entity_name) > 3:
        return entity_name[:-3] + "y"
    if entity_name.endswith("s") and not entity_name.endswith(("ss", "us")):
        return entity_name[:-1]
    return entity_name


def _pluralize_entity_name(entity_name: str) -> str:
    if entity_name.endswith("y") and len(entity_name) > 1 and entity_name[-2] not in "aeiou":
        return entity_name[:-1] + "ies"
    if entity_name.endswith("s"):
        return entity_name
    return entity_name + "s"


def _snake_to_camel(value: str) -> str:
    if "_" not in value:
        return value
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:] if part)


def _camel_to_snake(value: str) -> str:
    if "_" in value:
        return value
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _first_non_empty(values: list[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


class _Missing:
    pass


_MISSING = _Missing()
