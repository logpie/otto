# PM PRD Layer Plan

## Rationale

The current flat compile contract is already engineering-shaped: it extracts
claims, entities, actions, and illustrative journeys. That is useful for
verification, but it leaves page structure and navigation as a downstream Lead
decision. The new `product_overview` field makes the PM-level product model
canonical before engineering details are consumed.

The core invariant is: every primary page the compiler declares must be carried
through CHARTER IA and runner verification. If a page is in the PM PRD, the
architect must route and link it, and the runner must be able to find route or
component evidence for it.

## Schema Diff

- Bump flat spec `schema_version` from `2` to `3`.
- Add `FlatSpec.product_overview: dict = field(default_factory=dict)`.
- Serialize and load `product_overview` between `project_kind` and
  `intent_claims`, preserving the intended JSON ordering.
- Extend the structured output schema so new compile results must include
  `product_overview`.
- Keep legacy v1/v2 specs backward compatible: missing `product_overview`
  warns under non-strict validation and does not block legacy loading.

`product_overview` validation rules:

- `one_liner` is required, non-empty, and at most 120 characters.
- `top_level_pages` has at least one `{id, purpose}` entry.
- Webapps require at least one page and a non-empty
  `primary_navigation.sidebar`.
- Sidebar entries must reference declared top-level page IDs.
- `phases[].covers_primary_action_ids[]` must reference
  `core_entities[].primary_actions[].id`.

## File-by-File Changes

- `otto/spec_compile_flat.py`
  - Add the dataclass field and serialization/load support.
  - Add Step 0 PM PRD instructions to the compile prompt before the output
    structure.
  - Extend the JSON schema and structured-output payload key detection.
  - Add product overview validation and phase/action cross-reference checks.

- `otto/prompts/lead.md`
  - Require every `product_overview.top_level_pages[].id` to appear in
    `CHARTER.IA.routes[]`.
  - Require every `product_overview.primary_navigation.sidebar[]` page to be
    linked from `nav_surfaces[]`.

- `otto/v5_capability_inventory.py`
  - Cross-check CHARTER IA against the spec PM page model.
  - Warn if top-level pages lack IA routes or sidebar pages lack nav links.

- `otto/v5_verification_plan.py`
  - Add `page_resolves` to the deterministic check matrix.
  - For every PM top-level page, find the matching IA route and verify route or
    component evidence in code.

- Tests
  - Extend structured spec tests for required webapp PM overview fields,
    sidebar resolution, phase action cross-references, and JSON round-trip.
  - Add prompt text assertion for the Lead IA handoff.
  - Add coherence tests for missing PM routes/sidebar links.
  - Add runner check test for `page_resolves`.

## Verification

- Run focused tests:
  `uv run --extra dev pytest tests/ -q -k "v5 or spec_compile" --ignore=tests/integration`
- Run a direct round-trip check:
  build a `FlatSpec`, `asdict`, `json.dumps/loads`, and
  `validate_structured_spec(..., strict=True)`.
- After edits, grep for stale schema/prompt/check references:
  `rg "SCHEMA_VERSION = 2|product_overview|page_resolves|route_resolves" otto tests`

## Expected Test Count

At least seven new or updated assertions are expected from the requested tests:

- Product overview required for new webapp specs.
- Top-level pages must be listed for webapps.
- Phase action IDs must resolve.
- Sidebar entries must resolve.
- Lead prompt must mention PM page and sidebar obligations.
- Runner matrix must emit `page_resolves`.
- FlatSpec dict/JSON round-trip must include `product_overview`.

## Verification Results

- Focused compiler/IA/verification files:
  `44 passed`.
- Requested suite:
  `317 passed, 2260 deselected`.
- Explicit FlatSpec/asdict/json.dumps/json.loads validation:
  `schema_version=3`, `warnings=[]`.
