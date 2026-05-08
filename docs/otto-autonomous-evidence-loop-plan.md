# Otto Autonomous Evidence Loop Root Plan

Status legend: `[ ]` not started, `[~]` in progress, `[x]` complete.

This file is the persistent execution plan for the root-fix work around Otto's
autonomous build/repair loop, browser evidence, AppServer migration, planned
behavioral testing, shared product contracts, and merge-aware verification.
Implementation work should compare against this file before being considered
done.

## Goal

Otto should autonomously build real products from a canonical product contract,
execute work safely in parallel, run deterministic evidence at the right graph
boundary, feed failures back into the same agent repair loop, and land only when
the planned behavior contract is satisfied.

## Work Items

1. `[x]` Canonical product contract
   - Spec output captures user-facing features plus shared contracts:
     persistence, storage schema, data model, app shell/routing, import/export
     formats, shared build/config/test runner behavior, and evidence plans.
   - Build, merge, audit, and Mission Control consume the same persisted
     contract.

2. `[x]` Planned behavioral test plan
   - Spec compile emits deterministic behavior journeys as checklist steps:
     `action -> expected visible outcome -> assertion -> artifact`.
   - These planned journeys are separate from optional exploratory true-web
     behavior so debugging is reproducible.

3. `[x]` CheckBridge inside the agent loop
   - Build/repair agents receive authoritative Otto check evidence in the same
     provider thread/resumed repair loop.
   - The bridge works for BrowserJourney, repo tests, type checks, API probes,
     state invariants, and future oracle checks.
   - Provider-side browser failures do not replace Otto-owned check evidence.

4. `[x]` BrowserJourney preflight
   - Fail early for detectable runner/config problems: hard-coded occupied
     ports, missing `webServer`/`baseURL`, relative routes without base URL,
     ignored Otto browser env values, overbroad test selection, unsafe shared
     `agent-browser` sessions, and cheap dependency/config blockers.
   - Preflight failures become compact repair evidence before expensive browser
     launch attempts.

5. `[x]` AppServer leverage
   - Use AppServer thread resume, structured output, diff updates, approval
     hooks, and normalized subagent events correctly.
   - If AppServer supports native tool/MCP wiring for Otto checks, expose an
     `otto_check`/`otto_browser_journey` tool; otherwise feed Otto check evidence
     through resumed turns.

6. `[x]` Shared-core work graph
   - Spec compile extracts foundational shared contracts into foundation or
     shared-core components.
   - Feature groups depend on those components instead of independently
     patching stores, schemas, persistence, config, or app shell behavior.

7. `[x]` Scope enforcement with contract semantics
   - Harmless scope crossings can remain warnings.
   - Critical shared-contract crossings during initial build become structured
     contract deltas for merge/audit integration instead of brittle automatic
     attempt failures.
   - Truly unsafe scope crossings and merge-repair overreach still block with
     actionable repair evidence.
   - Enforcement is driven by declared contract ownership, not a large brittle
     path heuristic list.
   - Contract enforcement must distinguish product invariants from file
     monopolies: feature groups may add feature-owned implementation and
     evidence files, while shared store/schema/app-shell/runner/config
     contract changes route through the owner or amendment flow.

8. `[x]` Merge-aware integration
   - Merge uses the canonical product contract, shared contracts, group
     provided/consumed surfaces, compact diffs, and failed check evidence.
   - After meaningful parallel waves, Otto runs impacted integration checks
     before downstream dispatch.

9. `[x]` Fast failed-state replay
   - Keep a fast validation path that replays the last failed check/browser
     state and verifies that preflight/check-bridge fixes help without rerunning
     unrelated compile/build work.

10. `[~]` True-web validation
    - Validate with real Mission Control true-web runs after unit/replay tests.
    - Required pressure projects include finance dashboard and micro Twitter;
      add one or two more complex projects if those pass.
    - True-web uses planned journeys plus realistic user behavior such as logs,
      diffs, refresh, back/forward, project switching, scrolling, keyboard
      input, and layout inspection.

## Acceptance Criteria

- Browser/config infra failures that can be detected cheaply fail in preflight
  before long browser timeouts.
- Failed authoritative checks are fed back to the same repair thread/session,
  with compact logs and artifact paths.
- Specs can persist planned behavior journeys and shared contracts, and these
  fields round-trip through parse/serialize.
- Critical shared-contract path edits are no longer only silent/soft warnings.
- Critical shared-contract path edits become visible contract deltas that merge
  and audit must inspect against the shared invariant.
- Merge/integration checks remain tied to the product contract rather than
  ad hoc retry loops.
- Final validation includes focused unit tests and a true-web run on a recent
  problematic project shape.

## Implementation Notes

- 2026-05-07: Added persisted `behavior_journeys` and `shared_contracts` spec
  fields with parse/serialize/validate coverage. Build context packets now
  include both fields.
- 2026-05-07: Expanded BrowserJourney preflight for Playwright runner config,
  hard-coded non-env loopback ports, overbroad browser-suite selection, and
  unsafe shared/default `agent-browser` sessions.
- 2026-05-07: Added `group.check.feedback` journal events so authoritative
  check failures are explicit evidence for the same resumed provider repair
  thread.
- 2026-05-07: Added critical shared-contract scope detection in build attempts.
  A non-owner edit to a critical contract path became actionable evidence
  instead of only a soft warning.
- 2026-05-07: AppServer audit note from current code: Otto already uses
  AppServer `thread/start`, `thread/resume`, `turn/start`, structured output,
  diff updates, approval handling, and normalized events. A native Otto check
  tool is not yet wired; current bridge uses resumed turns/check feedback.
- 2026-05-07: Added `scripts/replay_browser_check.py` for fast failed-state
  replay of one BrowserJourney from a saved project/worktree.
- 2026-05-08: Compile now synthesizes behavior journeys when webapp specs omit
  them, promotes declared shared paths into a critical shared-product-core
  contract, and adds dependency edges from consuming groups to the contract
  owner when doing so is acyclic.
- 2026-05-08: Spec-review markdown now renders and parses editable Planned
  behavior journeys and Shared contracts sections, so Mission Control exposes
  the same persisted contract consumed by build, merge, and audit.
- 2026-05-08: Merge repair now writes build context packets for repair agents
  and emits `group.check.feedback` for merge verification failures. Audit
  evidence packets and prompts now include planned behavior journeys and shared
  contracts, with instructions to execute planned steps before exploratory
  audit.
- 2026-05-08: AppServer protocol schema was regenerated locally. Current
  `thread/start` and `turn/start` params expose resume, structured output,
  sandbox, approval, model, and effort but not a per-thread custom Otto tool
  registration field; this implementation therefore uses the supported resumed
  turn/evidence-feedback path for Otto-owned checks.
- 2026-05-08: True-web finance pressure validation reached the first parallel
  wave through Mission Control: the Web UI queued the job, compile produced an
  editable spec, visible approval advanced the run, foundation repaired two
  BrowserJourney failures from Otto-owned check feedback, and then
  `transactions-ledger`, `insights-dashboard`, and `budgets` started together
  with `group_concurrent=3`. Evidence ledger:
  `output/true-web-validation-20260508-finance/postfix-root-fixes-concurrency-ui/ledger.json`.
- 2026-05-08: The same run exposed an overconstrained contract bug:
  `browser-quality-contract` claimed `tests/browser/**`, blocking legitimate
  feature-owned BrowserJourney files such as `tests/browser/test_transactions.*`
  and `tests/browser/test_insights.*`. Root fix: shared browser contracts now
  retain shared runner/config paths, remove feature journey/artifact globs, and
  persist those patterns as explicit allowed extension surfaces. Compile prompts
  now state that feature groups may own their own browser journeys.
- 2026-05-08: Live monitoring also exposed that build agents can still reach
  operator memory/dotfiles unless prompted otherwise. Build-agent policy now
  explicitly forbids using user/Codex/agent memory or personal dotfiles as
  product context; product truth must come from the prompt, canonical spec,
  context packet, check feedback, named artifacts, and worktree files.
- 2026-05-08: First-principles integration direction: product contracts should
  define shared invariants and extension surfaces, not act as brittle file
  monopolies. The intended workflow is permissive branch work under contracts,
  structured contract-delta capture when a slice touches shared behavior, an
  intelligent merge/integration stage that retains the best compatible work
  across branches, and one integrated behavior/invariant validation pass before
  repair.
- 2026-05-08: Implemented the first workflow-level contract-delta path. Initial
  build no longer hard-fails compatible non-owner edits to declared critical
  shared-contract paths. It emits `contract.delta`, stores deltas on
  `GroupResult`/`BuildResult`, passes them into merge repair prompts/context
  packets, emits `contract.delta.merge`, and includes them in audit summaries
  so integration validates the final product invariant instead of policing a
  broad path glob.
