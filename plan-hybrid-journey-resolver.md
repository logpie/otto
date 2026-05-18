# Plan: Hybrid Journey Resolver (verification-protocol root fix)

Status: design (no Codex this campaign — overrides codex-gate). Author: Claude. Date: 2026-05-18.

## Why (root cause, generic)

`compile-spec` emits `behavior_journeys` whose steps carry **literal**
accessible selectors (label text, role+name). Independent leaf agents
build the UI with their own reasonable wording. `journey_ui_executor.py`
matches the literal selectors with strict Playwright queries. Two
independently-LLM-generated artifacts are forced to agree on arbitrary
strings → brittle by construction (`"Create workspace"` vs
`"Create your workspace"`; `aria-label "Search query"` vs `"Search"`).
This is a generic decomp-boundary wire-shape gap that hits **every**
multi-feature web product otto builds, not iTracker-specifically.
Symptom: journey 1 fails its first locator (`required input absent:
label='Name'`); journeys 2-4 cascade (shared sequential session) →
`merge_blocked` on a functionally-correct product.

User-approved fix: **Hybrid resolver (C)** — separate *intent* (stable)
from *selector* (UI-specific, drifts); split *locating* (adaptive,
semantic) from *asserting* (deterministic, evidenced). Repair-protocol
mitigation already shipped (`baf5baf7b`).

## Current architecture (grounded)

`otto/journey_ui_executor.py` (playwright.sync_api):
- `_run_one_journey` → per-step `_run_action`.
- `_action_locator(page, action)` → `(locator, control_label)`;
  `locator.count()==0` → `"required control absent: {control}"`. **Brittle.**
- `_input_locator(page, fill)` → `(field, field_label)`; absent →
  `"required input absent: {field_label}"`. **Brittle.**
- `_assert_dom_observable(page, observable, …)` → deterministic DOM
  assertion. **Trustworthy verdict layer — DO NOT make LLM-judged.**
- `compile-spec.md` ~921-934 + `behavior_journeys` schema (line 114):
  emits the literal step selectors. **Emitting side.**

## Design

### Invariant (anti-false-pass — the project's deepest value)
Only **locating** becomes adaptive. The **verdict** stays deterministic:
`_assert_dom_observable`, DOM/state/API checks, screenshots. An LLM may
choose *which control* a step means; it must never decide *whether the
step passed*. No oracle weakening.

### Phase 1 — semantic-resolution fallback in the locator layer (cheap, validates on existing checkpoint via fast resume)
When `_action_locator`/`_input_locator` resolve to `count()==0`:
1. Snapshot the page: `page.accessibility.snapshot()` + a compact list
   of visible interactive elements (role, accessible name, text,
   placeholder, nearby label, test-id, bounding box).
2. Resolve intent → element: a bounded, cached LLM call maps the step's
   intent (derived from the existing literal label/role/name treated as
   *fuzzy intent*, plus the step description) to the best-matching
   element; returns a robust locator (prefer test-id > role+name >
   text > css path). Deterministic tie-breakers; refuse on low
   confidence (fail closed → still `required X absent`, never a fake
   pass).
3. Cache resolution per (journey, step, dom-hash) so re-runs are
   deterministic and cheap; record the chosen locator + rationale in
   the journey artifact for audit.
4. The literal selector is still tried FIRST (fast path); semantic
   resolution only on miss → zero cost when labels happen to match.

### Phase 2 — emit intent, not literal strings (needs fresh build to validate)
`compile-spec` journey step schema gains an explicit `intent` (semantic
goal) alongside/instead of literal selectors; `behavior_journeys`
guidance updated so steps describe *what the user is trying to do* +
concrete observable outcome, not exact aria text. Phase 1 already works
without this (derives intent from existing strings); Phase 2 removes the
root coupling for new builds.

## Anti-false-pass safeguards
- Verdict layer unchanged (deterministic DOM/state assertions).
- Semantic resolver fails CLOSED (low confidence → absent, not pass).
- Resolution cached + logged (auditable; reproducible reruns).
- A resolver that returns a wrong element still fails the deterministic
  post-action observable → no false pass, just a different failure msg.

## Phased implementation + Verify
1. **P1a**: add `_semantic_resolve(page, intent, kind)` helper +
   accessibility snapshot capture; unit-test resolver on a fixture DOM
   with drifted labels ("Create workspace" vs intent "create your
   workspace"). *Verify:* unit test resolves drifted control; low-conf
   input → None (fail-closed).
2. **P1b**: wire fallback into `_action_locator`/`_input_locator`
   (literal-first, semantic-on-miss); resolution cache + artifact log.
   *Verify:* journey_ui_executor unit/integration test: a journey with
   a deliberately-drifted label passes via semantic fallback; a truly
   absent control still fails.
3. **P1c**: fast e2e — resume mib16 checkpoint (resume16<N>); the 4
   journeys that failed on label drift now pass locating, integration
   proceeds. *Verify:* root finalizes ≥partial + proof-packet; boot
   start.sh, drive register→workspace→issue in a browser.
4. **P2**: compile-spec intent schema + guidance; fresh full `otto v5
   run`. *Verify:* fresh build's journeys pass without any literal-label
   contract.

## Out of scope / non-goals
- No oracle weakening; no LLM-judged verdicts.
- Not fixing the pre-existing `test_startup_port_cleanup_routes_to_packet_repair`
  (fails at 2049cae00; unrelated; documented).

## Validation economics
P1 validates on the existing mib16 checkpoint via **fast ~2-min
resume** (executor change affects how journeys run vs the already-built
product). P2 needs a full ~40-min fresh build. Do P1 first.
