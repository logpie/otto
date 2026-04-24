## Observations

- Repro project: `/tmp/otto-greenfield-kanban`.
- The project is on `main` with tracked unstaged changes in `data/tasks.json`.
- Mission Control shows ready merge tasks and enables `Merge ready`.
- `otto merge --fast --all` refuses the merge because `repo_preflight_issues()` requires a clean tracked/index state before merging.
- The CLI failure copy prints `Merge incomplete (id: )` when the merge result has no merge id.

## Hypotheses

### H1: Mission Control does not expose merge preflight state (ROOT HYPOTHESIS)
- Supports: API landing payload reports ready tasks and collisions but no dirty/preflight blockers.
- Conflicts: project-level metadata does show `dirty: true`, but merge controls do not use it.
- Test: add a done queue task, modify a tracked file, and assert `/api/state` returns `landing.merge_blocked`.

### H2: The merge action should ignore runtime data files
- Supports: `data/tasks.json` was changed by using the generated app.
- Conflicts: the file is tracked product state and may overlap with task branches; auto-ignoring or auto-resetting it can lose user data.
- Test: inspect git status and branch diffs; treat tracked changes as a user decision point.

### H3: The CLI wording is only cosmetic
- Supports: merge preflight already prevents corruption.
- Conflicts: `id: ` is empty because no merge run exists yet, which makes the product feel broken and hides that this is a preflight block.
- Test: make no-id failures render `Merge blocked`.

## Experiments

- Confirmed H1 by querying the live portal API: `project.dirty` was true, but `landing` still showed three ready tasks and no blocker.
- Confirmed H2 should not be implemented as an automatic ignore/reset: `data/tasks.json` is tracked in the app repo and contains user-created cards.
- Confirmed H3 by reading `otto/cli_merge.py`: all unsuccessful results printed `Merge incomplete (id: {result.merge_id})` without checking for an empty merge id.

## Root Cause

Mission Control used queue completion state to enable merge actions but did not include the merge preflight clean-tree requirement in the landing queue model.

## Fix

- Added merge preflight fields to the landing queue API: `merge_blocked`, `merge_blockers`, and `dirty_files`.
- Disabled global and per-task merge buttons when the merge target has tracked/index blockers.
- Added a web API guard so stale clicks or direct `/api/actions/merge-all` calls return a 409 blocker instead of launching a doomed merge process.
- Rendered a blocker callout that names dirty files and tells the user to commit, stash, or revert before merging.
- Changed CLI no-id failures from `Merge incomplete (id: )` to `Merge blocked`.
- Added a regression test for tracked dirty-file merge blocking in the web landing queue.
