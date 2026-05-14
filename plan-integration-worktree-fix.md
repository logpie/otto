# Integration Worktree Regression Fix Plan

## Context

The failed v5 run at `/Users/yuxuan/otto-projects/v5-itracker-sc4-185629/`
ended with the project worktree checked out on
`i2p/integ/v5-3fe387d1fede` and dirty product/runtime files. Root integration
then operated from the wrong branch instead of a clean root target branch.

Current implementation facts:
- `otto/v5_branching.py::merge_branch_into()` checks out the target branch
  without a dirty preflight.
- `otto/v5_runner.py::_run_integration()` can reuse `project_dir` when the
  needed integration branch is already checked out there, but it does not
  commit integration-agent edits or restore `project_dir` afterward.
- Root integration in `run_v5_pipeline()` calls `run_lead()` directly from
  `project_dir`, so the branch currently checked out in `project_dir` is the
  branch the root integration agent sees.
- Step 0b recovery instructions are in `otto/prompts/lead-integration.md`.
  `otto/prompts/lead.md` has no Step 0b section; I will add only a leaf-agent
  commit hygiene guard there and put the Step 0b-specific rules in
  `lead-integration.md`.

## Gate Status

The repository instructions require `/codex-gate` Plan Gate before
non-trivial implementation. The only available Codex Gate skill depends on an
`mcp__codex__codex` tool that is not exposed in this session, so I cannot run
that external gate. I will keep the review trail in this plan, run the focused
regression tests after each fix, and run the requested v5/branching/merge test
selection before final handoff.

## Diff Strategy

### Fix 3: dirty preflight in `merge_branch_into()`

Files:
- `otto/v5_branching.py`
- `tests/test_v5_subtree_propagation.py`

Changes:
- Add `MergeWorktreeDirtyError(RuntimeError)` carrying current branch, target
  branch, source branch, project path, and dirty status lines.
- Add small git helpers for current branch and `git status --porcelain`.
- In `merge_branch_into()`, check dirty status before `git checkout
  <target_branch>`. If dirty, raise `MergeWorktreeDirtyError` before any
  checkout or merge attempt.
- Let that specific exception propagate instead of being converted into
  `(False, "merge crashed: ...")`.
- Update runner callers that should convert this into `merge_blocked` rather
  than an unclassified crash.

Verify:
- New test: `test_merge_branch_into_raises_on_dirty_worktree`.
- Run: `uv run --extra dev pytest tests/test_v5_subtree_propagation.py -q`.

Risk:
- Existing direct callers expecting tuple-only failures may now need to catch
  `MergeWorktreeDirtyError`. I will update v5 runner merge paths; tests should
  reveal any remaining local caller assumptions.

### Fix 1: deterministic integration worktree branch selection

Files:
- `otto/v5_runner.py`
- `tests/test_v5_integration_worktree.py` (new)

Changes:
- Add a v5-local root branch resolver using `config["default_branch"]` when
  present, otherwise `otto.config.detect_default_branch(project_dir)`, falling
  back to `main`.
- Add `_checkout_v5_branch_clean()` for deterministic branch checkout. It
  refuses dirty worktrees and verifies the post-checkout branch.
- At v5 pipeline start and immediately before root integration, checkout the
  root branch.
- In `_run_integration()`, after the integration Lead returns and runner-side
  integration commit handling completes, restore `project_dir` to the task's
  parent integration branch (for top-level nested integrations, `main`).
- Emit branch-restore events for observability.

Verify:
- New test: `test_nested_integration_restores_project_dir_to_parent_branch`.
- New test: `test_root_integration_starts_on_main_even_after_prior_branch`.
- Run: `uv run --extra dev pytest tests/test_v5_integration_worktree.py -q`.

Risk:
- If a project starts v5 from a dirty non-root branch, the new behavior fails
  early instead of carrying local edits across branches. That is intentional for
  v5 integration safety but could expose previously hidden dirty-state usage.

### Fix 2: runner-owned commit of integration agent worktree changes

Files:
- `otto/v5_branching.py`
- `otto/v5_runner.py`
- `tests/test_v5_integration_worktree.py`

Changes:
- Add a runner-managed integration commit helper that stages only a product
  allowlist:
  `frontend/`, `backend/`, `api/`, `client/`, `server/`, `web/`, `src/`,
  `app/`, `apps/`, `packages/`, `lib/`, `public/`, `scripts/`, `tests/`,
  `docs/`, `spec/`, `CHARTER.md`, `decisions.md`, common package/config files,
  `README.md`, and `.gitignore`.
- Extend Otto's managed gitignore with `.worktrees/`, `otto_logs/`,
  `uploads/`, and `*.db.bak`.
- Before staging, untrack files now covered by the managed gitignore, so
  runtime files already tracked by a bad prior commit are removed from the
  index without committing their contents.
- Commit staged integration changes with message
  `integration: <task_id> runner-managed changes`.
- Call this helper after every integration Lead returns, before subtree
  propagation, root recovery reconciliation, or branch restoration.
- If legitimate product edits are committed, emit an event with the commit
  detail. If disallowed dirty files remain, mark that integration result
  `merge_blocked` rather than silently proceeding.

Verify:
- New test: `test_runner_commits_integration_product_files_and_excludes_runtime_files`.
- Run: `uv run --extra dev pytest tests/test_v5_integration_worktree.py -q`.

Risk:
- A product with an unusual top-level layout could produce a disallowed dirty
  path and become `merge_blocked`. The allowlist intentionally includes common
  app/package layouts and root config files; failing closed is safer than
  committing runtime state.

### Fix 5: Step 0b prompt hygiene

Files:
- `otto/prompts/lead-integration.md`
- `otto/prompts/lead.md`
- `tests/test_v5_step0b_recovery.py`

Changes:
- In Step 0b, replace bare "Commit" with a scoped command recipe:
  inspect `git status --short`, stage only explicit product pathspecs, never
  `.worktrees/`, `otto_logs/`, `uploads/`, `*.db`, `*.db.bak`, or runtime logs,
  and verify `git diff --cached --name-only` before committing.
- Add a leaf-agent hard rule in `lead.md`: never `git add -A`; stage only files
  in the assigned subsystem plus `CHARTER.md`/`decisions.md` when applicable.

Verify:
- New test: `test_step0b_prompt_enumerates_commit_allowlist_and_runtime_excludes`.
- Run: `uv run --extra dev pytest tests/test_v5_step0b_recovery.py -q`.

Risk:
- Prompt-only hardening cannot guarantee compliance, which is why Fix 2 is the
  enforcement layer.

### Fix 4: integration prompt secondary guard

Files:
- `otto/prompts/lead-integration.md`
- `tests/test_v5_step0b_recovery.py`

Changes:
- Add explicit integration-agent instruction: cross-subsystem edits are
  allowed for arbitration, but the agent must commit its own edits before
  yielding, use an `integration:` message, run `git status --short` to verify
  cleanliness, and never use `git add -A`.

Verify:
- New test: `test_integration_prompt_requires_self_commit_with_integration_tag`.
- Run: `uv run --extra dev pytest tests/test_v5_step0b_recovery.py -q`.

Risk:
- The agent may still fail to comply. This is defense-in-depth only; the
  runner-managed commit remains authoritative.

## Overall Test Strategy

After each fix, run the focused test file for that fix. After all fixes, run:

```bash
uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration
```

If runtime is too slow or unrelated pre-existing failures appear, record the
exact failure and also run the narrower affected v5 files.

## Review Trail

### Plan Gate

- External `/codex-gate` not runnable in this session because the required
  `mcp__codex__codex` tool is unavailable.
- Local adversarial checks added to the plan:
  - Dirty preflight must fail before checkout, not after merge starts.
  - Runner commit must be allowlist-based and fail closed on unusual paths.
  - Nested integration must restore the parent integration branch, not always
    hard-code `main`, so deeper trees remain correct.
  - Root integration must explicitly checkout the root branch before running.

## Implementation Log

Implemented on 2026-05-14.

Files changed:
- `otto/v5_branching.py`
- `otto/v5_runner.py`
- `otto/prompts/lead-integration.md`
- `otto/prompts/lead.md`
- `tests/test_v5_subtree_propagation.py`
- `tests/test_v5_integration_worktree.py`
- `tests/test_v5_step0b_recovery.py`
- `plan-integration-worktree-fix.md`

Tests added:
- `tests/test_v5_subtree_propagation.py::test_merge_branch_into_raises_on_dirty_worktree`
- `tests/test_v5_integration_worktree.py::test_nested_integration_restores_project_dir_to_parent_branch`
- `tests/test_v5_integration_worktree.py::test_root_integration_starts_on_main_even_after_prior_worktree_state`
- `tests/test_v5_integration_worktree.py::test_runner_commits_integration_product_files_and_excludes_runtime_files`
- `tests/test_v5_step0b_recovery.py::test_step0b_prompt_enumerates_commit_allowlist_and_runtime_excludes`
- `tests/test_v5_step0b_recovery.py::test_integration_prompt_requires_self_commit_with_integration_tag`
- `tests/test_v5_step0b_recovery.py::test_leaf_prompt_commit_hygiene_scopes_pathspecs`

Tests run:
- After Fix 3:
  `uv run --extra dev pytest tests/test_v5_subtree_propagation.py -q`
  -> 4 passed.
- After Fix 3 broad gate:
  `uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration`
  -> first run found `.worktrees/` compatibility issue; after filtering only
  Otto's own untracked `.worktrees/`, 402 passed, 2156 deselected.
- After Fix 1 targeted:
  `uv run --extra dev pytest tests/test_v5_integration_worktree.py -q`
  -> 2 passed.
- After Fix 1 compatibility rerun:
  `uv run --extra dev pytest tests/test_v5_integration_worktree.py tests/test_v5_phase2.py::TestRunV5PipelineStubbed::test_root_inline_no_children tests/test_v5_phase2.py::TestRunV5PipelineStubbed::test_root_with_two_children_aggregates tests/test_v5_verification_plan.py::test_v5_pipeline_downgrades_agent_pass_and_preserves_input_verdict -q`
  -> 5 passed.
- After Fix 1 broad gate:
  `uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration`
  -> 404 passed, 2156 deselected.
- After Fix 2 targeted:
  `uv run --extra dev pytest tests/test_v5_integration_worktree.py -q`
  -> 3 passed.
- After Fix 2 ignore coverage:
  `uv run --extra dev pytest tests/test_v5_merge_noise.py tests/test_v5_step0b_recovery.py::test_default_gitignore_covers_runtime_artifacts -q`
  -> 7 passed.
- After Fix 2 broad gate:
  `uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration`
  -> 405 passed, 2156 deselected.
- After Fixes 5 and 4 targeted:
  `uv run --extra dev pytest tests/test_v5_step0b_recovery.py -q`
  -> 13 passed.
- Final requested broad gate:
  `uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration`
  -> 408 passed, 2156 deselected.
- Focused lint after implementation:
  `uv run ruff check otto/v5_branching.py otto/v5_runner.py tests/test_v5_integration_worktree.py tests/test_v5_step0b_recovery.py tests/test_v5_subtree_propagation.py`
  -> all checks passed.
- Final rerun after lint cleanups:
  `uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration`
  -> 408 passed, 2156 deselected.
- Final rerun after tightening same-branch clean checks:
  `uv run ruff check otto/v5_branching.py otto/v5_runner.py tests/test_v5_integration_worktree.py tests/test_v5_step0b_recovery.py tests/test_v5_subtree_propagation.py`
  -> all checks passed.
  `uv run --extra dev pytest tests/test_v5_integration_worktree.py tests/test_v5_subtree_propagation.py tests/test_v5_step0b_recovery.py -q`
  -> 20 passed.
  `uv run --extra dev pytest tests/ -q -k "v5 or branching or merge" --ignore=tests/integration`
  -> 408 passed, 2156 deselected.

Ambiguities resolved:
- Step 0b text is in `lead-integration.md`, not `lead.md`; I put the
  recovery-specific pathspec rules there and added a smaller leaf commit guard
  to `lead.md`.
- Nested integration restore targets the task's parent integration branch,
  not always `main`; for top-level nested integrations that branch is `main`.
- `_checkout_v5_branch_clean()` is strict only inside git repositories so the
  existing non-git stubbed v5 tests remain backward compatible.
- The dirty preflight ignores only Otto's own untracked `.worktrees/` directory
  for compatibility with older test repos that predate the managed ignore
  block. Product/runtime dirt such as `otto_logs/`, uploads, databases, and
  source edits still fails the preflight unless the managed ignore rules hide
  untracked runtime artifacts.
