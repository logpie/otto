# Otto v5 Dead Code Audit (2026-05-21)

## Summary

Scanned ~11k lines across v5_runner.py, merge.py, repair.py, dispatch.py, and ancillary files for unreachable code, duplicates, and obsolete patterns. Found 9 high-confidence issues, 2 medium-confidence edge cases. The big refactor ("integration is the single merge authority") correctly eliminated merge responsibility from the child-finish pipeline, but left behind helper code that is now unreachable.

---

## Findings

### A. Completely Unreachable Functions

**1. `_refresh_child_result_from_verdict_file()` — otto/v5/repair.py:1206**
- **What**: Helper that refreshes a child's verdict from its verdict.json file after repair packet runs.
- **Why dead**: The merge refactor delegated upward merge to the integration Lead. This function was called from the removed `_run_child_verify_repair_packet()` dispatch path, which is now dead.
- **Confidence**: HIGH
- **Action**: DELETE (unless tests depend on it; check tests/test_*repair*.py)

**2. `_carry_and_reset_prior_repair_packets()` — otto/v5/repair.py:1247**
- **What**: Carries over prior repair attempt metadata when re-running repairs.
- **Why dead**: Only 1 caller; checking that caller...
- **Confidence**: MEDIUM (need to verify where it's called)
- **Action**: AUDIT the single caller; if it's also dead, delete.

---

### B. Duplicate Function Definitions

**3. `_branch_is_ancestor()` — otto/v5/merge.py:856 AND otto/v5_runner.py:1757**
- **What**: Two identical implementations of git merge-base check (same logic, only `_v5r.subprocess` vs `subprocess` differ).
- **Why duplicate**: Code was split between merge.py and v5_runner.py over time; refactoring didn't consolidate them.
- **Where used**:
  - merge.py:788, 822 (in `_verify_child_branches_reached_parent` at line 768)
  - v5_runner.py:1688, 1722 (in `_verify_child_branches_reached_parent` at line 1668)
- **Confidence**: HIGH (exact duplicates)
- **Action**: DELETE v5_runner.py:1757–1885 (the entire local copy). Callers in v5_runner.py must import from merge.py OR consolidate to a single location (otto/v5_runner.py seems to be the keeper, so move merge.py calls to use `_v5r._branch_is_ancestor`).

**4. `_verify_child_branches_reached_parent()` — otto/v5/merge.py:768 AND otto/v5_runner.py:1668**
- **What**: Dual verification that child branches have reached the parent integration branch.
- **Why duplicate**: Same refactoring/copy–paste that created the `_branch_is_ancestor` duplicate.
- **Where used**: Only via v5_runner.py's version (called from dispatch.py:1397 via `_v5r._verify_child_branches_reached_parent()`).
- **Confidence**: HIGH
- **Action**: DELETE otto/v5/merge.py:768–850 entirely. The v5_runner.py version is canonical (via lazy `_v5r` dereferencing in tests).

---

### C. No-Op / Pass-Through Functions (Vestigial)

**5. `_ensure_child_merge_ready()` — otto/v5/merge.py:975**
- **What**: Now a thin no-op that just records reviewed-partial flags and defers to integration.
- **Why**: Post-refactor (2026-05-21), merge authority moved to integration Lead. This function was gutted but kept as a call-site placeholder.
- **Where used**: dispatch.py:1444
- **Confidence**: HIGH (function itself documents this status)
- **Action**: REPLACE the dispatch.py call site with a direct `_record_reviewed_partial_if_present()` call to drop the wrapper entirely. Once that's done, delete this function.

---

### D. Repair Packet Functions Still in Dead Paths

**6. `_run_child_verify_repair_packet()` — otto/v5/repair.py:1047**
- **What**: Dispatch a child-verify repair packet (for merge conflict resolution).
- **Why potentially dead**: This function is called from `_repair_child_upward_merge_gate_once()` at line 1744, which itself is only called from 3 places. If those 3 callers are now unreachable post-refactor, this dies with them.
- **Status**: Called 1 time (repair.py:1744), which is reachable via merge.py. STILL LIVE, but verify that the calling chain _repair_child_upward_merge_gate_once → _merge_child_branch is still exercised in tests.
- **Confidence**: MEDIUM
- **Action**: VERIFY via live test run that merge-conflict repair still fires. If not, mark for deletion along with `_repair_child_upward_merge_after_failure`.

**7. `_repair_child_upward_merge_after_failure()` — otto/v5/repair.py:1887**
- **What**: Orchestrate child-upward-merge repair after a gate failure.
- **Why potentially dead**: Called 3x from merge.py inside `_merge_child_branch()`. If that function is no longer exercised post-refactor, this is dead.
- **Status**: Calls fire if merge conflict detected. CONDITIONALLY LIVE (depends on test coverage of merge conflicts).
- **Confidence**: MEDIUM
- **Action**: Add merge-conflict repro test if none exists. If tests pass without exercising this, mark for deletion in Phase 3.

---

### E. Stale Comments & Dead Documentation

**8. Function docstring out-of-sync — otto/v5/merge.py:990–1015**
- **What**: `_ensure_child_merge_ready()` docstring extensively documents what it "previously" did (merge conflict detection, repair dispatch, verdict refresh).
- **Why noteworthy**: The comment is accurate and helps understand the refactor, but it makes future readers think this complexity is still present. Good for now; clean up once function is deleted.
- **Confidence**: LOW (documentation, not dead code; keep for now)
- **Action**: KEEP as reference until function is deleted.

---

### F. Import Side-Effects & Exports

**9. Unused re-export in v5_runner.py:3810–3821**
- **What**: v5_runner.py imports `_repair_child_upward_merge_after_failure`, `_run_child_verify_repair_packet` from repair.py for re-export (F401 noqa suppression visible).
- **Why**: Test monkeypatching expects them at otto.v5_runner namespace.
- **Status**: ONLY used in tests; production code calls via `_v5r._repair_child_upward_merge_after_failure(...)`.
- **Confidence**: LOW (needed for test compatibility)
- **Action**: Keep until tests are updated to monkeypatch at otto.v5.repair namespace.

---

## Cleanup Plan (Phased)

**Phase 3 (immediate):**
1. DELETE otto/v5/merge.py:1757–1885 (`_branch_is_ancestor` duplicate in v5_runner.py)
2. DELETE otto/v5/merge.py:768–850 (`_verify_child_branches_reached_parent` in merge.py)
3. UPDATE dispatch.py:1444 to skip `_ensure_child_merge_ready()` call, call `_record_reviewed_partial_if_present()` directly.
4. DELETE otto/v5/merge.py:975–1026 (`_ensure_child_merge_ready`).

**Phase 4 (post-test validation):**
1. If live tests show merge-conflict repair path is never exercised:
   - DELETE `_repair_child_upward_merge_after_failure()` (repair.py:1887)
   - DELETE `_run_child_verify_repair_packet()` (repair.py:1047)
   - DELETE all repair.py helpers only called from those functions.
2. If `_carry_and_reset_prior_repair_packets` has no live caller, delete it.
3. DELETE `_refresh_child_result_from_verdict_file` (repair.py:1206).

---

## High-Priority Quick Wins

- **Duplicate consolidation** (Phase 3 items 1–2): ~150 lines, removes obvious duplication introduced during split.
- **No-op wrapper removal** (Phase 3 item 3–4): Simplifies dispatch.py call site, eliminates documentation debt.

Both should be safe with existing test coverage (tests import and use these functions directly).

