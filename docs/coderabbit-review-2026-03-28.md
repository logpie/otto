# Otto Codebase Review — 2026-03-28

**Scope:** Full audit of 24 source files (~13,000 LOC) + 24 test files (~9,500 LOC)
**Reviewer:** CodeRabbit (3 rounds)
**Findings:** 50 total — 9 critical/high, 21 important, 20 suggestions

---

## Critical / High

### 1. Duplicate `_restore_workspace_state` call
**File:** `otto/runner.py` lines 2003–2016
**Issue:** Two identical back-to-back calls doing `git reset --hard` + untracked file deletion. The comment only applies to the second call — the first appears to be a copy-paste leftover.
**Risk:** Not destructive (idempotent), but suggests a misunderstanding about when workspace state drifts. May mask a missing state-management step.
**Fix:** Remove the first call after verifying no intermediate state change was intended.

### 2. `_subprocess_env` imported twice from different modules
**File:** `otto/runner.py` lines 21 and 51
**Issue:** Imported from `otto.agent` (which re-exports from `otto.testing`) and again directly from `otto.testing`. Both resolve to the same function today.
**Risk:** If the re-export chain changes, behavior diverges silently.
**Fix:** Remove the redundant import from `otto.agent`.

### 3. `_run_markdown_agent` missing `system_prompt` preset
**File:** `otto/spec.py` lines 333–335
**Issue:** Unlike all other agent invocations, this one does not set `system_prompt={"type": "preset", "preset": "claude_code"}`. Per CLAUDE.md: "system_prompt must use preset — NEVER None". A `None` system_prompt blanks CC defaults.
**Fix:** Add the preset system_prompt.

### 4. `_run_setup_query` uses raw string system_prompt
**File:** `otto/cli_setup.py` line 99
**Issue:** Uses `system_prompt="You are a project analyst..."` instead of the preset. The setup agent loses all CC built-in guidance.
**Fix:** Use the preset and move the analyst instructions into the prompt body.

### 5. `_run_coding_agent` return type and docstring both wrong
**File:** `otto/runner.py` lines 706, 715, 782–786
**Issue:** Type annotation declares 4-tuple `tuple[str | None, float, list[str], Any]`. Docstring says `(session_id, attempt_cost, log_lines, result_msg)`. Actual return is 3-tuple `(session_id, result_msg, agent_log_lines)`. The call site at line 1757 unpacks 3 values correctly, so it works by accident. Anyone trusting the docstring would introduce bugs.
**Fix:** Update both annotation and docstring to match actual return.

### 6. Silent rollback failure when `pre_batch_sha` is None
**File:** `otto/orchestrator.py` lines 1148, 1180
**Issue:** `_rollback_main_to_sha(project_dir, pre_batch_sha)` is called but `pre_batch_sha` can be None if `_current_head_sha` failed or if the code path bypassed the batch SHA capture. The function returns False for None without raising — rollback silently fails and main keeps bad commits.

### 7. `merge_parallel_results` does not restore main on fast-forward failure
**File:** `otto/orchestrator.py` lines 1674–1700
**Issue:** When `git merge --ff-only new_sha` fails, the function continues to the next task. The temp merge branch was already deleted, and `new_sha` is an orphaned commit. Subsequent task merges operate on whatever HEAD happens to be — potentially inconsistent state.

### 8. Signal handler accesses `context.pids` without synchronization
**File:** `otto/orchestrator.py` lines 718–738
**Issue:** `_signal_handler` iterates `context.pids` (a `set[int]`). Signal handlers can fire at any bytecode instruction. If the main code modifies `pids` while the handler iterates, `RuntimeError: Set changed size during iteration`. Mitigated by asyncio's single-threaded nature, but signals bypass that guarantee.
**Fix:** Use a `list(context.pids)` snapshot in the handler, or use `signal.set_wakeup_fd` to defer to the event loop.

### 9. `persist_learnings` has a read-then-append TOCTOU race
**File:** `otto/orchestrator.py` lines 119–152
**Issue:** Reads existing entries for dedup, then opens file for append. Between read and append, another concurrent task could write, causing duplicate entries. Not under a lock.

---

## Important

### 10. Broad `except Exception: pass` (49 occurrences)
**Files:** `otto/orchestrator.py` (21), `otto/runner.py` (13), 6 other files
**Issue:** Many wrap `update_task()` calls. If the task state file corrupts, the pipeline proceeds with stale state silently.
**Risk:** Silent state corruption in the task lifecycle.
**Recommendation:** At minimum, log a warning in `except` blocks around `update_task()`.

### 11. Greedy regex in `_parse_qa_verdict_json`
**File:** `otto/qa.py` line 226
**Issue:** `r'(\{.*"must_passed".*\})'` with `re.DOTALL` matches from first `{` to last `}` in the entire report.
**Risk:** Captures non-JSON content if the QA report contains braces in narration.
**Fix:** Use a non-greedy match or parse from the last `{` containing `must_passed`.

### 12. Redundant `import re as _re` inside function body
**File:** `otto/qa.py` line 217
**Issue:** `re` is already imported at module level (line 4). The inner import is unnecessary.
**Fix:** Remove the inner import.

### 13. Lock file handle leaked on early return
**File:** `otto/orchestrator.py` lines 685–689, `otto/cli.py` lines 376, 492, 643, 802
**Issue:** When `fcntl.flock` raises `BlockingIOError`, the function returns without closing `lock_fh`.
**Fix:** Close the file handle in the `except` block before returning.

### 14. Temp verdict file not cleaned up on exception
**File:** `otto/qa.py` lines 895–905, 999–1000
**Issue:** `NamedTemporaryFile(delete=False)` persists in `/tmp` if an exception occurs before cleanup.
**Fix:** Use `try/finally` around the temp file lifecycle.

### 15. `QAMode` is a plain class, not an Enum
**File:** `otto/context.py` lines 10–13
**Issue:** `QAMode.PER_TASK`, `.BATCH`, `.SKIP` are plain strings. No type safety.
**Recommendation:** Convert to `StrEnum` (Python 3.11+).

### 16. Missing test coverage for core modules
- `otto/git_ops.py` (668 lines) — no dedicated test file. `merge_candidate`, `_abort_merge_and_cleanup`, `create_task_worktree` lack direct tests.
- `otto/display.py` (1283 lines) — only partially covered by integration tests.
- `otto/observability.py` (32 lines) — no tests.

### 17. `pre_batch_sha` captured too late for serial mode
**File:** `otto/orchestrator.py` lines 852–854
**Issue:** In serial mode, tasks commit directly to main during `coding_loop`. By the time `pre_batch_sha` is captured, all serial tasks have already merged — making rollback a no-op.
**Fix:** Capture `pre_batch_sha` before the batch execution loop.

### 18. `fcntl.flock` on read-mode file descriptor
**File:** `otto/tasks.py` line 108
**Issue:** Lock file opened with `"r"` mode. Other parts of the codebase use `"a+"` which is more robust. Fragile on NFS.
**Fix:** Use `"a+"` for consistency.

### 19. `datetime.now()` without timezone
**File:** `otto/orchestrator.py` line 149
**Issue:** `persist_learnings()` uses `datetime.now()` (naive local time) while the rest of the codebase uses `datetime.now(timezone.utc)`.
**Fix:** Use `datetime.now(timezone.utc)`.

### 20. BFS queue uses `list.pop(0)` — O(n) per call
**Files:** `otto/tasks.py` line 199, `otto/orchestrator.py` line 209
**Issue:** For large task graphs, BFS becomes O(n^2).
**Fix:** Use `collections.deque` with `popleft()`.

### 21. Partial QA verdict treated as authoritative pass
**File:** `otto/qa.py` lines 869–893
**Issue:** When QA agent crashes after writing a partial verdict with `must_passed: True`, the partial verdict is returned as authoritative. No way to distinguish complete pass from incomplete testing.

### 22. `build_candidate_commit` uses `--allow-empty`
**File:** `otto/git_ops.py` line 368
**Issue:** If staging logic silently fails (all files otto-owned and filtered), creates an empty candidate commit. Tests pass against identical base, empty commit gets merged — silent no-op.

### 23. `_handle_no_changes` does not cancel `spec_task` on the "pass" path
**File:** `otto/runner.py` lines 1258–1278
**Issue:** When no-changes QA passes, `spec_task` may still be running in background. `_cancel_spec_task` is only called on error paths. Leaks an asyncio task + thread.

### 24. `_build_batch_qa_feedback` can produce empty feedback
**File:** `otto/orchestrator.py` lines 569–627
**Issue:** When batch QA has `must_passed: False` but verdict dict has empty `must_items`, `integration_findings`, and `raw_report`, the retry coding agent gets no actionable information.

### 25. Task prompts interpolated raw into agent-facing surfaces
**Files:** `otto/runner.py` lines 659–703, `otto/qa.py` lines 1002–1019
**Issue:** Task prompts from `tasks.yaml` interpolated into coding and QA prompts without fencing. `_fence_untrusted_text` exists for error output but is NOT applied to the task prompt itself.
**Mitigation:** tasks.yaml is user-authored (trusted). Becomes exploitable if otto ever accepts tasks from external sources.

### 26. `_write_regression_script` incomplete shell escaping
**File:** `otto/qa.py` lines 364–384
**Issue:** Escapes double quotes but not `$`, backticks, `$(...)`, or newlines. Raw `cmd` appended to shell script — injection vector if regression script is executed.

### 27. `worker.py` `run_verify` — `shell=True` with web-API-supplied commands
**File:** `worker.py` lines 234–243
**Issue:** `verify_cmd` from the manager web API (line 126) accepted from JSON body without sanitization. Shell injection possible.

### 28. `_combine_task_results` loses `error_code` on retry
**File:** `otto/orchestrator.py` lines 630–644
**Issue:** If retry fails with `error_code=None`, the original failure's error_code is lost. Makes post-run diagnosis harder.

---

## Suggestions

### 29. `discover_project_facts` reads `package.json` twice
**File:** `otto/config.py` lines 184–230
**Fix:** Read once, reuse the parsed result.

### 30. `locals()` for flow control in planner
**File:** `otto/planner.py` lines 967–974
**Fix:** Use a boolean flag instead of `'cost_usd' in locals()`.

### 31. Parameter `plan` shadows imported function
**File:** `otto/orchestrator.py` line 68
**Fix:** Rename parameter to `execution_plan`.

### 32. Unnecessary deferred imports
**Files:** Various
**Fix:** Move `from datetime import datetime` etc. to module level where safe.

### 33. Design doc inconsistency
**File:** `2026-03-27-smart-planner.md` lines 50–55, 116–118
**Issue:** Fallback description and model config section don't match the Decisions section.

### 34. `_spec_finish_time` declared but never written
**File:** `otto/runner.py` line 1370
**Issue:** Dead code from a removed feature.

### 35. `_result_error_code_unset` sentinel never used
**File:** `otto/runner.py` line 1372
**Issue:** Sentinel `object()` created but never referenced. Dead code.

### 36. Makefile detection substring match is imprecise
**File:** `otto/config.py` line 156
**Issue:** `"test:" in makefile.read_text()` matches substrings like `manifest:`.
**Fix:** Use `re.search(r"^test:", content, re.MULTILINE)`.

### 37. `_is_verification_command` allows `true` as a verify prefix
**File:** `otto/qa.py` line 300
**Issue:** QA agent running bare `true` produces a passing proof with no actual verification.

### 38. Duplicate git API endpoints in manager.py
**File:** `manager.py` lines 314, 327
**Issue:** `/api/git/log` and `/api/git-log` do the same thing.

### 39. `os._exit(0)` in signal handler bypasses shutdown hooks
**File:** `manager.py` line 449
**Issue:** Skips `atexit` handlers, unflushed buffers, and FastAPI shutdown event.

### 40. Inconsistent `_subprocess_env` CLAUDECODE handling
**File:** `otto/testing.py` lines 34–35
**Issue:** `env.pop(...)` followed by `env[...] = ""`. The pop is redundant.

### 41. Broad use of `Any` for known types
**Files:** `otto/runner.py`, `otto/qa.py`
**Issue:** `context: Any`, `telemetry: Any`, `emit: Any` could use concrete types.

### 42. `_revert_all` grep pattern too broad
**File:** `otto/cli.py` lines 811–814
**Issue:** `--grep=otto:` matches any commit mentioning "otto:". Use `--grep=^otto:`.

### 43. `conftest.py` `tmp_git_repo` assumes branch name `main`
**File:** `conftest.py` lines 36–53
**Fix:** Use `git init -b main`.

### 44. `cleanup_orphaned_worktrees` has dead legacy path
**File:** `otto/orchestrator.py` lines 155–166
**Issue:** Cleans `.worktrees/` but this was never the standard path. Dead code.

### 45. `replan` reuses variable name `remaining_tasks` for different types
**File:** `otto/planner.py` lines 894, 934
**Issue:** First assignment is formatted strings, second is dicts. Same name, different types.

### 46. `format_duration`/`format_cost` aliased unnecessarily
**File:** `otto/runner.py` lines 494–496
**Issue:** `_format_duration = format_duration` — the original names could be used directly.

### 47. Integration tests don't verify cost tracking
**File:** `tests/test_integration.py`
**Issue:** No assertions on `cost_usd`.

### 48. `PipelineContext` claims thread-safety but parallel mode uses threads
**File:** `otto/context.py` line 49
**Issue:** Docstring misleading — `asyncio.to_thread` callbacks run in thread pool.

---

## Summary

| Severity | Count | Key Themes |
|----------|-------|------------|
| Critical / High | 9 | Rollback failures, system_prompt violations, type lies, race conditions |
| Important | 19 | Silent state corruption, injection surfaces, resource leaks, test gaps |
| Suggestions | 20 | Dead code, imprecise detection, doc drift, type annotations |
| **Total** | **48** |  |

### Top Risks

| Theme | Findings | Risk |
|-------|----------|------|
| **Rollback / state corruption** | #6, #7, #17, #21 | High — silent failures in failure recovery |
| **system_prompt violations** | #3, #4 | High — blanks CC defaults, violates project invariant |
| **Injection surfaces** | #25, #26, #27 | Medium — mitigated by trusted input today |
| **Race conditions** | #8, #9 | Medium — mitigated by asyncio single-thread |
| **Type/doc lies** | #5 | Medium — accidents waiting to happen |
| **Resource leaks** | #13, #14, #23 | Low — OS cleanup compensates |

### Positive Observations

All three review rounds converged on the same strengths:

1. **Clean module architecture** — orchestrator, runner, planner, qa, git_ops each have clear boundaries and single responsibilities.
2. **Robust git safety** — worktree cleanup in `finally` blocks, durable candidate refs to prevent GC, `_is_otto_owned()` consistently filters otto runtime files from all agent-facing surfaces.
3. **Excellent observability** — structured logs per phase (live-state.json, task-summary.json, qa-tier.log, etc.) with well-documented debugging paths in CLAUDE.md.
4. **Defensive file locking** — `fcntl.flock` for task mutations via `_locked_rw()`, process-level lock for concurrent otto runs.
5. **Strong test coverage** for planner (714 lines), orchestrator (1416 lines), runner (473 lines), and v4.5 pipeline (1262 lines).
6. **Disciplined prompt engineering** — preset system prompts (never `None`), layered retry context, `_fence_untrusted_text()` for untrusted input.
7. **Well-crafted retry excerpt builder** (`retry_excerpt.py`) — handles jest/pytest/vitest/go/cargo, strips ANSI, preserves failure context windows.
8. **Flaky test detection** (`flaky.py`) — baseline failures tracked so pre-existing test failures don't block the coding agent.
9. **Graceful degradation** — planner falls back to serial plans on LLM failure, QA retries on infra errors, spec generation produces fallback specs on failure.
