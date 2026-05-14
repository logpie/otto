"""Flat spec compilation for v5.

Replaces v4's group/contract synthesis. Emits:
    - the user's intent (verbatim)
    - project_kind (detected)
    - intent_claims[]: raw claims extracted from intent.md
    - core_entities[]: product entities, fields, states, primary actions
    - cold_start_states[] / permissions[] / quality_constraints[]
    - behavior_journeys[]: capped illustrative journeys

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
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from otto import __version__ as OTTO_VERSION
from otto.agent import make_agent_options
from otto.observability import save_rendered_prompt, sha256_text, update_input_provenance
from otto.paths import session_intent
from otto.token_usage import token_usage_from_mapping
from otto.v5_spec_cache import (
    cache_key_payload,
    lookup_spec_cache,
    store_spec_cache,
)

logger = logging.getLogger("otto.spec_compile_flat")

SCHEMA_VERSION = 3
INTENT_CLAIMS_MAX = 30


class StructuredSpecValidationError(ValueError):
    """Raised when a newly compiled structured flat spec violates hard rules."""


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


# ---------------------------------------------------------------------------
# Structured contract validation
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _obj_id(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("id") or "").strip()


def _string_values(obj: Any) -> list[str]:
    """Collect shallow string/list scalar values from a contract object."""
    if not isinstance(obj, dict):
        return []
    out: list[str] = []
    for value in obj.values():
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(str(item) for item in value if isinstance(item, (str, int, float)))
    return out


def _references_claim(obj: Any, claim_id: str) -> bool:
    if not isinstance(obj, dict) or not claim_id:
        return False
    refs = obj.get("intent_claim_ids")
    if isinstance(refs, list) and claim_id in {str(r) for r in refs}:
        return True
    return any(claim_id in value for value in _string_values(obj))


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
                or "role" in j
            )
            for j in _as_list(spec.get("behavior_journeys"))
        ),
    ))


def _schema_version(spec: dict[str, Any]) -> int:
    try:
        return int(spec.get("schema_version", SCHEMA_VERSION))
    except (TypeError, ValueError):
        return SCHEMA_VERSION


def _product_overview_is_legacy_missing(spec: dict[str, Any]) -> bool:
    product_overview = spec.get("product_overview")
    return _schema_version(spec) < 3 and (
        not isinstance(product_overview, dict) or not product_overview
    )


def _validate_product_overview(
    spec: dict[str, Any],
    action_ids: set[str],
    *,
    legacy_missing_allowed: bool,
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    product_overview = spec.get("product_overview")

    if not isinstance(product_overview, dict) or not product_overview:
        message = "product_overview is required for schema_version 3 structured specs"
        if legacy_missing_allowed:
            warnings.append("legacy v1/v2 spec is missing product_overview; skipping PM overview checks")
        else:
            errors.append(message)
        return warnings, errors

    one_liner = str(product_overview.get("one_liner") or "").strip()
    if not one_liner:
        errors.append("product_overview.one_liner is required")
    elif len(one_liner) > 120:
        errors.append("product_overview.one_liner must be 120 characters or fewer")

    top_level_pages = product_overview.get("top_level_pages")
    if not isinstance(top_level_pages, list) or not top_level_pages:
        errors.append("product_overview.top_level_pages must contain at least one page")
        top_level_pages = []

    page_ids: set[str] = set()
    for idx, page in enumerate(top_level_pages):
        if not isinstance(page, dict):
            errors.append(f"product_overview.top_level_pages[{idx}] must be an object")
            continue
        page_id = _obj_id(page)
        if not page_id:
            errors.append(f"product_overview.top_level_pages[{idx}].id is required")
        else:
            page_ids.add(page_id)
        if not str(page.get("purpose") or "").strip():
            errors.append(f"product_overview.top_level_pages[{page_id or idx}].purpose is required")

    navigation = product_overview.get("primary_navigation")
    if not isinstance(navigation, dict):
        navigation = {}
        if spec.get("project_kind") == "webapp":
            errors.append("product_overview.primary_navigation is required for webapps")

    sidebar = navigation.get("sidebar") if isinstance(navigation, dict) else None
    sidebar_items = [str(item).strip() for item in sidebar] if isinstance(sidebar, list) else []
    sidebar_items = [item for item in sidebar_items if item]
    if spec.get("project_kind") == "webapp" and not sidebar_items:
        errors.append("product_overview.primary_navigation.sidebar must be non-empty for webapps")
    for page_id in sidebar_items:
        if page_id not in page_ids:
            errors.append(
                f"product_overview.primary_navigation.sidebar references unknown page {page_id!r}"
            )

    phases = product_overview.get("phases")
    if phases is not None and not isinstance(phases, list):
        errors.append("product_overview.phases must be a list when present")
        phases = []
    for phase_idx, phase in enumerate(_as_list(phases)):
        if not isinstance(phase, dict):
            errors.append(f"product_overview.phases[{phase_idx}] must be an object")
            continue
        phase_id = _obj_id(phase) or f"index {phase_idx}"
        for action_id in _as_list(phase.get("covers_primary_action_ids")):
            action_id_text = str(action_id).strip()
            if action_id_text and action_id_text not in action_ids:
                errors.append(
                    f"product_overview.phases[{phase_id}].covers_primary_action_ids "
                    f"references unknown action {action_id_text!r}"
                )

    return warnings, errors


def validate_structured_spec(spec: Any, *, strict: bool = False) -> list[str]:
    """Validate the structured flat-spec contract.

    ``strict=False`` is legacy-safe: missing new fields produce warnings only
    and callers can continue. ``strict=True`` is for newly compiled specs and
    raises ``StructuredSpecValidationError`` on any hard-rule violation.
    """
    spec_payload = _coerce_spec(spec)
    warnings: list[str] = []
    errors: list[str] = []

    structured = _is_structured_spec(spec_payload)
    if not structured:
        warnings.append("structured spec fields are absent; skipping structured contract checks")
        if strict:
            errors.append("structured spec fields are required for new v5 compilations")
    journeys = [j for j in _as_list(spec_payload.get("behavior_journeys")) if isinstance(j, dict)]
    intent_claims = [
        claim for claim in _as_list(spec_payload.get("intent_claims")) if isinstance(claim, dict)
    ]

    if len(intent_claims) > INTENT_CLAIMS_MAX:
        warnings.append(
            f"intent_claims has {len(intent_claims)} entries; target cap is <= {INTENT_CLAIMS_MAX}"
        )

    if len(journeys) > 5:
        errors.append(f"behavior_journeys has {len(journeys)} entries; maximum is 5")

    if structured:
        for idx, journey in enumerate(journeys):
            jid = _obj_id(journey) or f"index {idx}"
            if journey.get("role") != "illustrative":
                errors.append(f"behavior_journeys[{jid}].role must be 'illustrative'")
            if not isinstance(journey.get("covers_primary_actions"), list):
                errors.append(f"behavior_journeys[{jid}].covers_primary_actions must be a list")
            if not str(journey.get("start_state") or "").strip():
                errors.append(f"behavior_journeys[{jid}].start_state is required")
            if not str(journey.get("entry_route") or "").strip():
                errors.append(f"behavior_journeys[{jid}].entry_route is required")

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
        legacy_missing_allowed=_product_overview_is_legacy_missing(spec_payload),
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
            errors.append(
                f"core_entities.primary_actions id {action_id!r} is not covered by any behavior_journey"
            )

    journey_text = "\n".join(str(j.get("description") or "") for j in journeys)
    for claim in intent_claims:
        claim_id = _obj_id(claim)
        if not claim_id:
            errors.append("intent_claims entry is missing id")
            continue
        covered = (
            any(_references_claim(action, claim_id) for action in actions)
            or any(_references_claim(field_obj, claim_id) for field_obj in fields)
            or any(
                _references_claim(qc, claim_id)
                for qc in _as_list(spec_payload.get("quality_constraints"))
            )
            or claim_id in journey_text
        )
        if not covered:
            errors.append(
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
            errors.append(
                "webapp specs need at least one behavior_journey from an unauth/empty "
                "cold start state with entry_route '/'"
            )

    if errors and strict:
        raise StructuredSpecValidationError("; ".join(errors))
    return warnings + errors


# ---------------------------------------------------------------------------
# Compile entrypoint
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """You are a product spec compiler. Your job: read the user's intent and emit a structured JSON product contract for Otto v5.

INTENT:
{intent}

## Step 0: Product Overview (PM PRD)

Before listing entities and actions, produce a `product_overview` section. This is the PM-level model that engineering and design must respect.

- `one_liner`: a single sentence describing what this product is.
- `primary_users`: 2-4 user types (e.g., end_user, admin, developer). Don't invent personas with names; just role + one-line jobs-to-be-done.
- `top_level_pages`: the major navigation surfaces the user moves between. For a webapp, this is the sidebar+nav structure. For a CLI, this is the top-level command tree. For a library, the major module groupings.
- `primary_navigation.sidebar`: ordered list of page ids that should appear in the primary nav.
- `primary_navigation.command_palette`: top action ids exposed via keyboard shortcut palette (webapp only; omit if not applicable).
- `out_of_scope`: explicit non-goals. Include things the user might assume but you're deliberately not building.
- `phases`: ordered priority groups. Each phase lists action ids it covers. `must_have` is the smallest shippable slice; `should_have` and `nice_to_have` add incrementally.

The engineering layers (intent_claims, core_entities, primary_actions, journeys) MUST be consistent with the product_overview. If you can't fit something into a page, that's a signal it shouldn't be in primary_actions either.

## Required output structure

OUTPUT a single JSON object with this exact shape (no prose, no fences, just JSON):
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
    {{"id": "claim.snake_case_short_id", "text": "Raw bullet or claim from the intent.", "source_line": 1}}
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
          "success_observable": "What the user or API caller can observe after success.",
          "error_observable": "What the user or API caller can observe after failure.",
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
      "role": "illustrative",
      "description": "User-language steps describing what happens. Like a manual entry.",
      "covers_primary_actions": ["entity.action"],
      "start_state": "unauthenticated",
      "entry_route": "/"
    }}
  ]
}}

RULES (HARD — do NOT violate):
1. Behavior journeys MUST be in user-language. They describe what a USER does and SEES.
   GOOD: "User clicks 'Add Transaction', enters $50 with category 'Food', saves. The new transaction appears in the list."
   BAD:  "Click element with class .add-btn. Verify .txn-list has data-testid='row'."
   NEVER use: CSS selectors, getByRole, getByText, querySelector, .locator(), data-testid, DOM ids.

2. Emit at most 5 behavior_journeys. They are illustrative samples, not the full contract.
   Every journey MUST have role "illustrative". Journeys cover representative
   critical flows only; do NOT enumerate every button, error branch, or feature
   variant as its own journey.

3. Cover the full intent with structured fields:
   - Each intent_claims[].id MUST appear in at least one core_entities field/action
     via intent_claim_ids, one quality_constraint via intent_claim_ids, or literally
     in a behavior_journey description.
   - Every core_entities[].primary_actions[].id MUST appear in at least one
     journey's covers_primary_actions.

4. For webapps, at least one journey MUST start from an unauthenticated or empty
   cold-start state and use entry_route "/".

5. Include integration journeys when useful, but keep the journey list capped at 5.

6. IDs are stable, unique, and compact. Primary action IDs should look like
   "issue.create", "transaction.import_csv", "report.export".

7. Keep compile output capped and non-duplicative:
   - intent_claims cap <= 30. Consolidate repeated or low-priority claims.
   - Use terse, stable IDs; IDs are identifiers, not prose summaries.
   - Do not restate the same requirement across product_overview,
     intent_claims, quality_constraints, and journeys.
   - Lower-priority quality_constraints detail belongs in a `note` field, not
     as extra matrix-driving rows.
"""


_PROMPT_RETRY_SUFFIX = """

YOUR PREVIOUS OUTPUT WAS REJECTED FOR CONTRACT VALIDATION WARNINGS/ERRORS:
{warnings}

Re-emit the JSON. Fix every listed issue. Keep journeys in user-language; no DOM selectors anywhere.
"""


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

    Best-effort on journey-language lint: re-prompt up to ``max_retries``
    times, then accept with ``lint_warnings``. Structured contract violations
    are hard failures after retries because downstream v5 checks depend on
    those IDs.
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
                                "source_line": {"type": ["integer", "null"]},
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
                                "role": {"type": "string"},
                                "description": {"type": "string"},
                                "covers_primary_actions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "start_state": {"type": "string"},
                                "entry_route": {"type": "string"},
                            },
                            "required": [
                                "id",
                                "role",
                                "description",
                                "covers_primary_actions",
                                "start_state",
                                "entry_route",
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
            return spec

    # Single-turn compile with retry-on-lint-failure.
    last_warnings: list[str] = []
    last_structured_errors: list[str] = []
    parsed: dict[str, Any] | None = None
    preview = FlatSpec(intent=intent, intent_hash=intent_h)
    prompt_text = initial_prompt_text
    prompt_entry: dict[str, str] = {"template": "compile-spec-flat", "rendered_sha256": "", "rendered_path": ""}
    spec: FlatSpec = preview
    accepted = False
    attempts_run = 0
    prompt_bytes_total = 0
    output_bytes_total = 0
    total_tokens = 0
    output_tokens = 0
    first_token_ts: str | None = None
    for attempt in range(1, max_retries + 2):  # initial + max_retries
        attempts_run = attempt
        if attempt == 1:
            prompt_text = initial_prompt_text
        else:
            prompt_text = initial_prompt_text + _PROMPT_RETRY_SUFFIX.format(
                warnings="\n".join(f"  - {w}" for w in last_warnings)
            )

        prompt_subdir = spec_dir / ("compile-agent" if attempt == 1 else f"compile-agent-retry-{attempt:02d}")
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
            logger.warning("compile_flat_spec attempt %d: invalid JSON (%s); retrying", attempt, exc)
            last_warnings = [f"output was not valid JSON: {exc}"]
            continue

        # Build a FlatSpec preview to lint.
        if not isinstance(parsed, dict):
            last_warnings = [f"output was not a JSON object (got {type(parsed).__name__})"]
            continue
        journeys_raw = parsed.get("behavior_journeys")
        if not isinstance(journeys_raw, list):
            last_warnings = ["output 'behavior_journeys' is not a list"]
            continue

        preview = FlatSpec(
            intent=intent,
            intent_hash=intent_h,
            project_kind=str(parsed.get("project_kind") or project_kind_hint or "webapp"),
            product_overview=(
                dict(parsed.get("product_overview"))
                if isinstance(parsed.get("product_overview"), dict)
                else {}
            ),
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
                    "role": str(j.get("role") or ""),
                    "description": str(j.get("description") or ""),
                    "covers_primary_actions": [
                        str(a) for a in _as_list(j.get("covers_primary_actions"))
                    ],
                    "start_state": str(j.get("start_state") or ""),
                    "entry_route": str(j.get("entry_route") or ""),
                }
                for j in journeys_raw
                if isinstance(j, dict)
            ],
        )
        warnings = lint_spec(preview)
        structured_errors: list[str] = []
        try:
            warnings.extend(validate_structured_spec(preview, strict=True))
        except StructuredSpecValidationError as exc:
            structured_errors.append(str(exc))
            warnings.extend(structured_errors)
        if not warnings:
            spec = preview
            accepted = True
            break
        last_warnings = warnings
        last_structured_errors = structured_errors
        logger.warning(
            "compile_flat_spec attempt %d: %d validation warnings/errors; retrying",
            attempt,
            len(warnings),
        )
    if not accepted:
        if last_structured_errors:
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
                    "provider": provider,
                    "model": model,
                    "cache_hit": False,
                    "cache_key_hash": "",
                    "error": "; ".join(last_structured_errors),
                },
            )
            raise StructuredSpecValidationError("; ".join(last_structured_errors))
        # All attempts had lint warnings. Best-effort: accept anyway but record warnings.
        spec = preview
        spec.lint_warnings = last_warnings
        logger.warning(
            "compile_flat_spec: lint warnings persist after %d attempts; accepting anyway (best-effort)",
            max_retries + 1,
        )

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

    text, _cost, _session_id, _breakdown = await run_agent_with_timeout(
        prompt,
        options,
        log_dir=log_dir,
        phase_name="SPEC_COMPILE_FLAT",
        phase_label="compile-flat",
        timeout=int(options.max_turns or 60) * 30,  # generous; small prompt
        project_dir=project_dir,
    )
    messages_jsonl = log_dir / "messages.jsonl"
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

_STRUCTURED_OUTPUT_TOOL_NAMES = {
    "structuredoutput",
    "ottostructuredoutput",
    "flat_spec",
    "flatspec",
    "submit_spec",
    "submitspec",
    "final_answer",
    "finalanswer",
}
_STRUCTURED_OUTPUT_TOOL_NAME_KEYS = {
    re.sub(r"[^a-z0-9]", "", item) for item in _STRUCTURED_OUTPUT_TOOL_NAMES
}


def _looks_like_flat_spec_payload(value: Any) -> bool:
    return isinstance(value, dict) and _FLAT_SPEC_PAYLOAD_KEYS.issubset(value.keys())


def _is_structured_output_tool_name(name: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if normalized in _STRUCTURED_OUTPUT_TOOL_NAME_KEYS:
        return True
    return "structuredoutput" in normalized


def _read_structured_output_tool_input(messages_jsonl: Path) -> dict[str, Any] | None:
    """Return the latest flat-spec payload emitted through a structured-output tool."""
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
                    if not _is_structured_output_tool_name(block.get("name")):
                        continue
                    tool_input = block.get("input")
                    if _looks_like_flat_spec_payload(tool_input):
                        latest = dict(tool_input)
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
                structured_output = record.get("structured_output")
                if structured_output is not None:
                    last_result = json.dumps(structured_output)
                    continue
                result = record.get("result")
                if isinstance(result, str) and result.strip():
                    last_result = result
            return last_result
    except OSError:
        return None


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
