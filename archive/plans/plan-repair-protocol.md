# Agent-Native Repair + Verdict Protocol (MERGED — canonical)

_Merge of two independent first-principles designs:
`plan-repair-protocol-claude.md` + `plan-repair-protocol-codex.md`.
Where they agree it is settled; divergences are called out in §D for
the implementation phase._

## Executive stance (both designs, verbatim agreement)

Otto's repair disease is **not** a bad classifier or an undersized cap.
The controller pretends to debug by decomposing an integrated failure
into one classified symptom at a time, then throws the debugging session
away after every symptom. That is the wrong unit of work for an
agent-native system. Target:

- Deterministic **oracles** decide acceptance.
- **Agents** own diagnosis + repair inside a *complete* problem context.
- The **controller** owns only safety, budgets, artifact capture, final
  oracle gating.
- Warning text, failure strings, classifier labels = telemetry, never
  product verdicts.

The clean-deploy oracle is already the right foundation. The brittle part
is how repair and verdict aggregation *consume* its evidence.

## A. Agent-native repair protocol

**Unit of repair** = "make this worktree satisfy the clean-deploy
oracle", not "fix one classified issue". One durable agentic session
owns the whole failing-deploy problem, with the oracle command as its
internal acceptance loop; the controller reruns that same command as the
final gate. Exception: a *provably local + idempotent* deterministic
auto-fix (e.g. clear a known **owned** port) may run first; the moment it
fails or a code change is needed, control passes to the whole-problem
session. Auto-fix must never be the primary repair model.

**Persistence format** (Plan-Gate finding 9): `repair_packet.json` is an
**atomic latest snapshot** (write-temp-then-rename) holding current
state; `repair_packet.events.jsonl` is the append log (one JSON object
per line: sequence number, timestamp, oracle-result digest, event). Both
live under `otto_logs/<session>/repair/<unit>/` (a **gitignored** path —
NOT a worktree path that would dirty git and trip the composite gate;
R2-finding 3), under a **per-repair-unit flock** (extend the
`_merge_target_lock` pattern from commit 89d4bad54, keyed by repair-unit
id, not target branch) so concurrent nested-subtree repairs never
interleave. The pair must be replayable from disk after compaction/
restart (verification: kill+restart test).

**Oracle-result digest canonicalization** (R2-finding 5). The same-HEAD
digest hashes ONLY: sorted typed issue list, step ids + statuses +
return codes, normalized relative paths, normalized ports, normalized
command identities. It EXCLUDES `_written_at`/`started_at`/`duration_s`,
temp roots, absolute log paths, and raw stdout/stderr tails (unless
normalized). Without this, same-HEAD replay can never match and progress
is never grantable.

**Agent-run oracle invocations append under the lock** (R3-finding 2):
the agent runs the oracle inside its own loop, so the exact oracle
command in the packet carries `--repair-packet <path>` /
`OTTO_REPAIR_PACKET_PATH` env; EVERY oracle invocation (agent-run or
controller-run) atomically appends an `oracle_run` event (seq + digest)
to `repair_packet.events.jsonl` under the per-repair-unit flock. A kill
after an agent-run oracle but before the controller rerun therefore
loses no digest/timeline evidence (packet history is the union of all
invocations, not just controller ones).

**Input = the durable repair packet** (snapshot + events above).
Required fields:
1. `repair_unit` — worktree, branch, task/slice/integration id, allowed
   write scope + scope policy, repair phase (preflight | merge | child
   verify | proof | audit).
2. `acceptance_oracle` — exact command, env, timeout, expected artifact
   paths, structured success criteria.
3. `latest_oracle_result` — full clean-deploy/smoke JSON, **ALL** issues
   (not `_first_blocking_issue`), step-level results, typed failure
   domains, raw log paths.
4. `product_contract` — spec, IA/routes, CHARTER, amendments, owned
   paths/shared contracts, relevant P0–P4 routing decisions.
5. `integration_context` — integration packet, child verdicts, child
   diffs/SHAs, conflict packet, prior verifier/audit verdicts, scope
   violations.
6. `attempt_history` — prior attempts in this unit: diffs, commands +
   return codes, prior oracle results, agent ledger/escalations,
   timestamps, session ids.
7. `current_state` — git status, diff summary, changed files, manifests,
   generated artifacts/logs.
The packet carries **references to full artifacts**, not just curated
excerpts. It must NOT make `failure_kind` the scope, pass one `issue` as
"the problem", hide sibling issues, or narrow the write set from
classifier guesswork (only from product scope).

**Prompt** starts from the outcome, not a classifier:
> Repair this worktree so the clean-deploy oracle passes. Preserve the
> product contract, P0–P4 merge invariants, and owned-path/scope rules.
> Diagnose from the complete evidence packet. Run the oracle as your
> acceptance loop. Stop only when the oracle passes or you can produce a
> structured escalation record explaining why it cannot within budget.

Forbidden prompt openings: `Failure kind:`, single `Raw issue JSON` as
primary context, `Likely paths:` as a write whitelist, "PRE-FLIGHT
REPAIR ONLY", "narrowest relevant check", "and stop". Those may exist as
*metadata inside the packet*, never as scope.

**Controller residual job** (only these): build/append the packet; start
or resume one durable session per repair unit; enforce sandbox/branch/
dirty/owned-path safety; preserve logs+diffs+commands+oracle results+
timestamps; rerun the oracle after the agent exits; accept only oracle
pass or structured escalation; enforce wall-clock/turn/cost/idle
ceilings. It must NOT: select one blocking issue, classify text into a
repair kind that determines the prompt, count symptom attempts, stop
because "same kind failed twice", or downgrade a verdict from text.

**Progress = structured state delta with same-HEAD reproducibility**
(Plan-Gate finding 5 — flaky oracles must not count as progress). Record
an **oracle-result digest** per run. Progress is granted ONLY when: a
relevant **owned-path diff exists** AND the improved oracle state is
**reproducible at the same HEAD** (re-running the oracle without further
edits yields the same improved digest, not a rotated/flaky failure) AND
(oracle pass/fail transition OR previously-failing acceptance
journeys/tests now pass OR the typed issue set provably shrank). Issue
"moved to a later step" counts only if reproducible at same HEAD with a
diff. **No-progress** is a bounded-window judgement (after a minimum work
interval: digest structurally unchanged + no relevant diff + repeated
failed hypothesis + same command failing same typed step). Loop-breaker =
the budget schema above. **No symptom-count or per-kind cap** — it
punishes integrated failures for surfacing honestly (the iTracker Bug-A
failure).

**Escalation** is a product artifact (not a chat apology) and is honest
only when: oracle can't run due to typed infra/provider failure; scope
rules prohibit the fix; contract has a semantic conflict needing user
choice; platform constraint makes it impossible; budget exhausted after a
coherent session; or evidence shows the fix belongs outside this unit.
Record must contain: exact oracle command+env, final oracle result, ALL
issues, attempt timeline, commands+codes, files changed + why, final
diff summary, artifact paths, suspected root cause, ruled-out
hypotheses, blocked decision/external dep, keep/revert/review
recommendation.

**Acceptance is COMPOSITE, not clean-deploy-alone** (Plan-Gate finding
7). A unit is "repaired" only when ALL hold: clean-deploy/smoke oracle
green **AND** no dirty/uncommitted state **AND** no conflict markers/
unmerged paths **AND** no owned-path/scope violation **AND**
verdict-consistency + graph-state invariants hold. The scope check is
**baseline-relative, not raw-dirty** (R2-finding 7): the packet carries
a `scope_baseline` (pre-agent snapshot, reusing the
`merge_queue.py:1458` pattern); a violation = non-generated paths
modified *since baseline* (filtering Otto/generated artifacts per
`build.py:548`) that fall outside the repair unit's product scope. The
agent is told the composite definition; the controller checks all
components, not just smoke. Smoke-pass-but-P0-invariant-fail stays
`merge_blocked`.

**Oracle-infra-error is blocking, never silent** (Plan-Gate finding 8).
Today `_run_integration_smoke_preflight` sets `passed=False` on
exception but emits only a warning that `_integration_smoke_blocks`
ignores — a crashed oracle looks non-blocking. The new protocol: an
oracle that cannot run becomes a typed `oracle_infra_error` →
structured escalation, never a silent pass. (This is itself a latent
bug; fix lands in step 1.)

**Repair budget schema** (Plan-Gate finding 4) — replaces symptom caps
with explicit charged budgets: `wall_clock_s`, `cost_usd`,
`agent_turns`, `oracle_invocations`, `idle_s`, `diff_churn`.
Enforcement contract (R2-finding 1): `run_agent_with_timeout` awaits
terminal completion, so there is no mid-run pre-turn hook today.
Therefore **`agent_turns` = outer controller invocations**, each
dispatched with a bounded provider `max_turns`; the controller checks
wall/cost/idle/oracle/churn budgets *between* invocations (before
resuming the session and before each oracle rerun). **Mid-run enforcement is provider-realistic** (R3-finding 1): only
`wall_clock_s` and `idle_s` are reliably enforceable mid-run for ALL
providers. Dollar `cost_usd` is "absolute" mid-run ONLY for providers
that actually stream `total_cost_usd` (`otto/agent.py:3288`); for
token-only providers (OpenAI Agents assembles usage at final output
~`agent.py:1494`; Codex app-server streams token usage ~`agent.py:2733`)
the mid-run ceiling is a **provider-aware token / estimated-cost**
ceiling, and the precise dollar `cost_usd` budget is reconciled
*between* invocations. The plan must not claim a hard mid-run dollar
ceiling where the provider does not emit cost.

**Closeout reserve** (R3-finding 3): since `agent_turns` = outer
invocations, "request a final escalation record" would otherwise cost
one more invocation *after* the budget already fired. The controller
reserves a small **closeout budget** up front; the escalation request
spends only that reserve. If no closeout reserve remains, the
controller writes the structured escalation **from the packet**
(events.jsonl + last snapshot) with NO further agent turn. Symptom/
per-kind counts are NOT budgets. (Verification: a streamed run
exceeding wall/idle mid-run produces a structured escalation; a
cost-exhausted run with zero closeout reserve still produces a
packet-derived escalation without another agent turn.)

**Interop with P0–P4 / smoke**: unchanged and not weakened. P0–P4 emit
typed blocking records *into* the packet; the composite oracle is both
the agent's target and the controller's final arbiter; P0–P4 post-repair
verification still runs.

## B. Verdict-gating protocol

**Verdict is computed only from acceptance evidence**: declared
BrowserJourney/RepoTestCheck/ApiProbe/StateInvariant/contract checks; the
clean-deploy/smoke oracle; merge/scope/conflict invariants; structured
verifier/audit output. **Build/test output warnings are advisory
telemetry on the proof packet — never a gate.** If warning-free output
genuinely matters, it must be a declared oracle (a test that exits
non-zero, `pytest -W error` scoped to project code, a structured
"no warning from package X" assertion). The hard signal is the
assertion / non-zero exit, never the substring "DeprecationWarning".

**Verdict taxonomy** (Codex's, adopted):
- `pass` — all required acceptance oracles pass; clean-deploy passes
  where required; no unresolved conflict/scope/semantic block; proof
  artifacts exist and are consistent.
- `partial` — product work exists but ≥1 declared acceptance
  journey/check is missing/failing/inconclusive; reviewable, not proven
  complete. Advisory warnings may be *listed* here but cannot *cause*
  partial.
- `merge_blocked` — conflict/markers/scope-violation/dirty/protected-path/
  unresolved-user-decision blocks safe landing; or clean-deploy still
  failing after the repair protocol for a merge-required unit; or proof
  infra unavailable in a non-typeable way.
- `fail` — required behavior or clean-deploy deterministically fails and
  repair escalated with no in-scope fix. Distinct from `catastrophic`/
  `unverified` (provider/auth/quota/infra = typed infra escalation;
  missing evidence = unverified) — those are NOT product fail.

**Verification-plan catalog actions** (line refs to reconcile at
implementation — see §D):
- DELETE as gate → advisory: `_check_deprecation_warnings` /
  `_deprecation_lines` + its negation-regex (patch-on-patch proof the
  shape is wrong); the `needle in text` gate.
- KEEP as hard gate: declared acceptance journeys/tests with structured
  pass/fail; clean-deploy/smoke; conflict markers/unmerged paths;
  owned-path/scope violations; `verdict_consistency`; missing-required-
  passed-journeys.
- Decision rule: *a hard verdict gate must be a deterministic state
  check, a declared acceptance oracle, or a structured contradiction.
  Anything scanning unstructured stdout/stderr/transcript/broad source
  text is advisory until promoted to a real oracle.*

## C. Sibling hunt (merged, by blast radius)

**CRITICAL**
1. `v5_preflight_repair.py:117-208,467-560` — whack-a-mole loop +
   `classify_preflight_issue` + per-kind/fingerprint caps → one
   repair-unit session over the full packet, budget-gated, oracle-
   accepted.
2. `v5_runner.py:2454-2520` — throwaway prompt `Failure kind:`/one issue/
   likely-paths/stop → `run_oracle_repair_agent(repair_packet)`,
   persistent session, full context.
3. `v5_verification_plan.py:~654-780` — deprecation output text gates
   verdict (the iTracker Bug-B1) → advisory only.

**HIGH**
4. `v5_runner.py:779-915` `_child_verify_repair_intent` /
   `_ensure_child_merge_ready` — fresh one-retry session, no contract/
   packet, vague oracle → child merge-readiness packet (child spec,
   worktree, verdict, diff, exact oracle, budget, cross-attempt journal).
5. `v5_runner.py:1980-2164` — merge-conflict repair detects by strings,
   routes through the symptom agent → direct conflict-packet protocol
   (all unmerged paths, base/ours/theirs, contract deltas, merge oracle;
   CLAUDE.md no-blind-checkout preserved).
6. `v5_runner.py:1338-1440` — architect/scaffold retry clears verdict on
   preflight strings → scaffold-oracle repair session.
7. `audit.py:~944-984,2418-2445` — raw evidence text classified as infra
   then dropped from verdict → only typed runner/browser domains may
   suppress; regex infra classification advisory.
8. `build.py:3509-3597` — provider/check infra decided by raw text →
   typed terminal/infra codes; text fallback annotates only.
9. `runner.py:1318-1510` — quality findings token-matched onto features →
   require typed feature/group ids or use a group-wide repair packet.

**MEDIUM**
10. `merge_queue.py:831-1020,1608` — string fragments/fingerprints for
    merge progress → typed outcomes + oracle deltas + budget stop.
11. `audit_loop.py:310-567` — layer-2 attempt/pass caps → budgeted
    per-group repair session, structured no-progress, oracle gate.
12. `build.py:2982-3039` — "FIX ONLY THE FAILING FEATURE" over-narrows →
    "repair the failing acceptance cluster, preserve scope".
13. `build.py:3608-3666`, `merge_queue.py:1638-1668` — error-word excerpt
    selection → excerpts are summaries; full artifact paths in packet;
    excerpts never decide verdict.
14. `v5_preflight.py:225-421` — clean-oracle kinds mapped to legacy
    `PreflightIssue` strings → typed clean result canonical; names are
    display only.

**DOCS / GUARDRAIL**
15. `docs/autonomous-loop.md:62-121,263` — endorses small retry counts /
    deterministic playbooks → update to oracle-gated session model.
16. `tests/test_brittleness_guardrail.py` — extend (see §E).

**JUSTIFIED / GOOD MODELS TO EXTEND**
- `repair_gates.py:1-52` — already typed-gate, defaults gaps to repair;
  extend this pattern to preflight + audit infra.
- `v5_clean_verify.py:1065-1180` `verify_from_clean` — the correct
  oracle; make its structured result the canonical packet core instead
  of translating to one preflight issue.
- `spec_compile_flat.py:787-891` / `spec_compile.py:5091-5189` — JSON/
  shape retry at a parser boundary is acceptable; feed structured
  validation errors, keep warnings out of verdicts.

## D. Divergences to resolve at implementation

1. **Route/page/endpoint resolve checks** (`v5_verification_plan.py:
   ~321-417`). Claude initially classed these KEEP (deterministic).
   Codex showed they are `_grep_any` text-search heuristics (line ~327
   `any(needle in text for needle in needles)`). **Resolution: Codex is
   right on the evidence — DEMOTE to advisory unless the check is backed
   by a real router manifest / browser journey that proves the route.
   Verify the exact implementation per-check during implementation; a
   route "resolves" only if the app router or a journey proves it.**
2. **`_check_no_stub_text`** (`v5_verification_plan.py:~533-626`). Claude
   said delete-as-gate; Codex's design said conditionally keep. **Plan-
   Gate finding 10 overrides both: "user-facing stub text" is NOT
   reliably knowable from the current broad text scanner, so a
   conditional gate would itself be brittle. Final resolution: stub-text
   is ADVISORY telemetry by default; it may only gate if expressed as a
   declared product-text oracle (an explicit contract assertion the
   agent/oracle checks), never via the broad scanner.**

## E. Standing guardrail (extend `test_brittleness_guardrail.py`)

Scope note (Plan-Gate finding 10): the existing guardrail scans only
Python AST under `otto/`. These assertions are enforceable there;
behaviors that cannot be proven by AST (e.g. "stub text is user-facing")
are NOT made guardrail gates — they become advisory or declared-oracle
checks instead. Design-time AST assertions added to the guardrail:
- No `_run_*repair*agent` prompt may use `Failure kind` or a single
  `Raw issue JSON` as primary context unless it also passes a full
  repair packet (all issues + oracle command + contract + attempt
  history).
- No repair loop may cap by symptom count or per-kind attempts; only
  wall-clock/cost/idle/safety/oracle-pass/structured-escalation.
- No verdict gate may scan unstructured stdout/stderr/test_output/
  transcript/broad source text to set or downgrade a verdict.
- Any diagnostic text classifier must write advisory telemetry unless
  backed by a typed oracle/check result.
- Every repair packet must be reproducible from disk after compaction.

## F. One-sentence protocol

> Otto dispatches repair as **one durable agent session that owns a
> failing acceptance unit, handed the goal + the exact oracle + the full
> product/integration context + the prior-attempt journal**, and sets
> verdicts **only from deterministic state, declared oracles, or
> structured contradiction — never from reading free text.**

## G. Recommended implementation sequencing

**Step 0 — foundations (must land before any repair swap; Plan-Gate
findings 1, 2, 3).** Nothing downstream is shippable without these:
- **`CleanOracleResult`** — a serializable result type from
  `verify_from_clean` that does NOT short-circuit on first failure:
  carries the full normalized issue set, per-step command results
  (install/build/start/probe/tests/journeys), artifact path refs,
  exact command+env, and a stable result digest. This becomes the
  packet core (replaces the one-`failure_kind` `CleanVerifyResult` →
  one-legacy-`PreflightIssue` translation).
- **Deterministic oracle command** — expose the composite oracle as a
  stable in-worktree CLI (e.g. `otto internal clean-verify --json
  --scope <unit>`) so the repair agent runs the *exact* same oracle the
  controller does. Without this, agents invent near-equivalent checks =
  the "narrowest check" bug returns.
- **Durable session primitive** — `run_oracle_repair_agent` calls the
  **lower agent layer directly with `options.resume`** (the `resume`
  field exists on `AgentOptions` and providers wire it, but `run_lead`
  has no resume input and `_run_lead_with_fallback` starts fresh — so
  the primitive must NOT route through `_run_lead_with_fallback`;
  R2-finding 2). Persist `agent_session_id` in the packet; a controller
  rerun resumes the SAME session. Provider fallback is disabled once
  `agent_session_id` exists (a provider switch = a new repair-unit
  attempt with a new packet, not a resumed session). Ship with a
  kill+restart resume test.

**Step 0 implementation contract (R2 precision — must be honored):**
- *Oracle step-DAG (R2-finding 4):* `CleanOracleResult` does NOT naively
  run-all (that cascades meaningless build/start failures after an
  install failure). It runs the step DAG: independent steps continue;
  dependent steps are recorded `skipped_due_to:<upstream-step>`. "Full
  issue set" = all *independently knowable* issues, never synthetic
  cascade failures (cascades would also poison the progress digest).
- *CLI/env resolution (R2-finding 6):* the packet stores a **resolved
  executable + env**, e.g. the worktree's own
  `.venv/bin/python -m otto.cli clean-verify --json
  --verify-scope <scaffold|subtree|full>`, NOT a bare `otto ...` (the
  CLI venv guard at `otto/cli.py:38` blocks linked worktrees when Otto
  is loaded from a different repo venv). `--verify-scope` (the existing
  scaffold|subtree|full axis) is kept SEPARATE from repair-unit id/phase
  — do not collapse them into one `--scope`.
- *Compat-adapter commit preservation (R2-finding 3):* the Step 1
  adapter must preserve the existing commit/result/event contract
  exactly — today `_run_preflight_repair_agent` commits successful edits
  (`commit_worktree` for merge_conflict, `commit_integration_worktree`
  otherwise, `v5_runner.py:2521`). Either the adapter keeps doing that,
  or the new primitive takes a controller-owned commit hook keyed by
  repair phase. Without this, day-1: adapter delegates → oracle passes →
  tree still dirty → composite gate blocks a genuinely-repaired unit.

**Step 1 — repair-unit session, shippable via compat adapter** (Plan-
Gate finding 6). Introduce `run_oracle_repair_agent(repair_packet)` +
the composite-oracle gate + budget schema + oracle-infra-error fix.
`AgentRepairRequest`/`_run_preflight_repair_agent`/`repair_until_clean`
are **not deleted**; they become a thin compatibility adapter that
constructs a packet and delegates to the new primitive — so
merge-conflict repair (v5_runner.py:2099) and every other caller keeps
working. The tree is shippable after step 1; old callers migrate in
step 4 and the adapter is deleted only when the last caller is gone.

**Step 2 — verdict de-brittling** (B + DELETE list): dep/needle/route-
grep checks → advisory; lock the taxonomy. Explicit test migration
(Plan-Gate finding 11): `validate_lead_verdict` (v5_verification_plan.py:
~133) stops including `deprecation_warnings` in final verdict
computation; invert the exact tests that currently assert deprecation/
route downgrades in `tests/test_v5_verification_plan.py` (~290-441) to
assert "warnings/route-grep do NOT downgrade pass"; keep coverage that
they still appear as advisory telemetry on the proof packet.

**Step 3 — guardrail extension** (§E) — lands with steps 1–2 so
regressions are caught immediately.

**Step 4 — HIGH siblings #4–6** (child-verify, merge-conflict, scaffold)
migrated onto the packet protocol; delete the compat adapter when the
last caller is migrated.

**Step 5 — MEDIUM #10–14 + docs #15.**

Each step: Codex implements correctness-critical, Claude reviews
high-risk diffs, regression tests red→green, full smoke tier green
(currently 307), no regression in test_v5_p*_hardening /
test_brittleness_guardrail / test_v5_integration_worktree.

### Verification criteria (system-level; Plan-Gate additions)

Beyond unit red→green, these MUST pass before the protocol is accepted:
1. **Packet replay**: kill+restart after an oracle run; snapshot +
   events.jsonl remain valid/complete and resume the SAME agent session.
2. **Budget**: a fake oracle that rotates failure domains + a fake agent
   that churns diffs → repair exits by budget with a structured
   escalation record, no hidden retries, bounded cost.
3. **Composite gate**: agent makes smoke pass but leaves conflict
   markers / dirty state / scope violation → result stays
   `merge_blocked` (not pass).
4. **Flaky-oracle**: same HEAD alternates `ports_not_listening` /
   `start_failed` → NOT counted as progress (no same-HEAD reproducible
   improved digest).
5. **Concurrency**: two nested-subtree repairs with overlapping
   ports/integration branches → isolated packets, no interleaved
   writes, per-unit flock holds.
6. **iTracker field rerun**: Bug-A no longer hits a kind/fingerprint
   cap; Bug-B warnings are advisory only; final packet shows ONE
   durable repair unit, bounded cost, composite-oracle pass or an
   honest structured escalation (not a manufactured merge_blocked).

Per CLAUDE.md this plan goes through `/codex-gate` Plan Gate before
implementation; the Implementation Gate runs before merge.

## Plan Review

### Round 1 — Codex (REVISE → all 11 findings accepted)
- [ISSUE 1] verify_from_clean not packet-shaped — fixed: added Step 0 `CleanOracleResult` (no short-circuit, full issue set, per-step, artifact refs, digest)
- [ISSUE 2] durable session not implemented by dispatcher — fixed: Step 0 `run_oracle_repair_agent` on explicit session resume + persisted agent_session_id + resume test
- [ISSUE 3] agent can't run the oracle reliably — fixed: Step 0 deterministic in-worktree CLI oracle command in packet
- [ISSUE 4] caps removed without concrete budgets — fixed: §A explicit repair_budget schema (wall/cost/turns/oracle-invocations/idle/diff-churn), checked per turn + per oracle rerun
- [ISSUE 5] flaky oracle miscounts as progress — fixed: §A progress requires same-HEAD reproducible improved digest + owned diff
- [ISSUE 6] §G step 1 not shippable — fixed: step 1 keeps old primitive as compat adapter; callers migrate step 4; adapter deleted last
- [ISSUE 7] "P0–P4 unchanged" under-specified — fixed: §A acceptance redefined COMPOSITE (smoke + dirty + conflict + scope + verdict-consistency + graph)
- [ISSUE 8] smoke exception silently bypasses repair — fixed: §A oracle-infra-error → typed blocking/structured escalation (latent bug, fix in step 1)
- [ISSUE 9] packet persistence/concurrency under-specified — fixed: §A atomic snapshot json + events.jsonl + per-repair-unit flock + seq + digest
- [ISSUE 10] §D.2/§E overpromise AST enforcement — fixed: stub-text advisory unless declared product-text oracle; §E scoped to AST-enforceable only
- [ISSUE 11] regression expectations vague — fixed: §G step 2 explicit test-migration list (validate_lead_verdict + test_v5_verification_plan inversions + advisory coverage)
- [ADDED] 6 system-level verification criteria appended to §G (packet replay, budget, composite gate, flaky-oracle, concurrency, iTracker field rerun)

### Round 2 — Codex (REVISE → all 7 findings accepted)
- [ISSUE 1] budget not controller-visible per turn — fixed: agent_turns = outer controller invocations w/ bounded provider max_turns; wall/cost/idle/oracle/churn checked between invocations; streamed cost/idle enforces absolute ceiling within an invocation; + mid-run escalation test
- [ISSUE 2] resume only works if primitive bypasses Lead/fallback — fixed: run_oracle_repair_agent calls lower agent layer directly with options.resume, NOT via _run_lead_with_fallback; fallback disabled once agent_session_id exists
- [ISSUE 3] compat adapter must preserve commit boundaries — fixed: Step 1 adapter preserves exact commit/result/event contract (commit_worktree/commit_integration_worktree) or primitive takes a phase-keyed commit hook; packets under gitignored otto_logs/
- [ISSUE 4] no-short-circuit needs dependency semantics — fixed: oracle step-DAG; independent steps continue, dependents `skipped_due_to:<step>`; full issue set = independently knowable, no synthetic cascades
- [ISSUE 5] stable digest underspecified — fixed: explicit digest canonicalization (sorted typed issues + step ids/statuses/codes + normalized rel paths/ports/command identities; excludes timestamps/durations/temp roots/abs paths/raw tails)
- [ISSUE 6] CLI oracle not same in linked worktrees — fixed: packet stores resolved worktree .venv/bin/python -m otto.cli ...; --verify-scope (scaffold|subtree|full) kept separate from repair-unit id/phase
- [ISSUE 7] composite scope needs baseline not raw-dirty — fixed: packet carries scope_baseline (merge_queue.py:1458 pattern); violation = non-generated paths modified since baseline (build.py:548 filter) outside product scope

### Round 3 — Codex (REVISE → all 3 findings accepted)
- [ISSUE 1] cost ceiling overclaimed mid-run — fixed: only wall/idle reliably enforceable mid-run for all providers; dollar cost "absolute" mid-run only where total_cost_usd is streamed; token-only providers use provider-aware token/estimated ceiling, precise $ reconciled between invocations
- [ISSUE 2] agent-run oracle invocations not packet-locked — fixed: oracle command carries --repair-packet/OTTO_REPAIR_PACKET_PATH; every invocation (agent or controller) atomically appends oracle_run event under the per-unit flock; kill loses no evidence
- [ISSUE 3] final escalation spends after budget exhaustion — fixed: controller reserves a closeout budget up front; escalation request spends only that; zero reserve → escalation written from packet (events.jsonl + snapshot) with no further agent turn

### Round 4 — Codex
- APPROVED. No new blocking issues. Plan is implementable at implementation-contract granularity (provider-realistic budgets, packet-locked oracle events, closeout reserve, direct lower-layer resume, commit-preserving compat adapter, digest canonicalization, baseline-relative composite scope).
