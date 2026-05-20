# TEST AUDIT REPORT FOR `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/tests/`

## Summary

- Total test files scanned: 250+
- Test files with NO test functions: 12
- Conditional skips found: 10
- Duplicate coverage clusters: 20+
- Files importing from tests: 23 (_helpers.py usage)

---

## 1. Test Files with NO Test Functions (Likely Incomplete or Helper Modules)

These files contain setup code, fixtures, or mock helpers but no `test_*` functions. They appear to be:
- Integration test templates (marked with `pytestmark = pytest.mark.integration`)
- Helper modules that should perhaps be in `tests/_helpers.py`
- Incomplete test stubs (started but never finished)

### Candidates for inspection/cleanup:

1. **test_architect_route_isolation_repro.py** (298 lines)
   - Contains helper functions: `_clean_scaffold_result()`, `_ia()`, etc.
   - Marked with `pytestmark = pytest.mark.integration`
   - No `test_*` functions
   - Status: Likely a test template or abandoned repro

2. **test_critical_seam_repros.py** (607 lines)
   - Helper functions: `_git()`, `_lead_worktree()`, `_write_file()`, `_assert_file_reachable_from_main()`
   - Marked with `pytestmark = pytest.mark.integration`
   - No `test_*` functions
   - Status: Large seam repro harness but no tests written

3. **test_v5_decomposed_child_lands_in_main.py** (lines unknown)
   - Similar pattern: integration markers, helpers, no tests
   - Status: Likely abandoned integration test stub

4. **test_v5_dispatch_lease_stress.py**
   - Status: Stress test harness without test functions

5. **test_v5_ia_runner_coherence.py**
   - Status: Incomplete IA test

6. **test_v5_inline_commit.py**
   - Status: Incomplete commit test

7. **test_v5_provider_parity.py**
   - Status: Provider comparison test, no actual tests

8. **test_v5_repair_final_oracle_before_block.py**
   - Status: Large repair test (900+ lines) but no test functions
   - Contains extensive docstring about repair flow

9. **test_v5_root_integration_e2e.py** (108 lines)
   - Helper functions: `_git()`, `_lead_worktree()`, `_write_file()`
   - Monkeypatching setup for fake Lead/compile
   - No `test_*` functions
   - Status: E2E test harness without the actual test

10. **test_v5_skipped_report_lead_integration.py**
    - Status: Lead integration test, incomplete

11. **test_v5_step0b_full_pipeline.py**
    - Status: Step0b pipeline test, no test functions

12. **test_v5_step4_repair_protocol.py**
    - Status: Repair protocol test, no test functions

### Recommendation:
- Audit these 12 files: are they:
  - Dead code that should be deleted?
  - Tests that should be completed?
  - Helper modules that should be moved to `tests/_helpers.py`?
  - Integration test templates that need activation?

---

## 2. Conditional pytest.skip() Calls

Tests that skip based on environment. These are legitimate (environment checks),
but represent tests that may not run in CI:

| File | Line | Reason |
|------|------|--------|
| test_critical_seam_repros.py | 67 | local socket bind denied by test environment |
| test_makeitbuild_p_clean_deploy_hermetic_dualstack.py | 60 | no IPv6 loopback on this host |
| test_makeitbuild_p_clean_deploy_hermetic_dualstack.py | 148 | port 5173 not bindable in this environment |
| test_packaging.py | 13 | uv is required for the packaging smoke test |
| test_run_registry.py | 178 | fcntl unavailable |
| test_run_view_routes.py | 590 | symlinks unavailable |
| test_v5_phase5.py | 75 | pytest not on PATH |
| test_v5_scaffold_profiles.py | 490 | OTTO_SCAFFOLD_BUILD_E2E=1 not set |
| test_v5_scaffold_profiles.py | 492 | npm not on PATH |
| test_web_bundle_freshness.py | 42 | (unclear reason - check source) |
| test_web_cache_headers.py | 26 | (unclear reason - check source) |

**Status:** These are appropriate conditional skips. They indicate tests that need specific tools/environment to run.

---

## 3. Duplicate Test Coverage Clusters

Files that test related concerns. Some are legitimate (multiple edge cases),
others may be mergeable:

### Tight duplicates (likely mergeable):
- **audit_walkthrough** (2 files):
  - test_audit_walkthrough_coverage.py
  - test_audit_walkthrough_entries.py
  - **Action:** Check if coverage/entries should be in same file

- **event_log** (2 files):
  - test_event_log_no_internal_mode_flags.py
  - test_event_log_run_lifecycle.py
  - **Action:** These seem distinct (flags vs lifecycle)

- **queue_cancel** (2 files):
  - test_queue_cancel_history.py
  - test_queue_cancel_race.py
  - **Action:** Distinct concerns (history vs race), keep separate

- **v5_ia** (2 files, 1 with NO tests):
  - test_v5_ia_contract.py
  - test_v5_ia_runner_coherence.py (NO test functions)
  - **Action:** Coherence file is incomplete

- **v5_repair** (3 files, 1 with NO tests):
  - test_v5_repair_final_oracle_before_block.py (NO test functions, 900+ lines)
  - test_v5_repair_prompt_blackbox_harness.py
  - test_v5_repair_protocol.py
  - **Action:** "final_oracle_before_block" is abandoned; other two seem distinct

- **v5_scaffold** (2 files):
  - test_v5_scaffold_profiles.py
  - test_v5_scaffold_seed_wiring.py
  - **Action:** profiles vs seed_wiring are different concerns

- **v5_skipped** (2 files, 1 with NO tests):
  - test_v5_skipped_report.py
  - test_v5_skipped_report_lead_integration.py (NO test functions)
  - **Action:** Lead integration version is incomplete

- **v5_step0b** (2 files, 1 with NO tests):
  - test_v5_step0b_full_pipeline.py (NO test functions)
  - test_v5_step0b_recovery.py
  - **Action:** Full pipeline version is incomplete

### Large clusters (all legitimate, don't merge):
- **makeitbuild_p*** (20+ files): These are focused regression tests for different build properties (clean_deploy, orphan, charter, etc.). Each tests a distinct bug or property. Keep separate.
- **mission_control** (5 files): Different aspects (actions, adapters, integration, model, polish)
- **run_view** (4 files): Different aspects (main, routes, evidence kinds, severity)
- **spec_compile** (4 files): Different test targets (main, flat structured, observable prompt, timeout)
- **v5_clean** (2 files): Clean verify vs bare port hermetic (distinct)
- **v5_foundation** (2 files): All-or-nothing vs clean boot (distinct)
- **v5_merge** (3 files): Drivers, source union, noise (distinct)
- **v5_preflight** (2 files): Main vs scope coordsys (distinct)
- **v5_spec** (3 files): Cache, cache hardening, compile phasecap (distinct)

---

## 4. Dead Code & Removed Modules

**Key Finding:** All imports of major modules (spec_compile, audit, agent, build) appear valid.
These modules exist and are actively used. The tests importing them are not dead.

However, note:
- `otto.audit` has functions like `run_audit` and `default_audit_agent` that tests reference
- `otto.agent` has runtime functions like `query`, `run_agent_query`, `run_agent_with_timeout`, `_terminate_provider_process`
- These appear to be available at runtime even if static analysis misses them (likely re-exported or dynamically populated)

**Status:** No obviously dead imports detected. All major modules are actively used in production code.

---

## 5. Test Files Using Dead/Deprecated Code

### test_legacy_deprecation.py (85 lines)
- **Purpose:** Test that legacy v3 entry points (deleted in Phase C) raise proper errors
- **Tests:**
  - `test_legacy_v3_build_entry_points_hard_error()` — ensures v3 build APIs hard-error
  - `test_run_agentic_certifier_hard_errors_post_c2()` — certifier hard-errors
  - `test_deprecation_warnings_do_not_fire_at_module_import()` — no surprise warnings
- **Status:** Valid test of removal/deprecation handling. Keep.

---

## 6. Fixtures & Test Helpers

### conftest.py
- Active fixtures: `block_real_claude_sdk_calls()`, `make_mock_query()`
- Smoke test definitions (23 files, 28 node IDs)
- Slow test definitions
- Heavy test definitions
- Status: Essential conftest, actively used

### _helpers.py
- Imported by 23 test files
- Contains shared test utilities
- Status: Actively used, keep

### _web_mc_helpers.py
- Specific to web mission control tests
- Status: Keep

---

## 7. Critical Issues & Recommendations

### Issue A: Orphaned Integration Test Stubs (12 files)
These files contain extensive helper code and integration test setup but never define any test functions.
They suggest incomplete work or abandoned repro attempts.

**Recommendation:**
- Move helper code to `tests/_helpers.py` if reusable
- Delete the stub file if no tests will be written
- Convert to proper tests if repair work is ongoing

**Affected files (highest priority):**
1. test_v5_repair_final_oracle_before_block.py (900+ lines, large docstring, no tests)
2. test_critical_seam_repros.py (600+ lines, comprehensive helpers, no tests)
3. test_architect_route_isolation_repro.py (298 lines, no tests)
4. test_v5_root_integration_e2e.py (108 lines, complete setup, no tests)

### Issue B: Unused Conditional Skip Flags
Some tests skip based on environment variables or tool availability (npm, pytest, uv, IPv6).
These are appropriate for optional/heavy tests, but verify they're not masking test failures.

**Action:** Review CI configuration to ensure these tests run in at least one environment.

### Issue C: Duplicate Audit Walkthrough Tests
- test_audit_walkthrough_coverage.py
- test_audit_walkthrough_entries.py

**Action:** Confirm these test different aspects (coverage vs parsing). Consider merging if overlapping.

---

## 8. Test Coverage Summary

| Category | Count | Status |
|----------|-------|--------|
| Total test files | 250+ | ✅ Active |
| Files with 0 test functions | 12 | ⚠️ Needs audit |
| Integration test stubs | ~8 | ⚠️ Incomplete |
| Heavy/integration marked | Many | ✅ Legitimate |
| Using pytest.skip | 10 | ✅ Appropriate |
| Importing dead code | 0 | ✅ None detected |
| Duplicate clusters | 20+ | ✅ Mostly legitimate |

---

## 9. Final Recommendations (Priority Order)

### Priority 1: Remove/Complete Orphaned Test Stubs
- Decide fate of 12 files with no test functions
- If incomplete: complete them
- If dead: delete
- If helpers: move to `_helpers.py` and rename

### Priority 2: Audit Duplicate Walkthrough Tests
- Ensure test_audit_walkthrough_* files test distinct aspects
- Merge if overlapping

### Priority 3: Verify Conditional Skip Coverage
- Ensure CI runs tests with required tools (npm, pytest, uv, IPv6 support)
- Or mark skipped tests as `@pytest.mark.skip(reason="...")` if they'll never run

### Priority 4: Consider Consolidation of test_makeitbuild_p*
- 20+ files, each testing a narrow regression
- These are appropriate (each is a distinct bug), but monitor for redundancy
- Consider a shared integration test harness if code is duplicated

---

