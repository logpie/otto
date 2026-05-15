# Plan — Typed-Level Journey Verification (centralize + de-brittle)

_Base: branch `cc-i2p-2` @ `e7ca4406b` (agent-native repair protocol,
both gates APPROVED, in sync with origin). Net-new on top._

## Problem (proven, not hypothetical)

Otto v5 marked iTracker journey `new_user_onboard` `passed=True` while a
real-browser repro proved the shipped `/workspaces` is an infinite
loading skeleton — 0 controls, empty `body.innerText`, dead
`workspaces.create` API client. A non-working product reported
partial-with-journeys-passed.

Root cause (Codex, independent): **contract-to-oracle lowering
failure** — spec `behavior_journeys` are never lowered into executable,
node-appropriate checks, so API calls / static builds / the agent's own
prose stand in for the user-facing product promise. The disease is
amplified by **~5 overlapping journey-ish mechanisms** that drift:
the integration API audit (gates, no UI), `role:"illustrative"`
(escapes gating), `ia_missing_action_surface` coherence (warning-only),
the certifier (browser-capable but documentation-only), and
`lead_verify` "all native tests passed ⇒ every journey passed".

## Design (dual-validated: user's model + Codex adversarial review)

**One journey concept. One typed field. Each node runs the journeys it
can actually satisfy. Verdict gates. Delete the overlap.**

A `behavior_journey` carries `verification_level ∈ {ui, api}` where
**`api` = "non-UI, controller-run verification"** (NOT "HTTP" —
PG-R2 #4). `api` dispatches on a typed `probe_kind` adapter:
`http_api | cli_command | library_call | service_health`. One field +
a small adapter table, still no if/else tree.
- **Leaf** runs its `api`-level journeys via its own oracle, filtered
  to journeys whose `covers_primary_actions` are in the leaf's scope.
- **Root integration** runs `ui`-level journeys against the live
  assembled deployment as deterministic, controller-run, real-effect
  DOM probes.
- **Single applicability authority (PG-R2 #5):** one exhaustive
  policy **table** `(execution_scope, verification_level) → {run |
  defer | skip | fail}` is the *only* place that decides whether a
  journey runs at a node. No ad-hoc `if scope==root and level==ui`
  anywhere; a guard test proves only the policy module decides.
- Verdicts **gate**. No `illustrative` escape.

### Deterministic assignment rule — FAIL CLOSED (PG-R1 #1)
Assigned at **spec-compile** from the journey's own shape. The earlier
"empty entry_route → api" default silently downgraded webapp UI
journeys (recreating the bug for legacy/cached/implicit-route specs).
Corrected rule, fail-closed:
```
if project_kind == "webapp":
    if journey.entry_route is MISSING or malformed:
        HARD-FAIL spec-compile (error, not warning) — never auto-"api"
    elif journey.api_only == true and entry_route.startswith("/api"):
        verification_level = "api"
    else:
        verification_level = "ui"          # the webapp default
elif project_kind in ("cli","library","api","service"):
    verification_level = "api"
else:
    HARD-FAIL spec-compile: unknown project_kind
```
Precise distinction (PG-R3 wording): a *malformed/missing
`entry_route`* on a webapp journey is a **hard compile error** (reject
before any run); a *well-formed* webapp journey that isn't explicitly
`api_only` defaults to **`ui`**. A webapp can never silently get
`api`. Classifies all 5 iTracker journeys `ui`.

### Mechanisms DELETED (this is the brittleness reduction)
- `role:"illustrative"` as a verdict escape — `spec_compile_flat.py`
  (~315 schema/shape, ~469 prompt).
- "agent verdict `journeys[].passed` ⇒ journey passed" gate —
  `v5_verification_plan.py:~857`.
- "all native tests/browser runner passed ⇒ every in-scope journey
  passed" aggregate heuristic — `lead_verify.py:~109` (*the exact line
  that false-passed `new_user_onboard`*).
- `ia_missing_action_surface` as a separate warning-only pseudo-gate —
  demote to **probe input only** (CHARTER action-surface data may feed
  the ui probe's control lookup; it no longer emits a verdict channel).
- Certifier NOT extended for journeys — `audit.py` keeps journeys as
  documentation/evidence context only, never the gating executor.

Net: 5 overlapping mechanisms → 1 typed field + 1 deterministic rule +
1 executor dispatch. Less code, less branching.

### The genuinely-new pieces (bounded; honest "not free")
1. **Central fail-closed journey-verdict sink (PG-R1 #2 — build FIRST).**
   One code path computes every journey verdict, keyed by
   `verification_level`, defaulting **fail-closed**: no executor result
   / malformed / non-proof / no executor ⇒ `fail`/`unverified` (never
   silent pass). **Sink precedence (PG-R2 #1):** a controller-run
   executor `fail`/`unverified` **dominates** any legacy-adapter
   `pass`. The legacy adapter may supply a verdict ONLY for journey
   classes with no registered executor yet, and its result is tagged
   `source=legacy_adapter` (explicitly *not proof*); an executor
   result of any kind overrides it. Old heuristics are removed (S6)
   only once both executors feed the sink.
1b. **Declarative journey pass-model — schema + compiler + load-time
   validation (PG-R2 #2, scheduled BEFORE S4 gates ui).** Add the
   declarative step-model (start_state, setup, ordered actions,
   success observables, ready/settle, network expectations, final DOM
   assertions) to the spec schema; spec-compile emits it for
   `ui`-level journeys; load-time validation requires it. A `ui`
   journey lacking a valid model yields `verification_contract_missing`
   routed to **spec/contract repair** (or compile failure) — NOT
   generic product repair (a missing verification contract is not an
   app bug; routing it to product repair would loop forever).
   **Pass-model adequacy validator (PG-R3 — the last anti-smuggle
   gate).** Because spec-compile is LLM-authored, a *weak* model
   ("route loaded", "HTTP 200", "body non-empty", "text `Workspace`
   appears", skeleton present) would let a broken UI pass while still
   being controller-run — an agent-decided pass smuggled in as a
   too-weak oracle. So the model is itself validated for **effect
   strength**: every state-changing journey step MUST carry ≥1
   non-tautological post-action observable tied to a covered primary
   action / entity effect — a concrete DOM state transition, persisted
   data becoming visible in the UI, or an expected network
   method/path/status **plus** the matching UI effect. Models whose
   success criteria are only route-loaded / status / body-present /
   skeleton / generic-text are rejected as
   `verification_contract_invalid` (→ spec/contract repair, not
   product repair). A weak model can no longer launder a dead UI to
   pass.
2. **`execution_scope` enum, threaded (PG-R1 #5).** Replace scattered
   `task_id == root` checks with one explicit
   `execution_scope ∈ {leaf, subtree_integration, root_integration}`
   passed into `lead.run_lead` (today only `leaf|integration`,
   `lead.py:~277`) and the verifier. Root runs in `run_v5_pipeline` on
   `project_dir` (`v5_runner.py:~1715`); subtree runs `_run_integration`
   on a task worktree (`~3496`) — the enum is the single discriminator,
   not duplicated path checks. Only `root_integration` runs `ui`
   journeys; `subtree_integration` explicitly excluded.
3. **`api` executor family — real or fail closed (PG-R1 #3 + PG-R2 #4).**
   `api` is non-UI controller verification with typed adapters:
   `http_api` (stateful sequence: ordered steps, carried auth/state,
   asserted responses — existing `ApiProbe` lacks this so it's
   extended, not reused as-is), `cli_command` (run the built CLI,
   assert exit/stdout/fs effect), `library_call` (import + call +
   assert return/raise), `service_health` (start + health/event
   assertion). No HTTP `base_url` is required for cli/library. Any
   adapter with malformed/`proof_usable=false`/unsupported lowering ⇒
   **fail closed**, never an informational pass.
4. **UI executor: fail-only floor + optional declarative pass-model
   (PG-R1 #4).** The cheap floor is **fail-only**: it can FAIL a
   journey (route stuck/blank, control absent, click → no
   network/DOM effect — kills today's dead-end) but **cannot by itself
   grant `pass`** for a multi-step journey (e.g. `new_user_onboard` =
   register→verify→login→create-workspace→create-team→backlog). A
   journey earns `pass` only with a **declarative step model**
   (start_state, setup, ordered actions, success observables,
   ready/settle policy, network expectations, final DOM assertions);
   absent that model the journey is fail-closed `unverified`, routed
   to repair. Controller-run, deterministic, never agent-narrated.
5. **UI probe is a clean-oracle STEP, not a post-smoke add-on
   (PG-R1 #6).** `verify_from_clean_oracle` copies to temp, starts
   `start.sh`, probes ports, then KILLS the service; the DOM probe
   must run **while that temp deployment is alive**, on the declared
   ports, with artifacts preserved (timestamped network log, console
   errors, DOM snapshot) and **zero dirtying** of the real
   sqlite/uploads/worktree (assert no git diff after). Implement as an
   ordered step inside the clean oracle, before teardown.

## Rejected alternatives (why)
- **My earlier 3-stage "lowering chain" framing** — unnecessary
  scaffolding; the typed-level model expresses the same truth with one
  field and *deletes* mechanisms instead of adding stages.
- **Extend the certifier to gate journeys** — reintroduces a separate
  subsystem + the gate-vs-documentation split; the live probe at root
  integration *is* the check.
- **Keep `illustrative` / API-audit** — that is precisely the bug.
- **External certifier now** — explicitly deferred; in-loop
  self-verification is the floor, external certifier a later option.

## Implementation sequencing — RESEQUENCED so no step strands journeys (PG-R1 #2)

Old heuristics stay live until BOTH executors feed the central sink.
Each step shippable; Codex implements, Claude reviews high-risk.

**S1 — Typed field + fail-closed assignment + cache/load enforcement
(PG-R2 #3).** Add `verification_level` (+ `probe_kind` for `api`) to
the behavior_journey schema; assign at spec-compile via the
fail-closed rule; `role:"illustrative"` ignored as a gating input but
tolerated in old specs (deletion deferred to S6). **Bump the spec
schema version + cache key**; add **load-time validation/migration**
so cached/already-materialized specs cannot bypass compile-time
validation — a cache hit with missing/ambiguous `entry_route` or
absent typed fields is rejected or migrated *before* the runner
accepts it; unmigratable journeys fail closed. **Verify:** iTracker
spec → 5 journeys `ui`; webapp spec with missing/ambiguous
`entry_route` → compile ERROR (not silent `api`); cli/library spec →
`api` with correct `probe_kind`; a **cached legacy webapp spec with no
`entry_route`** is rejected/migrated on cache hit, never executed as
pre-S1 data; legacy spec containing `role` still compiles.

**S2 — Central fail-closed journey-verdict sink.** One code path that
maps `verification_level` → executor → verdict, defaulting fail-closed
(no result / malformed / non-proof / no executor ⇒ non-pass). Old
heuristics still feed it for now (adapter), so behavior is unchanged
but routing is centralized. **Verify:** all existing
green suites still pass through the sink unchanged; a journey with
absent evidence yields non-pass via the sink (unit).

**S3 — `execution_scope` enum + table-driven applicability (PG-R2 #5).**
Replace `task_id==root` checks with `{leaf, subtree_integration,
root_integration}` through `lead.run_lead` + verifier, AND a single
exhaustive policy table `(execution_scope, verification_level) →
{run|defer|skip|fail}` as the *only* applicability authority. No
ad-hoc scope/level conditionals outside the policy module. **Verify:**
unit covers every `(scope×level)` cell; a guard/static test proves
only the policy module decides applicability; depth-3 graph → exactly
one `root_integration`, subtree integrations excluded from `ui` by the
table (not by an inline check).

**S4 — `ui` executor as a clean-oracle step + gate (regression anchor).**
Lower `ui` journeys → fail-only real-effect DOM probe (+ declarative
pass-model where present), run as an ordered step inside
`verify_from_clean_oracle` while the temp deploy is alive, artifacts
preserved, no worktree dirtying; feed the S2 sink; gate the verdict.
**Verify:** re-run the *current broken iTracker build*
(`/Users/yuxuan/otto-projects/v5-itracker-protofix-133937`, its git
`main`) — `new_user_onboard` ui-probe FAILS at root integration
because `/workspaces` stays skeleton (`WorkspaceDashboard` gets no
`slug`) — fail on DOM-readiness/effect, NOT HTTP 200; run verdict must
NOT report that journey passed; post-probe `git diff` in the real
project is empty.

**S5 — `api` executor at leaves (stateful or fail-closed).** Lower
in-scope `api` journeys → stateful probe sequence; unsupported lowering
⇒ fail closed (never malformed-`ApiProbe` pass); filter by
`covers_primary_actions` ownership. **Verify:** API-only mutating
journey needing POST body/auth → fails closed if unsupported, passes
only on real stateful success; a leaf whose scope omits an action does
not run/claim that journey.

**S6 — Delete old heuristics + demote coherence/certifier; guardrail.**
Now that both executors feed the sink, remove
`v5_verification_plan.py:~149/857` + `lead_verify.py:~109` +
`role:"illustrative"` schema/prompt (with old-spec compat). Demote
`ia_missing_action_surface` to probe-input/advisory; `audit.py`
journeys documentation-only. Extend
`tests/test_brittleness_guardrail.py`: a journey verdict may be set
ONLY by the controller-run sink keyed by `verification_level` — never
from agent-narrated `journeys[].passed`, an aggregate "all tests
passed" heuristic, or output text. **Verify:** grep proves the 3
heuristics gone; guardrail fails if any returns; full suite green.

Each step: regression test RED pre-change / GREEN after; full smoke
tier stays green; no regression in the repair-protocol / hardening
suites; ruff + basedpyright clean.

## Verification criteria (system-level; not unit dupes)
1. **Regression anchor:** the existing broken iTracker build →
   `new_user_onboard` FAILS at root integration; run does not claim it
   passed. Only after onboarding UI genuinely works does it pass.
2. **No false pass:** a webapp whose UI is a dead-end but whose API is
   complete must NOT reach `pass` or journeys-passed; it routes to
   repair (ties into the agent-native repair protocol).
3. **api/ui split honored:** on a forced multi-subsystem field-test, a
   backend leaf verifies its `api` journeys in isolation; `ui`
   journeys only at root integration; no leaf claims a `ui` journey.
4. **Deletion proven:** grep shows the 3 deleted heuristics gone; the
   guardrail test fails if any returns.
5. **Centralization proven:** one code path computes journey verdicts,
   keyed by `verification_level`; no per-journey if/else.
6. **Shape-enforced E2E (post-impl):** field-tests 06-09
   (forced-recursion/failure) + a fresh iTracker, real provider, run
   to terminal verdict; honest pass/partial only when the real user
   flow works; verdicts trustworthy under the new oracle.
7. (PG-R1) Compile a legacy/cached webapp spec with missing
   `entry_route` → compile ERROR, never silent `api`, never pass
   without UI execution.
8. (PG-R1) Dead-UI/working-API fixture: API POST 201 but the UI
   control absent/inert → root verdict non-pass, journey FAIL.
9. (PG-R1) API-only mutating journey needing POST body/auth →
   unsupported lowering fails closed, not a malformed-`ApiProbe` pass.
10. (PG-R1) Depth-3 graph: exactly one `root_integration` verifier
    invocation; subtree integrations excluded from UI probes.
11. (PG-R1) iTracker anchor: probe fails on DOM readiness/effect
    (skeleton, `WorkspaceDashboard` no `slug`) — explicitly NOT on
    HTTP 200; artifacts include timestamped network log + console
    errors + DOM snapshot; no git diff in the real project post-probe.

## Plan Review
<!-- codex-gate Plan Gate trail appended below -->

### Round 1 — Codex (REVISE → all 3 CRITICAL + 4 IMPORTANT accepted)
- [CRITICAL 1] assignment rule silently downgraded webapp→api on empty entry_route — fixed: fail-closed (webapp ⇒ ui unless explicitly api-only; missing/ambiguous entry_route = compile ERROR)
- [CRITICAL 2] S3 deleted the only hard journey gate before S4 added api path → strands journeys — fixed: resequenced; central fail-closed sink built FIRST (S2), old heuristics removed LAST (S6) after both executors feed it
- [CRITICAL 3] ApiProbe lowering not real (no body/auth/state; missing base_url = informational pass) — fixed: S5 requires stateful probe sequence OR fail-closed-unsupported; malformed/non-proof evidence may never satisfy a journey
- [IMPORTANT 4] UI probe too weak to grant pass for multi-step journeys — fixed: fail-only floor (can fail, cannot solely pass) + optional declarative step-model required for pass; else fail-closed unverified
- [IMPORTANT 5] root/subtree marker not "one field" — fixed: explicit execution_scope enum {leaf,subtree_integration,root_integration} threaded through lead.run_lead + verifier (S3), not scattered task_id==root
- [IMPORTANT 6] UI probe lifecycle conflicts with clean oracle (service killed post-smoke) — fixed: UI probe is an ordered STEP inside verify_from_clean_oracle while deploy alive, declared ports, artifacts preserved, zero worktree dirtying
- [IMPORTANT 7] role:illustrative deletion underspecified/misdiagnosed (schema/prompt, not runtime gate) — fixed: S1 ignores it as gating input but tolerates old specs; schema/prompt deletion deferred to S6 with compat
- [ADDED] 5 extra verify criteria (7-11) folded into the plan

### Round 2 — Codex (REVISE → all 5 new findings accepted)
- [R2 #1] S2 sink adapter-precedence hole — fixed: controller-run executor fail/unverified DOMINATES legacy-adapter pass; legacy adapter only for no-executor classes, tagged source=legacy_adapter (not proof)
- [R2 #2] S4 strands webapp journeys (no scheduled pass-model compiler) — fixed: added S1b declarative pass-model schema+compiler+load-time validation BEFORE S4; missing model ⇒ verification_contract_missing → spec/contract repair, NOT product repair
- [R2 #3] fail-closed assignment bypassed by cached specs — fixed: S1 bumps schema/cache key + load-time validation/migration; cached spec with missing entry_route rejected/migrated before runner accepts it
- [R2 #4] `api` conflates HTTP with all non-UI — fixed: `api` = non-UI controller verification with typed probe_kind adapters {http_api, cli_command, library_call, service_health}; no base_url for cli/library; unsupported ⇒ fail closed
- [R2 #5] execution_scope only simpler if table-driven — fixed: single exhaustive (execution_scope×verification_level)→{run|defer|skip|fail} policy table is the only applicability authority; guard test proves no ad-hoc checks
- [ADDED] extra verify criteria folded (legacy-pass+ui-fail→non-pass source=ui_executor; model-removed→contract-missing not product-loop; cached-no-entry_route rejected on hit; cli+library journeys verified without HTTP; (scope×level) policy-module-only guard)

### Round 3 — Codex (REVISE → 1 blocker + wording, both accepted; sequencing+tables APPROVED)
- [R3 blocker] declarative pass-model could be an LLM-authored WEAK oracle (route-loaded / HTTP 200 / text-present) that launders a dead UI to pass while controller-run — fixed: added a **pass-model adequacy validator** in S1b/S4; every state-changing step needs ≥1 non-tautological post-action observable tied to a covered primary action/entity effect; weak models rejected as verification_contract_invalid → spec/contract repair
- [R3 wording] line 56 "ambiguity ⇒ ui" vs lines 61-62 "missing/ambiguous entry_route = compile error" — fixed: rule rewritten to distinguish malformed/missing entry_route (HARD compile error) from a well-formed non-api_only webapp journey (defaults ui)
- [ADDED verify] weak iTracker UI model (only /workspaces-loaded or HTTP 200) → rejected verification_contract_invalid; dead-skeleton + weak final assertion cannot pass; working UI passes only when the model asserts a real effect (created workspace/team/issue appears post-action)
- Codex confirmed: S1→S1b→S2→S3→S4→S5→S6 strand-free; probe_kind + (scope×level) tables are acceptable net-simplification given the exhaustive-data-table + guard-test requirement

### Round 4 — Codex
- APPROVED. No new blocker. Pass-model adequacy validator closes the last false-pass path (weak LLM-authored oracle rejected as verification_contract_invalid → spec/contract repair). Wording fix clean (missing/malformed webapp entry_route hard-fails compile; well-formed non-api_only webapp defaults ui). Sequencing S1→S1b→S2→S3→S4→S5→S6 strand-free; policy/probe tables constrained by exhaustiveness + guard tests.

Plan Gate: 4 rounds (7+5+1 findings) → APPROVED. Implementation Gate runs before merge.
