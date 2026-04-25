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

### A11. Large queue, limited concurrency
- enqueue 10 fast fake tasks; concurrency 3
- expected: at any time ≤3 running; all 10 complete; ordering FIFO modulo
  dependencies
- observability check: peak running count never exceeds 3 across full ls
  history

### A13. Queue cleanup
- complete some tasks; `otto queue cleanup --done`
- expected: worktrees removed, branches preserved, manifests preserved
- observability check: `git worktree list` clean, `git branch` still
  shows merged branches

### A14. Watcher heartbeat staleness detection
- write a state.json with stale heartbeat; check `_watcher_alive` returns
  false; CLI prints "Worker is not running" hint
- observability check: enqueue messaging tells user to start watcher

### A15. Fake Otto failure
- enqueue a fake task configured to fail
- expected: watcher marks the task failed and preserves useful failure output
- observability check: state, queue detail, and logs all show the failure

### A16. Dependency cascade
- enqueue dependent tasks where an upstream task fails
- expected: dependent tasks do not run after the failed prerequisite
- observability check: state explains the blocked dependency

## Set B — Merge mechanics

Hand-crafted git scenarios that exercise merge code paths without any LLM
where possible. Real LLM only where the agent actually does work.

### B1. `otto merge` with two clean branches
- create main + branch1 (touches A.py) + branch2 (touches B.py)
- run `otto merge build/branch1 build/branch2`
- expected: both merge cleanly via `git merge --no-ff`; no agent invoked;
  post-merge certifier runs unless the scenario uses `--no-certify`
- observability check: orchestrator log shows "auto-merged"; no conflict
  agent log

### B4. `--fast` bails on a synthetic conflict
- two branches create a conflict; run `otto merge --fast`
- expected: merge aborts cleanly without an agent call
- observability check: output mentions the conflict/bail path

### B5. Real conflict with fake task output + `--fast`
- two branches both edit the same lines of app.py
- expected: `--fast` refuses rather than invoking conflict resolution
- observability check: no conflict-agent session is created

### B6. Conflict agent: codex provider gate
- otto.yaml has provider=codex (not claude); attempt merge with conflicts
- expected: orchestrator refuses BEFORE first merge with clear message
- observability check: no partial merges land; message names provider

### B8. `--no-certify` skips post-merge verification
- successful merge with `--no-certify`
- expected: post-merge certifier NOT invoked
- observability check: log explicitly notes skip

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

### B13. Explicit branch argument
- pass a branch directly to `otto merge`
- expected: branch merges without relying on queue task-id lookup
- observability check: merge state records the explicit branch

### Candidate future fake/local coverage
- `.gitattributes` union driver for `intent.md`
- `.gitattributes` ours driver for `otto.yaml`
- non-fast conflict-agent resolution with a mocked or real agent
- conflict-agent validation failure from an out-of-scope edit
- PID-reuse safety and worktree-contention edge cases

## Set C — Real-LLM full-pipeline scenarios

Cost-controlled and gated by `OTTO_ALLOW_REAL_COST=1`. These scenarios use
simple intents and deliberately keep merge certification off so the budget stays
bounded.

### C1. Single real build through the queue
- `otto queue build "Create hello.py ..." --as hello-real -- --fast --no-qa`
- `otto queue run --concurrent 1` until the task is done
- expected: one real LLM build succeeds, creates a branch/worktree, records
  positive cost, and leaves no fake-otto markers
- observability check: queue state, manifest, proof-of-work, and live logs point
  at the real run

### C1B. Real build followed by non-agent merge
- same real queue build path as C1
- `otto merge --all --no-certify --cleanup-on-success`
- expected: merge succeeds without a triage/certifier LLM pass, graduates queue
  artifacts out of the worktree, and cleans up the completed worktree
- observability check: merged file exists on main, queue task is graduated, and
  manifest/proof paths still resolve after cleanup

### Candidate future coverage
- Two parallel real builds + clean merge with certification enabled.
- Two real builds with conflict + conflict-agent resolution.
- Real `otto improve bugs` through the queue + merge.

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
