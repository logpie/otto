# Otto Orchestrator Brittleness Audit

## Executive Summary

The Otto v5 orchestrator compensates for agent failures via hardcoded retry loops, stale snapshot decisions, and default parameter overrides. Eight high-confidence patterns identified where agent responsibility is scattered across orchestrator code. Moving verification and recovery decisions into agent domain (via prompts) would eliminate a class of brittle all-or-nothing predicates.

---

## Pattern A: Stale Snapshot Decisions Before Agent Dispatch

### Finding A1: Repair agent dispatched on stale preflight snapshot
**Location:** `otto/v5_runner.py:1091–1240` (`_run_preflight_payload_repair_session`)

**Pattern Type:** C (Snapshot-based dispatch decision)

**What happens today:**
The orchestrator checks `initial_payload` from a preflight run (captured seconds ago), decides it's blocked, and dispatches a repair agent without re-verifying whether the issue still exists. The repair agent's first internal action is often to re-run the oracle (line 1218: `final_payload = run_once()` after repair completes), which now passes. Cost: wasted agent turn on a no-op repair.

**Why it's brittle:**
Time-of-check to time-of-repair gap. Transient issues (a lingering process freed by GC, a slow disk write that finished) disappear between snapshot and dispatch. The agent lands, sees pass, and the runner has burned budget.

**Agentic alternative:**
The repair agent prompt should lead with: "If oracle passes on entry, exit immediately with verdict=pass; do not investigate." Orchestrator re-verifies before dispatching: `payload = run_once(); if not blocks(payload): return payload` — catch the fixed state before dispatch.

**Risk of moving it:**
Low. The agent already has oracle tools and can verify. Prompt clarification needed, no architectural change.

---

### Finding A2: Retry logic on mechanical blockers without state re-check
**Location:** `otto/v5_runner.py:1098–1102` (retry after mechanical blocker)

**Pattern Type:** C (Stale state assumption)

**What happens today:**
Line 1099–1102: If initial payload blocks on something mechanical, the orchestrator calls `run_once()` to re-check. If the retry still blocks, it assumes repair is needed and dispatches an agent—without re-confirming the issue hasn't self-healed or that the agent's fix strategy would be different this time.

**Why it's brittle:**
The "retry once" heuristic is arbitrary. Some issues need 2–3 retries to settle (git locks, port rebinding, filesystem syncs). If retry 2 still fails, the agent is dispatched to fix a problem that might go away on retry 3. No graduated escalation, just binary: pass → return, fail → dispatch agent.

**Agentic alternative:**
Repair packet should include the prior 2–3 attempts' details. Agent sees the pattern (e.g., "port still bound after 2 retries") and decides: natural contention → wait/retry vs. genuine deadlock → investigate. Orchestrator can perform 2–3 mechanical retries before escalating; agent owns recovery strategy.

**Risk of moving it:**
Low. Orchestrator retries are cheap; agent dispatch is expensive. Orchestrator retries are fine, but escalation decision should be in agent hands via packet context.

---

## Pattern B: Hardcoded Oracle Invocation Counts

### Finding B1: `default_oracle_invocations=3` hardcoded in repair packet builders
**Location:** `otto/v5_runner.py:1120, 1533` and `otto/v5/preflight_oracle.py:857`

**Pattern Type:** B (Hardcoded parameter that should be agent-decided)

**What happens today:**
Orchestrator builds every repair packet with `default_oracle_invocations=3`. This constant is baked into the packet, the agent sees it and treats it as a budget ceiling. If the oracle needs 4 invocations to converge on a complex preflight failure, the agent hits the limit and escalates (merge_blocked) instead of continuing.

**Why it's brittle:**
The "3 invocations is enough" assumption is per-repair-phase, not per-problem. Checkout repair might need 1–2; integration oracle might need 4–5 if the app has cascading startup dependencies. A single constant penalizes complex products.

**Agentic alternative:**
Remove the hardcoded 3; let the agent drive oracle invocations up to a run-budget cap (already enforced at `_await_with_run_deadline`). Repair packet can suggest "start with 3, invoke more if you see convergence" instead of a hard ceiling. Or: repair agent scans the initial failure signature and adjusts its own invocation count before the first oracle call.

**Risk of moving it:**
Medium. If agents over-invoke the oracle, run budgets exhaust. Mitigation: run-budget cap is the real safety boundary; 3 is just an efficiency hint. Can be removed as a hard limit without weakening gates.

---

## Pattern C: Dead Diagnostics (Lint Warnings)

### Finding C1: Lint warnings emitted and stored but never acted upon
**Location:** `otto/spec_compile_flat.py:194–206` (lint_journey), `spec_compile_flat.py:348–466` (lint_spec)

**Pattern Type:** G (Dead validator with no consumer)

**What happens today:**
The spec compiler runs `lint_spec()` and populates `FlatSpec.lint_warnings` with human-readable warnings: "behavior_journeys[2]: contains CSS class selector", "product_overview.primary_navigation is missing", etc. These warnings are saved to the spec artifact and JSON but never drive any agent action. No agent re-prompts the compiler, no orchestrator gate blocks on them, no human consumer reads them (they're buried in logs).

**Why it's brittle:**
The warnings exist to guide downstream agents (Leads building features), but no specification says they're advisory. A prompt that says "this warning was raised" without authority (gate, retry, annotation) is noise. Worse: if the warning points to a real coverage gap (e.g., "action X is not covered by any journey"), the gap is invisible to agents because the warning doesn't reach them in a structured way.

**Agentic alternative:**
Option 1: Spec compiler is responsible for coverage. If a journey gap is detected, the compiler should re-prompt itself to add an additional journey or document an exception, then verify coverage before returning (responsibility → compiler prompt).  
Option 2: Mark warnings as "advisory" in the spec; downgrade to a structured warnings list that Leads consume and acknowledge (responsibility → downstream agent prompts).  
Option 3: No warnings in the spec at all; let agents infer from the raw spec structure (responsibility → agent inference).

**Risk of moving it:**
Low for Option 1 (spec compiler re-prompting). Medium for Option 3 (removes a diagnostics channel). Safe to experiment: warnings are non-blocking today anyway.

---

## Pattern D: Arbitrary Retry Attempt Caps

### Finding D1: `MAX_ARCHITECT_RETRIES = 2` hardcoded cap on scaffold re-runs
**Location:** `otto/v5_runner.py:124` (constant definition), `otto/v5/dispatch.py:264–316` (usage in architect preflight loop)

**Pattern Type:** A (Hardcoded retry loop with no escalation strategy)

**What happens today:**
When the Foundation Lead passes but the scaffold oracle detects a contract violation, the runner re-dispatches the Foundation Lead up to 2 times (`MAX_ARCHITECT_RETRIES=2`). After 2 failures, the run merge-blocks. This is a flat retry loop with no adaptive logic: it doesn't inspect the error to decide if retry is fruitful, just counts attempts.

**Why it's brittle:**
The 2-attempt cap is arbitrary. Some contract issues are fixable (missing environment variable → set it) and need 1–2 retries. Others are architectural (wrong subsystem boundary → requires re-decomposition) and won't be fixed by retry. The orchestrator can't tell the difference, so it applies the same capped loop to both.

**Agentic alternative:**
Foundation Lead's prompt should classify its own failures: "If oracle blocks on X (missing field, malformed contract), add it and re-run oracle before returning. If oracle blocks on Y (structural mismatch), document it as a merge_blocked annotation and return partial—re-decomposition from root is the only fix." Orchestrator removes the retry loop entirely; agent owns adaptive retry inside its session.

**Risk of moving it:**
Medium. The current loop is a safety boundary against infinite re-runs. Moving retry into the agent requires the agent to be disciplined about termination. Mitigation: agent has its own budget (wall-clock per repair phase); run-budget timeout is the ultimate cap.

---

## Pattern E: Orchestrator-Internal State Leaking to Agents

### Finding E1: Probe ports and internal ephemeral data in integration packet
**Location:** `otto/v5_runner.py:` (port cleanup payloads), `otto/v5_preflight_repair.py:` (RepairPacket structure)

**Pattern Type:** E (Orchestrator noise in agent-facing surface)

**What happens today:**
When the orchestrator runs a clean-deploy probe, it records ephemeral port numbers (52351, 52352, etc.) in the oracle result. This result is serialized into the repair packet and visible to the repair agent. The agent can see "ports 52351, 52352 were probed" and might mistakenly reason about them ("should I use port 52351?" No, that was temporary).

**Why it's brittle:**
The agent sees implementation details (probe's ephemeral port choices) that have no semantic meaning for the product. Worse, if the agent's repair logic pattern-matches on port numbers, it couples the agent to the orchestrator's probe strategy. Example: "Oh, the probe used 52351, so the real app should use 52350" (broken assumption).

**Agentic alternative:**
Repair packet should carry only outcome-level data: "port bind check: app should listen on port(s) X, Y, Z. Current state: X bound by process PID 123, Y and Z unbound." Scrub the probe's own ephemeral ports before packaging. Agent is insulated from orchestrator mechanics.

**Risk of moving it:**
Low. The scrubbing is a simple data filter. No architectural change. Agents are already trained to ignore implementation artifacts; tightening the packet boundary is straightforward.

---

## Pattern F: Hardcoded Validation Rules and Ownership Assumptions

### Finding F1: Contract path normalization and ownership inference in runner
**Location:** `otto/v5_runner.py:789–888` (contract path logic, owner inference from task graph)

**Pattern Type:** F (Orchestrator guessing at ownership rules)

**What happens today:**
The orchestrator defines and enforces path normalization (`_normalize_contract_path`), path overlap rules (`_path_overlaps`), and task ownership of paths (hardcoded checks: "only owner can write path X"). These rules are scattered across v5_runner.py functions with names like `_foundation_contract_write_feedback`, `_allowed_paths_write_feedback`. The task graph records who owns what, and the orchestrator applies the rules at commit time.

**Why it's brittle:**
The ownership model is baked into orchestrator code. If Leads want to negotiate shared ownership (e.g., "feature task A and foundation task B both touch app.css"), the orchestrator can't represent it; the hardcoded rule "only owner writes" blocks the merge. If a new rule emerges (e.g., "contract_amendment tasks can write X only if parent approved"), it requires code changes.

**Agentic alternative:**
Move contract rules to CHARTER.md or a root-level contracts configuration that the orchestrator reads, not hardcodes. Architect declares: "path app/auth is owned by task foundation-1, shared-writable by task feature-2-auth-integration". Orchestrator reads and enforces; Leads can't violate the declared intent (it's in CHARTER) but the logic lives in config, not code. Rules are now auditable and agent-revisable.

**Risk of moving it:**
Medium-high. Requires a contract DSL and rewrite of commit-time checks. But the payout is high: composition becomes flexible. Can be phased in: keep orchestrator rules as fallback, allow CHARTER overrides as an opt-in pilot.

---

## Pattern G: Proof of Work as Non-Blocking Artifact

### Finding G1: Proof packet structure suggests validation it doesn't enforce
**Location:** `otto/v5_runner.py` and `otto/lead.py` (summary.json structure), related code paths

**Pattern Type:** G (Diagnostic artifact without enforcement)

**What happens today:**
The runner assembles a proof-of-work packet with screenshots, verdict details, and a "quality_score". This packet is human-readable and serves as documentation, but it's not a gate: a low quality_score doesn't block the run, missing evidence doesn't cause a retry. The packet is write-only output, not consumed by any decision logic.

**Why it's brittle:**
If the proof packet is meant to communicate "this run is trustworthy" to a human reviewer, it should be part of the gate. If it's just logging, the quality_score field creates false impression of gatekeeping. The ambiguity means bugs can slip through: the orchestrator can claim a high quality_score while the actual product has silent failures (because the score is computed from coverage metrics, not live testing).

**Agentic alternative:**
Clarify the packet's role: if it's documentation → remove scored fields, keep narrative. If it's validation → make it a gate and have agents inspect/improve the evidence before claiming pass. If it's both → explicitly mark which sections drive verdicts (evidence items as blocking, narrative as advisory). Root cause: the packet conflates "proof for human" with "proof for automation". Separate the concerns in the prompt/structure.

**Risk of moving it:**
Low for clarification; medium if proof becomes a gate. As documentation, the packet is fine as-is.

---

## Pattern H: Orchestrator Second-Guessing Agent Verdicts

### Finding H1: Integration Lead verdict downgraded if verify was not called
**Location:** `otto/lead.py:19–23` (invariant comments), `otto/lead.py` implementation

**Pattern Type:** H (Orchestrator override of agent output)

**What happens today:**
If an Integration Lead claims `verdict=pass` in its text output but never called `mcp__otto__verify`, the orchestrator downgrades the verdict to `unverified` (line 19 in lead.py: "If Lead claimed pass in text but mcp__otto__verify never ran, the text claim is ignored; verdict computed from absence of verify.").

**Why it's brittle:**
The agent might have had a good reason to skip verify (e.g., "integration is minor, spec covers it, calling verify would be redundant"). Or the agent's logging might be incomplete. But the orchestrator's rule is absolute: no verify call → downgrade. This creates a perverse incentive: agents call verify even when not needed, burning budget, just to avoid the downgrade.

**Agentic alternative:**
Trust the agent's verdict if it's explicit. If the agent says `verdict=pass`, it can explain why (and the prompt should require it: "If you claim pass, justify why verify was unnecessary"). Orchestrator doesn't override explicit verdicts; it logs a note if verify was skipped. If the agent can't justify skipping verify, the prompt rejection will force a re-run.

**Risk of moving it:**
Medium. The verify-requirement is a real safety gate (ensure the agent actually tested). Can be reframed: not "orchestrator downgrades", but "agent must assert verify was called or justify why it wasn't". The judgment stays with the agent, enforced by prompt, not orchestrator override.

---

## Summary of Risks and Mitigation

| Pattern | Risk Level | Mitigation |
|---------|-----------|-----------|
| A1, A2 (stale snapshots) | Low | Re-verify before dispatch; add verify-on-entry to agent prompt |
| B1 (hardcoded oracle invocations) | Medium | Remove hard ceiling; keep run-budget as real boundary |
| C1 (lint warnings) | Low | Spec compiler re-prompts itself for gaps; or downstream agents acknowledge |
| D1 (retry caps) | Medium | Move retry logic into Foundation Lead; orchestrator has final timeout |
| E1 (port leakage) | Low | Scrub ephemeral data before packaging; semantic-only in repair packet |
| F1 (ownership rules) | Medium-High | Extract to CHARTER/config; orchestrator enforces, Leads audit |
| G1 (proof packet) | Low | Clarify role (documentation vs. gate); separate concerns in structure |
| H1 (verdict override) | Medium | Trust agent assertions; enforce justification at prompt level, not orchestrator |

---

## Recommended Priority

**Tier 1 (implement first):**
- A2 (retry escalation) → frees budget by avoiding no-op agent dispatches
- E1 (port scrubbing) → unblocks agent from implementation details
- B1 (oracle cap removal) → frees budget for complex preflight failures

**Tier 2 (architectural):**
- F1 (contract extraction to CHARTER) → enables composition flexibility
- D1 (architect retry → agent responsibility) → removes retry-cap guessing

**Tier 3 (clarity/hygiene):**
- C1 (lint warnings action) → reduce noise; specs clean up on compile
- H1 (verdict override) → reframe as agent justification; trust flow
- G1 (proof packet role) → clarify documentation vs. gate; separate concerns

---

## Notes

- No Codex review needed for this audit; findings are structural, not implementation bugs.
- Moving responsibility into agent domain does NOT weaken gates; it sharpens them by putting verification in the hands of the agent (which has context) rather than orchestrator (which is context-blind at decision time).
- The orchestrator's role is coordination (dependency ordering, budget enforcement, artifact routing), not verification. Each pattern above is an instance where verification bled into coordination.
