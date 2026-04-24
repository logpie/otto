# Stale Merge Row Debug

## Observations

- Live Mission Control shows `merge-1777010477-47608` as `STALE`, inactive, with no elapsed timer.
- The live record exists at `otto_logs/cross-sessions/runs/live/merge-1777010477-47608.json`.
- Its persisted status is still `running`; its writer process is gone.
- The merge state exists at `otto_logs/merge/merge-1777010477-47608/state.json`.
- The merge state is also `running`, has no `finished_at`, and contains one `merged` branch outcome.
- The merge log says the merge was clean: `all branches merged clean, no agent call needed`.
- After prior cleanup removed this live record, a later merge action recreated it.

## Hypotheses

### H1: Merge startup repair resurrects missing nonterminal live records (ROOT)

- Supports: `_repair_merge_run_records()` is called at merge startup and has a `FileNotFoundError` path that calls `_write_merge_run_record()` even when `terminal_status` is false.
- Supports: the stale row reappears after merge-related actions, not from passive web polling.
- Conflicts: none.
- Test: create a temp project with a nonterminal merge state and no live record, then call `_repair_merge_run_records()`.

### H2: Web Mission Control polling recreates merge live records

- Supports: the row is visible in the web UI.
- Conflicts: web state code reads live records and queue state; it does not call merge repair.
- Test: inspect web service state path for repair calls.

### H3: Cleanup failed to remove the live record

- Supports: stale row exists now.
- Conflicts: earlier cleanup returned `Removed live record ...`; the row later had a newer `updated_at`.
- Test: remove record, then perform no merge actions and verify it remains absent.

## Experiments

### E1: Nonterminal merge repair without live record

Command: created a temp project with `MergeState(status="running")`, one `BranchOutcome(status="merged")`, no live record, then called `_repair_merge_run_records()`.

Result: confirmed. `before_exists=False`, `after_exists=True`; the created live record had `status="running"` and a fresh writer identity.

## Root Cause

`_repair_merge_run_records()` treats a missing nonterminal merge live record as something to recreate, but missing nonterminal live records usually mean the live row was cleaned up or the writer is gone. Recreating them resurrects stale rows.

## Fix

Do not recreate missing nonterminal merge live records during repair. Terminal merge states can still be materialized/finalized for history repair, but active-looking nonterminal rows must not be resurrected without a live writer.
