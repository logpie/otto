# Otto CLI Audit Report

**Date:** 2026-05-19  
**Scope:** otto/cli.py (1521 LOC) + otto/cli_*.py (6141 total LOC)  
**Context:** Multi-pivot codebase (v3→v4→v5→v6→v6.6→current), canonical i2p entrypoint is `otto v5 run`

---

## 1. Commands Actually Wired

Discovered via `main --help` and Python CLI introspection:

### Top-level commands (directly on main group)
- `build` — otto/cli.py:1105 — Compatibility wrapper routing to i2p
- `certify` — otto/cli.py:1330 — Compatibility wrapper routing to i2p
- `clean-verify` — otto/cli.py:286 — Deterministic oracle for worktree verification
- `cleanup` — otto/cli_cleanup.py:32 — Alias for `proof cleanup`
- `dashboard` — otto/cli.py:215 — Alias for `web`
- `debug` — otto/cli_proof.py:208 — Group: diagnostics for existing sessions
  - `debug narrative` — otto/cli_proof.py:215
- `history` — otto/cli_logs.py:147 — Alias for `proof list`
- `improve` — otto/cli_improve.py:184 — Group: improve bugs/feature/target
  - `improve bugs` — otto/cli_improve.py:206
  - `improve feature` — otto/cli_improve.py:328
  - `improve target` — otto/cli_improve.py:443
- `pow` — otto/cli_pow.py:22 — Alias for `proof open/path`
- `proof` — otto/cli_proof.py:115 — Group: proof packets and artifacts
  - `proof open` — otto/cli_proof.py:122
  - `proof path` — otto/cli_proof.py:135
  - `proof render` — otto/cli_proof.py:147
  - `proof list` — otto/cli_proof.py:181
  - `proof cleanup` — otto/cli_proof.py:199
- `queue` — otto/cli_queue.py:361 — Group: parallel worktree scheduling
  - `queue build` — otto/cli_queue.py:394
  - `queue v5` — otto/cli_queue.py:428
  - `queue improve` — otto/cli_queue.py:464
  - `queue certify` — otto/cli_queue.py:522
  - `queue ls` — otto/cli_queue.py:579
  - `queue show` — otto/cli_queue.py:640
  - `queue rm` — otto/cli_queue.py:698
  - `queue cancel` — otto/cli_queue.py:752
  - `queue resume` — otto/cli_queue.py:814
  - `queue cleanup` — otto/cli_queue.py:917
  - `queue run` — otto/cli_queue.py:1044
  - `queue dashboard` — otto/cli_queue.py:384 — **DEPRECATED** (hard error)
- `render` — otto/cli.py:1445 — Alias for `proof render`
- `replay` — otto/cli_logs.py:167 — Alias for `debug narrative`
- `run` — otto/cli_run.py:476 — Intent-to-product pipeline (Phase A: flat compile→build→merge→audit→render)
- `setup` — otto/cli_setup.py:115 — Generate CLAUDE.md
- `v5` — otto/cli_v5.py:45 — Group: Lead-driven pipeline
  - `v5 run` — otto/cli_v5.py:54 — **CANONICAL i2p entrypoint** (per memory)
  - `v5 list-pending` — otto/cli_v5.py:334
  - `v5 review` — otto/cli_v5.py:357
- `web` — otto/cli.py:251 — Web Mission Control (Uvicorn)

**Total: 47 commands (10 aliases + 37 functional)**

---

## 2. Definitely Dead / Safe to Delete

### A. Function stubs that just call sys.exit() with migration messages
- **`_exit_legacy_build_removed()`** — otto/cli.py:1077–1086
  - Called by: build() at line 1316
  - Purpose: Error message when legacy v3 build pipeline is requested
  - Status: Dead stub (v3 pipeline is gone per Phase C.3)
  - Safe to delete? **YES, but keep the call site** (to maintain error path clarity)

- **`_exit_legacy_certify_removed()`** — otto/cli.py:1089–1102
  - Called by: certify() at line 1442
  - Purpose: Error message when legacy certifier is requested
  - Status: Dead stub (legacy pipeline is gone)
  - Safe to delete? **YES, but keep the call site** (same reasoning)

- **`_build_locked()`** — otto/cli.py:1319–1327
  - Called by: **NOBODY** (verified via grep)
  - Purpose: Stub comment says "Phase C.3 stub — the legacy v3 build pipeline is gone"
  - Status: **COMPLETELY DEAD** — not even imported in tests
  - Safe to delete? **YES, immediately** (12 lines, zero dependencies)

### B. Unused validator functions (defined but never invoked during normal CLI flow)
- **`_load_config_or_exit()`** — otto/cli.py:420–425
  - Called by: **NOBODY** (verified via grep; similar logic inlined in cli_run.py, cli_v5.py)
  - Purpose: Load config or exit with error
  - Status: Dead code (only 6 lines, simple wrapper)
  - Safe to delete? **YES** — replaced inline in downstream modules

- **`_validate_brownfield_mode()`** — otto/cli_run.py:92–99
  - Called by: **NOBODY** (verified via grep)
  - Purpose: Validate brownfield mode enum
  - Status: Dead (8 lines, defined but never called)
  - Safe to delete? **YES, but check imports first** — no imports found

### C. Compatibility aliases that are just forwarders
These are intentional, NOT dead, but marked as deprecated in output:

- `dashboard` — otto/cli.py:215–248 — Calls `_run_web_command()` (shared with `web`)
- `cleanup` — otto/cli_cleanup.py:32–37 — Calls `cleanup_live_record_cli()` (shared)
- `pow` — otto/cli_pow.py:22–59 — Calls `_pow_html_path()` → `proof_html_path()`
- `history` — otto/cli_logs.py:147–166 — Calls `print_history()`
- `replay` — otto/cli_logs.py:167–180 — Calls `regenerate_narrative()`
- `render` — otto/cli.py:1445–1481 — Calls `_render_proof_packet()`
- `queue dashboard` — otto/cli_queue.py:384–391 — **HARDCODED DEPRECATION** (exits with error)

**Status: INTENTIONAL, NOT DEAD** (serve backward-compat; users warned)

---

## 3. Probable Dead Code (Needs Verification)

### A. Validation callbacks defined but possibly unused
- **`_positive_budget_option()`** — Defined 3× (cli.py:648, cli_run.py:102, cli_improve.py:83)
  - Status: **TRIPLE DUPLICATE** (identical implementations)
  - Used in: @click.option(..., callback=_positive_budget_option)
  - Evidence: All three versions are identical (tested via Python)
  - Fix: Extract to cli.py, import in cli_run.py and cli_improve.py

- **`_max_turns_option()`** — Defined 3× (cli.py:672, cli_run.py:112, cli_improve.py:107)
  - Status: **TRIPLE DUPLICATE** (identical)
  - Fix: **SAME AS ABOVE** — consolidate to cli.py

- **`_rounds_option()`** — Defined 2× (cli.py:658, cli_improve.py:93)
  - Status: **DOUBLE DUPLICATE** (identical)
  - Fix: **SAME AS ABOVE**

### B. Output formatting helpers (legacy build result handling)
These are only called if the build() command path is taken, which now routes to i2p:

- **`_print_build_result()`** — otto/cli.py:1005–1061 (57 lines)
  - Called by: **NOBODY** (verified via grep; build() routes to orchestrate_run())
  - Status: Dead legacy output formatter
  - Safe to delete? **LIKELY YES** — i2p has its own rendering pipeline

- **`_print_startup_context()`** — otto/cli.py:913–926 (14 lines)
  - Called by: **NOBODY** (verified via grep)
  - Status: Dead (probably from legacy build() path)
  - Safe to delete? **YES**

- **`_spent_line()`** — otto/cli.py:967–993 (27 lines)
  - Called by: `_print_build_result()` at line 1042 (itself dead)
  - Status: Dead (transitively)
  - Safe to delete? **YES, if _print_build_result() is deleted**

- **`_verification_heading()`** — otto/cli.py:996–1002 (7 lines)
  - Called by: `_print_build_result()` at line 1028 (itself dead)
  - Status: Dead (transitively)
  - Safe to delete? **YES, if _print_build_result() is deleted**

- **`_open_command_hint()`** — otto/cli.py:942–964 (23 lines)
  - Called by: `_print_build_result()` at line 1022 (itself dead)
  - Status: Dead (transitively)
  - Safe to delete? **YES, if _print_build_result() is deleted**

- **`_exit_for_lock_busy()`** — otto/cli.py:1063–1074 (12 lines)
  - Called by: **NOBODY** (verified via grep; lock handling moved to orchestrate_run())
  - Status: Dead (lock logic in otto/cli_run.py)
  - Safe to delete? **YES**

### C. Config/environment helpers used only in specific paths
- **`_signal_interrupt_guard()`** — otto/cli.py:629–645 (17 lines)
  - Called by: **NOBODY** (verified via grep; interrupt handling moved elsewhere)
  - Status: Dead (probably legacy build loop)
  - Safe to delete? **YES**

- **`_new_run_id()`** — otto/cli.py:928–939 (12 lines)
  - Called by: **NOBODY** (verified via grep; run_id generation moved to otto/runs/registry.py)
  - Status: Dead (has a comment "Fallback for legacy tests")
  - Safe to delete? **LIKELY YES** — verify no test patches reference it first

- **`_apply_build_cli_overrides()`** — otto/cli.py:490–625 (136 lines)
  - Called by: **NOBODY** (verified via grep; cli override handling moved to orchestrate_run())
  - Status: Dead (large legacy helper)
  - Safe to delete? **LIKELY YES** — but double-check for test mocking

---

## 4. Duplication / Consolidation Candidates

### A. Validator functions (HIGH PRIORITY)
**Problem:** Three identical definitions of `_positive_budget_option()`, `_max_turns_option()`, and `_rounds_option()` scattered across cli.py, cli_run.py, and cli_improve.py.

**Evidence:**
```
cli.py:648          def _positive_budget_option(...)
cli_run.py:102      def _positive_budget_option(...)    # identical
cli_improve.py:83   def _positive_budget_option(...)    # identical

cli.py:672          def _max_turns_option(...)
cli_run.py:112      def _max_turns_option(...)          # identical
cli_improve.py:107  def _max_turns_option(...)          # identical

cli.py:658          def _rounds_option(...)
cli_improve.py:93   def _rounds_option(...)             # identical
```

**Impact:**
- Maintenance burden (bugs in one version require fixing 2-3 copies)
- ~30 lines duplicated across 3 files
- Makes cli.py the de facto source, but others don't import from it

**Fix:**
1. Consolidate to otto/cli_options.py (new file, ~50 LOC)
2. Import from all three: `from otto.cli_options import _positive_budget_option, _max_turns_option, _rounds_option`
3. Or: Extract to cli.py and have cli_run.py and cli_improve.py import them

**Estimated savings:** 30–50 LOC

### B. Config banner and display logic
**Problem:** `_print_config_banner()` (otto/cli.py:798–895, 97 lines) is called only by build() path which now routes to i2p. No current use.

**Status:** Likely dead but worth verifying if any new CLI paths need it.

### C. Overlapping artifact paths logic
**Problem:** cli_pow.py and cli_proof.py both handle proof HTML paths.

**Evidence:**
- `cli_pow.py:16` calls `proof_html_path()` from cli_proof.py
- `cli_proof.py:17–40` defines `proof_html_path()` (shared utility)
- Both open/display proof packets

**Status:** **CORRECTLY SHARED** (cli_pow is thin alias) — NOT a problem

---

## 5. Critical Bugs Noticed

### A. Missing validation on `--tree-budget-usd` in v5 run
**File:** otto/cli_v5.py:69–70  
**Issue:** Option accepts any float, no min/max validation  
**Memory note:** Per feedback_time_budget_not_usd.md, the tree_budget_usd cap is still enforced at runtime (v5_runner.py:2808) even without --tree-budget-usd; silently caps at $25 with no CLI warning.  
**Severity:** LOW (runtime capping works, but CLI doesn't signal the limit)

### B. Stray function that's never called
**File:** otto/cli.py:1319–1327  
**Function:** `_build_locked()`  
**Issue:** Defined but never imported, never called, only exists as a stub  
**Severity:** LOW (dead code, but harmless)

### C. No duplication in import statements
**Status:** VERIFIED — cli_run.py and cli_improve.py do NOT import validators from cli.py; they redefined them locally

---

## 6. Estimated LOC Savings

### Immediate cleanup (safe delete)
- `_build_locked()` — 9 LOC
- `_load_config_or_exit()` — 6 LOC
- `_exit_for_lock_busy()` — 12 LOC
- `_exit_legacy_build_removed()` — 10 LOC (keep call site)
- `_exit_legacy_certify_removed()` — 14 LOC (keep call site)

**Subtotal: ~45 LOC with moderate risk** (need to verify if tests patch these)

### Higher-risk cleanup (verify test dependencies first)
- `_print_build_result()` + `_spent_line()` + `_verification_heading()` + `_open_command_hint()` — 127 LOC
- `_print_startup_context()` — 14 LOC
- `_signal_interrupt_guard()` — 17 LOC
- `_new_run_id()` — 12 LOC
- `_apply_build_cli_overrides()` — 136 LOC (large, high risk)
- `_print_config_banner()` — 97 LOC

**Subtotal: ~413 LOC** (high risk, need test audit first)

### Consolidation (refactoring, no delete)
- Consolidate `_positive_budget_option()`, `_max_turns_option()`, `_rounds_option()` — 30 LOC saved, 6 LOC new imports
- **Net savings: ~24 LOC**

### Total potential savings: 45–482 LOC (depending on risk tolerance)

---

## 7. Recommendations

### Phase 1 (Safe, immediate)
1. Delete `_build_locked()` — otto/cli.py:1319–1327 (9 LOC)
2. Delete `_load_config_or_exit()` — otto/cli.py:420–425 (6 LOC)
3. Delete `_validate_brownfield_mode()` — otto/cli_run.py:92–99 (8 LOC)
4. Consolidate `_positive_budget_option()`, `_max_turns_option()`, `_rounds_option()` into a shared module
5. **Test coverage:** Ensure no test patches reference these functions

### Phase 2 (Medium risk, verify first)
1. Audit whether `_exit_for_lock_busy()` is actually referenced anywhere in the codebase or tests
2. Audit whether `_new_run_id()` is used by any external test or script
3. If clear, delete both (~24 LOC)

### Phase 3 (High risk, needs research)
1. Verify that `_print_build_result()`, `_print_startup_context()`, `_spent_line()`, etc. are truly dead (i.e., build() path always routes to i2p, never executes these)
2. Check if any test mocks or patches reference `_apply_build_cli_overrides()`
3. If confirmed dead, delete the legacy output formatting path (127–413 LOC depending on scope)

### Phase 4 (Refactoring, not urgent)
1. Clean up `_print_config_banner()` and related helpers if they're not used by any CLI path
2. Consider whether `queue dashboard` deprecation should be an actual error earlier (currently hardcoded deprecation message)

---

## 8. Command Surface Stability

**Current state:** 47 commands, 10 of which are compatibility aliases.

**Future direction:** Per memory (project_otto_v5_hierarchy_arbitration.md), the "v5 one hard gate" pivot (2026-05-19) collapses the 170-commit tower into ONE hard gate. This suggests:
- Legacy build/certify/improve may eventually be removed entirely
- i2p becomes the only path (otto v5 run + review loop)
- Aliases (dashboard, pow, history, etc.) remain for backward compat until enough notice is given

**Immediate risk:** None. Current aliases work correctly. Legacy code just needs cleanup.

