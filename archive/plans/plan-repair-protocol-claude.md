# Agent-Native Repair + Verdict Protocol — Claude independent design

_Independent first-principles design. Written without reading Codex's
parallel doc. Merge happens after._

## 1. Diagnosis: the inversion

Every brittle surface in this class shares one shape — **the harness
hands the agent a SYMPTOM + a NARROWED SCOPE + an instruction to STOP,
when agent-native means handing it a GOAL + the ORACLE + FULL CONTEXT +
ownership of the loop.**

Concrete proof from the code:

- `AgentRepairRequest` (v5_preflight_repair.py:23) carries
  `failure_kind: str`, ONE `issue: dict`, `workspace_paths` (pre-narrowed),
  `instruction`. It does NOT carry: the spec/CHARTER/IA, the integration
  packet, child verdicts, prior-attempt diffs/journal, the full set of
  *all* current oracle failures, or the exact oracle command.
- The prompt (v5_runner.py:2486) literally says
  `"PRE-FLIGHT REPAIR ONLY ... Run the narrowest relevant check, write a
  canonical verdict.json, and stop."` It actively forbids the agent from
  doing what an engineer does: run the real deploy, see what breaks, fix,
  re-run, iterate to green.
- `classify_preflight_issue` (v5_preflight_repair.py:482) pre-decides the
  failure kind by string/kind; the controller surfaces only
  `_first_blocking_issue`; a fresh throwaway agent is spawned per symptom
  and discarded; a cap counts the rounds.

The agent never owns the problem. The harness owns it badly (whack-a-mole
+ cap) and rents the agent one myopic glance at a time.

## 2. The agent-native repair protocol

### 2.1 Unit of repair
One **durable repair session per failing acceptance unit**, not per
symptom. The acceptance unit for integration is "this worktree
clean-deploys and its declared acceptance passes." That whole problem is
ONE debugging session — exactly as a human engineer would take it.

### 2.2 Input/context (replace AgentRepairRequest wholesale)
The repair agent must receive:
- **The goal + the exact oracle command** it must make pass (e.g. the
  literal clean-deploy/smoke command), and the statement "the oracle is
  your acceptance test; you are done when it is green."
- **The full current oracle report** — ALL surfaced failures at once, not
  `_first_blocking_issue`. The agent decides ordering/causality.
- **The product contract**: spec, CHARTER, IA, the integration packet +
  child verdicts. Repair without the contract is guessing.
- **The prior-attempt journal**: for retry N, the diffs applied in 1..N-1,
  what each tried, and why the oracle still failed. No more amnesiac
  fresh agents.
- **The worktree + git state**, and a real **turn/time budget** it is
  told it owns.
NOT given: a pre-classified `failure_kind` that dictates scope, a
narrowed `workspace_paths` whitelist, or "stop after the narrowest
check." The agent scopes itself from the contract + oracle.

### 2.3 Prompt
Delete the myopia. The prompt states the goal, hands the oracle, gives
full context + journal, and says: "You own this until the oracle passes
or you can show with evidence it cannot. Iterate: change → run the oracle
→ read the result → continue. Do not stop at the first symptom."

### 2.4 Controller residual job (the ONLY things it keeps)
- Provision the full context packet (it's the librarian, not the
  diagnostician).
- Run the oracle as the acceptance gate after the session.
- Judge progress by **oracle-state delta** (did the failing set shrink /
  change toward green?), never by symptom count or string kind.
- One **wall-clock/turn ceiling** as the sole loop-breaker. No
  `max_total_attempts` symptom cap.
- Capture artifacts; on escalation, persist the full journal.

### 2.5 Escalation honesty
Escalate only when (a) the agent self-declares unfixable *with evidence*,
or (b) the ceiling is hit *and* the oracle state did not improve across
the budget. "3 different things were each fixed" is convergence, never
escalation — the Bug-A failure mode becomes structurally impossible.

### 2.6 Interop with P0–P4
The smoke/clean-deploy oracle and the merge/verdict gates are unchanged
and remain the acceptance truth. This protocol only changes *how repair
is dispatched* (one owning session) and *what it's told* (goal+oracle+
context, not symptom+scope+stop). The P0 "merge only on pass/reviewed-
partial" gate still sits downstream.

## 3. The verdict-gating protocol

**A verdict is set ONLY by deterministic state, declared oracles, or
structured contradiction — never by reading free text.**

- pass/partial/merge_blocked/fail is determined by: declared acceptance
  journeys/tests results, the smoke/clean-deploy oracle, deterministic
  contract/structural invariants (pages/routes/endpoints resolve, actions
  have tests, verdict-consistency), and merge/scope/conflict invariants.
- **stdout/stderr/source TEXT is never a gate.** Build/test warnings,
  deprecation chatter, `TODO`/stub markers → advisory telemetry on the
  proof packet, nothing more.
- Keep/demote/delete rule: *"Is this a deterministic state/contract/
  oracle check, or a heuristic read of free text? The former may gate.
  The latter may only advise."*

Concrete catalog actions in v5_verification_plan.py:
- DELETE-as-gate → advisory: `_check_deprecation_warnings` /
  `_deprecation_lines` (704/737) **and its negation-regex
  (661–666)** — that regex is patch-on-patch proof the whole check is
  the wrong shape; `_check_no_stub_text` (533) + its TODO regex (621–629)
  (a working product with a `// TODO later` comment must not fail);
  the `needle in text` gate (327).
- KEEP (deterministic, legitimate): pages/routes/endpoints resolve
  (348/381/401), actions-have-tests (457), entities-have-empty-states
  (509), verdict-consistency (629).

If "no deprecation warnings" is genuinely a product requirement for some
intent, it must be expressed as a real oracle assertion (a test that
exits non-zero), not inferred by grepping logs.

## 4. Sibling hunt (independent, file:line)

| Surface | file:line | Why non-agent-native | Agent-native fix |
|---|---|---|---|
| `AgentRepairRequest` | v5_preflight_repair.py:23 | impoverished input: symptom+scope, no contract/packet/journal/oracle | replace with full context packet (§2.2) |
| preflight repair prompt | v5_runner.py:2486 | "ONLY … narrowest … stop" myopia | goal+oracle+ownership prompt (§2.3) |
| `classify_preflight_issue` | v5_preflight_repair.py:482 | string/kind pre-classification dictates scope | stop pre-classifying; hand all issues, agent diagnoses |
| `_first_blocking_issue` loop | v5_preflight_repair.py:127 | one symptom/round + full oracle re-run + cap | one owning session, oracle-delta progress (§2.4) |
| `_child_verify_repair_intent` | v5_runner.py:779 | milder same disease: no contract/packet, vague "relevant oracle", fresh per `attempt-NN` | hand exact oracle + contract + cross-attempt journal |
| `_check_deprecation_warnings` | v5_verification_plan.py:737 | free-text scan gates verdict (Bug B1) | advisory only (§3) |
| `_check_no_stub_text` | v5_verification_plan.py:533 | source TODO/stub regex gates verdict | advisory only (§3) |
| merge-conflict repair | v5_runner.py:2454 (kind=merge_conflict) | same impoverished `_run_preflight_repair_agent` path | same packet protocol |

Blast radius: HIGH = preflight repair model + verdict string-gates (set
false merge_blocked / false partial on real products). MEDIUM =
child-verify-repair, merge-conflict repair (same shape, lower frequency).

## 5. Standing invariant (extend the brittleness guardrail)

Two assertions, added to `tests/test_brittleness_guardrail.py`:

1. **Repair dispatch invariant**: any autonomous repair/agent dispatch
   must pass a goal + the acceptance-oracle command + full context
   packet. Flag any `_run_*_agent` whose intent string is a templated
   single symptom (`"... ONLY"`, `"Failure kind: {…}"`, "narrowest",
   "and stop") or whose request struct lacks contract/oracle fields.
2. **Verdict-gate invariant**: no verdict may be set or downgraded from a
   stdout/stderr/source free-text scan. Flag any verdict/check that does
   `<text> in/regex` to produce a pass/partial/blocked decision; allow
   only deterministic state/contract/oracle checks.

The existing guardrail catches "success-on-error / parse-swallow / string
infra classifier." These two extend it to "symptom-scoped repair" and
"text-scan verdict gate" — the two faces of this disease — so it cannot
silently return.

## 6. One-sentence protocol statement

> Otto dispatches repair as **one durable agent session that owns a
> failing acceptance unit, handed the goal + the exact oracle + the full
> product/integration context + the prior-attempt journal**, and sets
> verdicts **only from deterministic state, declared oracles, or
> structured contradiction — never from reading free text.**
