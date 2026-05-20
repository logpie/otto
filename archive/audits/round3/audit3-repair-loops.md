# Repair Loop Audit — otto/v5/repair.py (2,196 LOC)

**Scope:** Function map, overlap analysis, stale-target removal validation, safe collapse candidates
**Repository State:** Post-Part-2 split; repair.py freshly extracted from v5_runner.py
**Background:** Prior audit (archive/audits/part2/audit2-runner.md) identified 7 repair loops; all now live in repair.py

---

## 1. Function Map: The 7 Repair Loops

| Loop # | Name | Entry Function | Retry Cap | Trigger Condition | Action per Attempt | State Persistence |
|--------|------|---|---|---|---|---|
| **1** | Spec Contract Amendment | `_schedule_foundation_contract_amendment()` L198 | `MAX_CONTRACT_AMENDMENT_ATTEMPTS=2` (L118 v5_runner) | Spec compile fails due to contract mismatch | Enqueue amendment task → agent fixes constraint → merge back to root | `.otto/task-graph.json`: `contract_amendment_attempts` dict (L156) |
| **2** | Foundation Contract Amendment | `_schedule_foundation_contract_amendment()` L198 (re-used) | `MAX_CONTRACT_AMENDMENT_ATTEMPTS=2` (L118 v5_runner) | Child merge blocks due to union conflict on owned contract (L1943 merge.py) | Enqueue amendment task for contract owner → agent fixes compatibility → unblock child (L664–679) | `.otto/task-graph.json`: `contract_amendment_blocked` state (L259), `contract_amendment_merge_context` (L266) |
| **3** | Integration Smoke Repair | `_schedule_smoke_repair_needed()` L415 | `MAX_CONTRACT_AMENDMENT_ATTEMPTS=2` (shared at L587) | Integration lead's preflight smoke test fails with routable issue paths (L1574 merge.py) | Extract issue paths → enqueue smoke-repair task → repair agent fixes out-of-scope breakage → re-route or retry merge | `.otto/task-graph.json`: `contract_amendment_attempts` dict (reused at L439–443) |
| **4** | Preflight Repair | `_run_preflight_payload_repair_session()` (imported from v5_preflight_repair.py L2774 v5_runner) | `MAX_PREFLIGHT_REPAIR_ATTEMPTS` (varies; typically 2, no centralized constant in repair.py) | Checkout, port cleanup, or smoke preflight sanity check fails | Invoke repair agent → fix mechanical issues (git state, port conflicts, file perms) → retry phase | Session logs only; no task-graph persistence |
| **5** | Merge Conflict Repair | `_repair_child_merge_conflict_once()` L2054 | 1 attempt only (implicit; called once per merge failure at L1436 merge.py) | Child upward merge hits git 3-way conflict | Analyze conflict packet → schedule merge-conflict repair task → agent resolves conflicts per path → commit & retry merge | Session-local only; no task-graph persistence |
| **6** | Stale Target Retry | `_repair_stale_target_and_retry_merge()` L1801 | **3 internal attempts** (implicit; calls `_repair_child_stale_target_gate_once()` once, then embeds merge retry logic) | Target branch (parent integration) advanced after child was dispatched; child's pre_merge_ref is stale (L1656, L2068, L2146 merge.py) | Repair upward-merge gate (L1838) → re-fetch merge-base → re-attempt merge → run optional smoke preflight (L1923–1982) | Session-local + feedback dict (L1763 returns `_StaleTargetRetryResult`) |
| **7** | Amendment Retry on Merge Failure | `_refresh_contract_amendment_retry_heartbeat_until_stopped()` L817 (coroutine) | Implicit; heartbeat cycle until amendment resolves or timeout (L774 hardcoded max_claims=2) | Amendment task itself fails to merge back to parent integration after being enqueued | Heartbeat refresh every 60s (L824 `CONTRACT_AMENDMENT_RETRY_HEARTBEAT_INTERVAL_SECONDS`) → if stale, mark exhausted (L795) | `.otto/task-graph.json`: `contract_amendment_retry_*` fields (L770–793) |

---

## 2. Overlap Matrix

### 2.1 Can Loop 3 (smoke repair) and Loop 5 (merge conflict repair) trigger on the same root cause?

**Evidence:** Yes, but sequentially, not simultaneously.

1. Child merge fails with conflict (detected at L1436 merge.py)
   → Loop 5 triggers: `_repair_child_merge_conflict_once()` L2054
   → Conflict repair agent runs and commits L2178–2181
   → Retry merge (handled inside Loop 5's `_repair_stale_target_and_retry_merge` call at L2068 merge.py if needed)
2. If merge succeeds after conflict repair, **smoke preflight runs** inside `_repair_stale_target_and_retry_merge()` at L1923–1982 repair.py
   → If smoke fails, `_route_out_of_scope_smoke_failure()` triggers (L1938 repair.py)
   → Loop 3 may fire if issue paths are routable

**Verdict:** Loops 3 and 5 can both fire in the same merge attempt, but Loop 5 runs FIRST (resolves conflicts), then Loop 3 (handles smoke fallout). No compound retry multiplication — Loop 3 waits for Loop 5 to finish.

### 2.2 Does Loop 6 (stale target) ever fire after Loop 5 (merge conflict)?

**Evidence:** Yes, with conditional smoke preflight.

In `_repair_stale_target_and_retry_merge()` L1801:
- Line 1838: Calls `_repair_child_stale_target_gate_once()` (the upward-merge-gate repair)
- Line 1910–1915: Attempts merge
- Line 1923–1982: **Optional** smoke preflight runs if `run_smoke_preflight=True` (L1669 merge.py passes this when called from merge-conflict recovery)

**Verdict:** Loop 6 embeds a conditional smoke preflight (Line 1923–1982), but doesn't itself retry the merge 3 times. The "3 internal attempts" mention in the prior audit is **incorrect** — Loop 6 calls the repair ONCE, then merges ONCE. If merge fails, Loop 6 returns terminal failure (not a retry cap). The confusion arises because `_repair_child_stale_target_gate_once()` calls `_repair_child_upward_merge_gate_once()` which itself calls `_run_child_verify_repair_packet()` (an agent run), which may internally retry. But that's agent-level, not loop-level.

### 2.3 Compound retry multiplication: do 2+ loops compound their retry caps?

**Evidence:** No.

Each loop is **gated by the previous loop's success**:
- Loop 1 (spec amend): exclusive to Phase B (spec compile). Runs before decomposition. No downstream loops can fire.
- Loop 2 (foundation amend): fires on child merge block. Unblocks child (L664), which re-queues for merge (L664–679). Child retry is **explicit re-enqueue**, not a multiplied retry cap.
- Loop 3 (smoke repair): fires on merge smoke failure (post-merge-attempt). Enqueues repair task. Terminal if exhausted (L587–615).
- Loop 5 (merge conflict): fires on git 3-way conflict (pre-merge-success). Single-attempt repair.
- Loop 6 (stale target): fires on merge failure. Single-attempt repair + optional smoke preflight.
- Loop 7 (amendment retry): embedded in Loop 2; waits for amendment to resolve.

**No multiplication.** Each loop gates the next; they don't multiply effort.

---

## 3. Stale Target Retry Removal — Proposal Validation

### 3.1 Current Implementation (L1801–2036 repair.py)

```python
async def _repair_stale_target_and_retry_merge(
    *,
    ...
    detail: str,
    prior_repair_detail: str,
    ...
    on_event: Any = None,
) -> _StaleTargetRetryResult:
    """Re-enter the existing child repair loop, retry merge, and own terminal blocks."""
```

**What it does:**
1. **Single repair attempt** (not 3): calls `_repair_child_stale_target_gate_once()` L1838 once
2. **Single merge attempt** (not 3): calls `merge_child_into_integration()` L1911 once
3. **Optional smoke preflight**: if `run_smoke_preflight=True`, runs smoke check at L1923–1982
4. **Terminal handling**: if any step fails, records failure and returns (no retry loop)

**Prior audit claim:** "3 attempts; re-fetch target ref and re-attempt merge" (archive/audits/part2/audit2-runner.md L145–150)

**Verdict:** **The audit's claim is INCORRECT.** There is NO 3-attempt loop in repair.py. The function is **single-attempt**.

### 3.2 Proposal: Always fetch fresh before merge

**Current behavior:**
- Calls `_repair_child_upward_merge_gate_once()` L1838, which runs a repair agent if the merge gate is blocked
- Then attempts merge (L1910)
- If merge fails, records terminal failure

**Proposed behavior:**
- Before ANY merge attempt, call `git fetch origin <target>` to ensure fresh refs
- Attempt merge
- If merge fails, record terminal failure (no retry)

**Questions to answer:**

**(a) Does it actually retry meaningfully, or do all attempts hit the same root cause?**

The stale-target function doesn't retry—it makes ONE repair attempt and ONE merge attempt. If the merge fails after repair, it's terminal. The name "stale target" is misleading; it doesn't actually detect or recover from a stale target condition. The repair agent (`_repair_child_upward_merge_gate_once`) handles upward-merge-gate failures (e.g., conflicting changes post-dispatch), not staleness.

**Verdict:** There's no retry loop to validate. The function is single-pass.

**(b) What would break if we remove it and add `git fetch origin <target>` before every merge attempt?**

The function `_repair_stale_target_and_retry_merge()` is called in 3 places (L1656, L2068, L2146 merge.py):

1. **L1656 merge.py**: After merge conflict repair, before retry
2. **L2068 merge.py**: As a fallback when merge fails (detail not a conflict)
3. **L2146 merge.py**: Integration branch merge (root integration)

If we **remove** `_repair_stale_target_and_retry_merge()` and **always call `git fetch` before merge**:

- **No loss:** The fetch is purely preventative. Stale refs cause merge failures; fresh fetch eliminates that failure mode.
- **Gain:** Simpler state handling. One pre-merge-step instead of a repair-then-merge compound.
- **Risk:** If the real problem is NOT staleness but a genuine conflict (code semantics), the fresh fetch doesn't help. But in that case, the repair agent (`_repair_child_upward_merge_gate_once()`) handles it via code changes, not re-fetching. So the fetch is orthogonal.

**Verdict:** Safe to remove the function and add unconditional `git fetch origin <target>` before all merge attempts.

**(c) Cite call sites.**

- **L1656 merge.py**: After `_repair_child_merge_conflict_once()`, before retry merge
- **L2068 merge.py**: Fallback for non-conflict merge failure
- **L2146 merge.py**: Integration root merge failure fallback

All three currently wrap a single merge attempt. Replace with:
```python
subprocess.run(["git", "fetch", "origin", target_branch], check=True)
merge_ok, merge_detail = merge_child_into_integration(...)
```

---

## 4. Safe Collapse Candidates

### Candidate A: **Merge Loops 5 & 6** (Merge Conflict Repair + Stale Target Retry)

**Current state:**
- Loop 5: `_repair_child_merge_conflict_once()` L2054 — fixes git 3-way conflicts
- Loop 6: `_repair_stale_target_and_retry_merge()` L1801 — repairs upward-merge-gate, then merges once, with optional smoke

**Overlap:**
- Both called in `_merge_child_branch()` (merge.py):
  - L1436: Loop 5 fires if `_looks_like_merge_conflict(detail)` L1430
  - L1656: Loop 6 fires if conflict repair succeeds (L1635 `if repair_verdict == "pass"`)
- Loop 6 embeds smoke preflight; Loop 5 does not

**Collapse proposal:**
- **Do NOT merge these.** Loop 5 is specialized for git conflicts (3-way resolution); Loop 6 is for general upward-merge-gate failures (code semantics, ownership). They solve different problems. Merging loses semantic clarity.

**Recommendation:** SKIP.

---

### Candidate B: **Remove Loop 6 (Stale Target Retry) — VALIDATE PROPOSAL**

**Current state:**
- `_repair_stale_target_and_retry_merge()` L1801 repairs upward-merge gate, then merges once
- Called 3 times (L1656, L2068, L2146 merge.py)
- Single-pass; no retry loop

**Collapse proposal:**
- Delete `_repair_stale_target_and_retry_merge()` L1801–2036
- Add unconditional `git fetch origin <target>` before all merge attempts
- Inline the smoke-preflight logic (L1923–1982) into the merge-retry path as a separate guard

**Effort:** ~1 hour (refactor call sites, extract smoke preflight, test)

**Risk:** LOW
- Semantics unchanged: fresh fetch prevents stale-ref-induced merge failures
- Smoke preflight is independent; can be called separately
- No compound retry multiplication lost

**Breakage if wrong:**
- Merge attempts could fail due to stale parent integration branch HEAD
- Symptom: "merge failed: cannot resolve conflict; target branch advanced"
- Mitigation: logs would show "git fetch failed" or merge report would show ref mismatch

**Verdict:** **SAFE TO REMOVE. Implement this.**

---

### Candidate C: **Merge Loops 1 & 2** (Spec Amendment + Foundation Amendment)

**Current state:**
- Loop 1: `_schedule_foundation_contract_amendment()` L198, triggered by spec compile failure
- Loop 2: Same function, triggered by child merge union conflict (L259)
- Both use shared retry counter: `contract_amendment_attempts` dict (L156)
- Both use `MAX_CONTRACT_AMENDMENT_ATTEMPTS=2` (L118 v5_runner)

**Overlap:**
- Same function, same retry cap, same state tracking
- Semantically different: Loop 1 is spec-layer (constraint); Loop 2 is merge-layer (ownership)
- Loop 2 has dependent resolution logic (L642–685) that Loop 1 doesn't

**Collapse proposal:**
- These are already collapsed into one function (`_schedule_foundation_contract_amendment()`)
- No further consolidation possible without losing semantic distinction

**Recommendation:** ALREADY DONE.

---

### Candidate D: **Merge Loops 3 & 5** (Smoke Repair + Merge Conflict Repair)

**Current state:**
- Loop 3: `_schedule_smoke_repair_needed()` L415 — routes smoke failures to repair agent
- Loop 5: `_repair_child_merge_conflict_once()` L2054 — fixes git conflicts directly

**Overlap:**
- Loop 3 is triggered by smoke failures (out-of-scope breakage); Loop 5 by git conflicts (code incompatibility)
- Both enqueue repair tasks; both retry merge after repair
- Loop 3 uses amendment-attempt counter (L439, L587); Loop 5 does not (implicit single-attempt)

**Collapse proposal:**
- Do NOT merge. Loop 3 detects semantic issues (missing files, env config); Loop 5 detects syntactic issues (git conflicts). Different detection, different fixes.

**Recommendation:** SKIP.

---

### Candidate E: **Remove Loop 7** (Amendment Retry on Merge Failure — Heartbeat Refresh)

**Current state:**
- `_refresh_contract_amendment_retry_heartbeat_until_stopped()` L817 — coroutine that refreshes heartbeat every 60s
- Called when amendment task is enqueued (L232 in integration_context during amendment scheduling?)
- No explicit loop cap; heartbeat terminates on timeout (max_claims=2 at L774)

**Overlap:**
- Embedded in Loop 2; not independent
- Ensures amendment task doesn't timeout before it resolves

**Collapse proposal:**
- Do NOT remove. This is a liveness mechanism (prevents false timeout). Removing it would cause amendments to timeout mid-repair, escalating to parent re-decomp.

**Recommendation:** KEEP.

---

## 5. Concrete Proposals

### Proposal 1: **Remove Stale Target Retry Function**

**What to change:**
- Delete `_repair_stale_target_and_retry_merge()` L1801–2036 (235 LOC)
- Delete `_repair_child_stale_target_gate_once()` L1749–1793 (45 LOC)
- Delete `_stale_target_gate_feedback()` L1702–1747 (45 LOC)
- Delete `class _StaleTargetRetryResult` L1795–1799 (5 LOC)

**What replaces it:**
- In `_merge_child_branch()` merge.py L1656, L2068, L2146: add `git fetch origin <parent_integration_branch>` before merge
- Extract smoke preflight logic (L1923–1982) into a standalone coroutine `_run_optional_smoke_preflight_after_merge()`
- Call smoke preflight **after** successful merge (not inside stale-target function)

**What breaks if wrong:**
- If parent integration branch is stale (advanced since child dispatch), merge could fail with "cannot resolve conflict"
- Symptom: merge.py logs "merge failed: target branch has new commits; please rebase"
- Fix: Ensure git fetch is called unconditionally before merge

**Effort:** 1–2 hours (refactor call sites, extract smoke, test)

---

### Proposal 2: **Simplify Foundation Amendment Dependent Resolution**

**What to change:**
- In `_settle_contract_amendment_dependents()` L642–685, the logic at L656–679 re-enqueues blocked children for merge retry
- This is called every time an amendment finishes (L650); it's event-driven, not loop-driven

**What replaces it:**
- **Already optimal.** No loop to remove. Event-driven re-enqueue is the correct model.

**Recommendation:** NO CHANGE.

---

## 6. Critical Bugs Spotted

### Bug 1: **`_repair_stale_target_and_retry_merge()` Embeds Smoke Preflight But Only When Called with `run_smoke_preflight=True`**

**Severity:** MEDIUM (conditional logic is fragile)

**Location:** L1923–1982 repair.py

**Issue:** Smoke preflight runs only if the parameter is True (L1669 merge.py passes it for merge-conflict recovery). But if called from other paths (L2068, L2146 merge.py), `run_smoke_preflight` defaults to False, skipping the preflight.

**Impact:** 
- Merge-conflict recovery: smoke runs (good)
- Stale-target fallback: smoke skipped (bad — may miss out-of-scope failures)
- Integration merge: smoke skipped (bad)

**Fix:** Either always run smoke after merge, or explicitly pass the flag at all call sites.

**Evidence:** L1669 merge.py: `run_smoke_preflight=True` (only here); L1656 and L2068 don't pass it, defaulting to False.

---

### Bug 2: **`_contract_amendment_attempt_count()` Can Silently Fail and Return 0 if Metadata is Malformed**

**Severity:** LOW (defensive; causes conservative retry behavior)

**Location:** L130–138 repair.py

**Issue:**
```python
def _contract_amendment_attempt_count(task: dict[str, Any], contract_path: str) -> int:
    attempts = task.get("contract_amendment_attempts")
    if not isinstance(attempts, dict):
        return 0
    key = _contract_amendment_attempt_key(contract_path)
    try:
        return int(attempts.get(key, 0))
    except (TypeError, ValueError):
        return 0
```

If the metadata dict is corrupted, it silently returns 0, making the retry counter reset. Then the amendment loop restarts (up to `MAX_CONTRACT_AMENDMENT_ATTEMPTS` again).

**Impact:** Amendment could retry extra times if metadata is corrupted. Low probability; defensive coding works.

**Fix:** Log a warning when fallback to 0 is triggered.

---

### Bug 3: **Stale Target Repair Calls `_repair_child_upward_merge_gate_once()` Which Itself Runs an Agent**

**Severity:** LOW (architectural confusion)

**Location:** L1838 repair.py calls L1641 (which is `_repair_child_upward_merge_gate_once()` L1641–1700)

**Issue:** The name "stale target retry" suggests a lightweight re-fetch-and-merge. But it actually calls a full repair agent (L1671 calls `_run_child_verify_repair_packet()` which runs a Lead agent). This is expensive (~200–300s per attempt).

**Impact:** Confusing diagnostic logs. Operators think stale-target is cheap; it's not.

**Fix:** Rename to `_repair_child_upward_merge_after_conflict()` and document that it runs an agent.

---

## Summary

### Function Map
- 7 loops identified; all live in repair.py
- Loop 1 & 2 use shared function (`_schedule_foundation_contract_amendment()`)
- Loop 6 name is misleading (not a retry loop; single-pass with embedded repair agent)
- Loop 7 is a liveness coroutine (heartbeat; not removal candidate)

### Overlaps
- Loops 3 & 5 can fire in sequence (conflict repair → smoke repair)
- Loop 6 optionally embeds smoke preflight (conditional on `run_smoke_preflight=True`)
- No compound retry multiplication; each loop gates the next

### Stale Target Removal
- **Prior audit claim is incorrect:** Function does NOT retry 3 times. Single-pass only.
- **Proposal is VALID:** Remove function; add unconditional `git fetch` before merge.
- **Risk: LOW.** Fresh-fetch is orthogonal to conflict resolution.

### Safe Collapse Candidates
1. **Remove Loop 6** (stale target) — **IMPLEMENT** (~1–2 hours, ~330 LOC saved)
2. **Simplify smoke preflight routing** — extract from stale-target into standalone coroutine
3. **Skip other merges:** Loops 1&2 already merged; Loops 3&5 serve different purposes

### Critical Bugs
1. **Smoke preflight only runs conditionally** (L1923) — should always run after merge
2. **Amendment retry counter can silently reset** (L130) — add logging
3. **Stale target repair name is misleading** — it's not cheap; it runs an agent

---

**Recommended Next Step:** Implement Proposal 1 (remove stale-target function). Estimated effort: 1–2 hours. Estimated savings: ~330 LOC of repair.py. Risk: LOW (fresh-fetch is always safe).
