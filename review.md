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

## Implementation Gate — 2026-03-25 — otto setup + CLAUDE.md suggestion

### Round 1 — Codex
- [IMPORTANT] setting_sources=["project"] loads existing CLAUDE.md on overwrite — fixed by Codex: setting_sources=[]
- [IMPORTANT] No error handling on file reads — fixed by Codex: _read_text_if_possible wrapper
- [IMPORTANT] glob recursive=True missing — fixed by Codex
- [IMPORTANT] grep -rl not cross-platform — fixed by Codex: pure Python rglob
- [NOTE] find subprocess not cross-platform — fixed by Codex: os.walk

### Round 2 — Codex
- [IMPORTANT] _suggest_claude_md reads entire files with no size/count cap — fixed: 20 file cap, 5KB read limit, 50KB file skip
- [NOTE] No test coverage for setup command — accepted, manual e2e tested
- APPROVED after fix.

## Implementation Gate — 2026-03-25 — Proof-of-Work + Claim Verification

### Round 1 — Codex
- [CRITICAL] Regression script replays server-start/background commands — fixed by Codex: expanded denylist (npm run dev, flask run, uvicorn, gunicorn, nohup, etc.)
- [CRITICAL] QA infrastructure retry reads stale verdict (verdict=None → crash) — fixed by Codex: check infrastructure_error before reading verdict, rebind all variables after retry
- [IMPORTANT] Claim verification uses failing worktree log when pre-check overrides — fixed by Codex: skip claim verify when verify_overridden_by_precheck
- [IMPORTANT] Pattern 3 (unacknowledged test failure) rarely fires in production — documented limitation, kept for future log format expansion
- [IMPORTANT] Stale proof files from previous attempts not cleaned — fixed by Codex: rmtree + mkdir at start of _write_proof_artifacts()

### Round 2 — Codex
- [IMPORTANT] `"&" in cmd` too broad — catches `2>&1` and `&&` — fixed by Codex: regex for standalone trailing `&` only
- [NOTE] `"npx next"` too broad — catches `npx next build` — fixed by Codex: narrowed to `npx next dev` and `npx next start`

### Round 3 — Codex
- APPROVED. No new issues. 344 tests pass.

## Implementation Gate — 2026-03-25 — Retry excerpt (bloated retry prompt fix)

### Round 1 — Codex
- [IMPORTANT] Go `ok\t` in _SUMMARY_RE retains all passing lines — fixed by Codex: moved to _PASS_NOISE_RE
- [IMPORTANT] KEEP_AFTER=25 drops middle of large failure blocks — fixed by Codex: continuation-aware window extension + merge
- [IMPORTANT] `console.error` suppressed too broadly — fixed by Codex: only suppress when followed by React act() pattern

### Round 2 — Codex
- [IMPORTANT] Plain unindented text in failure bodies still truncated — fixed by Codex: delimiter-based extension with _FAILURE_BLOCK_END_RE
- [NOTE] ACT_WARNING_RE phrase-based false positive risk — accepted

### Round 3 — Codex
- [NOTE] 200-line max extension cap is intentional tradeoff
- APPROVED. 372 tests pass.

## Implementation Gate — 2026-03-25 — Cost delta fix (per-invocation, not cumulative)

### Round 1 — Codex
- [NOTE] Fix matches documented SDK contract (per-query cost, not session-cumulative)
- [NOTE] No other code depends on old cumulative assumption
- [NOTE] Future drift risk if SDK changes — no regression test locks the contract
- APPROVED. 372 tests pass.
