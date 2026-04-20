# E2E Findings — `otto queue` + `otto merge`

Append-only log. Each finding has: scenario → symptom → diagnosis → fix
(commit ref) → verification.

## Format

```
### [F#] <short title> — scenario <id>

**Symptom**: what failed
**Diagnosis**: root cause
**Fix**: what changed; commit <sha>
**Verification**: how I confirmed it
**Observability improvement**: any new log line / instrumentation
```

---

### [F1] Manifest path mismatch between watcher and spawned otto — pre-flight code read

**Symptom**: Code review of `_finalize_task_from_manifest` and `manifest_path_for`
shows the watcher reads `<main_project>/otto_logs/queue/<task-id>/manifest.json`
but the spawned `otto build` runs with `cwd=<worktree>` and writes its
manifest to `<worktree>/otto_logs/queue/<task-id>/manifest.json`. In real
queue runs, the watcher would never find the manifest and every task would
be marked failed with `exited 0 but no manifest written`.

**Diagnosis**: `otto/cli.py` line 698: `project_dir = Path.cwd()`. When
spawned by the queue runner, cwd is the worktree, not the main project.
`write_manifest` uses that as project_dir and writes inside the worktree.
The watcher's `_finalize_task_from_manifest` uses `self.project_dir` (the
main project) → mismatch.

The existing test fixture in `tests/test_queue_runner.py` masks this by
setting `OTTO_PROJECT_DIR` in the test process env (which Popen inherits)
and having the fake-otto.sh write to that env-var path. The runner never
sets `OTTO_PROJECT_DIR` in production.

**Fix**: Runner now sets `OTTO_QUEUE_PROJECT_DIR=<main>` when spawning
children. `manifest_path_for` reads that env var when `OTTO_QUEUE_TASK_ID`
is set and uses it instead of the caller's `project_dir`. Result: both
sides resolve to the same `<main>/otto_logs/queue/<task-id>/manifest.json`.

Test fixture renamed `OTTO_PROJECT_DIR` → `OTTO_QUEUE_PROJECT_DIR` and a
new test asserts the runner sets this env var on spawn.

**Verification**: `pytest tests/test_queue_runner.py tests/test_manifest.py`
+ E2E scenario A1 with real `otto queue` end-to-end.

**Observability improvement**: `_finalize_task_from_manifest` now logs the
exact manifest path it expected when it can't find one. Was: "exited 0 but
no manifest written"; now: "exited 0 but no manifest at <path>".

---

### [F2] `otto queue run` requires otto.yaml but other queue commands don't — UX inconsistency — A1

**Symptom**: `otto queue build "X"` succeeds in a repo with no otto.yaml,
adding the task to the queue. But `otto queue run --concurrent 1` exits
with rc=2 and message "otto.yaml not found. Run `otto setup` first." The
task sits in the queue forever, the user has no idea why.

**Diagnosis**: `cli_queue.py:queue.run` had a hard `if not config_path.exists()`
gate. But `load_config()` already returns defaults when the file is absent,
and `_enqueue` (used by build/improve/certify) doesn't check. So the gate
was dead defensive code that broke the otherwise-consistent "defaults
everywhere" UX.

**Fix**: Removed the gate. Watcher now uses defaults (concurrent=3,
worktree_dir=".worktrees", on_watcher_restart="resume") when otto.yaml is
absent — matching `otto queue build`.

**Verification**: A1 reaches `done` end-to-end without otto.yaml.

---

### [F3] `otto merge` requires otto.yaml — same UX inconsistency as F2 — B4, B8

**Symptom**: `otto merge --all --no-certify` and `otto merge --all --fast`
both fail with rc=2 + "otto.yaml not found" in repos that use defaults.

**Diagnosis**: Same hard gate pattern as F2, this time in `otto/cli_merge.py`.

**Fix**: Removed the gate. Falls through to `load_config()` defaults.

**Verification**: B4, B8 progress past the otto.yaml check.

---

### [F4] otto's bookkeeping not auto-gitignored — `otto merge` fails on dirty tree — B4, B8

**Symptom**: After `otto queue build` runs, `otto merge --all --no-certify`
fails with `working tree must be clean before merge (uncommitted changes
detected)`. The "dirty" files are otto's own bookkeeping: `.otto-queue.yml`,
`otto_logs/`, `.worktrees/`, `.otto-queue-state.json`, etc. — none of which
the user wants in their repo.

**Diagnosis**: Otto creates these artifacts during normal operation but had
no mechanism to add them to `.gitignore`. Result: every queued task taints
the working tree from git's perspective, every merge precondition fails.

**Fix**:
1. New module `otto/setup_gitignore.py` with `ensure_gitignore(project_dir)`
   that idempotently appends otto's runtime patterns under an `# Otto
   runtime (auto-managed; safe to edit comments)` header.
2. New `first_touch_bookkeeping(project_dir, config)` in `otto/config.py`
   that calls `ensure_gitignore` AND `setup_gitattributes.install` (the
   merge driver setup), then auto-commits the changed files in a single
   `chore(otto): set up runtime bookkeeping (.gitignore, .gitattributes)`
   commit so the working tree stays clean.
3. Wired into `otto queue build|improve|certify` (`_enqueue`) and `otto merge`.
4. Auto-commit is gated: skipped if the user has other staged work
   (would otherwise bundle their changes into a chore commit).
5. Tests in `tests/test_setup_gitignore.py`.

Without this, every new otto user would need to run `otto setup` first AND
manually commit `.gitignore` before any merge could succeed. Now: zero
manual steps.

**Verification**: B4 + B8 pass end-to-end (no `otto setup` required).
`tests/test_setup_gitignore.py` covers fresh / preserve-existing / idempotent
/ partial-overlap.

**Observability improvement**: `setup_gitignore` logs the patterns added
and the auto-commit message at INFO level (visible with --verbose). Failure
modes (other staged files, git not present) log WARNING with the exact
reason instead of silently skipping.

---

### [F5] `otto merge` requires `.gitattributes` from `otto setup` — same root cause as F4 — B4, B8

**Symptom**: After F4 was fixed, `otto merge` next failed with
`.gitattributes precondition failed: Missing required `.gitattributes`
rules: intent.md merge=union, otto.yaml merge=ours / Run `otto setup` to
install`. Same UX failure: a brand-new user can't merge without manual
setup.

**Diagnosis**: `setup_gitattributes.assert_setup` is a hard precondition in
the merge orchestrator, but `ensure_bookkeeping_setup` (which installs
those rules) was only called from `otto setup`, not from queue/merge entry
points.

**Fix**: Bundled into `first_touch_bookkeeping` from F4. `_enqueue` and
`cli_merge` now call `first_touch_bookkeeping`, which installs the
`.gitattributes` rules (and `merge.ours` driver) and auto-commits.

**Verification**: B4 + B8 now pass without manual `otto setup`.

---

### [F6] `--cleanup-on-success` was a no-op — flag plumbed but never used — B10

**Symptom**: `otto merge --all --no-certify --cleanup-on-success` reports
"Merge complete" but the worktree of the merged task is still present in
`git worktree list`.

**Diagnosis**: `MergeOptions.cleanup_on_success` is defined and threaded
from CLI → orchestrator, but `orchestrator.run_merge` never reads it.
Half-finished feature.

**Fix**: Implemented `_cleanup_worktrees_for_merged_tasks()` and called from
both the no-cert and cert paths in `run_merge`. Looks up each merged
branch's task via `queue_lookup`, runs `git worktree remove --force`,
preserves the underlying branch (default git behavior). Errors are logged
WARNING (best-effort post-merge cleanup); failures don't fail the merge
result the user already saw succeed.

**Verification**: B10 now passes — worktree removed, branch preserved.

**Observability improvement**: orchestrator logs `cleanup-on-success: removed
worktrees for [<task-ids>]` at INFO; per-task failures log WARNING with
the git error.

---

### [F7] Watcher logger had no handlers — spawn/reap events invisible to user — observed during real LLM C1 run

**Symptom**: Running `otto queue run` shows the spawned otto's stdout
interleaved but no information about the watcher itself: when it dispatched
each task, when it reaped, what cost it observed, how long it took. The
`logger.info(...)` calls in `runner.py` (and there are many: spawn, reap,
cancel, terminate, reconcile, ECHILD-deferred reap) all go nowhere because
no handler is attached to `otto.queue.runner`.

User-impact: a user who runs `otto queue run` and sees their task seemingly
hang has no way to tell whether the watcher spawned it (vs. silently
queued it). When investigating a stuck task, they see only the spawned
otto's output — no watcher metadata. Debugging is guesswork.

**Diagnosis**: `cli_queue.run` never called `logging.basicConfig` or
attached any handler. Library convention is to leave that to the application
entry point — but the entry point forgot.

**Fix**: New `_install_runner_logging(project_dir, *, quiet)` in cli_queue.
Always installs a file handler at `otto_logs/queue/watcher.log` (append,
ISO-8601 timestamps, INFO level). Unless `--quiet`, also installs a stdout
handler with short `[HH:MM:SS] message` format so events stream live.
Stamped events:

    [02:32:26] spawned add-a-calculator: pid=2241, branch=build/add-...
    [02:32:28] reaped add-a-calculator: done (cost=$0.42, duration=1.0s)
    [02:32:28] SIGTERM: immediate shutdown

The file handler in `otto_logs/queue/watcher.log` survives across watcher
restarts (append mode) — useful for diagnosing "what happened during last
night's run" without scrollback.

**Verification**: A1 with `OTTO_E2E_KEEP=1` shows both files populated
correctly. New `--quiet` flag gives users an opt-out.

---

### [F8] Merge orchestrator + agents had no log handler — same bug as F7, different namespace — observed during real LLM C2/C3 runs

**Symptom**: After F7 fixed the queue runner, the merge orchestrator,
conflict-agent, and triage-agent all still logged into the void. The user
sees the high-level "Merging..." → "Merge complete" CLI banners but no
record of:
- which branches the orchestrator started/completed
- conflict-agent attempts, retries, validation failures
- triage-agent reasoning / story coverage decisions

**Diagnosis**: `otto.merge.*` and `otto.cli_merge` loggers had no handlers
attached.

**Fix**: New `_install_merge_logging(project_dir)` in cli_merge.py. Attaches
a single FileHandler to the parent `otto.merge` logger (which the children
inherit) and to `otto.cli_merge`. Output goes to
`otto_logs/merge/merge.log` (append, ISO-8601, INFO+). Idempotent across
reruns in the same Python process.

**Verification**: real merge run in /tmp/otto-real-cert wrote
"merge merge-... starting: target=main, branches=[...]" to the log file.

---

## Real-LLM Set C results

Three real-LLM scenarios run ad-hoc (not yet automated since the harness
still depends on capturing live cost; the underlying CLI paths work):

**C1 - single real build via queue → merge --no-certify --cleanup-on-success**
- Cost: $0.47 build + $0 merge = $0.47 total
- Time: 75s build + ~3s merge
- Result: calc.py with add(a,b) committed; branch merged into main; worktree
  cleaned up; first-touch bookkeeping (.gitignore, .gitattributes) auto-
  committed cleanly.

**C2 - 2 parallel real builds → merge --all (with conflict agent)**
- Cost: 2×$0.19 builds + $0.24 conflict agent = $0.62 total
- Time: ~30s/build + ~15s conflict resolution
- Setup: Both builds touch math.py with different functions
- Result: branch `add` merged clean; branch `mul` triggered conflict agent;
  agent merged both functions correctly into math.py with both
  `add(a,b)` and `multiply(a,b)`. Validation passed (no scope creep, no
  diff --check failures, HEAD unchanged). Commit landed cleanly.
- conflict-agent.log captured the agent's Read/Write tool calls per file.

**C3 - 2 parallel real builds → merge --all (with triage + cert)**
- Cost: 2×$0.15 builds + $0 triage (trivial — no stories yet) = $0.29 total
- Result: clean merge; triage emitted "no stories collected; nothing to
  verify" plan; cert skipped because plan was empty. This is correct — the
  fast/no-qa builds didn't produce STORY_RESULT lines, so there's nothing
  to verify. Triage cost $0 because it short-circuited on empty input.

**Total real-LLM cost**: ~$1.40 across 5 builds + 1 conflict resolution +
1 triage.

---

### [F9] `otto merge --all` silently skips improves that "failed" cert — observed during P1 benchmark

**Symptom**: P1 benchmark queues 3 parallel `otto improve feature -n 1`
runs against the same TODO CLI. Each improve makes real commits adding
its feature (priority/duedate/tags), but the certifier finds 7 unrelated
gaps and exits status="failure" because cert didn't pass within the round
limit. `otto merge --all` then sees 0 done tasks and silently merges only
the base build — the user's 3 improves' work is invisible to `--all`.

**Diagnosis**: `_resolve_branches(all_done_queue_tasks=True)` filters by
`status == "done"` only. There's no facility for "merge anything queued
that has a branch with commits" (the realistic improves-fail-cert case).

**Workaround**: pass branch names explicitly. The conflict agent merged
all 3 cleanly (1 clean + 2 conflict_resolved), producing a working CLI
with all 9 commands. Total merge cost: $1.35.

**Recommendations** (deferred — would be follow-up work):
1. `otto merge --all-with-branches` (or `--include-failed`) opt-in flag
   that merges any task whose branch has commits, regardless of status.
2. When `--all` finds 0 done tasks but N failed tasks have branches,
   print: `0 done tasks to merge; 3 failed tasks have branches with
   commits — use 'otto merge <branch> ...' to merge them.`

**Verification**: working `todo.py` post-salvage at
`/var/folders/.../bench-p1-poh9vxro/todo.py` (now 145 lines, all features).


**End-to-end coverage**:
- ✅ queue dispatch → real `otto build` → manifest → reap (F1 path)
- ✅ first-touch bookkeeping auto-commit (F4, F5)
- ✅ clean merge → cleanup-on-success (F6)
- ✅ real conflict → real conflict agent → validation → commit
- ✅ post-merge triage agent (trivial input edge)
- ✅ watcher logging visible (F7)
- ✅ merge logging visible (F8)






