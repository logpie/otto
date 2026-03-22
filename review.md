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
