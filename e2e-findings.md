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




