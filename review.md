# Implementation Gate — 2026-03-16 — Architect phase + parallel test contamination fix

## Round 1 — Codex
- [CRITICAL] Sibling test tampering: coding agent could edit sibling tests in parallel worktree, corrupted tests merged via `git add -u` — fixed: exclusion now happens in disposable verification worktree only
- [IMPORTANT] Pytest-specific `--ignore` flags don't work for other frameworks — fixed: replaced with file deletion using actual paths from testgen
- [IMPORTANT] `otto arch` not serialized with runner, could race — fixed: added `otto.lock` to `arch` and `arch --clean`
- [IMPORTANT] Partial architect output treated as success, resets staleness — fixed: require `all()` core files present

## Round 2 — Codex
- [CRITICAL] File deletion in task worktree pollutes git diff on merge — fixed: moved deletion to disposable verification worktree only
- [IMPORTANT] `--clean` bypasses process lock — fixed: added lock guard
- [IMPORTANT] `any()` should be `all()` for core file check — fixed

## Round 3 — Codex
- [IMPORTANT] Framework mismatch: exclusion paths derived from keys don't match actual testgen output — fixed: pass actual `Path` objects from `pre_tests` dict through the entire chain

## Round 4 — Codex
- APPROVED. No new issues.

---

# Implementation Gate — 2026-03-16 — Otto v2 (4 phases: file-plan, holistic testgen, pilot, enhanced integration gate)

## Round 1 — Codex
- [CRITICAL] Holistic test files stored under .git/otto/testgen/ can't be git-added — fixed: copy to tests/ before staging
- [CRITICAL] Cross-task review `git clean -fd` deletes user untracked files — fixed: use `_snapshot_untracked()` + `_remove_otto_created_untracked()`
- [IMPORTANT] Pilot tool contracts mismatch: prompt says verify/merge separately but `run_coding_agent` calls full `run_task()` — fixed: updated prompt
- [IMPORTANT] MCP server `sys.path` points to temp file, not project root — fixed: use `resolve()` + preflight `importlib.import_module("mcp")`
- [IMPORTANT] Cross-task review doesn't stage newly created files — fixed: stage otto-created untracked files
- [NOTE] Cross-task review diff anchored to old commits — fixed: added `run_start_sha` parameter

## Round 2 — Codex
- [IMPORTANT] Pilot `run_coding_agent` doesn't consume holistic testgen output — fixed: check `tests/test_otto_{key}.py`
- [IMPORTANT] Pilot integration gate missing `run_start_sha` — fixed: capture at MCP server startup
- [IMPORTANT] Holistic conftest.py not committed before worktree branching — fixed: commit before test file loop

## Round 3 — Codex
- [IMPORTANT] Pilot `run_holistic_testgen` tool doesn't copy/commit files to tests/ — fixed: added copy+stage+commit in tool

## Round 4 — Codex
- [IMPORTANT] Pilot parallel `run_coding_agents` doesn't pass pre_generated_test or sibling_test_files — fixed: build pre-test map, remap into worktrees
- [IMPORTANT] Pilot serial `run_coding_agent` uses stale test files — fixed: freshness tracking via `_CURRENT_RUN_TESTS` set
