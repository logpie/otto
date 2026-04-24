# Code Health Summary

Target: `otto/web`, `otto/mission_control`, `otto/merge`, and focused Mission
Control/merge tests.

Branch: `fix/codex-provider-i2p`.

Dirty state: worktree intentionally contains the TS/product hardening changes
being prepared for commit.

## Rounds

- Rounds requested: 1
- Rounds run: 1
- Hunter mode: local
- Convergence: no remaining CRITICAL or IMPORTANT findings after fixes

Subagents were not used because runtime policy requires explicit user
permission before spawning them.

## Candidate Counts

- fixed: 7
- deferred: 2
- duplicate: 0
- invalid: 3
- needs-more-evidence: 0

## Fixed

- IMPORTANT: merge ids now include entropy to prevent same-process state
  overwrites.
- IMPORTANT: `merge --all` skips branches already recorded as merged.
- IMPORTANT: all-done/already-merged `merge --all` now reports a clear
  no-unmerged-branches message.
- IMPORTANT: fast merge no longer falls into post-merge certification.
- IMPORTANT: Mission Control action subprocesses start in a new session.
- IMPORTANT: web destructive actions use inspectable in-app confirmation.
- IMPORTANT: overview active count uses watcher task state.

## Tests

- Baseline: `uv run pytest -x -q`
  - `917 passed, 18 deselected in 103.20s`
- Focused after merge-id fix:
  - `uv run pytest tests/test_merge_state.py::test_new_merge_id_is_unique_within_one_process_tick tests/test_merge_orchestrator.py::test_merge_all_skips_queue_tasks_already_merged_by_prior_run -q`
  - `2 passed in 0.78s`
- Final gate: `uv run pytest -x -q`
  - `918 passed, 18 deselected in 103.56s`
- `git diff --check`
  - passed
- `npm run web:typecheck`
  - passed
- `npm run web:build`
  - passed

## E2E Evidence

- Real web E2E project: `/tmp/otto-web-e2e-kanban`
- Browser tool: `agent-browser`
- Final scenario: one new ready branch plus two previously merged queue tasks.
- Outcome: web `Merge 1 ready` merged only
  `build/smoke-labels-2026-04-24`.
- Post-merge project tests: `15 passed in 0.26s`.
- Screenshot: `audits/2026-04-24-0222/mission-control-final-e2e.png`

## Deferred

- Cleanup action copy should clarify queue bookkeeping vs worktree cleanup.
- Long-running provider jobs need budget/time controls in the overview.
- Claude visual certification should be hardened to require live UI evidence for
  final walkthrough claims.
