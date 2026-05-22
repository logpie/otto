# Audit: orchestrator pre/post-flight that duplicates downstream agent work

**Lens (derived from the merge-orchestration fix we just shipped):** the
orchestrator runs a probe / check / validator that the next agent is
about to do anyway. On failure it dispatches a separate repair agent.
The state is often stale by dispatch time. The repair agent often
exits as a no-op because by the time it runs, either (a) the
transient cleared, or (b) the next agent in line would have handled
it naturally.

The merge case (just fixed): orchestrator tried `git merge` per child
on finish → conflict → dispatched repair agent → repair agent's
oracle was already passing because integration would resolve it → no-op
exit → integration runs → does the real merge.

Below: other places in Otto where this pattern is hiding.

---

## Finding 1 — `_run_integration_smoke_preflight_with_repair` at `pre_agent` phase

**Location:** `otto/v5/dispatch.py:1666` (called before every integration
Lead dispatch).

**What happens today:**

1. Children are done; integration Lead is about to start
2. Orchestrator runs a clean-deploy probe against the merged tree
3. If the probe fails (build error, port bind, startup failure), the
   orchestrator dispatches a repair agent (`_run_oracle_repair_agent`)
4. Repair agent diagnoses + fixes
5. THEN integration Lead runs → first thing it does is start the
   stack (per `lead-integration.md` Step 1) → would have hit the
   same failure → diagnosed + fixed it itself (it has the same tools:
   Bash, chrome-devtools, Edit)

**Why redundant:** integration Lead's post-Phase-1 prompt is the unified
behavioral verifier — it owns starting the stack, probing endpoints,
fixing startup ordering bugs, etc. The pre-flight probe and its repair
agent dispatch are doing integration's work pre-emptively, creating
the same no-op repair shape we just fixed for merges.

**Agentic fix:** drop the `pre_agent` smoke preflight. Integration Lead
runs `./start.sh` as its first action; if it fails, integration Lead
fixes it inline (same code-edit-and-restart loop it already does for
journey diagnostics). Same with the per-phase preflights at child
sessions if any.

**Risk:** medium-low. The preflight catches startup bugs early so
integration knows immediately what to fix. Removing it means
integration discovers the bug on its own start.sh, which is fine —
the diagnose-and-fix loop is the same either way.

---

## Finding 2 — Foundation_gate clean-boot probe

**Location:** `otto/v5/dispatch.py:784` (after architect/foundation
child finishes).

**What happens today:**

1. Foundation Lead declares pass (and per the new "verify as platform"
   prompt, has self-verified by hitting its own endpoints)
2. Orchestrator independently runs a clean-deploy probe in a separate
   foundation_gate session dir
3. If probe fails → dispatches `_run_oracle_repair_agent`
4. Repair agent's no-op pattern we documented earlier surfaced in
   `linkboard-validate-pass-105752`: oracle passed on retry without
   any agent action; agent burned $0.36 for nothing

**Why redundant:** lead-architect.md "Verify the foundation AS A
PLATFORM" prompt makes the foundation Lead RESPONSIBLE for proving
its own platform boots and serves its declared contracts. The
orchestrator's independent re-probe duplicates that responsibility.

**Agentic fix:** trust the foundation Lead's verdict. If the foundation
Lead says pass after platform-verify, that IS the platform check. If
it lied (declared pass without driving its contracts), that's a
foundation-Lead prompt issue, not an orchestrator backstop concern.

**Risk:** low. The platform-verify prompt is the trust contract.
Orchestrator backstop creates the no-op-repair pattern at $0.36 per
run.

---

## Finding 3 — Multiple `_preflight_repair_escalated` checkpoints

**Locations:** `otto/v5_runner.py:2187, 2206, 2450, 2464` — at:
- Pipeline start branch checkout
- Startup declared-port cleanup
- Pre-integration checkout
- Pre-integration preflight

**What happens today:** each one runs a preflight, on escalation
dispatches a repair agent, on escalation-of-repair sets root to
merge_blocked.

**Why redundant (most of them):** several of these (port cleanup,
checkout) are pure orchestrator operations that don't need an
agent — they're mechanical (kill a process, switch a branch). The
repair-agent dispatch is using an LLM to do filesystem/git
operations. Some are environmental issues (no usable Python, no
git) that no agent can fix anyway.

**Agentic fix:** classify these:
- Mechanical issues (port bound, git lock, dirty worktree) →
  retry with backoff at the orchestrator level, no agent needed
- Environmental issues (missing toolchain) → fail loud, no agent
  needed (the run can't proceed regardless)
- Real product issues → defer to the next agent in line

Today's "dispatch a generic repair agent" treats all three as the
same shape and burns budget on LLM work for mechanical retries.

**Risk:** medium. Tightening this requires classification logic at
the orchestrator level. Same general shape as audit-orchestrator-
brittleness.md A2 ("retry escalation").

---

## Finding 4 — `validate_lead_verdict` post-agent check matrix

**Location:** `otto/lead.py:352` (every Lead session).

**What happens today:** after the agent declares its verdict and
writes `verdict.json`, the orchestrator runs a separate check matrix
(`validate_lead_verdict` in `v5_verification_plan.py`) that
re-derives findings from the spec and code: page_has_ia_route,
entity_has_empty_state, action_has_test, page_resolves, etc.

**Why partially redundant:** the CHECK_KINDS demote fix
(`bd89feb96`) reduced this to two checks that actually gate
(local_scope_check, verdict_consistency); the rest are advisory. So
the gate-impact is small. But the orchestrator-side validator is
still running these checks AFTER the agent declared its verdict —
the agent could be making these decisions during its own work.

**Why this is different from Findings 1-3:** the agent's verdict is
an explicit promise; the orchestrator's check is verifying the
promise. There's value in a separate verifier (the agent might be
wrong about its own work). It's not pure duplication.

**Agentic fix:** keep the CHECK_KINDS gates (they're cheap and catch
agent dishonesty). Demote the advisory checks further — currently
they emit and are advisory; the next step is to STOP RUNNING THEM
ENTIRELY post-agent and just trust the agent's intent_coverage
declarations.

**Risk:** medium. Removing the advisory checks loses some
diagnostic signal. But that signal is mostly noise (the linkboard
validation runs showed 7-11 advisory warnings each, none drove
action).

---

## Finding 5 — `journey_verdict_sink` credibility check

**Location:** `otto/journey_verdict_sink.py` —
`agent_self_verified_executor_results` requires `detail >= 40 chars`
+ `evidence` list non-empty before counting the agent's journey
claim as `proof_usable`.

**What happens today:** agent writes `journeys[]` with passed +
detail + evidence. Sink validates the credibility (detail length +
evidence presence) before counting.

**Why this isn't a clear duplicate:** the sink is fail-closed
accounting, not orchestrator pre-flight. It's protecting against
agents that write `{"passed": true, "detail": "yes"}` — which is a
real failure mode without the gate. The 40-char threshold is a
shape-of-evidence check, not a re-verification.

**Verdict:** NOT a fit for this lens. Keep.

---

## Finding 6 — Contract amendment dispatch

**Location:** `otto/v5/repair.py` and the contract amendment retry
mechanism.

**What happens today:** when a Lead writes outside its bound
contract paths, the orchestrator detects, blocks the merge, and
dispatches a separate `contract_amendment` agent to either fix the
violation or formally amend the contract.

**Why this is partially the pattern:** the violating Lead detected
nothing; the orchestrator detected from the diff. A second agent is
brought in to "fix" what the first agent did wrong. If the violation
is a real bug (Lead reached outside its scope), the contract
amendment agent has to decide: was the intent supposed to include
this path, or is the Lead violating? Same diagnosis the violating
Lead could have done before writing.

**Agentic fix:** before merging the Lead's verdict, give the Lead a
chance to self-detect the partition violation (read its own
`owned_paths`, walk the diff, raise an explicit amendment request
if needed). Today the orchestrator does the detection and the
amendment dispatch is the second-pass diagnosis. If the violating
Lead detected its own violation pre-yield, no amendment dispatch
needed.

**Risk:** medium-high. Contract amendment is intricate; the current
flow handles the case where the architect's contract was
under-specified vs the case where the Lead overstepped. Restructuring
needs care.

---

## Summary

| # | Pattern | Risk | Value |
|---|---|---:|---:|
| 1 | pre_agent integration smoke preflight | M-L | Saves ~$0.20+ per integration on no-op repair dispatches |
| 2 | Foundation_gate clean-boot probe | L | Saves ~$0.36 per run when foundation is correct |
| 3 | Mechanical-issue preflight repair dispatches | M | Saves LLM cost on retries that should be orchestrator-side |
| 4 | validate_lead_verdict advisory checks | M | Removes a class of noise without losing signal |
| 5 | journey_verdict_sink credibility check | — | NOT a fit; keep |
| 6 | Contract amendment dispatch | M-H | Reduces the second-agent cost when first could self-detect |

## The meta-pattern

These all share the shape: **orchestrator captures a snapshot, makes
a decision based on it, dispatches an agent to fix the snapshot's
findings.** The snapshot ages between capture and dispatch. The
downstream agent often re-checks first and exits early — confirming
the snapshot was the right answer, just unnecessary work.

The agentic fix in each case: **let the next agent in line re-check
at use time.** Where the orchestrator IS the right place (mechanical
retries, schema enforcement, fail-closed accounting), keep it. Where
an agent will do the check anyway, let it.

## Recommended next steps

1. **Validate the current refactor (Phases 1+2)** — confirms the
   approach holds end-to-end before generalizing.
2. **Ship Finding 1** (drop pre_agent integration smoke preflight) —
   same shape, same pattern; gives a second data point.
3. **Ship Finding 2** (drop foundation_gate clean-boot probe) — same
   shape; the foundation Lead's verify-as-platform prompt is the
   trust boundary.
4. **Defer Findings 3, 4, 6** — each requires more careful design
   (classification logic, contract-amendment restructure).
