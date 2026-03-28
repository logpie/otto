## Implementation Gate — 2026-03-27 — Batch QA pipeline + Smart planner

### Round 1 — Codex
- [CRITICAL] Batch QA after code on main, no rollback — fixed by Codex: pre-batch HEAD snapshot, git reset --hard on failure
- [CRITICAL] Planner analysis not enforced by code — fixed by Codex: _normalize_plan synthesizes conflicts, serializes dependent pairs
- [IMPORTANT] Infrastructure error lost in batch QA — fixed by Codex: preserved through finalizer
- [IMPORTANT] Batch QA passes with incomplete coverage — fixed by Codex: expected (task_key, spec_id) matrix, rejects incomplete
- [IMPORTANT] Candidate commits absorb unrelated untracked files — noted, pre-existing
- [NOTE] Spec ID renumbering after sort — noted, pre-existing

### Round 2 — Codex
- [IMPORTANT] Dependency order not directional + replan bypasses normalization — fixed by Codex

### Round 3 — Codex
- APPROVED. No new issues.

---

## Implementation Gate — 2026-03-26 — LLM-based merge conflict resolution

### Round 1 — Codex
- [CRITICAL] Agent SDK used for untrusted file content — fixed by Codex: plain `claude` CLI subprocess, no tools
- [IMPORTANT] error_code mismatch (merge_failed vs post_merge_test_fail) — fixed by Codex
- [IMPORTANT] Prompt needs base/ours/theirs 3-way view — fixed by Codex: git show :1/:2/:3
- [IMPORTANT] Partial resolution leaves repo dirty — fixed by Codex: all-or-nothing
- [IMPORTANT] Async complexity unnecessary — fixed by Codex: plain subprocess.run
- [REFACTOR] Extract from git_ops — fixed by Codex: new otto/merge_resolve.py

### Round 2 — Codex
- error_code already fixed in worktree (Codex read main repo)

### Round 3 — Codex
- [IMPORTANT] File corruption on non-UTF-8/binary — fixed: skip non-text files, no errors="replace"
- [IMPORTANT] Cleanup unverified — fixed: check MERGE_HEAD, fallback to hard reset
- [NOTE] Triple-backtick delimiters brittle — fixed: unique sentinels

### Round 4 — Codex
- [IMPORTANT] Prompt as argv exceeds OS limit on large files — fixed: send via stdin
- [IMPORTANT] MERGE_HEAD path wrong in linked worktrees — fixed: git rev-parse --git-path

### Round 5 — Codex
- APPROVED. No new issues.

---

## Implementation Gate — 2026-03-26 — Parallel batch execution (post-phase-3 fixes)

### Round 1 — Codex
- [IMPORTANT] Stale error_code not cleared on merge-retry reset — fixed by Codex: added error_code field to TaskResult, cleared on reset
- [IMPORTANT] attempts=0 gives fresh retry budget on merge-failed rerun — fixed by Codex: removed attempts=0 from reset
- [NOTE] Merge-failure detection is stringly typed — fixed by Codex: uses r.error_code == "merge_failed"
- [REFACTOR] Function still named cherry_pick_candidate — fixed: renamed to merge_candidate, all docs/comments/tests updated

### Round 2 — Codex
- [IMPORTANT] attempts=0 in run_task_v45() wipes prior count on rerun — fixed by Codex: removed attempts=0 from task start
- [NOTE] Remaining cherry-pick/post-rebase references — fixed by Codex: full terminology pass

### Round 3 — Codex
- APPROVED. No new issues.

---

## Implementation Gate — 2026-03-21 — Rich display rewrite + already-merged fix

### Round 1 — Codex
- [CRITICAL] Already-merged detection false positive on fresh tasks — **fixed**: replaced git diff heuristic with task-status-based check
- [CRITICAL] Test-pass heuristic not task-specific — **fixed**: now requires tasks.yaml "passed" status + merge commit SHA + task fingerprint
- [IMPORTANT] Rich markup injection in user-provided strings — **fixed**: added rich_escape() at all interpolation sites
- [IMPORTANT] Progress events not filtered by task_key — **fixed**: added _active_task_key tracking
- [NOTE] Stale phase fields on retry — **fixed**: reset phase dict on "running" entry

### Round 2 — Codex
- [IMPORTANT] Skip gate doesn't bind to current task contents — **fixed**: added task fingerprint (sha256 of prompt+spec)
- [IMPORTANT] One remaining unescaped detail path — **fixed**: escaped default tool display

### Round 3 — Codex
- APPROVED. No new issues.

---

## Implementation Gate — 2026-03-21 — UX/display + speed optimizations

### Round 1 — Codex
- [IMPORTANT] Phase display not retry-safe — **fixed**: reset printed marker on re-enter running
- [IMPORTANT] JSONL truncation detection unsound — **fixed**: track st_ino for file replacement
- [NOTE] qa_finding markers inconsistent — **fixed**: aligned emitter and display marker sets
- [NOTE] QA prompt too restrictive — **accepted**: speed target intentional, allows targeted probes

### Round 2 — Codex
- APPROVED. No new issues.

---

## Implementation Gate — 2026-03-21 — Hybrid pilot + observability overhaul

### Round 1 — Codex
- [CRITICAL] QA failures treated as pass — **fixed**: require explicit QA VERDICT: PASS
- [CRITICAL] Nested query() in MCP subprocess — **accepted**: mitigated, 0 hangs across 20+ projects
- [IMPORTANT] TOCTOU race on _active_phase_display — **fixed**: capture local ref
- [IMPORTANT] JSONL partial line reads — **fixed**: carry buffer
- [IMPORTANT] QA report not persisted — **fixed**: writes qa-report.md
- [IMPORTANT] _install_deps not isolated — **deferred**: ephemeral venv is future work
- [IMPORTANT] abort_task uses global config — **deferred**: minor
- [NOTE] live-state.json torn reads — **accepted**: readers handle JSONDecodeError

## Implementation Gate — 2026-03-22 — verify.py venv isolation

### Round 1 — Codex
- [CRITICAL] PATH update doesn't propagate to test execution — fixed: _install_deps returns venv_bin, threaded through run_tier1
- [IMPORTANT] Venv creation failures crash verification — fixed: existence check + skip fallback
- [IMPORTANT] run_integration_gate missing _install_deps — fixed: added
- [NOTE] Windows compatibility (hardcoded "bin") — deferred (existing Unix-only assumptions)
- [NOTE] No test coverage — fixed: 3 regression tests added by Codex

### Round 2 — Codex
- [IMPORTANT] Fallback to sys.executable re-introduces contamination — fixed: skip pip install entirely

### Round 3 — Codex
- APPROVED. No new issues.
---

## Implementation Gate — 2026-03-22 — Otto v4 PER Pipeline

### Fresh Review Round 1 — Codex (read-only)
- [CRITICAL] v4 never runs QA — `run_task()` parallel path skips QA entirely — **fixed by Codex**: `coding_loop()` calls `run_task_with_qa()` directly
- [CRITICAL] Task marked "passed" before merge — **fixed by Codex**: resolved by using `run_task_with_qa()` which marks passed only after merge
- [CRITICAL] Planner output not validated for coverage/uniqueness — **fixed by Codex**: added `_plan_covers_pending()`, falls back to `default_plan()`
- [IMPORTANT] Interrupt exits 0 with unexecuted tasks — **fixed by Codex**: `return 1 if failed or interrupted or missing`
- [IMPORTANT] `context.pids` never populated — **deferred**: requires SDK integration
- [IMPORTANT] Preserved diverged branches destroyed by `_setup_task_worktree` — **fixed by Codex**: checks `error_code="merge_diverged"`
- [IMPORTANT] Worktree cleanup leaves stale metadata — **deferred**: existing behavior
- [IMPORTANT] Telemetry doesn't write live-state.json — **deferred**: follow-up work
- [IMPORTANT] `run_qa_agent()` doesn't require explicit PASS — pre-existing, intentional
- [IMPORTANT] v3 Ctrl-C doesn't thread interrupted — pre-existing v3 behavior
- [NOTE] Research/learnings scaffolding not wired — deferred, design only

### Fresh Review Round 2 — Codex (read-only)
- [CRITICAL] Parallel `run_task_with_qa()` on shared checkout is unsafe — **fixed by Codex**: removed `asyncio.gather()`, batch tasks execute sequentially
- [IMPORTANT] Replan coverage not validated — **fixed by Codex**: validates with `_plan_covers_pending()`, falls back to remaining plan
- [IMPORTANT] AllDone telemetry doesn't reflect missing tasks — **fixed by Codex**: added `total_missing_or_interrupted` field
- [NOTE] live-state.json racy under parallel — resolved by sequential execution
- APPROVED. No new issues.

---

## Implementation Gate — 2026-03-23 — Otto v4.5 Pipeline Redesign

### Round 1 — Codex
- [CRITICAL] Merge-diverged error_code not persisted — fixed by Codex: `_result()` now accepts and persists `error_code`
- [CRITICAL] Spec gen failure silently skips QA — fixed by Codex: fallback to prompt-only QA with synthetic spec
- [CRITICAL] Spec gen races coding in same checkout — acknowledged: design tradeoff accepted
- [IMPORTANT] Early return paths bypass cleanup — fixed by Codex: all post-prepare failures route through cleanup
- [IMPORTANT] review_ref not propagated to TaskResult — fixed by Codex
- [IMPORTANT] Candidate ref anchoring ignores failures + lexicographic sort — fixed by Codex
- [NOTE] Tier 0 dead code — deferred: requires spec-to-test mapping
- [NOTE] Candidate ref 0-based — fixed by Codex: 1-based attempt_num

### Round 2 — Codex
- [IMPORTANT] Prompt-only fallback QA under-tiers visual tasks — fixed by Codex: forced Tier 2
- [IMPORTANT] review_ref not persisted/displayed — fixed by Codex: persisted + `otto show`
- [NOTE] Spec gen before remaining check — fixed by Codex
- [NOTE] Spec gen failure drops cost — fixed by Codex

### Round 3 — Codex
- [IMPORTANT] Stale review_ref not cleared on rerun — fixed by Codex: cleared at task start
- APPROVED. No new issues. 406 tests pass.

## Implementation Gate — 2026-03-25 — CLI refactor: extract monolithic cli.py

### Round 1 — Codex
- [IMPORTANT] Duration formatting changed: display.py `format_duration` returns `2m0s` vs original `2m00s` — fixed: zero-padded seconds
- [NOTE] cli_setup.py imports `_require_git` from cli.py creating back-edge — fixed: moved to config.py

### Round 2 — Codex
- [NOTE] `require_git()` uses `print(stderr)` instead of original `error_console.print(style="error")` — fixed: uses error_console

### Round 3 — Codex
- APPROVED. No new issues.

## Implementation Gate — 2026-03-25 — SDK boilerplate extraction (otto/agent.py)

Worktree: /Users/yuxuan/work/cc-autonomous/.claude/worktrees/agent-a99163b9

### Round 1 — Codex
- [IMPORTANT] run_agent_query() joins text with "\n" instead of "" — fixed: use "".join()
- [NOTE] tool_use_summary() truncates mid-word vs old _truncate_at_word — fixed: word-boundary truncation
- [NOTE] worker.py not migrated — out of scope (top-level entry point)
- [NOTE] _subprocess_env awkward home in verify.py — acknowledged tech debt

### Round 2 — Codex
- APPROVED. No new issues.

## Implementation Gate — 2026-03-25 — runner.py decomposition (run_task_v45)

Worktree: /Users/yuxuan/work/cc-autonomous/.claude/worktrees/agent-a424edc5

### Round 1 — Codex
- [CRITICAL] _handle_no_changes() uses stale spec after await — fixed by Codex: capture return value
- [IMPORTANT] No-changes QA tier hardcodes attempt=0 — fixed by Codex: thread attempt param
- [IMPORTANT] QA cost applied late, stale in live-state — fixed by Codex: add_cost callback

### Round 2 — Codex
- APPROVED. 323 tests pass.

## Implementation Gate — 2026-03-25 — display.py cleanup (add_tool/add_finding)

Worktree: /Users/yuxuan/work/cc-autonomous/.claude/worktrees/agent-a352153a

### Round 1 — Codex
- [IMPORTANT] Edit streak not flushed at phase boundary — fixed by Codex: _flush_edit_streak_locked()
- [IMPORTANT] Phase snapshot captured outside lock — fixed by Codex: snapshot in first locked section

### Round 2 — Codex
- APPROVED. 323 tests pass.
