# P1 — Flask Todo (T1 smoke) — judgment: **FAIL**

Session: `/tmp/otto-e2e/p1-flask-todo/otto_logs/sessions/2026-05-04-082355-19d15c`

## Phase summary (run.log)

| Phase | Result | Cost | Wall |
|---|---|---|---|
| Compile | 3 slices, project_kind=webapp | – | – |
| Build | 3/3 slices passing | $0.41 | 127s |
| Merge | **2 landed, 1 blocked** | $0.37 | – |
| Audit | **verdict: passed** ⚠️ inconsistent with merge | $1.07 | 465s |
| Render | proof packet emitted | – | – |

**Otto declared PASSED while one slice was blocked at merge time.** The
fix-agent during audit committed directly to `main`, bypassing the merge
queue entirely. Product happens to work manually, but Otto's pipeline was
fundamentally dishonest.

## Per-rubric-dimension verdicts

### Dim 1 — Compile honesty: **PARTIAL**

- ✅ Schema-valid spec produced.
- ❌ Validator warnings dropped silently — root bug **V1** (fixed
  mid-run, see below).
- 🟡 `todo_actions` slice has `owned_paths=[]` but legitimate via
  shared_scaffold; non-failure but worth flagging.
- ❌ Multi-slice spec has `cross_slice_checks: 0`. The S4 warning fired
  in the validator but was discarded by V1.

### Dim 2 — Build honesty: **PARTIAL**

- ✅ All 3 slices got real per-slice branches (branch_real=true in
  events).
- ✅ All 3 slice branches contain commits beyond their parent ref.
- ❌ home_page slice produced templates on its branch (commit 2428429)
  but never landed via merge_queue.

### Dim 3 — Merge honesty: **FAIL**

- ❌ Only **2 real merge commits** (shell `e2b4ecd`, todo_actions
  `02aee0d`). home_page was BLOCKED by merge_queue.
- ❌ home_page's templates appeared on `main` via a **direct
  `git commit`** by the audit fix-agent (commit `3686294`), not via
  `git merge --no-ff`. Reflog confirms:
  ```
  3686294 commit: i2p(home_page): build slice on home_page  ← rogue
  02aee0d merge i2p/.../todo_actions: Merge made by 'ort' strategy
  e2b4ecd merge i2p/.../shell: Merge made by 'ort' strategy
  ```
- ❌ Branch isolation violated. The fix-agent recognized "the commit
  was on a separate branch and never merged to main" and `git add . &&
  git commit` directly. Otto's design says fix-agents work on slice
  branches and re-route through merge_queue; current code lets them
  bypass entirely.

### Dim 4 — Audit honesty: **FAIL**

- ❌ Verdict `passed` while `merge_result.blocked_ids = ["home_page"]`.
  These are inconsistent. The audit's `_compose_verdict` does not cap
  on merge BLOCKED, only on contract test, capability verdicts, and
  chain review.
- ❌ The audit fix-agent ran 3 attempts. Attempt 2 of attempt-01 used
  bash to commit directly to main. The fix-loop has no rule "fix work
  must be on slice branch" — V3 root cause.
- ✅ Audit agent `permission_mode = bypassPermissions` (read-only) —
  C3 assertion held.

### Dim 5 — Product quality: **PASS** (manually)

Pure-luck pass — depends on the fix-agent's rogue commit having actually
worked.

```
GET /:           200
POST /add:       302  (item appears in DB and renders on /)
POST /toggle/1:  302
POST /delete/1:  302
```

App imports cleanly, routes respond, templates render, DB persists.

### Overall: **FAIL**

Per rubric: any dimension FAIL = overall FAIL. Dim 3 and Dim 4 both fail.
This is the false-positive class the user warned about: Otto's verdict
contradicts its own merge journal.

## Root bugs to fix (V1–V5)

### V1 — validator warnings silently dropped (✅ FIXED mid-run)

`compile_spec` checked `result.valid` but discarded `result.warnings`.
Fix: log via `logger.warning`, attach to `spec._validator_warnings`,
print in `cli_run.py:_compile_phase`. 41 tests pass after fix.

### V2 — pre-merge slice check runs on base_branch, not slice content (CRITICAL Pattern D regression)

`merge_queue._process_candidate` runs slice + cross-slice checks BEFORE
calling `_merge_slice_branch`. Pre-merge state = `base + previous slices`,
NOT `base + this_slice`. Any slice whose check tests its own deliverables
(home_page → templates, almost every slice) BLOCKS at merge time despite
having passed at build time on the slice branch.

**Fix**: switch to merge-first-then-verify-with-rollback:

1. Try `_merge_slice_branch`. On conflict → repair (B1 path).
2. On success: now worktree IS `base + this_slice`. Run slice +
   cross-slice checks here.
3. If any check fails: `git reset --hard <pre_merge_head>` to undo
   merge, route to repair.

### V3 — fix-agent bypasses branch isolation (CRITICAL design violation)

`audit.run_audit` fix-loop invokes `fix_agent` (= `default_build_agent`)
on whatever branch is checked out (typically `main`). The agent has
`acceptEdits` permission and full bash access. It can `git commit`
directly to main, bypassing merge_queue.

**Fix**:

1. Before invoking fix_agent, checkout the slice's branch.
2. After fix_agent succeeds, commit changes to slice branch via
   `_commit_slice_work`.
3. Route the slice through merge_queue again to land properly.
4. Restore base_branch checkout after fix attempt.

### V4 — verdict ignores merge_result.blocked_ids (FALSE-POSITIVE root)

`_compose_verdict` has caps for: LLM verdict, contract test, chain
review, quality<3, capability blocked%. **No cap for "slices BLOCKED at
merge"**. So a run with N slices blocked can still be PASSED.

**Fix**: add a merge cap — if `merge_result.blocked_ids` is non-empty,
floor verdict at PARTIAL; if all PASSING slices were blocked at merge,
floor at BLOCKED. Append narrative section listing blocked slice ids.

### V5 — fix-loop accepts unverified fixes as terminal (follows from V3)

Once V3 is fixed (fix-agent must commit on slice branch + re-route
through merge_queue), V5 disappears: a fix is only "successful" once
its slice's branch lands a real merge commit on main.

## Plan

1. Implement V2 (merge-first-then-verify in `_process_candidate`).
2. Implement V3 (fix-loop checkout + commit + re-merge).
3. Implement V4 (merge-blocked cap in `_compose_verdict`).
4. Add unit tests for each: merge-time scope check, fix-loop branch
   isolation, verdict floored on blocked.
5. Re-run P1 from clean tmp dir.
6. Only after P1 PASSES on all 5 dimensions, advance to T2 (Microfeed-
   class) project.
