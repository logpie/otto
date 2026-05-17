"""Flat spec compilation for v5.

Replaces v4's group/contract synthesis. Emits:
    - the user's intent (verbatim)
    - project_kind (detected)
    - intent_claims[]: raw claims extracted from intent.md
    - core_entities[]: product entities, fields, states, primary actions
    - cold_start_states[] / permissions[] / quality_constraints[]
    - behavior_journeys[]: capped typed verification journeys

NO groups, NO owned_paths, NO shared_contracts, NO frozen ownership of any kind.
The Lead at runtime decides decomposition; integration audit at every merge node
provides scope accountability.

Behavior journeys MUST be written in user-language. A lint pass rejects
implementation-language tokens (CSS selectors, getByRole, data-testid, etc.)
and re-prompts the compiler on rejection. After 2 retries, the compile is
written with a `lint_warnings` list so the user can see the issue but the run
continues (best-effort).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import __version__ as OTTO_VERSION
from otto.agent import make_agent_options
from otto.journey_contracts import (
    PASS_MODEL_KEYS,
    PROBE_KINDS,
    VERIFICATION_LEVELS,
    VerificationContractError,
    normalize_journey_contracts,
)
from otto.observability import save_rendered_prompt, sha256_text, update_input_provenance
from otto.paths import session_intent
from otto.token_usage import token_usage_from_mapping
from otto.v5_spec_cache import (
    cache_key_payload,
    lookup_spec_cache,
    store_spec_cache,
)

logger = logging.getLogger("otto.spec_compile_flat")

SCHEMA_VERSION = 4
INTENT_CLAIMS_MAX = 30
ROOT_SPEC_ARTIFACT_FILENAMES = frozenset({
    "product-contract.json",
    "product_contract.json",
    "spec.json",
    "flat-spec.json",
    "flat_spec.json",
    "otto-spec.json",
    "otto_spec.json",
})
# Bounded spec-repair attempts for pass-model verification-contract failures.
# Was 2 — run #9 (mib9) died at compile: the spec model emitted an http_api
# journey whose pass_model.steps lacked the required payload assertion, and 2
# repair attempts could not add one (spec_contract_repair_exhausted →
# merge_blocked before decomposition even started). The contract itself is a
# legitimate anti-false-pass guard (an API journey asserting nothing must not
# pass) and must NOT be relaxed; the brittle part is the tiny attempt cap.
# Each attempt is one cheap spec LLM call and run_budget_seconds bounds the
# total, so give the repair real room (same "rigid low limit is the villain"
# pattern as the 400→1200s repair wall-clock).
PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS = 5
PASS_MODEL_CONTRACT_REPAIR_CODES = frozenset({
    "verification_contract_invalid",
    "verification_contract_missing",
})


class StructuredSpecValidationError(ValueError):
    """Raised when a newly compiled structured flat spec violates hard rules."""


class SpecContractRepairExhaustedError(StructuredSpecValidationError):
    """Raised when bounded pass-model contract repair cannot produce a valid spec."""

    def __init__(self, exc: VerificationContractError, *, attempts: int) -> None:
        self.code = exc.code
        self.path = exc.path
        self.message = exc.message
        self.attempts = attempts
        super().__init__(
            f"{exc.code} at {exc.path}: {exc.message} "
            f"(pass-model contract repair exhausted after {attempts} attempts)"
        )


@dataclass
class FlatSpec:
    """The structured contract v5 produces for hierarchical runs."""

    schema_version: int = SCHEMA_VERSION
    intent: str = ""
    intent_hash: str = ""
    project_kind: str = "webapp"
    product_overview: dict[str, Any] = field(default_factory=dict)
    intent_claims: list[dict[str, Any]] = field(default_factory=list)
    core_entities: list[dict[str, Any]] = field(default_factory=list)
    cold_start_states: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    quality_constraints: list[dict[str, Any]] = field(default_factory=list)
    behavior_journeys: list[dict[str, Any]] = field(default_factory=list)
    lint_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _PassModelRepairRequest:
    payload: dict[str, Any]
    error: VerificationContractError
    target: str


# ---------------------------------------------------------------------------
# Behavior journey lint
# ---------------------------------------------------------------------------

# Patterns that indicate implementation-language leakage in journey text.
# A behavior journey is supposed to read like a user manual ("the user clicks
# 'Save'"), not like a Playwright test ("page.getByRole('button', ...)").
_IMPL_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bclass\s*=\s*['\"][^'\"]+['\"]", "CSS class selector"),
    (r"\bid\s*=\s*['\"][^'\"]+['\"]", "DOM id selector"),
    (r"\bdata-testid\b", "data-testid attribute"),
    (r"\bgetByRole\b", "Playwright getByRole"),
    (r"\bgetByText\b", "Playwright getByText"),
    (r"\bquerySelector\b", "DOM querySelector"),
    (r"page\.\w+\(", "Playwright page.*() API"),
    (r"\.locator\(", "Playwright locator"),
    (r"\bbody\s*>\s*", "CSS descendant selector"),
    (r"#[a-zA-Z][\w-]*\s*\{", "CSS id rule"),
    (r"\.[a-zA-Z][\w-]*\s*\{", "CSS class rule"),
)


def lint_journey(text: str) -> list[str]:
    """Return a list of human-readable warnings for one journey description.

    Empty list = clean (user-language only).
    """
    issues: list[str] = []
    for pattern, label in _IMPL_LEAK_PATTERNS:
        if re.search(pattern, text):
            issues.append(f"contains {label}")
    return issues


def _coerce_spec(spec: Any) -> dict[str, Any]:
    """Return a JSON-shaped flat spec payload.

    Public flat-spec validators are called both with the in-memory ``FlatSpec``
    returned by the compiler and with ``json.loads(spec.json)`` dictionaries
    from runner-side code. Normalize once at the boundary so validation logic
    does not accidentally depend on either representation.
    """
    if isinstance(spec, dict):
        return dict(spec)
    if is_dataclass(spec) and not isinstance(spec, type):
        payload = asdict(spec)
        return payload if isinstance(payload, dict) else {}
    return {}


def lint_spec(spec: Any) -> list[str]:
    """Lint every behavior journey. Returns aggregated warnings."""
    spec_payload = _coerce_spec(spec)
    warnings: list[str] = []
    for j in _as_list(spec_payload.get("behavior_journeys")):
        if not isinstance(j, dict):
            continue
        jid = j.get("id") or "<unnamed>"
        desc = j.get("description") or ""
        per_journey = lint_journey(desc)
        for issue in per_journey:
            warnings.append(f"journey {jid!r}: {issue}")
    return warnings


def _cleanup_root_spec_artifacts(project_dir: Path) -> list[Path]:
    """Remove redundant spec files a compile agent may have written at repo root.

    The canonical flat spec is persisted under ``session_dir/spec/spec.json``.
    Some providers try to recover from output-size pressure by writing a JSON
    contract into the project root. Those files are unused and can block later
    merge worktree checks, so remove only a narrow allowlist of known names.
    """
    removed: list[Path] = []
    for name in sorted(ROOT_SPEC_ARTIFACT_FILENAMES):
        artifact = project_dir / name
        if not artifact.is_file():
            continue
        try:
            artifact.unlink()
        except OSError as exc:
            logger.warning("compile_flat_spec: could not remove stray root spec artifact %s: %s", artifact, exc)
            continue
        removed.append(artifact)
        logger.warning("compile_flat_spec: removed stray root spec artifact %s", artifact)
    return removed


# ---------------------------------------------------------------------------
# Structured contract validation
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _obj_id(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("id") or "").strip()


def _is_structured_spec(spec: dict[str, Any]) -> bool:
    """True when the new contract fields are present enough for hard validation."""
    return any((
        spec.get("product_overview"),
        spec.get("intent_claims"),
        spec.get("core_entities"),
        spec.get("cold_start_states"),
        spec.get("permissions"),
        spec.get("quality_constraints"),
        any(
            isinstance(j, dict)
            and (
                "covers_primary_actions" in j
                or "start_state" in j
                or "entry_route" in j
            )
            for j in _as_list(spec.get("behavior_journeys"))
        ),
    ))


def _validate_product_overview(
    spec: dict[str, Any],
    action_ids: set[str],
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    product_overview = spec.get("product_overview")

    if not isinstance(product_overview, dict) or not product_overview:
        warnings.append("product_overview is missing; downstream agents will infer product structure")
        return warnings, errors

    one_liner = str(product_overview.get("one_liner") or "").strip()
    if not one_liner:
        warnings.append("product_overview.one_liner is missing")
    elif len(one_liner) > 120:
        warnings.append("product_overview.one_liner is longer than 120 characters")

    top_level_pages = product_overview.get("top_level_pages")
    if not isinstance(top_level_pages, list) or not top_level_pages:
        warnings.append("product_overview.top_level_pages is empty or missing")
        top_level_pages = []

    page_ids: set[str] = set()
    for idx, page in enumerate(top_level_pages):
        if not isinstance(page, dict):
            warnings.append(f"product_overview.top_level_pages[{idx}] should be an object")
            continue
        page_id = _obj_id(page)
        if not page_id:
            warnings.append(f"product_overview.top_level_pages[{idx}].id is missing")
        else:
            page_ids.add(page_id)
        if not str(page.get("purpose") or "").strip():
            warnings.append(f"product_overview.top_level_pages[{page_id or idx}].purpose is missing")

    navigation = product_overview.get("primary_navigation")
    if not isinstance(navigation, dict):
        navigation = {}
        if spec.get("project_kind") == "webapp":
            warnings.append("product_overview.primary_navigation is missing for webapp")

    sidebar = navigation.get("sidebar") if isinstance(navigation, dict) else None
    sidebar_items = [str(item).strip() for item in sidebar] if isinstance(sidebar, list) else []
    sidebar_items = [item for item in sidebar_items if item]
    if spec.get("project_kind") == "webapp" and not sidebar_items:
        warnings.append("product_overview.primary_navigation.sidebar is empty for webapp")
    for page_id in sidebar_items:
        if page_id not in page_ids:
            warnings.append(
                f"product_overview.primary_navigation.sidebar references unknown page {page_id!r}"
            )

    phases = product_overview.get("phases")
    if phases is not None and not isinstance(phases, list):
        warnings.append("product_overview.phases must be a list when present")
        phases = []
    for phase_idx, phase in enumerate(_as_list(phases)):
        if not isinstance(phase, dict):
            warnings.append(f"product_overview.phases[{phase_idx}] should be an object")
            continue
        phase_id = _obj_id(phase) or f"index {phase_idx}"
        for action_id in _as_list(phase.get("covers_primary_action_ids")):
            action_id_text = str(action_id).strip()
            if action_id_text and action_id_text not in action_ids:
                warnings.append(
                    f"product_overview.phases[{phase_id}].covers_primary_action_ids "
                    f"references unknown action {action_id_text!r}"
                )

    return warnings, errors


def validate_structured_spec(spec: Any, *, strict: bool = False) -> list[str]:
    """Return advisory warnings for the structured flat-spec contract.

    ``strict`` is kept for caller compatibility. The compiler no longer turns
    cross-coverage gaps into hard failures; build and integration agents can
    repair product interpretation from context, while tests protect behavior.
    """
    spec_payload = _coerce_spec(spec)
    try:
        spec_payload = normalize_journey_contracts(
            spec_payload,
            current_schema_version=SCHEMA_VERSION,
        )
    except VerificationContractError as exc:
        if strict:
            raise StructuredSpecValidationError(str(exc)) from exc
        return [str(exc)]
    warnings: list[str] = []
    errors: list[str] = []

    structured = _is_structured_spec(spec_payload)
    if not structured:
        warnings.append("structured spec fields are absent; skipping structured contract checks")
    journeys = [j for j in _as_list(spec_payload.get("behavior_journeys")) if isinstance(j, dict)]
    intent_claims = [
        claim for claim in _as_list(spec_payload.get("intent_claims")) if isinstance(claim, dict)
    ]

    if len(intent_claims) > INTENT_CLAIMS_MAX:
        warnings.append(
            f"intent_claims has {len(intent_claims)} entries; target cap is <= {INTENT_CLAIMS_MAX}"
        )

    if len(journeys) > 5:
        warnings.append(f"behavior_journeys has {len(journeys)} entries; target cap is <= 5")

    if structured:
        for idx, journey in enumerate(journeys):
            jid = _obj_id(journey) or f"index {idx}"
            if not isinstance(journey.get("covers_primary_actions"), list):
                warnings.append(f"behavior_journeys[{jid}].covers_primary_actions should be a list")
            if not str(journey.get("start_state") or "").strip():
                warnings.append(f"behavior_journeys[{jid}].start_state is missing")
            if not str(journey.get("entry_route") or "").strip():
                warnings.append(f"behavior_journeys[{jid}].entry_route is missing")

    action_ids: set[str] = set()
    fields: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for entity in _as_list(spec_payload.get("core_entities")):
        if not isinstance(entity, dict):
            continue
        for field_obj in _as_list(entity.get("fields")):
            if isinstance(field_obj, dict):
                fields.append(field_obj)
        for action_obj in _as_list(entity.get("primary_actions")):
            if isinstance(action_obj, dict):
                aid = _obj_id(action_obj)
                if aid:
                    action_ids.add(aid)
                    actions.append(action_obj)

    product_warnings, product_errors = _validate_product_overview(
        spec_payload,
        action_ids,
    )
    warnings.extend(product_warnings)
    errors.extend(product_errors)

    covered_action_ids = {
        str(action_id)
        for journey in journeys
        for action_id in _as_list(journey.get("covers_primary_actions"))
        if str(action_id).strip()
    }
    for action_id in sorted(action_ids):
        if action_id not in covered_action_ids:
            warnings.append(
                f"core_entities.primary_actions id {action_id!r} is not covered by any behavior_journey"
            )

    journey_text = "\n".join(str(j.get("description") or "") for j in journeys)
    for claim in intent_claims:
        claim_id = _obj_id(claim)
        if not claim_id:
            warnings.append("intent_claims entry is missing id")
            continue
        refs_text = json.dumps(
            {
                "actions": actions,
                "fields": fields,
                "quality_constraints": _as_list(spec_payload.get("quality_constraints")),
            },
            sort_keys=True,
            default=str,
        )
        if claim_id not in refs_text and claim_id not in journey_text:
            warnings.append(
                f"intent_claims id {claim_id!r} is not covered by actions, fields, "
                "quality_constraints, or a behavior_journey description"
            )

    if spec_payload.get("project_kind") == "webapp" and structured:
        has_root_cold_start = any(
            str(j.get("entry_route") or "") == "/"
            and (
                "unauth" in str(j.get("start_state") or "").lower()
                or "empty" in str(j.get("start_state") or "").lower()
            )
            for j in journeys
        )
        if not has_root_cold_start:
            warnings.append(
                "webapp specs need at least one behavior_journey from an unauth/empty "
                "cold start state with entry_route '/'"
            )

    if errors and strict:
        raise StructuredSpecValidationError("; ".join(errors))
    return warnings + errors


# ---------------------------------------------------------------------------
# Compile entrypoint
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """You are a product spec compiler. Read the user's intent and emit a compact JSON product contract for Otto v5.

INTENT:
{intent}

## Output

Emit one JSON object through StructuredOutput/final structured JSON. Do not
write `product-contract.json`, `spec.json`, or any other project-root spec file.

Use this shape. Keep it compact; empty arrays are fine for supporting fields when a field is not useful, but `behavior_journeys` must contain at least one executable verification journey.
{{
  "project_kind": "webapp" | "cli" | "api" | "library" | "service",
  "product_overview": {{
    "one_liner": "Issue tracking workspace for teams to plan and ship work.",
    "primary_users": [
      {{"id": "engineer", "description": "individual contributor who owns issues day-to-day"}},
      {{"id": "team_lead", "description": "plans cycles and triages backlog"}}
    ],
    "top_level_pages": [
      {{"id": "inbox", "purpose": "see what needs attention now", "primary_users": ["engineer", "team_lead"]}},
      {{"id": "team.backlog", "purpose": "plan and triage upcoming work", "primary_users": ["engineer", "team_lead"]}}
    ],
    "primary_navigation": {{
      "sidebar": ["inbox", "team.backlog"],
      "command_palette": ["issue.create"]
    }},
    "out_of_scope": ["mobile native app; responsive web only"],
    "phases": [
      {{"id": "must_have", "rationale": "core flow without which the product cannot ship", "covers_primary_action_ids": ["issue.create"]}}
    ]
  }},
  "intent_claims": [
    {{"id": "claim.snake_case_short_id", "text": "Raw intent claim.", "source_line": 1}}
  ],
  "core_entities": [
    {{
      "id": "entity.snake_case",
      "name": "Entity name",
      "fields": [
        {{"id": "entity.field", "name": "field_name", "type": "string", "intent_claim_ids": ["claim.snake_case_short_id"]}}
      ],
      "states": ["empty", "active"],
      "primary_actions": [
        {{
          "id": "entity.action",
          "verb": "create|update|delete|send|export|...",
          "success_observable": "What changes after success.",
          "error_observable": "What the user sees after failure.",
          "intent_claim_ids": ["claim.snake_case_short_id"]
        }}
      ]
    }}
  ],
  "cold_start_states": [
    {{"id": "unauthenticated", "name": "Unauthenticated visitor", "description": "Fresh visitor with no session."}},
    {{"id": "empty_workspace", "name": "Empty workspace", "description": "Authenticated user with no records yet."}}
  ],
  "permissions": [
    {{"id": "member", "name": "Member", "gates": ["entity.action"]}}
  ],
  "quality_constraints": [
    {{"id": "quality.visible_feedback", "text": "All forms have user-visible feedback on submit.", "intent_claim_ids": ["claim.snake_case_short_id"], "note": "Lower-priority detail goes here instead of becoming another matrix-driving row."}}
  ],
  "behavior_journeys": [
    {{
      "id": "snake_case_short_id",
      "description": "User-language steps describing what happens.",
      "covers_primary_actions": ["entity.action"],
      "start_state": "unauthenticated",
      "entry_route": "/",
      "verification_level": "ui",
      "pass_model": {{
        "start_state": "unauthenticated",
        "setup": [],
        "actions": [
          {{
            "id": "entity.action.effect",
            "description": "Use the visible UI to perform entity.action.",
            "state_changing": true,
            "role": "button",
            "name": "Create entity",
            "inputs": [
              {{"label": "Name", "value": "Example entity"}}
            ],
            "covers_primary_actions": ["entity.action"],
            "success_observables": [
              {{"kind": "persisted_data_visible", "primary_action_id": "entity.action", "description": "The created/changed entity appears in the UI after the action.", "text": "Example entity"}}
            ],
            "network_expectations": []
          }}
        ],
        "success_observables": [
          {{"kind": "persisted_data_visible", "primary_action_id": "entity.action", "description": "The created/changed entity appears in the UI after the action.", "text": "Example entity"}}
        ],
        "ready_policy": {{"route": "/", "wait_for": "interactive"}},
        "settle_policy": {{"after_action": "dom_or_network_effect", "timeout_ms": 5000}},
        "network_expectations": [],
        "final_dom_assertions": [
          {{"kind": "persisted_data_visible", "primary_action_id": "entity.action", "description": "The created/changed entity appears in the UI after the action.", "text": "Example entity"}}
        ]
      }}
    }}
  ]
}}

Guidance:
- Behavior journeys are representative user-language samples and must include typed executable verification fields.
- Webapp UI journeys use `verification_level: "ui"` and a pass_model with accessible locators: role/name for controls, label/name for inputs, and text/role/name/label assertions for resulting UI. Avoid CSS selectors and data-testids unless no accessible locator exists.
- Every state-changing UI pass_model action must include `success_observables` with a non-tautological post-action observable tied to one of that journey's covered primary actions or to a concrete entity/effect. Example: after `comment.mention`, assert that the mentioned user's inbox contains the comment/issue title; do not merely assert "comment text appears" or "route loaded".
- The action-level observable, top-level `success_observables`, and `final_dom_assertions` should all point at the same post-action entity/effect so the runner can prove the button actually changed user-visible state.
- API/CLI/library/service journeys use `verification_level: "api"`, the adapter `probe_kind`, and a strong pass_model: http_api needs response payload assertions/extracted state; cli_command needs stdout/stderr or filesystem effects; library_call needs expect_return or expect_raises; service_health needs health plus payload assertion.
- An `http_api` pass_model has NO `actions`/`final_dom_assertions` (that is the UI shape). It MUST be a `steps` array; every step needs `path` + `method` + `expect_status`, and at least one step MUST carry a strong payload assertion — `expect_json`, `expect_body_contains`, `expect_json_path`, or `extract` (a status code alone is NOT sufficient and fails the contract). Emit this shape directly; do not produce a UI pass_model for an api journey. Concrete example for an `http_api` journey (PAT auth → POST create → GET verify):
  `"pass_model": {{"probe_kind": "http_api", "steps": [{{"name": "create issue", "method": "POST", "path": "/api/issues", "headers": {{"Authorization": "Bearer $PAT"}}, "body": {{"title": "Example"}}, "expect_status": 201, "expect_json": {{"identifier": "ENG-1"}}, "extract": {{"issue_id": "$.id"}}}}, {{"name": "fetch it back", "method": "GET", "path": "/api/issues/{{issue_id}}", "headers": {{"Authorization": "Bearer $PAT"}}, "expect_status": 200, "expect_body_contains": "Example"}}]}}`
- Do not use route-loaded, HTTP-200, body-present, skeleton, or generic text as the only success observable.
- Use at most 5 representative critical flows; avoid DOM APIs.
- IDs should be terse and stable, e.g. `issue.create` or `report.export`.
- Consolidate repeated or low-priority claims; intent_claims cap <= 30.
- Prefer useful product structure over perfect cross-reference coverage. Build agents can reason from context.
"""


_PROMPT_RETRY_SUFFIX = """

YOUR PREVIOUS OUTPUT WAS NOT USABLE JSON FOR OTTO:
{warnings}

Re-emit one JSON object. Keep journeys in user-language; prefer accessible role/name/label/text locators over DOM selectors.
"""


_PROMPT_CONTRACT_REPAIR_SUFFIX = """

YOUR PREVIOUS OUTPUT FAILED OTTO'S PASS-MODEL ADEQUACY VALIDATOR:
- code: {code}
- path: {path}
- reason: {message}

Regenerate ONLY the offending `pass_model` for behavior journey `{target}`.
Keep the same top-level JSON object and keep all other journeys, claims, entities,
routes, and product fields unchanged. The repaired pass_model must satisfy this
hard rule: every state-changing action must include at least one
`success_observables[]` entry that is a non-tautological post-action observable
tied to a covered primary action or entity effect. Use an executable DOM assertion
such as text/role/name/label for the created, updated, deleted, sent, exported,
or notified entity. Do not use route-loaded, HTTP-200, body-present, skeleton, or
generic text as the only success observable.

Previous JSON to repair:
```json
{previous_json}
```
"""


def _json_schema_any_of(*types: str) -> dict[str, Any]:
    return {"anyOf": [{"type": kind} for kind in types]}


def _contract_error_is_pass_model_repairable(exc: VerificationContractError) -> bool:
    # A verification-contract mismatch (e.g. verification_level / entry_route)
    # is a compiler-fixable spec error — route it through the SAME bounded
    # spec-repair loop as pass_model errors so it ends in an honest terminal
    # (SpecContractRepairExhaustedError) on exhaustion, NEVER catastrophic.
    if exc.code == "verification_contract_invalid":
        return True
    return exc.code in PASS_MODEL_CONTRACT_REPAIR_CODES and ".pass_model" in exc.path


def _contract_error_target(exc: VerificationContractError) -> str:
    match = re.search(r"behavior_journeys\[([^\]]+)\]", exc.path)
    return match.group(1) if match else ""


def _find_journey_by_target(journeys: list[Any], target: str) -> dict[str, Any] | None:
    for journey in journeys:
        if isinstance(journey, dict) and str(journey.get("id") or "") == target:
            return journey
    try:
        index = int(target)
    except ValueError:
        return None
    if 0 <= index < len(journeys) and isinstance(journeys[index], dict):
        return journeys[index]
    return None


def _merge_repaired_pass_model(
    base_payload: dict[str, Any],
    repair_payload: dict[str, Any],
    *,
    target: str,
) -> dict[str, Any] | None:
    base = deepcopy(base_payload)
    base_journeys = base.get("behavior_journeys")
    repair_journeys = repair_payload.get("behavior_journeys")
    if not isinstance(base_journeys, list) or not isinstance(repair_journeys, list):
        return None
    base_journey = _find_journey_by_target(base_journeys, target)
    repair_journey = _find_journey_by_target(repair_journeys, target)
    if not isinstance(base_journey, dict) or not isinstance(repair_journey, dict):
        return None
    repaired_model = repair_journey.get("pass_model")
    if not isinstance(repaired_model, dict):
        return None
    base_journey["pass_model"] = deepcopy(repaired_model)
    return base


def _preview_from_compiler_payload(
    parsed: dict[str, Any],
    *,
    intent: str,
    intent_hash: str,
    project_kind_hint: str | None,
) -> FlatSpec:
    journeys_raw = parsed.get("behavior_journeys")
    if not isinstance(journeys_raw, list):
        raise TypeError("output 'behavior_journeys' is not a list")
    return FlatSpec(
        intent=intent,
        intent_hash=intent_hash,
        project_kind=str(parsed.get("project_kind") or project_kind_hint or "webapp"),
        product_overview=_as_dict(parsed.get("product_overview")),
        intent_claims=[dict(j) for j in _as_list(parsed.get("intent_claims")) if isinstance(j, dict)],
        core_entities=[dict(j) for j in _as_list(parsed.get("core_entities")) if isinstance(j, dict)],
        cold_start_states=[
            dict(j) for j in _as_list(parsed.get("cold_start_states")) if isinstance(j, dict)
        ],
        permissions=[dict(j) for j in _as_list(parsed.get("permissions")) if isinstance(j, dict)],
        quality_constraints=[
            dict(j) for j in _as_list(parsed.get("quality_constraints")) if isinstance(j, dict)
        ],
        behavior_journeys=[
            {
                "id": str(j.get("id") or ""),
                "description": str(j.get("description") or ""),
                "covers_primary_actions": [
                    str(a) for a in _as_list(j.get("covers_primary_actions"))
                ],
                "start_state": str(j.get("start_state") or ""),
                "entry_route": str(j.get("entry_route") or ""),
                **({"api_only": bool(j.get("api_only"))} if "api_only" in j else {}),
                **({"pass_model": dict(j["pass_model"])} if isinstance(j.get("pass_model"), dict) else {}),
                **(
                    {"verification_level": str(j.get("verification_level") or "")}
                    if "verification_level" in j
                    else {}
                ),
                **({"probe_kind": str(j.get("probe_kind") or "")} if "probe_kind" in j else {}),
            }
            for j in journeys_raw
            if isinstance(j, dict)
        ],
    )


def _flat_spec_from_normalized_payload(
    normalized_preview: dict[str, Any],
    *,
    intent: str,
    intent_hash: str,
) -> FlatSpec:
    return FlatSpec(
        schema_version=SCHEMA_VERSION,
        intent=str(normalized_preview.get("intent") or intent),
        intent_hash=str(normalized_preview.get("intent_hash") or intent_hash),
        project_kind=str(normalized_preview.get("project_kind") or "webapp"),
        product_overview=_as_dict(normalized_preview.get("product_overview")),
        intent_claims=[dict(j) for j in _as_list(normalized_preview.get("intent_claims")) if isinstance(j, dict)],
        core_entities=[dict(j) for j in _as_list(normalized_preview.get("core_entities")) if isinstance(j, dict)],
        cold_start_states=[
            dict(j) for j in _as_list(normalized_preview.get("cold_start_states")) if isinstance(j, dict)
        ],
        permissions=[dict(j) for j in _as_list(normalized_preview.get("permissions")) if isinstance(j, dict)],
        quality_constraints=[
            dict(j) for j in _as_list(normalized_preview.get("quality_constraints")) if isinstance(j, dict)
        ],
        behavior_journeys=[
            dict(j) for j in _as_list(normalized_preview.get("behavior_journeys")) if isinstance(j, dict)
        ],
    )


def _render_contract_repair_prompt(
    request: _PassModelRepairRequest,
    initial_prompt_text: str,
) -> str:
    return initial_prompt_text + (
        _PROMPT_CONTRACT_REPAIR_SUFFIX.format(
            code=request.error.code,
            path=request.error.path,
            message=request.error.message,
            target=request.target,
            previous_json=json.dumps(request.payload, indent=2, sort_keys=True),
        )
    )


async def compile_flat_spec(
    project_dir: Path,
    session_dir: Path,
    intent: str,
    config: dict[str, Any],
    *,
    project_kind_hint: str | None = None,
    max_retries: int = 2,
) -> FlatSpec:
    """Compile a flat spec for the user's intent.

    Best-effort on contract quality: retry only for unusable JSON shape, then
    accept valid JSON with advisory ``lint_warnings``. Downstream build and
    integration agents can reason over imperfect contracts.
    """
    intent = (intent or "").strip()
    if not intent:
        raise ValueError("compile_flat_spec: intent is empty")

    compile_started_ts = _utc_now()
    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "spec.json"

    # Persist intent verbatim per philosophy invariant.
    intent_path = session_intent(project_dir, session_dir.name)
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(intent, encoding="utf-8")

    intent_h = sha256_text(intent)

    options = make_agent_options(project_dir, config, agent_type="spec")
    # Force structured output via the SDK's output_format machinery.
    setattr(
        options,
        "output_format",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "project_kind": {"type": "string"},
                    "product_overview": {
                        "type": "object",
                        "properties": {
                            "one_liner": {"type": "string"},
                            "primary_users": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["id", "description"],
                                },
                            },
                            "top_level_pages": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "purpose": {"type": "string"},
                                        "primary_users": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["id", "purpose"],
                                },
                            },
                            "primary_navigation": {
                                "type": "object",
                                "properties": {
                                    "sidebar": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "command_palette": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["sidebar"],
                            },
                            "out_of_scope": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "phases": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "rationale": {"type": "string"},
                                        "covers_primary_action_ids": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": [
                                        "id",
                                        "rationale",
                                        "covers_primary_action_ids",
                                    ],
                                },
                            },
                        },
                        "required": [
                            "one_liner",
                            "primary_users",
                            "top_level_pages",
                            "primary_navigation",
                            "out_of_scope",
                            "phases",
                        ],
                    },
                    "intent_claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "source_line": _json_schema_any_of("integer", "null"),
                            },
                            "required": ["id", "text", "source_line"],
                        },
                    },
                    "core_entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "fields": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                },
                                "states": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "primary_actions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "verb": {"type": "string"},
                                            "success_observable": {"type": "string"},
                                            "error_observable": {"type": "string"},
                                            "intent_claim_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                        "required": [
                                            "id",
                                            "verb",
                                            "success_observable",
                                            "error_observable",
                                        ],
                                    },
                                },
                            },
                            "required": ["id", "name", "fields", "states", "primary_actions"],
                        },
                    },
                    "cold_start_states": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "permissions": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "quality_constraints": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "behavior_journeys": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "covers_primary_actions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "start_state": {"type": "string"},
                                "entry_route": {"type": "string"},
                                "api_only": {"type": "boolean"},
                                "verification_level": {
                                    "type": "string",
                                    "enum": list(VERIFICATION_LEVELS),
                                },
                                "probe_kind": {
                                    "type": "string",
                                    "enum": list(PROBE_KINDS),
                                },
                                "pass_model": {
                                    "type": "object",
                                    "properties": {
                                        **{
                                            key: _json_schema_any_of(
                                                "array",
                                                "object",
                                                "string",
                                                "number",
                                                "boolean",
                                            )
                                            for key in PASS_MODEL_KEYS
                                        },
                                        "steps": {"type": "array", "items": {"type": "object"}},
                                        "api_steps": {"type": "array", "items": {"type": "object"}},
                                        "command": {"type": "array", "items": {"type": "string"}},
                                        "start_command": {"type": "array", "items": {"type": "string"}},
                                        "module": {"type": "string"},
                                        "function": {"type": "string"},
                                        "expect_return": {},
                                        "expect_raises": {"type": "string"},
                                        "health_url": {"type": "string"},
                                        "expect_body_contains": {"type": "string"},
                                        "expect_json": {},
                                    },
                                },
                            },
                            "required": [
                                "id",
                                "description",
                                "covers_primary_actions",
                                "start_state",
                                "verification_level",
                                "pass_model",
                            ],
                        },
                    },
                },
                "required": [
                    "project_kind",
                    "product_overview",
                    "intent_claims",
                    "core_entities",
                    "cold_start_states",
                    "permissions",
                    "quality_constraints",
                    "behavior_journeys",
                ],
            },
        },
    )

    initial_prompt_text = _PROMPT_TEMPLATE.format(intent=intent)
    provider = str(getattr(options, "provider", None) or "")
    model = str(getattr(options, "model", None) or "")
    key_payload = cache_key_payload(
        intent_hash=intent_h,
        prompt_hash=sha256_text(initial_prompt_text),
        provider=provider,
        model=model,
        schema_version=SCHEMA_VERSION,
        otto_version=OTTO_VERSION,
    )
    cache_disabled = bool(config.get("spec_compile_no_cache") or config.get("no_cache"))
    if not cache_disabled:
        cache_hit = lookup_spec_cache(project_dir, key_payload)
        if cache_hit is not None:
            prompt_entry = save_rendered_prompt(
                prompts_dir=session_dir / "prompts",
                template="compile-spec-flat",
                rendered_text=initial_prompt_text,
            )
            shutil.copyfile(cache_hit.spec_path, spec_path)
            spec = load_flat_spec(spec_path)
            spec_text = spec_path.read_text(encoding="utf-8")
            _write_compile_metrics(
                session_dir,
                {
                    "start_ts": compile_started_ts,
                    "end_ts": _utc_now(),
                    "first_token_ts": None,
                    "prompt_bytes": len(initial_prompt_text.encode("utf-8")),
                    "output_bytes": len(spec_text.encode("utf-8")),
                    "total_tokens": 0,
                    "output_tokens": 0,
                    "validation_retries": 0,
                    "provider": provider,
                    "model": model,
                    "cache_hit": True,
                    "cache_key_hash": cache_hit.key_hash,
                },
            )
            update_input_provenance(
                session_dir=session_dir,
                intent={
                    "fallback_reason": "",
                    "resolved_text": intent,
                    "sha256": intent_h,
                    "source": "cli-argument",
                },
                spec={
                    "source": "spec-cache",
                    "path": str(spec_path),
                    "sha256": sha256_text(spec_text),
                    "cache_key_hash": cache_hit.key_hash,
                    "cache_path": str(cache_hit.cache_dir),
                },
                prompts=[prompt_entry],
            )
            _cleanup_root_spec_artifacts(project_dir)
            return spec

    # Single-turn compile with retry for unusable JSON shape and a bounded
    # pass-model repair path for validator feedback.
    last_warnings: list[str] = []
    last_contract_error: VerificationContractError | None = None
    repair_request: _PassModelRepairRequest | None = None
    parsed: dict[str, Any] | None = None
    preview = FlatSpec(intent=intent, intent_hash=intent_h)
    prompt_text = initial_prompt_text
    prompt_entry: dict[str, str] = {"template": "compile-spec-flat", "rendered_sha256": "", "rendered_path": ""}
    spec: FlatSpec = preview
    accepted = False
    attempts_run = 0
    shape_attempts_run = 0
    contract_repair_attempts = 0
    prompt_bytes_total = 0
    output_bytes_total = 0
    total_tokens = 0
    output_tokens = 0
    first_token_ts: str | None = None
    max_total_attempts = max_retries + 1 + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS
    while attempts_run < max_total_attempts:
        attempts_run += 1
        active_repair_request = repair_request
        repair_request = None

        if active_repair_request is not None:
            contract_repair_attempts += 1
            prompt_text = _render_contract_repair_prompt(active_repair_request, initial_prompt_text)
            prompt_name = f"compile-agent-contract-repair-{contract_repair_attempts:02d}"
        else:
            shape_attempts_run += 1
            if shape_attempts_run > max_retries + 1:
                break
            if shape_attempts_run == 1:
                prompt_text = initial_prompt_text
            else:
                prompt_text = initial_prompt_text + _PROMPT_RETRY_SUFFIX.format(
                    warnings="\n".join(f"  - {w}" for w in last_warnings)
                )
            prompt_name = (
                "compile-agent"
                if shape_attempts_run == 1
                else f"compile-agent-retry-{shape_attempts_run:02d}"
            )

        prompt_subdir = spec_dir / prompt_name
        prompt_subdir.mkdir(parents=True, exist_ok=True)
        prompt_entry = save_rendered_prompt(
            prompts_dir=session_dir / "prompts",
            template="compile-spec-flat",
            rendered_text=prompt_text,
        )

        prompt_bytes_total += len(prompt_text.encode("utf-8"))
        result_text = await _run_compile(prompt_text, options, prompt_subdir, project_dir)
        output_bytes_total += len(result_text.encode("utf-8"))
        message_metrics = compile_message_metrics_from_jsonl(prompt_subdir / "messages.jsonl")
        if first_token_ts is None:
            first_token_ts = message_metrics.get("first_token_ts")
        total_tokens += int(message_metrics.get("total_tokens") or 0)
        output_tokens += int(message_metrics.get("output_tokens") or 0)

        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError as exc:
            logger.warning("compile_flat_spec attempt %d: invalid JSON (%s); retrying", attempts_run, exc)
            last_warnings = [f"output was not valid JSON: {exc}"]
            if active_repair_request is not None:
                last_contract_error = active_repair_request.error
                if contract_repair_attempts < PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS:
                    repair_request = active_repair_request
                    continue
                break
            continue

        # Build a FlatSpec preview to lint.
        if not isinstance(parsed, dict):
            last_warnings = [f"output was not a JSON object (got {type(parsed).__name__})"]
            if active_repair_request is not None:
                last_contract_error = active_repair_request.error
                if contract_repair_attempts < PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS:
                    repair_request = active_repair_request
                    continue
                break
            continue
        journeys_raw = parsed.get("behavior_journeys")
        if not isinstance(journeys_raw, list):
            last_warnings = ["output 'behavior_journeys' is not a list"]
            if active_repair_request is not None:
                last_contract_error = active_repair_request.error
                if contract_repair_attempts < PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS:
                    repair_request = active_repair_request
                    continue
                break
            continue

        if active_repair_request is not None:
            repaired = _merge_repaired_pass_model(
                active_repair_request.payload,
                parsed,
                target=active_repair_request.target,
            )
            if repaired is None:
                last_contract_error = active_repair_request.error
                last_warnings = [
                    "contract repair output did not include a pass_model for "
                    f"behavior_journey {active_repair_request.target!r}"
                ]
                if contract_repair_attempts < PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS:
                    repair_request = active_repair_request
                    continue
                break
            parsed = repaired

        preview = _preview_from_compiler_payload(
            parsed,
            intent=intent,
            intent_hash=intent_h,
            project_kind_hint=project_kind_hint,
        )
        try:
            normalized_preview = normalize_journey_contracts(
                asdict(preview),
                current_schema_version=SCHEMA_VERSION,
            )
        except VerificationContractError as exc:
            if (
                _contract_error_is_pass_model_repairable(exc)
                and contract_repair_attempts < PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS
            ):
                last_contract_error = exc
                target = _contract_error_target(exc)
                repair_request = _PassModelRepairRequest(
                    payload=parsed,
                    error=exc,
                    target=target,
                )
                last_warnings = [str(exc)]
                logger.warning(
                    "compile_flat_spec attempt %d: pass_model contract error at %s; "
                    "requesting bounded spec repair %d/%d",
                    attempts_run,
                    exc.path,
                    contract_repair_attempts + 1,
                    PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS,
                )
                continue
            _cleanup_root_spec_artifacts(project_dir)
            if _contract_error_is_pass_model_repairable(exc):
                raise SpecContractRepairExhaustedError(
                    exc,
                    attempts=contract_repair_attempts,
                ) from exc
            raise StructuredSpecValidationError(str(exc)) from exc
        preview = _flat_spec_from_normalized_payload(
            normalized_preview,
            intent=intent,
            intent_hash=intent_h,
        )
        warnings = lint_spec(preview)
        warnings.extend(validate_structured_spec(preview, strict=True))
        preview.lint_warnings = warnings
        if warnings:
            logger.warning(
                "compile_flat_spec attempt %d: accepted with %d advisory warnings",
                attempts_run,
                len(warnings),
            )
        spec = preview
        accepted = True
        break
    if not accepted:
        _cleanup_root_spec_artifacts(project_dir)
        if (
            last_contract_error is not None
            and contract_repair_attempts >= PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS
        ):
            raise SpecContractRepairExhaustedError(
                last_contract_error,
                attempts=contract_repair_attempts,
            ) from last_contract_error
        message = (
            "compile_flat_spec: valid JSON shape not produced after "
            f"{attempts_run} attempts; refusing empty fallback"
        )
        logger.error("%s: %s", message, "; ".join(last_warnings))
        raise StructuredSpecValidationError(message)

    _cleanup_root_spec_artifacts(project_dir)

    # Persist spec.json.
    spec_path.write_text(_serialize_spec(spec) + "\n", encoding="utf-8")
    spec_text = spec_path.read_text(encoding="utf-8")
    cache_store = None
    if not cache_disabled:
        cache_store = store_spec_cache(
            project_dir=project_dir,
            key_payload=key_payload,
            spec_path=spec_path,
        )
    _write_compile_metrics(
        session_dir,
        {
            "start_ts": compile_started_ts,
            "end_ts": _utc_now(),
            "first_token_ts": first_token_ts,
            "prompt_bytes": prompt_bytes_total,
            "output_bytes": output_bytes_total,
            "total_tokens": total_tokens,
            "output_tokens": output_tokens,
            "validation_retries": max(attempts_run - 1, 0),
            "contract_repair_attempts": contract_repair_attempts,
            "provider": provider,
            "model": model,
            "cache_hit": False,
            "cache_key_hash": cache_store.key_hash if cache_store is not None else "",
        },
    )

    # Update input provenance.
    update_input_provenance(
        session_dir=session_dir,
        intent={
            "fallback_reason": "",
            "resolved_text": intent,
            "sha256": intent_h,
            "source": "cli-argument",
        },
        spec={
            "source": "compile-agent-flat",
            "path": str(spec_path),
            "sha256": sha256_text(spec_text),
            "cache_key_hash": cache_store.key_hash if cache_store is not None else "",
        },
        prompts=[prompt_entry],
    )

    return spec


async def _run_compile(prompt: str, options: Any, log_dir: Path, project_dir: Path) -> str:
    """Run one compile attempt. Returns the LLM's text output."""
    from otto.agent import run_agent_with_timeout

    text, _cost, _session_id, breakdown = await run_agent_with_timeout(
        prompt,
        options,
        log_dir=log_dir,
        phase_name="SPEC_COMPILE_FLAT",
        phase_label="compile-flat",
        timeout=int(options.max_turns or 60) * 30,  # generous; small prompt
        project_dir=project_dir,
    )
    messages_jsonl = log_dir / "messages.jsonl"
    structured_result = _structured_flat_spec_payload(
        breakdown.get("structured_output") if isinstance(breakdown, dict) else None
    )
    if structured_result is None:
        structured_result = _read_last_success_structured_output(messages_jsonl)
    if structured_result is not None:
        return json.dumps(structured_result)
    if _provider_uses_claude_structured_tool(getattr(options, "provider", None)):
        structured_tool_input = _read_structured_output_tool_input(messages_jsonl)
        if structured_tool_input is not None:
            return json.dumps(structured_tool_input)
    result_text = _read_last_success_result_text(messages_jsonl)
    return _extract_first_json_object(result_text if result_text is not None else (text or ""))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_compile_metrics(session_dir: Path, metrics: dict[str, Any]) -> None:
    payload = {"schema_version": 1, "_written_at": _utc_now(), **metrics}
    (session_dir / "compile_metrics.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compile_message_metrics_from_jsonl(messages_jsonl: Path) -> dict[str, Any]:
    """Extract compile timing/token metrics from one provider transcript."""
    first_token_ts: str | None = None
    last_usage: dict[str, int] = {}
    try:
        fh = messages_jsonl.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {"first_token_ts": None, "total_tokens": 0, "output_tokens": 0}
    with fh:
        for raw_line in fh:
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if first_token_ts is None and _record_has_assistant_text(record):
                first_token_ts = str(record.get("ts") or "") or None
            usage = token_usage_from_mapping(record.get("usage"))
            if usage:
                last_usage = usage
    return {
        "first_token_ts": first_token_ts,
        "total_tokens": int(last_usage.get("total_tokens", 0) or 0),
        "output_tokens": int(last_usage.get("output_tokens", 0) or 0),
    }


def _record_has_assistant_text(record: dict[str, Any]) -> bool:
    if record.get("type") != "assistant":
        return False
    blocks = record.get("blocks")
    if not isinstance(blocks, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and bool(str(block.get("text") or ""))
        for block in blocks
    )


_FLAT_SPEC_PAYLOAD_KEYS = {
    "project_kind",
    "product_overview",
    "intent_claims",
    "core_entities",
    "cold_start_states",
    "permissions",
    "quality_constraints",
    "behavior_journeys",
}

_CLAUDE_STRUCTURED_OUTPUT_TOOL_NAME = "StructuredOutput"


def _looks_like_flat_spec_payload(value: Any) -> bool:
    return isinstance(value, dict) and _FLAT_SPEC_PAYLOAD_KEYS.issubset(value.keys())


def _read_structured_output_tool_input(messages_jsonl: Path) -> dict[str, Any] | None:
    """Return the latest Claude ``StructuredOutput`` flat-spec tool payload."""
    try:
        with messages_jsonl.open(encoding="utf-8") as fh:
            latest: dict[str, Any] | None = None
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                for block in _as_list(record.get("blocks")):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    if block.get("name") != _CLAUDE_STRUCTURED_OUTPUT_TOOL_NAME:
                        continue
                    tool_input = block.get("input")
                    if _looks_like_flat_spec_payload(tool_input):
                        latest = _as_dict(tool_input)
            return latest
    except OSError:
        return None


def _read_last_success_result_text(messages_jsonl: Path) -> str | None:
    """Return the final successful result text from a lossless agent transcript."""
    try:
        with messages_jsonl.open(encoding="utf-8") as fh:
            last_result: str | None = None
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "result" or record.get("subtype") != "success":
                    continue
                result = record.get("result")
                if isinstance(result, str) and result.strip():
                    last_result = result
            return last_result
    except OSError:
        return None


def _read_last_success_structured_output(messages_jsonl: Path) -> dict[str, Any] | None:
    """Return the latest typed structured output from a successful result record."""
    try:
        with messages_jsonl.open(encoding="utf-8") as fh:
            latest: dict[str, Any] | None = None
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "result" or record.get("subtype") != "success":
                    continue
                payload = _structured_flat_spec_payload(record.get("structured_output"))
                if payload is not None:
                    latest = payload
            return latest
    except OSError:
        return None


def _structured_flat_spec_payload(value: Any) -> dict[str, Any] | None:
    return _as_dict(value) if _looks_like_flat_spec_payload(value) else None


def _provider_uses_claude_structured_tool(provider: Any) -> bool:
    name = str(provider or "").strip().casefold()
    if not name:
        return True
    return name.startswith("claude") or name.startswith("anthropic")


def _extract_first_json_object(text: str) -> str:
    """Pull the first balanced JSON object out of free-form LLM output.

    Handles: bare object, ```json fenced block, JSON followed by explanation
    prose, JSON preceded by a header.
    """
    text = text.strip()
    # First, strip code fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    # Find the first '{' and walk to the matching '}'.
    start = text.find("{")
    if start < 0:
        return text  # no object; return as-is for json.loads to fail with clear error
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]  # unterminated; let parser report


def _serialize_spec(spec: FlatSpec) -> str:
    return json.dumps(
        {
            "schema_version": spec.schema_version,
            "intent": spec.intent,
            "intent_hash": spec.intent_hash,
            "project_kind": spec.project_kind,
            "product_overview": dict(spec.product_overview),
            "intent_claims": list(spec.intent_claims),
            "core_entities": list(spec.core_entities),
            "cold_start_states": list(spec.cold_start_states),
            "permissions": list(spec.permissions),
            "quality_constraints": list(spec.quality_constraints),
            "behavior_journeys": list(spec.behavior_journeys),
            "lint_warnings": list(spec.lint_warnings),
        },
        indent=2,
    )


def load_flat_spec(spec_path: Path) -> FlatSpec:
    """Load a previously-compiled flat spec from disk."""
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    try:
        data = normalize_journey_contracts(
            data,
            current_schema_version=SCHEMA_VERSION,
        )
    except VerificationContractError as exc:
        raise StructuredSpecValidationError(str(exc)) from exc
    return FlatSpec(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        intent=str(data.get("intent", "")),
        intent_hash=str(data.get("intent_hash", "")),
        project_kind=str(data.get("project_kind", "webapp")),
        product_overview=dict(data.get("product_overview", {}))
        if isinstance(data.get("product_overview", {}), dict)
        else {},
        intent_claims=list(data.get("intent_claims", [])),
        core_entities=list(data.get("core_entities", [])),
        cold_start_states=list(data.get("cold_start_states", [])),
        permissions=list(data.get("permissions", [])),
        quality_constraints=list(data.get("quality_constraints", [])),
        behavior_journeys=list(data.get("behavior_journeys", [])),
        lint_warnings=list(data.get("lint_warnings", [])),
    )
