# Implementation Review Log

## Implementation Gate — 2026-03-11 — CC Autonomous v2

### Round 1 — Codex
- [CRITICAL] Task queue has no project affinity — stranded in_progress tasks on worker crash — **fixed**: added `requeue_stale_tasks()` in worker.py, `_requeue_worker_tasks()` in manager.py
- [IMPORTANT] Task completed without checking ResultMessage success — **fixed**: check `result_msg.subtype != "success"`, skip verification on non-success
- [IMPORTANT] Verification discovery inconsistent between worker and manager — **fixed**: extracted shared `_detect_verification()` into `verify_utils.py`
- [IMPORTANT] Unsafe delete/retry on in_progress tasks — **fixed**: return 409 Conflict
- [IMPORTANT] Stopped/crashed workers strand tasks — **fixed**: stop_worker requeues tasks
- [NOTE] Lock file naming — **fixed**: documented shared convention in comments

### Round 2 — Codex re-reviewed fixes
- [CRITICAL] Stale requeue uses only started_at — can requeue healthy long-running tasks — **fixed**: heartbeat mechanism (`heartbeat_at` updated every 60s, `_heartbeat_task()` async background task)
- [IMPORTANT] stop_worker requeues before process is confirmed dead — **fixed**: `terminate()` → `wait(timeout=5)` → `kill()` escalation before requeue
- [NOTE] Requeued tasks retain stale run-state fields — **fixed**: shared `_reset_task_for_requeue()` clears all fields

### Round 3 — Codex re-reviewed fixes
- [IMPORTANT] Shutdown paths don't requeue tasks — **fixed**: both FastAPI shutdown handler and signal handler use stop-and-requeue pattern
- [NOTE] stale_timeout has no lower bound relative to heartbeat interval — **fixed**: `MIN_STALE_TIMEOUT = HEARTBEAT_INTERVAL * 5` with clamping

### Round 4 — Codex re-reviewed fixes
- [IMPORTANT] Heartbeat stops during blocking `subprocess.run()` in `run_verify()` — **fixed**: `asyncio.to_thread(run_verify, ...)` keeps event loop unblocked
- APPROVED. No new issues.

## Implementation Gate — 2026-03-13 — Otto v3 full implementation

### Round 1 — Codex
- [CRITICAL] Tasks forked from whatever branch is checked out, not default_branch — fixed: create_task_branch checks current branch and checks out default_branch first
- [IMPORTANT] Failed agent attempt contaminates retry with partial edits — fixed: git reset --hard + clean -fd on agent exception
- [IMPORTANT] Generated test files get wrong pathname for non-pytest frameworks — fixed: preserve testgen_file.name, use test_file_path only for directory
- [IMPORTANT] Tier 2 command construction hardcodes runners and drops configured options — fixed: use configured test_command and append test file path
- [IMPORTANT] Signal/timeout doesn't kill subprocess trees — acknowledged, deferred (start_new_session covers timeout; full os.killpg requires Popen refactor)
- [IMPORTANT] Cleanup not unified across failure paths — fixed: _cleanup_task_failure helper used by retries-exhausted, interruption, and unexpected exceptions

### Round 2 — Codex
- [IMPORTANT] Tier 2 still hardcodes runners instead of using configured test_command — fixed: run_tier2 takes test_command param, appends file to configured command
- [IMPORTANT] Broad try/except can corrupt post-merge state — fixed: narrowed try/except to pre-merge phases only

### Round 3 — Codex
- [IMPORTANT] Verify log writing and commit amend unprotected after narrowing — fixed: verify log wrapped in try/except OSError, commit amend in its own try/except
- [IMPORTANT] JS framework detection collapses jest/vitest/mocha to "jest" — fixed: return concrete runner name, use npx {framework} for fallback

### Round 4 — Codex
- [IMPORTANT] Commit amend missing check=True so failures silently pass — fixed: added check=True and text=True, stderr captured in error message
- APPROVED. No new issues.

## Implementation Gate — 2026-03-15 — Session: adversarial testgen, smart context, cost tracking, mutation checks

### Round 1 — Codex
- [CRITICAL] git clean -fd deletes user's untracked files — fixed: snapshot pre-existing, only clean otto-created
- [CRITICAL] Rubric mode no-changes skip causes false PASS — fixed: check test_file_path_val
- [CRITICAL] Integration gate commits to main before passing — fixed: run in worktrees, commit only after pass
- [IMPORTANT] validate_generated_tests no_tests status not rejected — noted, deferred
- [IMPORTANT] Rubric mode hardcoded to pytest — noted, deferred (Python-only MVP)
- [IMPORTANT] Mutation check can't distinguish syntax from behavioral failures — noted, deferred
- [IMPORTANT] parse_markdown_tasks doesn't validate rubric field type — noted, deferred
- [NOTE] Import graph misses some relative imports — noted
- [NOTE] tempfile.mktemp TOCTOU, minor dead code — noted

### Round 2 — Codex reviewed fixes
- [IMPORTANT] Pre-existing untracked file corruption risk — acknowledged as known limitation
- [IMPORTANT] Integration gate copy-back RuntimeError not caught — fixed: wrapped in try/except
- APPROVED. No new issues.
