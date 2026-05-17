# Agent-native repair and verdict protocol

## Executive stance

Otto's current repair disease is not a bad classifier or an undersized cap. It is the controller pretending to debug by decomposing an integrated failure into one classified symptom at a time, then throwing away the debugging session after every symptom. That is exactly the wrong unit of work for an agent-native intent-to-product system.

The design target should be:

- Deterministic oracles decide acceptance.
- Agents own diagnosis and repair inside a complete problem context.
- The controller owns safety, budgets, artifact capture, and final oracle gating.
- Warning text, failure strings, and classifier labels are telemetry, never product verdicts.

The clean-deploy oracle is already the right foundation. The brittle part is how repair and verdict aggregation consume its evidence.

## A. Agent-native repair protocol

### The right unit of repair

The unit of preflight repair is "make this worktree satisfy the clean-deploy oracle", not "fix one classified issue". One agentic repair session should own the whole failing deploy problem, with the clean-deploy command as its internal acceptance loop and the controller rerunning that same command as the final gate.

Current behavior in `otto/v5_preflight_repair.py:117-208` is structurally whack-a-mole:

- select `_first_blocking_issue`
- classify it with `classify_preflight_issue`
- dispatch one fresh agent for one issue
- rerun the whole oracle
- repeat until caps or a different symptom appears

That forbids the real engineering loop. A real engineer starts from "the app does not deploy", reads all failures, forms an integrated hypothesis, fixes related causes together, runs the same deploy oracle again, and iterates with memory. Otto should give the repair agent that same shape.

The only exception should be cheap deterministic auto-fixes that are provably local and idempotent, such as clearing a known owned port before any code repair. Even then, once an auto-fix fails or a code change is needed, control should pass to the whole-problem repair session. Auto-fix helpers must not become the primary repair model.

### Prompt

The repair prompt should begin from the outcome, not from a classifier:

> Repair this worktree so the clean-deploy oracle passes. Preserve the product contract, P0-P4 merge invariants, and owned-path/scope rules. Diagnose from the complete evidence packet. Run the oracle as your acceptance loop. Stop only when the oracle passes or when you can produce a structured escalation record explaining why it cannot pass within this budget.

The prompt must not begin with:

- `Failure kind: <kind>`
- `Failure message: <msg>`
- `Likely paths: ...`
- "PRE-FLIGHT REPAIR ONLY" scoped to one symptom

Those fields can exist as display metadata inside the full packet, but they must not dictate scope. The agent should be asked to diagnose; the harness should not pre-diagnose by string kind.

The prompt should explicitly require:

- inspect the full oracle result before editing
- preserve user and sibling-agent changes
- run the exact oracle command, or record why it could not be run
- keep a repair ledger with hypotheses, edits, commands, and outcomes
- write a final structured repair verdict with pass/escalate and evidence links

### Context and input packet

Repair input should be a single durable `repair_packet.json` plus referenced artifacts. It should be created before the agent starts and appended to after each controller-side oracle run.

Required fields:

1. `repair_unit`
   - worktree path
   - branch
   - task id / slice id / integration id
   - allowed write scope and scope-violation policy
   - whether this is preflight, merge integration, child verify, proof repair, or final audit repair

2. `acceptance_oracle`
   - exact command to run
   - environment variables
   - timeout budget
   - expected artifact paths
   - structured success criteria

3. `latest_oracle_result`
   - full clean-deploy/smoke result JSON
   - all surfaced issues, not only the first
   - step-level results: install, build, start, probe, tests, browser journeys
   - typed failure domains when available
   - raw logs by path, not copied as small excerpts only

4. `product_contract`
   - spec/product contract
   - IA/routes
   - CHARTER/architecture notes
   - amendments
   - owned paths and shared contracts
   - relevant P0-P4 routing decisions

5. `integration_context`
   - integration packet
   - child verdicts
   - child diffs or commit SHAs
   - merge/conflict packet if relevant
   - previous verifier/audit verdicts
   - scope violations or protected files

6. `attempt_history`
   - prior repair attempts in the same unit
   - prior diffs
   - commands run and return codes
   - prior oracle results
   - agent notes/escalations
   - timestamps and session ids

7. `current_state`
   - git status
   - current diff summary
   - key changed files
   - dependency manifests
   - generated artifacts and logs

The agent should receive full artifact references, not just a curated "interesting" excerpt. Summaries are useful, but references are the source of truth.

What the packet must not do:

- make `failure_kind` the repair scope
- pass one `issue` as if it is the problem
- hide sibling issues because `_first_blocking_issue` won
- provide likely paths as a narrowed write set unless that write set comes from product scope, not classifier guesswork
- treat "no progress" fingerprints as facts

### Controller residual job

The controller should be smaller and stricter:

1. Build the repair packet.
2. Start or resume one durable repair session for the repair unit.
3. Enforce sandbox, branch, dirty-state, and owned-path safety.
4. Preserve logs, diffs, commands, oracle results, and timestamps.
5. Run the acceptance oracle after the agent exits.
6. Accept only oracle pass or structured escalation.
7. Enforce wall-clock, turn, cost, and idle ceilings.

The controller should not:

- select one blocking issue
- classify text into a repair kind that determines the prompt
- count symptom attempts
- stop because "the same kind failed twice"
- downgrade product verdicts from warning text

### Progress without brittle heuristics

Progress should be judged from structured state, not log strings:

- oracle pass/fail transition
- failing oracle step ids changed
- required acceptance journeys/tests passed that previously failed
- typed issue set shrank or moved to a later oracle step
- code diff exists in relevant owned paths
- the agent produced a coherent repair ledger with a new hypothesis and command outcome
- the controller can reproduce the improved oracle result

No-progress is not "same normalized message". It is a bounded-window judgement:

- after a minimum useful work interval, the oracle result is structurally unchanged
- no relevant diff or only churn was produced
- the agent's ledger repeats the same failed hypothesis
- the same command is failing in the same typed oracle step
- no new evidence is being collected

At that point the controller should ask the same session for a final escalation record. If the agent cannot produce one, the controller writes a machine escalation with the evidence it has.

The loop breaker should be budget, not symptom count:

- wall-clock ceiling
- token/cost ceiling
- max idle time without command, diff, or ledger update
- hard safety violation
- final oracle pass
- structured escalation

An absolute wall/cost ceiling is justified. Infinite repair loops are real. A symptom-count cap is not justified because it punishes integrated failures for surfacing honestly.

### Escalation

Escalation is honest when:

- the oracle cannot run due to typed infrastructure/provider failure
- scope rules prohibit the necessary fix
- the product contract has a semantic conflict that needs user choice
- dependency or platform constraints make the requested behavior impossible
- the wall/cost/turn budget is exhausted after a coherent repair session
- the agent has evidence that the correct fix belongs outside this repair unit

The escalation record must contain:

- exact oracle command and environment
- final structured oracle result
- all current issues, not just first issue
- timeline of attempts with timestamps
- commands run and return codes
- files changed and why
- final diff summary
- artifact/log paths
- suspected root cause
- ruled-out hypotheses
- blocked decision or external dependency
- whether any changes should be kept, reverted, or reviewed

Escalation is a product artifact, not a chat apology.

### Interop with P0-P4 and smoke

P0-P4 should remain deterministic merge and verdict gates. This protocol does not weaken them.

The change is where repair authority sits:

- P0-P4 may emit typed blocking records into the repair packet.
- The smoke/clean-deploy oracle remains the acceptance gate.
- The repair agent owns diagnosis across the full packet.
- The controller reruns the oracle before declaring success.
- P0-P4 post-repair verification still runs exactly as before.

The smoke oracle is not regressed. It becomes more important: it is the controller's final arbiter and the repair agent's target.

## B. Verdict-gating protocol

### Source of truth

Verdict must be computed from acceptance evidence:

- declared BrowserJourney, RepoTestCheck, ApiProbe, StateInvariant, and contract checks
- the clean-deploy/smoke oracle
- merge/scope/conflict invariants
- structured verifier/audit output

Build/test output warnings are advisory telemetry on the proof packet. They are never a verdict gate by themselves.

If warning-free output matters, it must be declared as an oracle:

- a test that fails on project-owned warnings
- a linter configured to return non-zero
- `pytest -W error` scoped to project code
- a structured warning capture that asserts "no warning from package X"

The hard signal is the assertion or non-zero exit code, not the text "DeprecationWarning" appearing in output.

### Verdict meanings

`pass`:

- all required acceptance oracles for the unit pass
- clean-deploy/smoke passes when required for merge or final certification
- no unresolved merge conflict, scope violation, or semantic conflict remains
- proof artifacts exist and are internally consistent

`partial`:

- code/product work exists, but at least one declared acceptance journey/check is missing, failing, inconclusive, or downgraded by a real proof gap
- the product may be reviewable, but it is not proven complete
- advisory warnings may be listed here, but cannot by themselves cause partial

`merge_blocked`:

- merge conflict, conflict markers, scope violation, dirty state, protected path edit, or unresolved user decision blocks safe landing
- clean-deploy/smoke remains failing after the repair protocol for a merge-required unit
- proof infrastructure is unavailable in a way that prevents safe merge and cannot be typed as external provider noise

`fail`:

- the required product behavior or clean-deploy oracle deterministically fails and repair has escalated without a viable fix inside scope
- this is a product/system failure, not provider auth, quota, browser-launch, or CI-infra failure

Existing `catastrophic` and `unverified` states should remain distinct from product `fail`:

- provider/auth/quota/runner infrastructure: typed infra escalation, not product fail
- missing evidence: unverified/proof gap, not pass and not product fail

### Existing verification-plan checks

Delete as verdict gates:

- `otto/v5_verification_plan.py:654-780` `_check_deprecation_warnings`
- `CHECK_KINDS` entry `deprecation_warnings`
- tests in `tests/test_v5_verification_plan.py:290-441` that encode output-warning downgrades

These may be replaced with an advisory telemetry section that records warnings by source if a structured collector exists. The advisory section must not alter `pass`.

Demote unless replaced by structured evidence:

- `otto/v5_verification_plan.py:321-329` `_grep_any` route/page/endpoint resolution
- `otto/v5_verification_plan.py:348-417` route/page/endpoint checks based on text search
- `otto/v5_verification_plan.py:457-481` action_has_test text indexing
- `otto/v5_verification_plan.py:484-506` mutating_action_has_feedback string scan

These are useful hints for a reviewer or repair agent, but they are not product proof. A route exists if the app route manifest, router, or browser journey proves it. A mutating action has feedback if a journey or component-level state assertion proves the user sees it.

Keep as hard gates:

- declared acceptance journeys/tests/checks with structured pass/fail
- clean-deploy/smoke oracle
- merge conflict markers and unmerged paths
- owned-path/scope violations
- structured verdict consistency in `otto/v5_verification_plan.py:629-651`
- missing required passed journeys in `otto/v5_verification_plan.py:812-825`

Conditionally keep:

- stub/placeholder checks in `otto/v5_verification_plan.py:533-626` only when scoped to user-facing product text or explicit contract promises. Otherwise report as advisory quality telemetry.

Decision rule:

Hard verdict gates must be either deterministic state checks, declared acceptance oracles, or structured contradictions. Any check that scans unstructured stdout/stderr, provider transcript text, or source text for a broad phrase is advisory until it is turned into a real oracle.

## C. Sibling hunt

### Critical blast radius

1. `otto/v5_preflight_repair.py:117-208`, `467-517`, `527-560`
   - Problem: one blocking issue, string classification, per-kind caps, fingerprint progress.
   - Redesign: replace with one repair-unit session over the full clean-deploy packet, budgeted by wall/cost and accepted only by oracle pass.

2. `otto/v5_runner.py:2454-2520`
   - Problem: fresh throwaway agent prompt starts with `Failure kind`, one issue JSON, likely paths, and narrow stop condition.
   - Redesign: dispatch `run_oracle_repair_agent(repair_packet)` with persistent session id and full contract/integration/oracle context.

3. `otto/v5_verification_plan.py:654-780`
   - Problem: output deprecation text changes product verdict.
   - Redesign: delete as gate; warnings are advisory unless a declared warning oracle fails by exit code or structured assertion.

### High blast radius

4. `otto/v5_runner.py:803-915`
   - Problem: child verify repair is a fresh one-retry session around previous result payload.
   - Redesign: child merge-readiness repair session with full child spec, worktree, child verdict, diff, oracle, and budget.

5. `otto/v5_runner.py:1980-2164`
   - Problem: merge conflict repair detects conflicts by strings and routes through the preflight symptom agent.
   - Redesign: direct merge-repair protocol over a conflict packet with all unmerged paths, base/theirs/ours refs, contract deltas, and merge oracle.

6. `otto/v5_runner.py:1338-1440`
   - Problem: architect scaffold retry clears verdict based on preflight string messages and reruns the architect.
   - Redesign: scaffold repair session owns "make this generated scaffold satisfy the scaffold oracle"; architect only re-enters if the product contract is invalid.

7. `otto/audit.py:944-984`, `2418-2445`
   - Problem: raw evidence text can be classified as check infrastructure and then ignored in verdict composition.
   - Redesign: only typed runner/browser failure domains may suppress product verdict impact; regex-derived infra classification is advisory.

8. `otto/build.py:3509-3597`
   - Problem: provider/check infrastructure decisions use raw text patterns.
   - Redesign: providers and check runners emit typed terminal/infra codes; text pattern fallback can annotate, not decide.

9. `otto/runner.py:1318-1382`, `1442-1510`
   - Problem: product-wide quality findings are token-matched onto features.
   - Redesign: audit output must include affected feature ids/group ids or a repair_scope; absent that, use a product/group-wide repair packet instead of guessing.

### Medium blast radius

10. `otto/merge_queue.py:831-835`, `964-1020`, `1608-1615`
    - Problem: merge failure and progress use string fragments/fingerprints, although the surrounding context packet is much better than preflight.
    - Redesign: typed merge outcomes plus oracle issue deltas; use wall/cost budgets, not retry/fingerprint logic, as the stop condition.

11. `otto/audit_loop.py:310-567`
    - Problem: layer-2 repair still has attempt/pass caps.
    - Redesign: keep group coalescing, but use a budgeted repair session per group/product gap with structured no-progress and final oracle gate.

12. `otto/build.py:2982-3039`
    - Problem: single-feature repair prompt says "FIX ONLY THE FAILING FEATURE", which can over-narrow root-cause work.
    - Redesign: "repair the failing acceptance cluster while preserving scope"; include all related failures and allow shared-contract edits when the contract permits.

13. `otto/build.py:3608-3666` and `otto/merge_queue.py:1638-1668`
    - Problem: "interesting" log excerpts are selected by error-word scans.
    - Redesign: excerpts are fine as summaries, but the full artifact paths must be in the packet and excerpts must not decide verdict.

14. `otto/v5_preflight.py:225-421`
    - Problem: clean oracle failure kinds are mapped into old `PreflightIssue` strings.
    - Redesign: keep the typed clean oracle result canonical; issue names are display/event compatibility only, not repair scope.

15. `docs/autonomous-loop.md:62-96`, `112-121`, `263`
    - Problem: old governance endorses small retry counts and deterministic repair playbooks.
    - Redesign: update docs to the oracle-gated repair-session model once the protocol is adopted.

16. `tests/test_brittleness_guardrail.py:232-242`, `363-371`, `408-431`
    - Problem: existing guardrail catches substring classifiers but not single-issue repair prompts.
    - Redesign: add a guardrail that any repair-agent dispatch must include full repair packet, all issues, oracle command, prior attempts, and product contract; no `Failure kind` primary prompt without packet.

### Lower blast radius or justified

17. `otto/spec_compile_flat.py:787-891`
    - Status: retrying invalid JSON/shape is acceptable at a parser boundary.
    - Improvement: feed back structured validation errors, not broad prose warnings, and keep warnings advisory once shape is valid.

18. `otto/spec_compile.py:5091-5189`
    - Status: transient provider retry and schema validation are mostly justified.
    - Improvement: keep validation warnings out of product verdicts.

19. `otto/repair_gates.py:1-52`
    - Status: good model. It defaults product gaps into repair and only marks typed provider/auth/quota failures non-repairable.
    - Improvement: extend this typed-gate pattern to preflight and audit infrastructure classification.

20. `otto/v5_clean_verify.py:1065-1180`
    - Status: correct foundation. `verify_from_clean` is the oracle Otto should repair toward.
    - Improvement: make its structured result the canonical repair packet core instead of translating it into one preflight issue.

## Standing guardrail

Standing invariant:

> Any autonomous repair dispatch must be scoped by a repair unit and an acceptance oracle, not by one classified symptom. Its input packet must include the full latest oracle result, all current issues, the exact oracle command, the product contract/spec/IA, integration context, worktree state, current diff, and prior attempt history. The controller may enforce safety and budgets, and may accept only oracle pass or structured escalation. It may not decide product verdict from stdout/stderr warning substrings, symptom fingerprints, or one-line failure classifications.

Design-time assertions:

- no `_run_*repair*agent` prompt may use `Failure kind` or one `Raw issue JSON` as the primary context unless it also passes a full repair packet with all issues and oracle command
- no repair loop may cap by symptom count or per-kind attempts; use wall-clock, cost, idle, safety, oracle pass, or structured escalation
- no verdict gate may scan unstructured `stdout`, `stderr`, `test_output`, provider transcript text, or broad source text for warnings to downgrade `pass`
- any diagnostic text classifier must write advisory telemetry unless backed by a typed oracle/check result
- every repair packet must be reproducible from disk after context compression

If adopted, this makes the class structurally hard to reintroduce: agents receive the whole problem, oracles decide acceptance, and the harness stops pretending that string labels are engineering judgement.
