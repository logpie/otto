# Journey Setup-Precondition Protocol — Root Cause + Fix Plan

Written: 2026-05-18 (resume16o terminal merge_blocked, root-caused from real evidence)

## Verified Root Cause (NOT a product bug — an oracle false-negative)

The clean_deploy UI-journey oracle (`otto/journey_ui_executor.py::_run_one_journey`,
~L218-297) does: `validate_ui_pass_model` -> `_ready_route` -> `page.goto(entry_route)`
-> run `pass_model.actions` -> `final_dom_assertions`. It **never reads
`pass_model.setup`** and never establishes the journey's declared `start_state`.
grep `setup|seed|start_state|precondition` across journey_ui_executor.py /
v5_clean_verify.py = zero hits.

Spec asymmetry (resume16o compiled spec.json `behavior_journeys`), proven against
real per-journey verdict.json + saved dom.html:

| journey | start_state | entry_route | setup | clean_deploy result |
|---|---|---|---|---|
| register_and_create_first_issue | unauthenticated | /register | 0 | PASS (cold, self-bootstrapping) |
| update_issue_status_via_dropdown | workspace_with_issue | /issues/ENG-1 | 1 seed | FAIL — DOM literally "Issue not found" |
| comment_with_mention_appears_in_inbox | workspace_with_two_members | /issues/ENG-1 | 2 seed | FAIL — same empty page |
| search_with_status_and_priority_operators | workspace_with_issues | /search | 2 seed | FAIL — seeded issue not in results |
| api_pat_issue_lifecycle | authenticated_with_pat | /api | 0 | (not in failing UI set) |

Product is correct: `IssueListView` links `to=/issues/${identifier}`; router
`path:'issues/:identifier' -> <IssueDetailPage/>` auto-discovered via
`App.tsx import.meta.glob('./features/*/routes.tsx')`; `store.fetchIssue` ->
`GET /api/issues/{identifier}`; backend `GET /issues/{issue_id_or_identifier}`
resolves `SELECT * FROM issues WHERE identifier=?`. It renders "Issue not found"
because `ENG-1` was **never seeded** — the oracle skipped `pass_model.setup`.

This explains the whole saga: only the cold journey ever passed; the repair
agent thrashed in otto's own codebase (the product had nothing to fix); budget
scaling / hybrid resolver / route-isolation / port fixes were all irrelevant to
the actual failure.

## Why generic

Any product whose behavior journeys declare a non-cold `start_state` with
`pass_model.setup` preconditions will spuriously fail every such journey with
"control absent" / "not visible". This is a systemic oracle FALSE-NEGATIVE
(product correct, oracle fails it for skipping the journey's own declared
contract). Fixing it is NOT gate-weakening — it makes the oracle correctly
establish the declared precondition so the journey tests what it is contracted
to test.

## Design (protocol, not patch — per patches-to-protocols)

Two linked otto root causes:

- R1 (oracle): `_run_one_journey` ignores `pass_model.setup`.
- R2 (compiler): `setup` is emitted as ABSTRACT declarations
  (`{action:"seed",entity:"issue",fields:{...}}`) with no executable recipe —
  even a fixed R1 oracle cannot execute an abstract seed against a black-box
  clean deployment without a product backdoor.

Principled fix = **"every behavior_journey pass_model must be executable from a
cold clean deployment"**:

1. Compiler contract: each non-cold journey's precondition is compiled to
   CONCRETE executable steps (UI actions or declared API calls) that reuse the
   same primitives `pass_model.actions` already use — the compiler has the
   product model (core_entities API contracts; journey 1 demonstrates the
   create flow), the oracle stays dumb.
2. Compile-time validator (`otto/journey_contracts.py::validate_ui_pass_model`):
   REJECT abstract/non-executable `setup` (shift-left, fail at compile not at
   clean_deploy — per fail-early-compile-time). A `seed` with no executable
   recipe is a hard compile error.
3. Oracle: `_run_one_journey` executes the (now concrete) `setup` steps via the
   existing `_run_action` machinery BEFORE `page.goto(entry_route)`; a setup
   failure = journey infra failure with a precise reason (not a silent skip).

Not gate-weakening: a genuinely broken product still fails the post-setup
actions/assertions; this only stops mis-failing a correct product for a
precondition the oracle never established.

## Concrete mechanism (decided)

- **Cold-state signal (generic, non-brittle):** the spec emits top-level
  `cold_start_states` (list, each `{id,name,description}`). A journey is COLD
  iff `journey.start_state` matches a `cold_start_states[].id`. Cold ⇒ setup
  may be empty (actions self-bootstrap). NON-cold ⇒ `pass_model.setup` MUST be
  non-empty AND every step executable. No hardcoded state-name list — uses the
  spec's own declaration. Threaded from `normalize_journey_contracts` (has the
  full payload incl `cold_start_states`) down into `validate_ui_pass_model`.
- **Executable setup-step shape:** same as `pass_model.actions` (role/name/
  label/text/selector locator + optional input fills) PLUS an optional per-step
  `route` (navigate before the step). Reuses the executor's existing
  `_run_action` — no new execution engine. An abstract
  `{action:"seed",entity:...,fields:...}` with no UI_LOCATOR_KEYS and no
  `route` is NON-executable → rejected.
- **R2 validator rule** (`validate_ui_pass_model`, fail-early): non-cold
  journey + (empty setup OR any setup step lacking route/locator) →
  `verification_contract_invalid` at compile, not a clean_deploy false-negative.
- **R1 oracle** (`_run_one_journey`, before `_ready_route`/goto): for each
  setup step → optional `page.goto(route)` then `_run_action`; on failure
  `_finalize_journey(status="fail", detail="setup precondition failed: ...")`.
  Cold journeys unchanged (empty setup → loop is a no-op → zero behavior change,
  so register_and_create_first_issue and all currently-passing cold journeys
  across every product are unaffected).
- **compile-spec.md:** add the contract — every non-cold behavior_journey's
  `pass_model.setup` is concrete executable steps (navigation + actions, same
  primitives as `actions`) that establish `start_state` from a cold clean
  deploy; abstract `seed` declarations are forbidden.

## Validation

Compiler + oracle pipeline change -> a mib16 RESUME reuses the OLD compiled
spec (abstract setup) so it cannot validate the true fix. Requires a FRESH
`otto v5 run --tier modular` (~40min) so the journeys recompile to concrete
self-bootstrapping form. Regression tests (red->green) gate the code change
first; fresh run validates end-to-end.

## Status

- [ ] R1+R2 implemented with regression tests
- [ ] ruff + journey/contract suites green
- [ ] commit ONLY fix files (not pre-existing uncommitted test edits/DEBUG/lock)
- [ ] FRESH otto v5 run validates >=1 non-cold journey passes
