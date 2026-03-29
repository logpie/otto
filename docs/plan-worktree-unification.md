# Plan: Unify Serial + Parallel to Worktree-Based Execution

**Goal:** Eliminate the branch-based serial code path. All task execution happens in worktrees. One merge path.

**Why:** The branch-based serial path causes bugs (tasks.yaml corruption, git reset on project_dir), requires ~15 `is_parallel` branches in runner.py, and has special-case hacks for batch QA serial mode. Unifying eliminates a class of bugs structurally.

## Design Invariant

> Every task execution path, including retries and one-off runs, uses a detached worktree.
> Tasks return `verified` (candidate ready to merge) or `passed` (no-change).
> One orchestrator merge function handles all merging.

## Architecture Change

### Before
```
Serial:    orchestrator → coding_loop(project_dir) → run_task_v45 creates branch → merge_to_default()
Parallel:  orchestrator → worktree → coding_loop(worktree) → merge_parallel_results() → merge_candidate()
One-off:   cli.py → run_task_v45(project_dir) → creates branch → merge_to_default()
```

### After
```
All paths:  orchestrator → worktree → coding_loop(worktree) → merge_batch_results() → merge_candidate()
One-off:    cli.py → run_per() with single-task plan (same path as multi-task)
```

## Step-by-step Plan

**Safe ordering: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9**

Principle: enable new paths before switching callers, convert ALL callers before deleting old code. No intermediate regressions.

### Step 1: Extract `_run_task_in_worktree()` helper in orchestrator.py

New function: create worktree → install deps → coding_loop → cleanup worktree.

```python
async def _run_task_in_worktree(
    task_plan, context, config, project_dir, telemetry, tasks_file,
    base_sha, qa_mode, sibling_context=None,
) -> TaskResult:
    wt_dir = create_task_worktree(project_dir, task_plan.task_key, base_sha)
    try:
        await _install_deps(wt_dir, config)
        return await coding_loop(
            task_plan, context, config, project_dir, telemetry, tasks_file,
            task_work_dir=wt_dir, qa_mode=qa_mode, sibling_context=sibling_context,
        )
    finally:
        cleanup_task_worktree(project_dir, task_plan.task_key)
```

Extract from `_run_batch_parallel` which already does this per-task.

**Verify:** Unit test — mock coding_loop, verify worktree created and cleaned up.

### Step 2: Move post-merge bookkeeping to orchestrator

**Must happen before Step 7 (deleting serial merge block) to avoid regression.**

Currently in serial merge block of runner.py (lines 2042-2056):
- testgen dir cleanup
- Proof report commit SHA append

**Add** (duplicate, don't move yet) into merge_parallel_results() so parallel merges get it. Keep the original in runner.py's serial merge block — it will be deleted in Step 7 after all callers use the new path.

**Verify:** Proof reports have commit SHAs after merge in parallel path. Serial still works via old code.

### Step 3: All tasks go through merge path (rename + unconditional)

Remove the `if (use_parallel or qa_mode == QAMode.BATCH)` condition. ALL batches go through merge after coding — including serial single-task and `--no-qa` mode.

Rename `merge_parallel_results()` → `merge_batch_results()`.

**Why before Step 4:** Serial tasks still use branches at this point. They still merge inside runner.py (returning `passed`, not `verified`), so the new unconditional merge path is effectively a no-op for them — merge_batch_results() only processes `verified` results. But once Step 4 switches serial to worktrees (returning `verified`), the merge path is ready to handle them.

**Verify:** Serial single-task run (still branch-based) still works — returns `passed`, merge_batch_results() skips it. `--no-qa` works. No regression.

### Step 4: Serial loop uses `_run_task_in_worktree()`

Change serial execution in `run_per()`:
```python
# Before:
result = await coding_loop(task_plan, ..., task_work_dir=None, ...)
# After:
base_sha = _get_head_sha(project_dir)
result = await _run_task_in_worktree(task_plan, ..., base_sha=base_sha, ...)
```

**Safe because:** Step 3 already made merge unconditional, so `verified` results will be merged.

**Verify:** `test_single_task_success`, `test_batch_tasks_run_sequentially_when_max_parallel_1`.

### Step 5: Fix retry paths

Retry paths call `coding_loop()` directly on project_dir:
- orchestrator.py:958 (merge conflict retry)
- orchestrator.py:1077 (batch QA retry)

Switch to `_run_task_in_worktree()`. Merge feedback passed via task feedback field.

**Why before Step 7:** Must convert all callers before removing branch code from runner.py.

**Verify:** Merge conflict retry creates worktree. Retry paths don't mutate project_dir.

### Step 6: Fix one-off `otto run "prompt"` path

cli.py:82 `_run_one_off_with_display()` calls `run_task_v45()` directly.

**Approach:** Route one-off through `run_per()` with a single-task plan. Write temp tasks.yaml, call `run_per()`, display results. This maintains the invariant — one merge function for all paths.

**Verify:** `otto run "add hello world"` creates worktree, merges result, cleans up worktree.

### Step 7: Remove is_parallel branches from runner.py

All callers now use worktrees. Safe to delete:
- `is_parallel = task_work_dir != project_dir` (always true)
- `if is_parallel:` branch at line 1572
- Serial merge block (lines 2035-2067)
- Batch QA serial checkout hack (lines 2025-2031)
- `_handle_no_changes()` branch cleanup params
- `merge_diverged` failure mode
- Move merge-phase telemetry to orchestrator merge path

**Verify:** `grep -r "is_parallel" otto/` returns zero.

### Step 8: Delete dead code in git_ops.py

Remove:
- `create_task_branch()` — no more branches
- `merge_to_default()` — no more serial merge
- `cleanup_branch()` — no more branches to clean
- `rebase_and_merge()` — already unused
- `parallel` param + branch-cleanup block from `_cleanup_task_failure()`
- Otto-owned file save/restore from `_restore_workspace_state()` (no longer needed)

**NOT dead:** `_restore_workspace_state()` itself — still used for within-task retry resets inside worktree.

**Verify:** `grep -r "create_task_branch\|merge_to_default\|cleanup_branch\|rebase_and_merge" otto/` returns zero.

### Step 9: Simplify _cleanup_task_failure()

No branch cleanup on failure. Worktree removal handled by `_run_task_in_worktree` finally block. `_cleanup_task_failure()` reduces to: update tasks.yaml status + restore workspace within worktree.

**Verify:** tasks.yaml not corrupted after serial task failure.

## Dead Code Summary

| File | Function/Code | Lines |
|------|--------------|-------|
| git_ops.py | `create_task_branch()` | ~40 lines |
| git_ops.py | `merge_to_default()` | ~20 lines |
| git_ops.py | `cleanup_branch()` | ~15 lines |
| git_ops.py | `rebase_and_merge()` | ~30 lines |
| git_ops.py | `parallel` param + branch cleanup in `_cleanup_task_failure()` | ~15 lines |
| git_ops.py | Otto-owned file save/restore in `_restore_workspace_state()` | ~20 lines |
| runner.py | `is_parallel` branches | ~15 conditionals |
| runner.py | Serial merge block (lines 2035-2067) | ~30 lines |
| runner.py | Batch QA serial checkout hack (lines 2025-2031) | ~7 lines |
| runner.py | `_handle_no_changes()` branch cleanup params | scattered |
| runner.py | `merge_diverged` failure mode | ~10 lines |
| **Total** | | **~200 lines deleted** |

## Risks

1. **Dep install regression** — Serial tasks didn't install deps before. Must thread venv_bin/env through baseline + pre-test phases so they use worktree's installed deps. Most likely operational regression.
2. **Retry paths missed** — Merge conflict + batch QA retry paths must use worktrees. Covered in Step 5.
3. **Telemetry change** — Serial coding_loop currently emits TaskMerged + merge-phase progress. Must move to orchestrator. `otto status -w` merge-phase display must still work.
4. **otto/{key} branches gone** — Workflows relying on preserved branches after merge_diverged break. Candidate refs are the durable artifact.
5. **Worktree overhead** — ~10-30s per task for worktree + dep install. Acceptable for multi-task; slightly worse for one-off. Optimize: skip dep install for projects with no manifest.
6. **One-off path** — Must be converted (Step 6) before deleting branch code (Step 8).
7. **--no-qa serial path** — QAMode.SKIP serial tasks currently merge inside runner.py. Must go through orchestrator merge after Step 3.
8. **live-state.json** — Written under repo-root `otto_logs/{key}/`, not inside worktree, so location is fine. Merge-phase live updates must move from runner to orchestrator.

## Test Plan

- [ ] All existing 429 tests pass
- [ ] `grep -r "is_parallel\|create_task_branch\|merge_to_default\|cleanup_branch" otto/` → zero matches
- [ ] Serial single-task run: creates worktree, merges via merge_candidate
- [ ] Serial multi-task run: each task gets own worktree, merged sequentially
- [ ] Parallel multi-task run: unchanged behavior
- [ ] Serial --no-qa run: merges through orchestrator path
- [ ] Batch QA serial: no checkout hack needed
- [ ] Merge conflict retry: uses worktree, doesn't mutate project_dir
- [ ] Batch QA retry: uses worktree
- [ ] One-off `otto run "prompt"`: uses worktree, cleans up, leaves main checkout untouched
- [ ] tasks.yaml not corrupted after serial failure
- [ ] Proof reports have commit SHAs
- [ ] `otto status -w` shows merge-phase progress
- [ ] Dep install works in worktree (baseline tests run correctly)

## Codex Review Trail

**Round 1:** NEEDS_REVISION — Missing one-off path, --no-qa path, step ordering unsafe, _restore_workspace_state not fully dead, otto status -w telemetry
**Round 2:** NEEDS_REVISION — Step ordering still unsafe (Step 3 before Step 4 leaves serial unmerged; Steps 5-6 before 8-9 breaks retry/one-off callers). Option A for one-off conflicts with invariant.
**Round 3:** NEEDS_REVISION — Step 2 "move" should be "add" (keep old until Step 7). Step 3 rationale corrected: serial returns `passed` not `verified`, so merge path is no-op until Step 4.
**Round 4:** APPROVED — No remaining blocking inconsistencies.
