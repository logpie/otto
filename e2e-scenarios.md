# E2E Scenarios — `otto queue` + `otto merge`

Scope: validate the parallel-otto branch (Phases 1–6) by running real flows
end-to-end, as a user would. Mix of cheap (fake otto_bin) and expensive
(real LLM) scenarios. Findings → `e2e-findings.md`. Fixes are committed.

## Conventions

- All scenarios run in `/tmp/otto-e2e-<name>/` with their own git repo —
  never in the parallel-otto worktree itself.
- Each scenario records: setup → action → expected outcome → actual outcome
  → observability check (what's in logs?).
- "Real otto user" means: invoke the CLI, read the printed output, look at
  files in `otto_logs/queue/` and `.otto-queue.yml`, fix problems via the
  same UX a user would have.

## Set A — Queue mechanics (cheap, fake otto_bin)

Uses `scripts/fake-otto.sh` — a stand-in that mimics the bits of `otto`
that the queue runner cares about: argv parsing, exit code, manifest write,
git commit. Lets us drive the watcher hard without LLM cost.

### A1. Single build, happy path
- enqueue one `otto queue build "X"` against an empty queue
- start watcher with `--concurrent 1`
- expected: state queued → running → done; manifest exists; branch created;
  cost recorded
- observability check: `.otto-queue.yml` shows entry, state.json mirrors it,
  `otto queue ls` shows status, `otto queue show <id>` has full detail

### A2. Three parallel builds, no dependencies
- enqueue 3 builds; start watcher with `--concurrent 3`
- expected: all spawn within first tick; complete independently; 3 worktrees
  exist, 3 branches created
- observability check: heartbeat advances, no spurious "queued" → "queued"
  state thrash

### A3. `--after` chain (A→B→C)
- enqueue A, then B `--after a`, then C `--after b`
- watcher concurrency 3, but only 1 should run at a time due to chain
- expected: B starts only after A done; C only after B done
- observability check: state.json shows blocked-by reason; ls reflects pending

### A4. Cancel a running task
- enqueue a long-running fake (sleep 30); start watcher
- after 5s, `otto queue cancel <id>`
- expected: state goes running → terminating → cancelled within ~3s
  (SIGTERM grace), child process actually dead (no zombie), worktree
  preserved
- observability check: state.json `failure_reason` set; `otto queue show`
  describes cancellation; `ps` shows no orphan

### A5. Remove a queued task
- enqueue 2 tasks; start watcher with `--concurrent 1`; remove the second
  before it dispatches
- expected: queue.yml + state.json reflect removal; watcher does not spawn
- observability check: removed entry vanishes from `otto queue ls`

### A6. Remove a running task
- enqueue + run a long fake; `otto queue rm <id>`
- expected: behaves like cancel + cleanup; child killed; final status
  `cancelled` or `removed`
- observability check: as A4, plus removal from queue.yml

### A7. Watcher already running (lock contention)
- start watcher; in another shell, start another watcher in same project
- expected: second one prints WatcherAlreadyRunning + clear next-step message,
  exits non-zero
- observability check: lock file exists with first watcher's PID; second
  doesn't clobber state

### A8. Branch slug collisions
- enqueue 3 builds with intent strings that slugify to the same prefix
  (e.g. "add csv export", "add CSV Export!", "add csv export feature")
- expected: each gets a unique branch name (hash suffix on collision)
- observability check: 3 distinct branches in `git branch`, no clobber

### A9. Watcher crash + respawn (resume)
- start watcher; let one task start; SIGKILL the watcher
- restart with `otto queue run`
- expected: orphan child detected; resume per `on_watcher_restart` policy
  (default: resume if checkpoint exists)
- observability check: state.json child entry shows pre-crash details;
  reaper handles ECHILD safely; no double-spawn

### A10. Worktree contention (same task ID)
- enqueue task; manually create worktree at expected path before watcher
  spawns it
- expected: spawn fails with "branch already checked out" error; task marked
  failed with clear reason
- observability check: state.json failure_reason explicit; user can fix
  with `git worktree remove` and re-enqueue

### A11. Large queue, limited concurrency
- enqueue 10 fast fake tasks; concurrency 3
- expected: at any time ≤3 running; all 10 complete; ordering FIFO modulo
  dependencies
- observability check: peak running count never exceeds 3 across full ls
  history

### A12. PID-reuse safety
- not directly testable without a kernel-level race, but verify
  `child_is_alive` returns False for a recycled PID (use a stub PID
  reference, simulate by manipulating start_time_ns in state.json)

### A13. Queue cleanup
- complete some tasks; `otto queue cleanup --done`
- expected: worktrees removed, branches preserved, manifests preserved
- observability check: `git worktree list` clean, `git branch` still
  shows merged branches

### A14. Watcher heartbeat staleness detection
- write a state.json with stale heartbeat; check `_watcher_alive` returns
  false; CLI prints "Worker is not running" hint
- observability check: enqueue messaging tells user to start watcher

## Set B — Merge mechanics

Hand-crafted git scenarios that exercise merge code paths without any LLM
where possible. Real LLM only where the agent actually does work.

### B1. `otto merge` with two clean branches
- create main + branch1 (touches A.py) + branch2 (touches B.py)
- run `otto merge build/branch1 build/branch2`
- expected: both merge cleanly via `git merge --no-ff`; no agent invoked;
  triage agent runs (LLM)
- observability check: orchestrator log shows "auto-merged"; no conflict
  agent log

### B2. .gitattributes union driver for intent.md
- two branches both append to intent.md
- run merge; expected: union driver concatenates both, no conflict
- observability check: intent.md has both contents in order

### B3. .gitattributes ours driver for otto.yaml
- two branches both modify otto.yaml differently
- expected: ours driver keeps target's version
- observability check: target's otto.yaml unchanged

### B4. Real conflict on application file → conflict agent invoked
- two branches both edit the same lines of app.py
- expected: orchestrator detects conflict, conflict agent invoked, agent
  resolves (real LLM), validation passes (no scope creep, no diff --check
  failure, HEAD unchanged), commit made
- observability check: conflict-agent.log written with full agent output;
  manifest path tracked

### B5. Conflict agent fails validation → retry from snapshot
- contrive a scenario where the agent likely creates an out-of-scope edit
  (or simulate via mock); verify retry-from-snapshot works
- needs careful test design — likely simulate via patched conflict_agent
  module with mocked agent that escapes scope

### B6. Conflict agent: codex provider gate
- otto.yaml has provider=codex (not claude); attempt merge with conflicts
- expected: orchestrator refuses BEFORE first merge with clear message
- observability check: no partial merges land; message names provider

### B7. `--fast` mode bails on conflict
- two-conflict scenario; `otto merge --fast`
- expected: pure git merge attempted; on first conflict, abort, print
  conflict file list, exit non-zero
- observability check: no agent invoked; intermediate state cleanly aborted

### B8. `--no-certify` skips post-merge verification
- successful merge with `--no-certify`
- expected: triage agent NOT invoked; no certifier run
- observability check: log explicitly notes skip

### B9. `--full-verify` covers full story union
- successful merge with `--full-verify`
- expected: certifier verifies all stories from all branches (no
  skip_likely_safe filtering)
- observability check: triage plan entries == union of branches' stories

### B10. `--cleanup-on-success` removes worktrees
- successful merge with this flag
- expected: worktrees removed for merged tasks; branches still exist locally
- observability check: `git worktree list` no longer shows merged tasks;
  `git branch` still shows them

### B11. `--target` alternate branch
- create `develop` branch; merge into it instead of main
- expected: merge happens on develop; main untouched
- observability check: develop has merges; main HEAD unchanged

### B12. `--post-merge-preview` collision detection
- enqueue 2 done tasks that touch overlapping files
- run `otto queue ls --post-merge-preview`
- expected: lists pairs with overlap
- observability check: output sorted, deterministic

## Set C — Real-LLM full-pipeline scenarios

Cost-controlled. Use simple intents. Each scenario is one full user
journey from `otto queue build` → `otto merge`.

### C1. Two parallel builds + clean merge
- `otto queue build "make a python calculator"`
- `otto queue build "make a python fibonacci"` (different files)
- `otto queue run --concurrent 2` until both done
- `otto merge --all`
- expected: both LLM builds succeed; merge clean; triage agent emits
  reasonable plan; certifier verifies
- observability check: full chain of logs across queue + merge dirs

### C2. Two parallel builds with conflict + agent resolution
- two builds that both touch the same file (e.g., both add a function to
  utils.py)
- expected: conflict on utils.py; conflict agent resolves; triage + cert run
- observability check: conflict-agent.log shows real agent output; merged
  utils.py contains contributions from both branches

### C3. Real `otto improve` via queue + merge
- start with a buggy app; `otto queue improve bugs`; merge result
- expected: improve runs, finds + fixes bug; merge clean
- observability check: improve logs intact in worktree; merge picks up
  improvements

## Bug-fix protocol

When something fails:
1. **Read real logs first** — `agent.log`, `state.json`, `queue.yml`,
   conflict-agent.log, etc.
2. **Reproduce minimally** — don't fix until reproducible.
3. **Diagnose root cause** — write the diagnosis in e2e-findings.md.
4. **Fix at root** — no spot-fixes.
5. **Verify fix** — re-run the scenario.
6. **Improve observability** — if the bug was hard to find, add a log
   line that would have made it obvious. Only add NEW info, no spam.
7. **Add unit test** — if the bug type could regress.

## Observability priorities

Things to check across all scenarios:
- Are state transitions logged with timestamps?
- Are spawn/cancel/reap events visible in runner logs?
- Is the failure reason on a failed task self-explanatory?
- Can a user diagnose from `otto queue show <id>` alone?
- Do orchestrator logs mention each merged branch and its outcome?
- Are conflict-agent attempts numbered + logged separately?
