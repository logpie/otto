# Structured Contract + Check Matrix Plan

## Scope Boundary

Implement only the v5 hierarchical path:
- Structured flat spec contract in `otto/spec_compile_flat.py`.
- Architect CHARTER IA prompt additions in `otto/prompts/lead.md`.
- CHARTER IA parsing/coherence validation in `otto/v5_capability_inventory.py`.
- Runner-side deterministic verification plan in `otto/lead.py`, implemented mostly in a helper module.
- Tests for the new validators and deterministic check matrix.

Do not change the monolithic I2P path, do not add browser/Playwright checks, and do not rewrite `v5_runner.py`.

## Current State

- `otto/spec_compile_flat.py` emits `project_kind` and `behavior_journeys[]`, with journey lint only.
- The spec compiler prompt is embedded in `otto/spec_compile_flat.py`; there is no active `otto/prompts/spec.md`.
- `otto/prompts/lead.md` contains the architect instructions and requires `CHARTER.md`, but no machine-readable IA block.
- `otto/v5_capability_inventory.py` injects a managed Detected Infrastructure block and runs warning-only coherence checks against CHARTER operating notes.
- `otto/lead.py` reads an agent-authored `verdict.json` around the post-agent phase, trusts valid verdicts, writes graph verdict + `summary.json`, and keeps the input `verdict.json` untouched.

## File-by-File Changes

### `otto/spec_compile_flat.py`

Add new dataclass fields:
- `intent_claims: list[dict[str, Any]]`
- `core_entities: list[dict[str, Any]]`
- `cold_start_states: list[dict[str, Any]]`
- `permissions: list[dict[str, Any]]`
- `quality_constraints: list[dict[str, Any]]`

Update compile output schema and prompt:
- Require the spec agent to produce structured fields.
- Cap `behavior_journeys` at 5.
- Journey entries must include `role: "illustrative"`, `covers_primary_actions`, `start_state`, and `entry_route`.

Add normalization/validation helpers:
- Normalize legacy specs without new fields to empty lists plus warnings.
- Add `validate_structured_spec(spec) -> list[str]`.
- Keep backward compatibility by storing validation warnings in `lint_warnings` for old/incomplete payloads unless a newly generated structured spec violates hard rules.

### `otto/prompts/lead.md`

Extend architect instructions:
- Require `## Information Architecture Contract` in `CHARTER.md`.
- Require a fenced JSON object matching the IA shape.
- Tell architects to align `action_surfaces[].id` with spec `core_entities[].primary_actions[].id`.

### `otto/v5_capability_inventory.py`

Add IA contract support:
- `InformationArchitectureContract` dataclass or typed dict payload.
- `parse_information_architecture_contract(charter_text_or_path)`.
- `validate_information_architecture_contract(project_dir, spec=None)`.
- Extend `CoherenceFinding.kind` values to include IA failures.

Validation rules:
- Missing IA section fails coherence gate for webapps.
- `action_surfaces[].target_route` must resolve to `routes[].id`.
- `action_surfaces[].surfaces[]` must use known surface kinds or known declared surface IDs.
- Every spec primary action id must have a matching `action_surfaces[].id`.

Known surface kinds:
- `route`, `nav`, `sidebar`, `topbar`, `toolbar`, `empty_state`, `keyboard`, `command_palette`, `modal`, `form`, `button`, `card`, `list`, `table`, `settings`, `global`.
- Dotted references like `backlog.empty_state` are valid if their final segment is a known kind.

### `otto/lead.py`

Surgical post-verdict hook:
- After `_read_agent_verdict(session_dir)`, call a new helper when a verdict payload exists.
- The helper writes `<session_dir>/verification_plan.json`.
- The helper returns runner check results and a possibly downgraded verdict.
- Do not mutate `verdict.json`.
- Store the final verdict in graph and `summary.json`; append runner check notes to `result.verify_result["summary"]` when applicable.

### New `otto/v5_verification_plan.py`

Implement deterministic checks from spec + CHARTER IA:
- Load `spec/spec.json`.
- Locate `CHARTER.md` from the Lead worktree or project root.
- Parse IA with the new capability inventory parser.
- Build `verification_plan.checks[]`.
- Execute filesystem/code grep checks only.
- Write timestamped `verification_plan.json`.

Check implementation choices:
- `route_resolves`: pass when route `path` or route `id`/component-ish token appears in non-noise source files.
- `endpoint_resolves`: pass when endpoint `path` or id tail appears in non-noise source files.
- `action_has_test`: pass when an action id appears in a test file or in verdict/evidence text.
- `mutating_action_has_feedback`: pass when code contains the action id or verb near feedback patterns (`toast`, `notification`, `alert`, `redirect`, `navigate`, `success`, `error`).
- `entity_has_empty_state`: pass when every entity id/name has an IA `empty_states[]` entry with `cta_present: true`.
- `no_stub_text`: fail on `TODO`, `Lorem ipsum`, or `placeholder text` in user-facing built/source artifacts, skipping source comments where cheaply detectable.
- `verdict_consistency`: fail when `intent_coverage.built` overlaps textually with `partial` or `skipped`.

Downgrade order:
1. If any runner check fails and agent said `pass`, set final verdict to `partial`.
2. If any required journey has no `passed` entry, set final verdict to `partial`.
3. Preserve `partial`/`unverified`/`merge_blocked`, but append `runner_checks` to summary.

### Tests

Add focused tests:
- `tests/test_spec_compile_flat_structured.py`
  - valid structured spec passes.
  - uncovered intent claim fails.
  - unreferenced primary action fails.
  - webapp requires a root journey from unauth/empty state.
  - legacy spec loads without crashing and reports warnings.
- `tests/test_v5_ia_contract.py`
  - parser extracts JSON under the heading.
  - missing IA fails webapp coherence.
  - bad target route fails.
  - bad surface kind fails.
  - missing action surface for spec primary action fails.
- `tests/test_v5_verification_plan.py`
  - unit coverage for each check kind.
  - pass verdict is downgraded to partial on route/action check failure.
- Integration-style stub:
  - Run `run_lead()` with agent execution monkeypatched to write `verdict.json`.
  - Fixture spec + CHARTER IA create a failing deterministic check.
  - Assert `verification_plan.json` exists, graph/summary verdict is `partial`, and input `verdict.json` remains `pass`.

## Schema Diff

`spec.json` adds:

```json
{
  "intent_claims": [
    {"id": "claim.issue-crud", "text": "Users can create issues", "source_line": 3}
  ],
  "core_entities": [
    {
      "id": "issue",
      "name": "Issue",
      "fields": [
        {"id": "claim.issue-title", "name": "title", "type": "string", "intent_claim_ids": ["claim.issue-crud"]}
      ],
      "states": ["open", "closed"],
      "primary_actions": [
        {
          "id": "issue.create",
          "verb": "create",
          "success_observable": "Issue appears in the backlog",
          "error_observable": "Inline error explains why creation failed",
          "intent_claim_ids": ["claim.issue-crud"]
        }
      ]
    }
  ],
  "cold_start_states": [
    {"id": "unauthenticated", "name": "Unauthenticated visitor"}
  ],
  "permissions": [
    {"id": "member", "name": "Member", "gates": ["issue.create"]}
  ],
  "quality_constraints": [
    {"id": "claim.form-feedback", "text": "All forms have user-visible feedback on submit"}
  ],
  "behavior_journeys": [
    {
      "id": "create_issue_from_backlog",
      "role": "illustrative",
      "description": "A member opens the backlog, creates an issue, and sees it appear.",
      "covers_primary_actions": ["issue.create"],
      "start_state": "authenticated_seeded_workspace",
      "entry_route": "/"
    }
  ]
}
```

Backward-compatible reader behavior:
- Missing new arrays load as `[]`.
- Existing journey entries without the new keys remain readable.
- Structured validation warnings are emitted rather than crashing old projects.

## Validator Rules

| Rule | Applies To | Failure |
| --- | --- | --- |
| `behavior_journeys` max 5 | New/structured specs | Compile validation error |
| Journey role is `illustrative` | New/structured specs | Compile validation error |
| Every intent claim covered by action, field, quality constraint, or journey description | New/structured specs | Compile validation error |
| Every primary action covered by at least one journey | New/structured specs | Compile validation error |
| Webapp has root journey from unauth/empty state | New/structured webapp specs | Compile validation error |
| CHARTER IA missing for webapp | Coherence gate | Coherence finding/fail event |
| IA action surface target route resolves | Coherence gate | Coherence finding |
| IA action surface surface kind known | Coherence gate | Coherence finding |
| Spec primary action has IA action surface | Coherence gate | Coherence finding |
| Runner deterministic check fails after agent pass | Lead verdict read | Final verdict downgraded to `partial` |

## Verification Plan

Focused tests while iterating:

```bash
uv run --extra dev pytest tests/test_spec_compile_flat_structured.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py -q
```

Required final suite:

```bash
uv run --extra dev pytest tests/ -x -q
```

Manual grep verification after edits:
- Search for old prompt text `Aim for 5-12 journeys`.
- Search for new schema field references across `otto/`.
- Search for `verification_plan` write/read paths.

## Plan Review

`/codex-gate` is mandatory by project instructions, but the required `mcp__codex__codex` tool is not available in this session. The plan therefore records this gate as blocked by tooling; implementation proceeds with focused unit/integration tests and a final self-review.

## Final Change Log

### Files Changed

- `otto/spec_compile_flat.py`: added schema version 2 structured fields, structured output prompt/schema, strict structured validator, serialization/load compatibility, and hard compile failures for structured contract violations after retries.
- `otto/prompts/lead.md`: added required CHARTER `## Information Architecture Contract` JSON block instructions for architects.
- `otto/v5_capability_inventory.py`: added IA JSON extraction, validation, surface/route/action coherence checks, and integrated IA findings into `check_coherence()`.
- `otto/v5_verification_plan.py`: added deterministic runner-side check matrix and `verification_plan.json` writer.
- `otto/lead.py`: added post-`verdict.json` runner verification hook and final-verdict downgrade handling without mutating the agent input file.
- `tests/test_spec_compile_flat_structured.py`: added structured spec validator tests.
- `tests/test_v5_ia_contract.py`: added CHARTER IA parser/coherence tests.
- `tests/test_v5_verification_plan.py`: added per-check tests plus a stubbed v5 pipeline downgrade test.

### Test Results

- PASS: `uv run --extra dev pytest tests/test_spec_compile_flat_structured.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py -q`
  - `21 passed`
- PASS: `uv run --extra dev pytest tests/test_v5_capability_inventory.py tests/test_v5_verdict_recovery.py tests/test_v5_phase1.py tests/test_v5_phase2.py -q`
  - `69 passed`
- PASS: `uv run --extra dev pytest tests/test_spec_compile_flat_structured.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py tests/test_v5_capability_inventory.py tests/test_v5_verdict_recovery.py tests/test_v5_phase1.py tests/test_v5_phase2.py -q`
  - `90 passed`
- PASS: `uv run --extra dev ruff check otto/lead.py otto/spec_compile_flat.py otto/v5_capability_inventory.py otto/v5_verification_plan.py tests/test_spec_compile_flat_structured.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py`
- FAIL: `uv run --extra dev pytest tests/ -x -q`
  - Stopped at `tests/integration/test_brownfield_compile_real.py::test_brownfield_compile_against_real_fixture_no_base`
  - Failure: `Group.__init__() got an unexpected keyword argument 'title'`
  - This failure is in the brownfield/monolithic compile path, outside the requested v5 hierarchical scope.
- INCONCLUSIVE: `uv run --extra dev pytest tests/ -x -q --ignore=tests/integration/test_brownfield_compile_real.py`
  - Hung silently for several minutes; sandbox prevented process inspection and process termination (`ps`/`pkill` denied process-list access).

### Known Limitations

- CHARTER IA coherence emits findings through the existing warning/finding path; it does not add a new architect retry/blocking mechanism.
- `route_resolves` is grep-based and treats route path or component-ish route ID as sufficient evidence.
- `mutating_action_has_feedback` is heuristic grep for action/entity/verb near feedback patterns; it is deterministic but not AST-aware.
- `no_stub_text` skips obvious comment lines but does not parse JavaScript/TypeScript string ASTs.

### Deferred Items

- No browser, Playwright, or agentic certifier checks were added per scope.
- No monolithic/brownfield spec path changes were made.
- Requested commits could not be created because the sandbox cannot write the worktree's Git index metadata under `/Users/yuxuan/work/cc-autonomous/.git/worktrees/cc-i2p-2/`.
