# Research: S2 Amendment Retry Race and Recovery

## Current state

- Amendment-blocked leaves are persisted by `set_contract_amendment_blocked()` in `otto/queue/task_graph.py`. It clears the prior terminal verdict, sets `blocked_pending_contract_amendment=true`, stores `blocked_on_task_id`, clears retry flags, and may preserve `contract_amendment_merge_context`.
- When an amendment task passes, `_settle_contract_amendment_dependents()` calls `clear_contract_amendment_blocked_tasks()`, which clears the blocker and sets `contract_amendment_retry_merge=true`, then re-enqueues the original leaf for merge-only retry.
- `take_ready()` in `otto/queue/subtask.py` skips tasks whose graph entry has `contract_amendment_retry_in_progress=true`, but otherwise returns retry-merge leaves like ordinary pending entries, reconciled with graph state.
- `_run_child()` handles `contract_amendment_retry_merge` by skipping Lead dispatch and calling `_merge_child_branch()` with a reconstructed `LeadResult`. Before this fix it called `mark_contract_amendment_retry_in_progress()` but ignored the returned bool.
- `_locked_graph()` in `otto/queue/task_graph.py` is the existing cross-process mutation point. It uses an exclusive `fcntl.flock`, reads `task_graph.json`, lets the caller mutate, writes a temp file, and atomically replaces the graph.

## Bugs fixed

- Second-runner race: two processes could both receive the same ready retry leaf before either wrote `contract_amendment_retry_in_progress`. Since `_run_child()` ignored the mark result and the mark function did not reject already-in-progress, both could merge.
- Crash/restart deadlock: once a retry was marked in-progress, a crashed owner left the leaf skipped forever by `take_ready()`. There was no bounded recovery path from durable context.

## Constraints

- Stay S2-only. Do not edit `tests/test_v5_phase2.py`, `tests/test_ownership_primitive_s0.py`, or `tests/test_ownership_s1_isolation_gate.py`.
- Preserve R1 fixes: amendment crash settlement, bound contract writes, and bounded amendment churn.
- Recovery must never clear into ordinary Lead dispatch. It must use `contract_amendment_merge_context` and `contract_amendment_retry_merge`, or terminalize honestly as `merge_blocked`.
- Total retry work must be bounded and compose with the existing `MAX_CONTRACT_AMENDMENT_ATTEMPTS` cap.

## Implementation

- `mark_contract_amendment_retry_in_progress()` is now a graph-lock CAS. It returns `False` without semantic mutation when the task is blocked, terminal, not retry-merge, already claimed by a live owner, or claim attempts are exhausted.
- Winning claims persist owner token, pid, host, started timestamp, heartbeat timestamp, claim count, max claim count, and a retry claim marker in `contract_amendment_merge_context`.
- Existing in-progress retries are stale only if their same-host owner pid is gone, or their heartbeat/start timestamp exceeds the bounded timeout. A stale retry with remaining claim budget can be re-claimed under the same lock.
- Exhausted stale retries terminalize through a task-graph helper as structured `merge_blocked` without ordinary dispatch.
- `take_ready()` keeps live in-progress retries globally non-runnable, but lets stale retry-merge entries surface to `_run_child()` for merge-only CAS recovery.
- `_run_child()` consumes a `False` claim by returning before `_merge_child_branch()` unless the stale-exhausted helper terminalized the task.

## Verification targets

- New regression: two concurrent/duplicate `_run_child()` attempts against the same ready retry produce exactly one merge call; the loser returns without merging.
- New regression: stale in-progress retry is not treated as ordinary work, but a fresh `_run_child()` can reclaim and restore pass after merge, or bounded-exhaust to structured `merge_blocked`.
- Existing S2 restart-window and durable-verdict tests still pass.
