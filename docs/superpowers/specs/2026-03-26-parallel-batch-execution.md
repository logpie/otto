# Parallel Batch Execution

## Problem

All tasks run serially even when independent. 5 tasks = 30+ min. Tasks within the same batch have no dependencies — they could run simultaneously.

## Concept

```
Batch 1: [task A, task B, task C]  ← independent → run in PARALLEL
          ↓ all verified, then merge serially
Batch 2: [task D]                  ← depends on A+B → runs AFTER batch 1
```

Within-batch = parallel (tasks are independent).
Cross-batch = serial (later batches depend on earlier results).

## Architecture

### Two-path model

```
repo_root    = /project/              ← main checkout, untouched during run
task_work_dir = /project/.otto-worktrees/otto-task-{key}/  ← per-task worktree
```

**Every function that touches files must accept both paths.**
`repo_root` for git operations, log writes, config reads.
`task_work_dir` for coding agent CWD, tests, QA, spec gen.

### Task state machine

```
pending → running → verified → merge_pending → passed
                  ↘ failed (coding/test/QA)    ↘ merge_failed
```

New states:
- `verified` — coding + test + QA all passed, candidate ready
- `merge_pending` — waiting for serial merge phase
- `merge_failed` — rebase conflict or post-rebase test failure

Tasks only become `passed` after merge succeeds. Display, telemetry, and summaries must handle the new states.

### Worktree lifecycle

```
1. git worktree add .otto-worktrees/otto-task-{key} {base_sha} --detach
2. Task runs in worktree (coding → test → QA)
3. Candidate commit anchored as ref
4. git worktree remove --force .otto-worktrees/otto-task-{key}
5. git worktree prune  (cleanup metadata)
```

Never delete a branch while its worktree exists. Use detached HEAD + anchored refs instead of task branches in worktrees.

### Serial merge phase

After all parallel tasks complete:

```
For each verified task (in task ID order):
  1. Create temp branch from current main HEAD
  2. Cherry-pick/rebase candidate ref onto temp branch
  3. If conflict → mark merge_failed (retry in next session or re-run)
  4. Run test suite on rebased result (post-rebase verification)
  5. If tests fail → mark merge_failed
  6. Fast-forward main to temp branch
  7. Mark task passed
  8. Delete temp branch
```

Post-rebase verification is required — a clean rebase doesn't guarantee correctness.

### Concurrency

```yaml
# otto.yaml
max_parallel: 3    # 0 or 1 = serial (current behavior, default)
```

Default is 1 (serial) — parallel is opt-in. Users must understand cost implications.

Orchestrator uses `asyncio.Semaphore(max_parallel)` to limit concurrent tasks.

### Display

One orchestrator-owned display, not per-task Rich Live:

```
  Batch 1  3 tasks (parallel)

  ● #1  Add heat index...        coding · 45s · $0.32
  ● #2  Fix humidity null...     test · 22s
  ✓ #3  Add wind chill...        verified · 3m · $0.75

  Merging...
  ✓ #3 merged
  ✓ #1 merged (rebased onto #3)
  ✗ #2 merge conflict — will retry on updated main
```

Per-task status uses `otto_logs/{key}/live-state.json` (not one shared file).
Orchestrator aggregates per-task files for the combined display.

### Concurrency-safe state

- `tasks.yaml` updates use file lock + run ownership token
- Each task gets its own `live-state.json` under `otto_logs/{key}/`
- Telemetry JSONL writes serialized through orchestrator (not per-task)
- Spec agent log scoped to `otto_logs/{key}/spec-agent.log` (not global)
- `.otto-worktrees/` added to otto-owned path filters + `.git/info/exclude`

## Implementation phases

### Phase 1: Foundations (prep for parallelism)

No parallel execution yet. Just fix the assumptions that block it.

1. **Split `repo_root` / `task_work_dir`** — thread both through runner, QA, spec, display
2. **Add `verified` and `merge_pending` task states** — update state machine, display, status
3. **Per-task live-state.json** — move from global to `otto_logs/{key}/live-state.json`
4. **Per-task spec log** — scope `spec-agent.log` to task directory
5. **Add `.otto-worktrees/` to otto-owned paths** — git_ops, .git/info/exclude
6. **Worktree-safe branch handling** — detached HEAD + anchored refs, no branch deletion while worktree exists
7. **Task state ownership token** — prevent concurrent updates from clobbering

All tests must pass. Serial execution still works identically.

### Phase 2: Parallel execution

1. **`create_task_worktree()` / `cleanup_task_worktree()`** — proper `git worktree add/remove/prune`
2. **`asyncio.gather` in orchestrator** — semaphore-limited parallel task execution
3. **Orchestrator-owned display** — aggregate per-task progress into one view
4. **`max_parallel` config** — default 1 (serial), user opts in

### Phase 3: Serial merge phase

1. **`merge_parallel_results()`** — cherry-pick/rebase verified candidates sequentially
2. **Post-rebase verification** — run test suite after each merge
3. **Merge conflict handling** — mark `merge_failed`, report to user
4. **Disable "local pass beats worktree fail" heuristic** in merge phase

### Phase 4: Robustness

1. **Crash recovery** — per-task run manifest at `otto_logs/{key}/run-manifest.json` stores base_sha, candidate ref, worktree path, owner token, phase. Recovery reads manifest to decide resume/requeue/discard.
2. **SIGINT handling** — remove worktrees, mark tasks as interrupted
3. **Timeout per-worktree** — separate install timeout from test timeout
4. **Batch-local test policy** — each task's tests must pass on base+task alone

### Concurrency safety details

**Ownership token**: every state transition is conditional on `(expected_status, owner_token)`. Workers set owner on `running`, clear on completion. Dedicated heartbeat thread updates `run-manifest.json` every 30s via atomic write (`tmp` + `os.replace`). Stale owner (>2min no heartbeat) can be reclaimed. CLI commands (drop, revert) check ownership before mutating. Manifest includes schema version; incomplete/corrupt manifests are rejected on recovery.

**Frozen batch context**: `PipelineContext` is frozen per batch. Parallel tasks see the same learnings/observations from prior batches. Sibling learnings don't leak mid-flight. Aggregation (costs, results) happens after all tasks complete.

**Terminal output**: workers emit structured events only (no direct `console.print`). Orchestrator is the sole terminal renderer. Per-task events routed via bounded async queue (1000 items). Replaceable events (heartbeats, tool chatter) coalesced — only latest kept. Phase transitions and final results always preserved. If queue full, drop oldest replaceable events.

**Post-rebase verification**: runs from a fresh disposable worktree created from the rebased ref — not from the mutable task worktree. Verify the exact ref that will become main HEAD.

### CLI behavior for new states

| State | status | show | drop | revert | retry |
|-------|--------|------|------|--------|-------|
| pending | dim ○ | basic info | ✓ remove | n/a | n/a |
| running | cyan ● | live progress | ✗ refuse | n/a | n/a |
| verified | blue ◉ | full info, no commit yet | ✓ remove | n/a | n/a |
| merge_pending | blue ◉ "merging..." | full info | ✗ refuse | n/a | n/a |
| passed | green ✓ | full + commit SHA | ✓ remove from queue | ✓ revert commit | n/a |
| failed | red ✗ | full + error | ✓ remove | n/a | ✓ reset to pending |
| merge_failed | red ✗ "merge conflict" | full + conflict info | ✓ remove | n/a | ✓ reset to pending |

## What doesn't change

- Single-task runs (`otto run "prompt"`) — unchanged (no worktree needed)
- QA behavior per task — each gets its own QA session
- Proof-of-work — per-task, unaffected
- `max_parallel: 1` (default) — serial execution, identical to today

## Verification criteria

1. Two independent tasks run in parallel, both verify, both merge
2. Two tasks touching different files — no conflict, both merge
3. Two tasks touching same file — conflict detected, one merge_failed
4. One task fails QA, other passes — failed reported, passed merges
5. `max_parallel: 1` — falls back to serial (current behavior, default)
6. Interrupted run — worktrees cleaned up, tasks marked interrupted
7. Post-rebase test failure — task marked merge_failed, not passed
8. `otto status -w` shows multiple tasks in flight without corruption
9. `verified` state visible in `otto status` before merge
10. Spec logs and telemetry don't interleave across tasks
11. `.otto-worktrees/` never shows as dirty user state
12. Crash during merge phase — worktrees pruned on next run

## Plan Review

### Round 1 — Codex (5 CRITICAL, 6 HIGH)
- [CRITICAL] No merge_pending state — fixed: added verified/merge_pending/merge_failed states
- [CRITICAL] project_dir hardcoded everywhere — fixed: split repo_root/task_work_dir
- [CRITICAL] Branch lifecycle not worktree-safe — fixed: detached HEAD + anchored refs
- [CRITICAL] Display races on shared live-state — fixed: per-task live-state, orchestrator aggregation
- [CRITICAL] Task state not concurrency-safe — fixed: ownership token per task
- [HIGH] Shared log files — fixed: per-task spec log, serialized telemetry
- [HIGH] Single otto.lock model — fixed: orchestrator owns lock, per-task ownership
- [HIGH] .otto-worktrees not in otto-owned paths — fixed: added to filters + exclude
- [HIGH] Post-rebase verification needed — fixed: required after every merge
- [HIGH] Local-pass-beats-worktree unsafe — fixed: disabled in merge phase
- [HIGH] Resource contention on npm ci — fixed: separate install/test timeouts
- [HIGH] Batch-local test policy — fixed: each task's tests must pass on base+task alone

### Round 2 — Codex (2 HIGH, 4 MEDIUM)
- [HIGH] Ownership token underspecified — fixed: conditional transitions, heartbeat, lease expiry
- [HIGH] Crash recovery needs run manifest — fixed: per-task run-manifest.json with full state
- [MEDIUM] Batch context semantics — fixed: frozen context per batch, no sibling leakage
- [MEDIUM] Terminal output races — fixed: workers emit events only, orchestrator renders
- [MEDIUM] Post-rebase verification needs fresh worktree — fixed: verify rebased ref in disposable worktree
- [MEDIUM] CLI semantics for new states — fixed: full state × command behavior table

### Round 3 — Codex (1 HIGH, 2 MEDIUM)
- [HIGH] Heartbeat by mtime unsafe for long phases — fixed: dedicated heartbeat thread, 30s interval, 2min reclaim
- [MEDIUM] Manifest partial writes — fixed: atomic writes (tmp + os.replace), schema version, reject corrupt
- [MEDIUM] Event queue needs backpressure — fixed: bounded queue (1000), coalesce replaceable events, preserve phase transitions
