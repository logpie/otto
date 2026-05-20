# Plan: S2 Amendment Retry Atomic Claim and Stale Recovery

## Step 1: Graph-lock CAS claim

Change `mark_contract_amendment_retry_in_progress()` so the existing `_locked_graph()` `fcntl` lock is the only mutation point for retry ownership. A valid claim requires: task exists, not blocked, not terminal, `contract_amendment_retry_merge=true`, and no live in-progress owner. Winning claims persist owner/pid/host, started/heartbeat timestamps, claim attempt count, max attempts, and an updated merge context marker. Losing claims return `False` without writing.

Why: the second-runner race is cross-process, so the compare-and-set has to live in the durable task graph lock, not in a process-local runner lease.

Verify: duplicate calls to `_run_child()` for the same ready retry cause one `_merge_child_branch()` call and one skipped claim.

## Step 2: Stale retry detection and bounded terminalization

Add a reusable stale predicate in `task_graph.py`: an in-progress retry is stale when its same-host pid no longer exists, or its heartbeat/start timestamp is older than the bounded timeout. Stale retries with remaining claim budget can be reclaimed. Stale retries whose claim budget is exhausted are terminalized by a new graph helper as structured `merge_blocked`.

Why: a crash after claim must be recoverable from durable graph state, but repeated crash/restart must not spin forever.

Verify: a task with stale in-progress metadata is not returned as a normal runnable leaf after terminal exhaustion; it becomes `merge_blocked` with structured reason and retry flags cleared.

## Step 3: `take_ready()` recovery aperture

Keep live in-progress retry leaves globally non-runnable. Let only stale `contract_amendment_retry_merge` entries pass through to `_run_child()` so they resume the merge-only branch using durable context and are never dispatched to Lead.

Why: `take_ready()` is the scheduler gate; it must avoid the deadlock without making stale retries ordinary work.

Verify: the stale regression asserts the task entry still has retry-merge state before `_run_child()`, and no ordinary dispatch hook is used.

## Step 4: Runner consumption of claim result

In `_run_child()`, check the CAS return. If `False`, emit a skip event and return a non-terminal result without merge. If the task was terminalized by stale exhaustion, return a `merge_blocked` result reflecting the graph state. Only `True` proceeds to `_merge_child_branch()`.

Why: the CAS only prevents double merge if losers actually stop.

Verify: the loser in the concurrent claim regression does not call `_merge_child_branch()` and does not terminalize the task.

## Plan Gate Trail

- Requested `/codex-gate` Plan Gate per local instructions, but no Codex MCP `/codex-gate` tool is exposed in this session. Proceeding with the documented plan because the user requested a direct S2 fix and approval policy is non-interactive.
