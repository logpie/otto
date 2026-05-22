# Otto v5 Snapshot-Staleness Audit

**Date:** 2026-05-21
**Scope:** Read-only investigation of snapshot-staleness patterns in v5_runner.py, v5/dispatch.py, v5/merge.py, v5/repair.py
**Budget:** 25 minutes / 30-40 file reads
**Findings:** 7 high-confidence patterns identified

---

## Finding 1: Architect Scaffold Oracle Decision on Stale Verdict

**Location:** `otto/v5/dispatch.py:256-323` (dispatch loop, architect preflight)

**Pattern Type:** A (Capture-then-decide-then-dispatch)

**What's Captured / When:**
- Line 256: `tasks = (graph.get("tasks") or {})` — reads task graph snapshot from disk
- Line 260: Checks `architect_task.get("verdict") == VERDICT_PASS` — architect verdict is frozen at snapshot time
- Line 264: `retry_count = get_retry_count(project_dir, architect_tid)` — retry attempt count captured

**What's Decided / When:**
- Line 268: Deduplicates oracle run on `(architect_tid, retry_count)` pair
- Line 270: Dispatches `_v5r.verify_from_clean_oracle()` if not yet deduplicated for this retry_count
- Lines 282-316: Makes repair/partial verdict decision based on scaffold oracle result

**Time Window:** T1=graph read (line 256) → T2=oracle dispatch (line 270). Intermediate: architect preflight deduplication check, multiple condition tests (~50-200ms)

**Risk if Stale:**
- If architect verdict changed from `VERDICT_PASS` to something else between snapshot-read and decision, the condition fails and no scaffold probe runs
- Conversely: if another parallel worker marked the architect `pass` AFTER our snapshot read but BEFORE we check, we run the oracle twice (minor cost waste, not correctness)
- More serious: if the scaffold oracle found failures and repair passed, but the architect task was re-entered by another scheduler round, retry_count may have incremented. The old retry_count key in `architect_preflight_done` becomes orphaned; the same oracle runs again on the new retry_count

**Agentic Fix:**
- Oracle result carries its own validation timestamp; agent can re-check if stale (pass, but scaffold now fails)
- Orchestrator could re-read architect verdict immediately before oracle dispatch; cost is 1 disk read per architect

---

## Finding 2: Budget Cap Check Snapshot → In-Flight Drain Stale

**Location:** `otto/v5/dispatch.py:158-210` (dispatch loop, budget cap gate)

**Pattern Type:** A (Capture-then-decide-then-dispatch)

**What's Captured / When:**
- Line 158: `tree_total_cost(project_dir, _v5r.ROOT_TASK_ID) > tree_budget_usd` — total cost snapshot read once per loop

**What's Decided / When:**
- Lines 166-210: If budget exceeded, drain in-flight tasks and mark them `merge_blocked`
- Drain happens via `asyncio.gather()` on lines 168-171, which can take seconds (agent tasks still running)

**Time Window:** T1=budget check (line 158) → T2=drain complete (line 210). Intermediate: in-flight task draining (seconds to minutes depending on agent timeouts)

**Risk if Stale:**
- Cost can change while draining: child agents complete, final costs are recorded AFTER the snapshot budget check
- If a child finishes and writes its final cost during drain, it may push tree cost FURTHER over budget, but we've already committed to draining all in-flight
- Minor: child that finished legitimately BEFORE budget check gets marked `merge_blocked` in the drain loop (line 191), even though it had a real result. The drain result loop at line 176 checks `if not isinstance(drain_result, BaseException): continue`, so successful completions are skipped. **This is safe.**

**Agentic Fix:**
- Drain loop can re-snapshot total cost before final verdict assignment; if under budget, revert the drain decision
- Simpler: accept stale cost—orchestrator is conservative (stops early vs. overshooting), cost snapshot is single point-in-time, not real-time

---

## Finding 3: Architect Task Verdict Snapshot Passed to Repair Packet

**Location:** `otto/v5/dispatch.py:318-331` (scaffold repair dispatch)

**Pattern Type:** C (Snapshot-passed-to-agent)

**What's Captured / When:**
- Line 256: Task graph snapshot read
- Line 260, 321: Architect's verdict pulled from snapshot `architect_task.get("verdict")`
- Line 321: LeadResult constructed with snapshot verdict: `verdict=str(architect_task.get("verdict") or VERDICT_PARTIAL)`
- Lines 323-330: Snapshot verdict passed to repair agent in `architect_result` parameter

**What's Decided / When:**
- Repair agent receives the architect's old verdict from the snapshot
- Agent may use this to construct its repair intent ("architect was pass but scaffold broke, let me fix")
- Meanwhile, parallel dispatch loop could re-enter the architect → verdict changes on disk

**Time Window:** T1=snapshot (line 256) → T2=repair packet written and agent dispatched (line 323). Intermediate: oracle check (line 270, ~30-60s), dedup logic, repair packet construction (~100-500ms)

**Risk if Stale:**
- Repair agent's intent mentions "architect was PASS", but by agent run-time, architect is actually `partial` or `merge_blocked` (re-entered)
- Agent's self-check might be confused if it reads task graph live and sees a different verdict than what the packet said
- Low severity: agent's repair is about the SCAFFOLD, not the architect decision itself; verdict mismatch is noise

**Agentic Fix:**
- Repair packet can include both snapshot verdict AND the timestamp/git-ref of the snapshot; agent can self-check if stale
- Safer: pass architect's current git state / build branch, not verdict (verbs are stable, verdicts flip)

---

## Finding 4: Pre-merge Foundation Contract Snapshot Used After Long-Running Child Build

**Location:** `otto/v5/merge.py:1390-1456` (child merge flow)

**Pattern Type:** A (Capture-then-decide-then-dispatch) + E (Existence/membership checks before action)

**What's Captured / When:**
- Line 1390: `worktree_contract_violation = _v5r._foundation_contract_write_feedback(...)` — reads parent's foundation contracts from disk
- Lines 1394: `changed_paths=_v5r._git_diff_name_only(child_worktree)` — diff captured from child's worktree
- Line 1397: `commit_worktree()` called — long-running git operation (seconds)
- Line 1435: Pre-merge ref captured: `pre_merge_ref = _v5r._git_capture(project_dir, ["rev-parse", parent_integration_branch])`
- Line 1436-1440: Fresh diff computed between pre_merge_ref and source_branch, contracts re-checked

**What's Decided / When:**
- Lines 1420-1434: If worktree contract violation found, child merge is blocked (annotation-only, but decision made on snapshot)
- Line 1435: After commit, parent branch ref is snapshotted
- Lines 1436-1442: Branch delta contracts re-checked AFTER commit but BEFORE merge

**Time Window:** Multiple snapshots at different intervals:
  1. Pre-commit (line 1390) → commit (line 1397): ~1-5s (changed_paths stale during commit)
  2. Pre-merge-ref (line 1435) → merge (line 1458): ~100-200ms
  3. Between contract reads (line 1390 → line 1436): ~5-10s

**Risk if Stale:**
- Pre-commit snapshot: child edits during commit could race; stale by the time commit completes
- Pre-merge-ref: parent's integration branch could move if another child merged in parallel
- If parent integration branch advanced (another child merged), pre_merge_ref is stale; the branch delta (source_branch → pre_merge_ref) doesn't include the freshly-merged code, so diff misses contracts violated by the other child
- Bug: if child A and child B both write to `src/shared.ts`, and child A merges first, child B's pre-merge-ref doesn't include child A's changes. Child B's branch delta looks clean (no overlap with parent contracts), but the actual merge will be the UNION of both children's changes

**Agentic Fix:**
- Re-snapshot parent branch ref immediately before merge (line 1457), not 100ms earlier
- Current code is CLOSE to correct: pre_merge_ref is captured after commit but immediately before merge. Risk is low, but contract delta is not the true merge delta

---

## Finding 5: Integration Packet Child Results Snapshot Stale During Long Agent Session

**Location:** `otto/v5/dispatch.py:1757-1805` (integration packet construction)

**Pattern Type:** C (Snapshot-passed-to-agent)

**What's Captured / When:**
- Lines 1762-1765: Parameters `child_results` and `integration_results` dicts passed to `_write_integration_packet()`
- Lines 1772-1774: For each child, snapshots are extracted from these dicts
- Line 1773: `entry = get_task(project_dir, cid) or {}` — additional task graph snapshot
- Line 1774: `result = integration_results.get(cid) or child_results.get(cid)` — verdict snapshots from memory dicts passed in

**What's Decided / When:**
- Lines 1777-1805: Packet is written with child verdicts, verify_results, branch names
- Agent is then dispatched (line 1695) with packet pointing to these verdicts
- Agent reads packet, performs integration work based on child verdicts

**Time Window:** T1=packet write (line 1757) → T2=agent dispatch and execution (line 1695+). Intermediate: packet file write, agent startup, agent work (minutes)

**Risk if Stale:**
- Child verdicts in packet become stale if child is re-attempted or repair runs while integration agent is running
- Integration agent's Step 0 / Step 0b (merge children) may see children in different state than packet claimed
- Low severity: agent re-checks verdict on each child before merging (agent is defensive); stale packet doesn't block, just outdates the agent's notes

**Agentic Fix:**
- Packet can carry "packet_timestamp" / git state hash so agent knows it's stale
- Safer: agent always re-reads child verdicts from disk, doesn't trust packet (current practice)

---

## Finding 6: Ready Task List Snapshot → Dispatch After Long Preflight Checks

**Location:** `otto/v5/dispatch.py:231-249` (dispatch loop, ready task discovery)

**Pattern Type:** A (Capture-then-decide-then-dispatch)

**What's Captured / When:**
- Line 232: `active_task_ids = await dispatch_lease.active_task_ids()` — snapshot of currently-active tasks
- Line 233: `ready = take_ready(project_dir, completed_task_ids=completed, in_flight_task_ids=set(in_flight.keys()) | active_task_ids)` — ready list computed from snapshot
- Lines 239-249: Ready list is filtered by descendants and blocking preflight issues

**What's Decided / When:**
- Line 885-898 (dispatch loop): Ready tasks are spawned up to max_parallel
- Lines 251-630: Intermediate: architect preflight (scaffold oracle, repairs, toolchain checks) — can take **30-120 seconds**
- Line 632: Graph re-read for scheduler feedback, but ready list is unchanged

**Time Window:** T1=ready list snapshot (line 233) → T2=dispatch (line 889). Intermediate: architect preflight (30-120s), foundation scheduler checks (~1s)

**Risk if Stale:**
- A ready task might have been dispatched by a parallel runner during the preflight checks
- Another scheduler might have marked a task not-ready (dependency fail) during preflight
- Ready list is not re-checked before dispatch; if a task was marked completed/failed between snapshot and dispatch, we dispatch it anyway
- Current safeguard: dispatch loop checks `await dispatch_lease.try_acquire(tid)` (line 887), which is exclusive. If task is already acquired by another runner, acquire fails and dispatch is skipped. **This is safe.**

**Agentic Fix:**
- None needed; dispatch lease provides mutual exclusion
- Could re-snapshot ready list after architect preflight (line 632) before spawning, but cost is high

---

## Finding 7: Retry Count Snapshot in Contract Amendment Retry Loop

**Location:** `otto/v5/dispatch.py:264-267` (architect preflight dedup)

**Pattern Type:** D (Counter-based decisions on shared state)

**What's Captured / When:**
- Line 264: `retry_count = get_retry_count(project_dir, architect_tid)` — retry count snapshot read from task metadata
- Line 265: Dedup key formed: `preflight_key = (architect_tid, retry_count)`
- Line 266: Check if already seen: `if preflight_key in architect_preflight_done: continue`

**What's Decided / When:**
- If dedup key is new, architect preflight oracle runs (line 270)
- If architect is re-entered (contract amendment), retry_count increments
- On next dispatch loop iteration, retry_count may have changed, but `architect_preflight_done` set is not cleared

**Time Window:** T1=retry_count read (line 264) → T2=oracle dedup check (line 266). Intermediate: none (~10ms). But across loop iterations: oracle runs at T1, task graph updates at T2, loop continues, retry_count may have incremented, but dedup set persists

**Risk if Stale:**
- Architect is re-entered with a contract amendment → retry_count increments on disk
- Next dispatch loop iteration reads the NEW retry_count (line 264)
- Dedup key changes, so architect oracle runs AGAIN, even though it was just run 100ms ago
- Result: duplicate oracle runs, wasted time
- Partial fix applied: dedup set is per-loop, reset each iteration (no persistence across runs)

**Agentic Fix:**
- Dedup could include a timestamp or git-ref hash, not just retry count
- Safer: track which architect task + scaffold state was last probed; if scaffold hasn't changed (same git SHA), skip re-probe

---

## Summary

| Finding | File | Pattern | Risk | Severity |
|---------|------|---------|------|----------|
| 1 | dispatch.py:256-323 | A | Architect verdict changes between snapshot and oracle dispatch | Low |
| 2 | dispatch.py:158-210 | A | Cost changes during in-flight drain; stale budget check | Very Low |
| 3 | dispatch.py:318-331 | C | Repair agent receives stale architect verdict in packet | Very Low |
| 4 | merge.py:1390-1456 | A+E | Parent branch contract check stale during child commit/merge | Medium |
| 5 | dispatch.py:1757-1805 | C | Integration packet carries stale child verdicts | Low |
| 6 | dispatch.py:231-249 | A | Ready task list stale but mitigated by dispatch lease | None (safe) |
| 7 | dispatch.py:264-267 | D | Duplicate architect oracle runs due to stale retry_count | Low |

---

## Recommendations

1. **Highest priority (Finding 4):** In `_merge_child_branch`, re-snapshot `parent_integration_branch` ref immediately before merge (line 1457 area), not 100+ms earlier. Small cost, high confidence gain.

2. **Medium priority:** Architect retry_count dedup (Finding 7) could track scaffold git hash instead of retry count, eliminating duplicate oracle runs on re-entry.

3. **Lower priority:** Findings 1, 3, 5 are observability/robustness improvements, not correctness bugs. Packets/verdicts can carry timestamps for agent self-checks.

4. **Safe-by-design (Finding 6):** Dispatch lease already prevents concurrent dispatch of same task. No action needed.

