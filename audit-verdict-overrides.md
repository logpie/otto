# Otto v5 Verdict Override Audit

## Executive Summary

Found **7 high-confidence override patterns** where the orchestrator second-guesses or demotes agent verdicts. Some are intentional structural safeguards (evidence gates, journey verification); others are borderline (verdict consistency checks, preflight smoke tests) and risk undermining agent authority. Most have been partially mitigated by moving doc-coherence checks to advisory-only in Phase 1 (P0a), but three categories warrant revisiting.

---

## Findings (Priority Order)

### 1. **No-verify auto-downgrade to unverified** (Type: A, F)
**Location:** `otto/lead.py:331-339`

**Pattern:** If Lead completes without calling `mcp__otto__verify`, verdict is automatically downgraded to `unverified` regardless of what the agent claimed in text.

```python
# lead.py:331-339
elif is_integration:
    result.verdict = "unverified"
    failure_reason = _verdict_failure_reason(session_dir, integration=True)
else:
    result.verdict = "unverified"
    failure_reason = _verdict_failure_reason(session_dir, integration=False)
```

**Agent claimed:** Could be `pass`, `partial`, or any verdict in text or intent.

**Orchestrator changed to:** `unverified`

**Reason orchestrator overrode:** Audit is the only PASS gate (by design); absence of verify call is treated as agent choice not to verify. Architectural principle is sound.

**What agent had:** Context about why verify was skipped (e.g., inline mode, no tests, deferred to human).

**What orchestrator has:** System-level constraint that verifier is mandatory for PASS.

**Assessment:** **INTENTIONAL & CORRECT.** This is a hard gate, not a bug. Agent knows the constraint; if it doesn't call verify and claims pass in text, the downgrade to unverified correctly signals "verdict not certified." Documented in `lead.py:19-20` and reflected in spec.

---

### 2. **Evidence-gate auto-downgrade on missing journeys/tests/evidence** (Type: C)
**Location:** `otto/lead.py:1143-1144, 1155-1156, 1162-1163`

**Pattern:** If agent claims `pass` but `_pass_payload_has_evidence()` returns False, verdict is downgraded to `unverified`.

```python
# lead.py:1143-1144
if verdict == VERDICT_PASS and not _pass_payload_has_evidence(canonical):
    verdict = "unverified"
```

Evidence sources checked: journeys, evidence list, deliverables, artifacts, runner_checks, tests, test_command, intent_coverage.

**Agent claimed:** `pass`

**Orchestrator changed to:** `unverified`

**Reason orchestrator overrode:** Pass without proof is untrustworthy; honesty gate.

**What agent had:** Potential intentional reason to claim pass with no written evidence (ran tests outside SDK, verified manually).

**What orchestrator has:** Structural requirement that pass = verifiable claim.

**Assessment:** **INTENTIONAL & SOUND.** The `_pass_payload_has_evidence()` function is a credibility filter: it rejects stub claims. This is not a second-guess — it's enforcing a contract ("if you claim pass, show your work"). Documented in v5_verification_plan.py:42-44.

---

### 3. **Journey-failure downgrade: pass → partial** (Type: A, E)
**Location:** `otto/v5_verification_plan.py:309-310`

**Pattern:** If agent claimed `pass` but journey verification found failures, verdict is downgraded to `partial`.

```python
# v5_verification_plan.py:309-310
if final_verdict == VERDICT_PASS and journey_failures:
    final_verdict = "partial"
```

**Agent claimed:** `pass` (agent-verified via browser MCP + recorded in verdict.json)

**Orchestrator changed to:** `partial`

**Reason orchestrator overrode:** Agent's self-verified journey results passed `resolve_journey_verdicts()` fail-closed check. Journey failure = product defect.

**What agent had:** Real-time observation via chrome-devtools; recorded specific screenshots + journey steps in verdict.json.

**What orchestrator has:** Cross-check via `resolve_journey_verdicts()` which reads agent's own verdict.json + applies credibility gates (detail length ≥40 chars, evidence list non-empty).

**Assessment:** **CORRECT & NECESSARY.** This is not second-guessing; it's **fail-closed verification**. The agent is the executor; the orchestrator is auditing the agent's own recorded evidence. If the agent claimed pass for journey `J1` but verdict.json shows `"passed": False` or missing detail, the sink correctly fails-closed. The fix (Phase 1, unified verifier) moved this responsibility INTO the agent's verdict.json write, not a counter-check. No override here — it's consistent evidence interpretation.

---

### 4. **CHECK_KINDS gate failures: pass → partial** (Type: A, B)
**Location:** `otto/v5_verification_plan.py:307-308`

**Pattern:** If agent claimed `pass` but `validate_lead_verdict()` found failures in CHECK_KINDS (local_scope_check, verdict_consistency), verdict is downgraded to `partial`.

```python
# v5_verification_plan.py:307-308
if final_verdict == VERDICT_PASS and gate_failures:
    final_verdict = "partial"
```

Where `gate_failures` are only checks with `kind in CHECK_KINDS` (post-P0a fix).

**Agent claimed:** `pass`

**Orchestrator changed to:** `partial`

**Reason orchestrator overrode:** Honesty checks failed (e.g., missing evidence for a journeys field, or verdict_consistency detected entity in both `built` and `partial` lists).

**What agent had:** Semantic context for why the apparent inconsistency is valid (e.g., re-tried a journey and intentionally updated state).

**What orchestrator has:** Structural integrity check — detects provable claims of dishonesty.

**Assessment:** **INTENTIONAL & CORRECT.** These are not doc-vs-doc checks (those moved to ADVISORY_KINDS in P0a). `local_scope_check` and `verdict_consistency` are HONESTY gates: they ask "did the agent provide evidence for what it claimed?" An agent that can't explain a contradiction should be downgraded. No override — it's a contract violation detection.

---

### 5. **Smoke-test block demotes integration verdict: pass → partial or merge_blocked** (Type: B)
**Location:** `otto/v5_runner.py:1223, 1275-1276`; `otto/v5/preflight_oracle.py:1232-1240`

**Pattern:** Post-repair integration smoke tests fail (`_integration_smoke_blocks()` returns True), so even if repair agent claimed `pass`, final state is downgraded.

```python
# v5_runner.py:1223
terminal_state = (
    "continued" if repair.verdict == "pass" and not _integration_smoke_blocks(final_payload)
    else "escalated"
)

# preflight_oracle.py:1232-1240
def _integration_smoke_blocks(payload: dict[str, Any]) -> bool:
    if payload.get("error") and payload.get("passed") is False:
        return True
    issues = payload.get("issues") or []
    return any(
        isinstance(issue, dict)
        and issue.get("severity") in ("error", "block")
        for issue in issues
    )
```

**Agent (repair) claimed:** `pass`

**Orchestrator changed to:** Escalated state (terminal_state="escalated"), leading to merge_blocked or partial (via `_record_task_merge_blocked_reason()` → chokepoint logic at `otto/v5/merge.py:1098-1102`)

**Reason orchestrator overrode:** Live smoke test (git checkout, build start, port reachability, etc.) shows environment blockers that the repair didn't address.

**What agent had:** Intent to fix a specific issue (e.g., git conflict); may not have run full smoke suite.

**What orchestrator has:** Real post-repair environment state; independent truth check.

**Assessment:** **BORDERLINE BUT JUSTIFIED.** This is not second-guessing the agent's verdict quality — it's **live environment validation**. The agent fixed the issue it was asked to fix; smoke tests check broader integration health. The override is appropriate because: (a) smoke failures are INFRA concerns, not the repair agent's scope; (b) the fail-closed stance is correct — don't promote an integration with failed smoke. However, this relies on smoke tests being reliable; flaky smoke tests will create false demotes.

**Risk:** If smoke tests are flaky, agents will stop trusting their own repair verdicts.

---

### 6. **Verdict aggregation: child failures downgrade parent** (Type: G)
**Location:** `otto/queue/task_graph.py:995-1018`

**Pattern:** Parent's verdict is computed as the worst child verdict (worst = highest severity). Even if integration agent (parent) claimed `pass` or `partial`, if any child is `merge_blocked`, parent becomes `merge_blocked`.

```python
# task_graph.py:995-1018
def aggregate_verdict(project_dir: Path, task_id: str) -> Verdict:
    severity: dict[Verdict, int] = {
        "pass": 0,
        "pending_children": 1,
        "partial": 2,
        "unverified": 3,
        "merge_blocked": 4,
        "catastrophic": 5,
    }
    worst = own
    for kid in entry.get("child_task_ids", []):
        kid_v: Verdict = kid_entry.get("verdict") or "pending_children"
        if severity.get(kid_v, 0) > severity.get(worst, 0):
            worst = kid_v
    return worst
```

**Parent (integration Lead) claimed:** `pass` or `partial` after merging all children

**Orchestrator changed to:** `merge_blocked` (if any child is merge_blocked)

**Reason orchestrator overrode:** Architectural rule: a product with blocked subtasks cannot be coherent. Pessimistic aggregation.

**What parent had:** Observation of merged state + what was actually committed. If integration agent successfully merged a child despite it being marked `merge_blocked`, that should be visible.

**What orchestrator has:** Task graph verdict entries (snapshot at child-finish time, may be stale).

**Assessment:** **CORRECT BUT POST-REFACTOR FRICTION.** Integration Lead is now the single merge authority (2026-05-21 refactor, task #84 in progress). After integration's upward merge succeeds, the child's merge_blocked verdict should be refreshed to reflect reality (agent merged it anyway). If that refresh is missing, aggregation falls back to stale verdicts and creates false pessimism. This is a **structural gap in the refactor**, not an override bug per se.

**Mitigation needed:** Integration Lead should call out which merge_blocked children it merged, so the final aggregation reflects actual product state.

---

### 7. **Canonical-form normalization downgrades on non-standard field names** (Type: C)
**Location:** `otto/lead.py:1150-1164`

**Pattern:** Agent wrote verdict in a non-standard field (`status`, `result`, `outcome`, `passed`, `success`) instead of top-level `verdict`. Orchestrator canonicalizes it, often changing meaning. For example:

```python
# lead.py:1150-1157
for key in ("status", "result", "outcome", "terminal_outcome", "state"):
    verdict = _verdict_token_to_canonical(payload.get(key))
    if verdict is not None:
        if verdict == VERDICT_PASS and _tests_explicitly_fail(payload):
            verdict = "partial"  # DOWNGRADE if tests exist + failed
        elif verdict == VERDICT_PASS and not _pass_payload_has_evidence(payload):
            verdict = "unverified"  # DOWNGRADE if no evidence
```

**Agent claimed:** `{"status": "pass", "test_outcome": "some tests failed"}` (agent didn't realize `status != verdict`)

**Orchestrator changed to:** `partial` (via line 1154 logic)

**Reason orchestrator overrode:** Best-effort canonicalization; schema normalization.

**What agent had:** Possible intent (e.g., "I moved a lot of code, tests failed but that's expected").

**What orchestrator has:** Heuristic that `tests_explicitly_fail()` means partial, not pass.

**Assessment:** **ACCEPTABLE BUT LOSSY.** This is a **schema mismatch**, not an override. The agent should use top-level `verdict` field per spec. Canonicalization is best-effort and inevitably loses nuance. The override is justified because it prevents invalid states, but it does silence the agent's actual intent. **Recommendation:** Tighten the spec so agents always use `verdict` top-level; defer non-standard forms to advisory fields.

---

## Summary Table

| # | Location | Type | Override | Agent → Orchestrator | Justified? | Actionable? |
|---|----------|------|----------|----------------------|-----------|-----------|
| 1 | lead.py:331-339 | A,F | No-verify → unverified | (text claim) → unverified | YES (hard gate) | No — by design |
| 2 | lead.py:1143-1156 | C | Evidence-gate | pass → unverified | YES (honesty gate) | No — correct contract |
| 3 | v5_verification_plan.py:309-310 | A,E | Journey-fail | pass → partial | YES (fail-closed) | No — agent writes evidence |
| 4 | v5_verification_plan.py:307-308 | A,B | CHECK_KINDS fail | pass → partial | YES (honesty gate) | No — correct contract |
| 5 | v5_runner.py:1223 | B | Smoke-block escalate | pass → partial/merge_blocked | BORDERLINE | YES — flaky smoke → false demotes |
| 6 | task_graph.py:1018 | G | Child-worst aggregation | parent pass → merge_blocked | CORRECT | YES — stale verdict refresh missing |
| 7 | lead.py:1150-1164 | C | Field canonicalization | status=pass → partial | ACCEPTABLE | YES — tighten schema spec |

---

## Recommendations

### Immediate (No changes needed)
- **#1, #2, #3, #4:** These are intentional architectural safeguards. Document them in ARCHITECTURE.md as "Hard Gates" with their contracts.

### Medium term
- **#5 (smoke test reliability):** Audit smoke tests for flakiness. If >5% false-fail rate, deprioritize or move to async validation.
- **#6 (stale verdict refresh):** After integration Lead merges a child, call `set_verdict()` to refresh child's verdict to reflect actual merge state before final aggregation.
- **#7 (schema tightness):** Enforce top-level `verdict` in agent prompts; move `status`, `result`, `outcome` to optional-advice fields only.

---

## Conclusion

The orchestrator is **NOT second-guessing agent authority wantonly**. All 7 overrides serve structural integrity: honesty gates, fail-closed verification, and architectural constraints. The agent's "authority" is correctly scoped: it owns the _execution decision_ (fix this bug, build this feature), not the _credibility verdict_ (prove it worked). The orchestrator's job is auditing that proof. Most overrides are correct and necessary; three (#5, #6, #7) have minor friction or reliability concerns worth addressing.

