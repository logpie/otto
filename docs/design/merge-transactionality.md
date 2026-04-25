# Merge Transactionality

Last updated: 2026-04-25

## Current Semantics

`otto merge` mutates the target worktree incrementally. A clean branch can be
merged and committed before a later branch conflicts, fails certification, or is
cancelled. In that case Otto records the terminal merge state and leaves the
repository in the corresponding Git state; it does not roll the target branch
back to the pre-merge SHA.

This behavior is now covered by `test_fast_mode_bails_on_conflict`, which
asserts that a successful earlier merge remains on the target when a later
branch conflicts in fast mode.

## Why Not Change It In Place

Transactional rollback is not a small local patch because merge currently owns
Git state, live run records, queue artifact graduation, history snapshots,
certification, cancellation, and conflict-agent handoff in one target worktree.
Adding rollback directly to that flow risks deleting useful conflict state,
misreporting evidence, or losing artifacts from already-merged queue tasks.

## Safer Target Design

A transactional merge should use staging:

1. Capture the target branch SHA and create a staging branch/worktree from it.
2. Run all branch merges, conflict resolution, and certification in staging.
3. Preserve the full staging evidence packet: state, logs, diffs, cert report,
   source proof-of-work links, and conflict-agent output.
4. If staging succeeds, fast-forward or atomically reset the target branch to
   the verified staging SHA while holding the merge lock.
5. Graduate queue artifacts and append history only after the target update
   succeeds.
6. If staging fails or is cancelled, remove only staging resources and keep the
   target branch at the original SHA.

The staging work should be implemented behind an explicit option/config flag
first, with tests for conflict, cert failure, cancellation, process interruption,
and artifact graduation before making it the default.
