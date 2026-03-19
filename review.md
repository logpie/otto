# Implementation Gate — 2026-03-19 — Rename rubric → spec throughout codebase

## Round 1 — Codex
- [NOTE] Stale `rubric` references in docs (README.md, architecture docs, TODO.md) — deferred (non-runtime)
- APPROVED. No runtime issues found.

---

# Implementation Gate — 2026-03-19 — Phase 9+10: Coding agent CC parity + subagents

## Round 1 — Codex
- [CRITICAL] AgentDefinition import can break SDK fallback (whole import falls to stub) — fixed: split into separate try/except
- [CRITICAL] env + setting_sources exposes secrets to repo-controlled instructions — rejected: otto's threat model is user's own repos, same as `claude -p`
- [IMPORTANT] Subagents not actually read-only (Bash in tool list) — fixed: removed Bash from subagent tools
- [NOTE] Agent env inconsistent with verify subprocess env — fixed: using `_subprocess_env()`

## Round 2 — Codex
- [CRITICAL] Secret exposure still present via `_subprocess_env()` — rejected: same reasoning, full env needed for agent to function
- [IMPORTANT] Bash still in subagent tools — fixed (already applied in round 1 response)
- [IMPORTANT] SDK version compatibility for tools param — fixed: wrapped in try/except TypeError

## Round 3 — Codex
- [IMPORTANT] `agent_opts.agents` assignment can also fail on old SDKs — fixed: widened to except (TypeError, AttributeError, Exception)

## Round 4 — Codex
- [NOTE] Broad exception swallows unrelated errors — fixed: narrowed to (TypeError, AttributeError, ValueError)
- APPROVED

---

# Implementation Gate — 2026-03-19 — Otto v3 Refactoring (simplify to reliability harness)

## Round 1 — Codex
- [CRITICAL] Dirty-tree protection disabled — `check_clean_tree` only called from deleted `run_all()` — fixed: added calls to `run_piloted()` and one-off CLI path
- [IMPORTANT] `--tdd` with one-off prompt silently does nothing (no rubric) — fixed: added warning
- [IMPORTANT] TDD tests lost on agent error reset (reset drops TDD commit) — fixed: added `tdd_commit_sha` for retry preservation
- [IMPORTANT] TDD accepts non-adversarial outputs (`no_tests`, `all_pass`) — fixed: reject both
- [IMPORTANT] Dangling `role="testgen"` in testgen.py (role removed from architect) — fixed: changed to `role="coding"`
- [NOTE] CLI ImportError too broad — fixed: separated mcp check from pilot import

## Round 2 — Codex
- [IMPORTANT] False-pass path: no agent changes after TDD → early success without verification — fixed: guard with `and not tdd_commit_sha`

## Round 3 — Codex
- APPROVED. No new issues.

---

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
