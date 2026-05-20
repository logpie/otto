# Spec/Build Core Audit Report
**Date:** 2026-05-19  
**Scope:** `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/`

---

## Executive Summary

The spec/build core contains significant duplication and dead code post-v5-pivot:

- **spec_compile.py** (5480 LOC): Legacy pipeline compiler, **still live but deprecated** via warning in cli_run.py:644-650
- **spec_compile_flat.py** (1643 LOC): v5 i2p path compiler, **active and used by v5_runner.py and cli_v5.py**
- **audit.py + audit_loop.py**: Two parallel repair mechanisms; audit.py includes legacy fix-loop, audit_loop.py drives Layer 2
- **repair_gates.py** (93 LOC): Minimal, focused; still active in audit_loop.py
- **spec_state.py** (706 LOC): Append-only journal; active across i2p and legacy paths
- **spec_warnings.py** (87 LOC): Legacy warning infrastructure, still used by spec_compile.py only
- **spec_amend.py** (691 LOC): Amendment chain; used by legacy build.py and spec_review_routes.py
- **build.py** (3818 LOC): Legacy per-slice build; **still active** for the legacy `otto run` path (runner.py)

**Total audit surface: ~16,345 LOC across 10 modules**

---

## 1. What's Live (With Caller List)

### 1.1 spec_compile_flat.py ✅ ACTIVE i2p
- **Schema version:** 4
- **Exported functions:** `compile_flat_spec()`, `load_flat_spec()`, `compile_message_metrics_from_jsonl()`
- **Callers (8 modules + 26 test files):**
  - **otto/v5_runner.py:99** (main i2p executor)
  - **otto/cli_v5.py:296** (v5 run command)
  - **otto/lead_verify.py:33** (verification gate)
  - **otto/v5_clean_verify.py** (clean verify harness)
  - **otto/v5_verification_plan.py** (pass-model planning)
  - Tests: 26 test files including `test_v5_root_integration_e2e.py`, `test_v5_phase*.py`
- **Status:** Central to v5 i2p pipeline; no removal candidate

### 1.2 spec_compile.py ⚠️ LEGACY ACTIVE (deprecated)
- **Schema version:** 3
- **Exported functions:** `compile_spec()`, `load_spec()`, `append_amendment()`, `persist_spec()`, `Spec`, `Feature`, `Group`, `Component`, etc. (136 defs)
- **Callers (7 modules + 12 test files):**
  - **otto/cli_run.py:74** (legacy `otto run` command) — explicitly deprecated at line 644-650
  - **otto/runner.py:90** (legacy pipeline executor)
  - **otto/build.py:55** (legacy build layer)
  - **otto/audit.py:65** (legacy audit, reads Feature/Spec types)
  - **otto/merge_queue.py** (legacy merge)
  - **otto/seed.py** (legacy seed layer)
  - **otto/render.py** (render reads Spec for proof output)
  - Tests: 12+ test files (test_cli_run.py, test_merge_*, test_spec_compile.py, etc.)
- **Status:** Still active; cli_run.py warns user it's deprecated. Used by legacy `otto run` command which still functions
- **Deprecation status:** Warning shown at lines 644-650: users directed to use `otto v5 run` instead

### 1.3 build.py ⚠️ LEGACY ACTIVE
- **Exports:** BuildResult, GroupResult, GroupStatus, BuildBudget, run_build(), BuildAgentInput, etc. (40+ types/functions)
- **Callers (7 modules + 14 test files):**
  - **otto/runner.py** (legacy pipeline)
  - **otto/merge_queue.py** (shares GroupStatus)
  - **otto/audit.py** (reads BuildResult, runs per-group fix)
  - **otto/cli_run.py** (legacy pipeline)
  - Tests: test_cli_run.py, test_runner.py, test_build_*.py (14 files)
- **Status:** Still used by legacy `otto run` path via runner.py and cli_run.py
- **Why not dead:** v5_runner.py does NOT use build.py; it implements parallel journey execution independently. Legacy runner.py still owns the old per-slice Group execution model

### 1.4 audit.py ✅ ACTIVE (dual mode)
- **Exports:** run_audit(), AuditResult, AuditVerdict, FeatureAudit, AuditAgentInput, default_audit_agent (3031 LOC, ~50 defs)
- **Key exports:** `run_audit()` (legacy fix loop), `run_checks()` integration, verdict/evidence types
- **Callers (6 modules + 7 test files):**
  - **otto/runner.py** (legacy runner uses legacy fix loop)
  - **otto/cli_run.py** (legacy pipeline)
  - **otto/audit_loop.py** (Layer 2 reads verdict types)
  - Tests: test_audit.py, test_cli_run.py, test_runner.py, etc.
- **Status:** HYBRID — contains two audit paths:
  - Legacy fix-agent loop in `run_audit()` (lines 260+): used by legacy runner.py for backward-compat; NOT used by v5
  - Structured audit verdict emission: used by all runners

### 1.5 audit_loop.py ✅ ACTIVE (v5 Layer 2 repair)
- **Exports:** repair_failing_features(), select_failing_features(), FailingFeature, RepairAttempt, RepairResult (691 LOC, ~25 defs)
- **Callers (1 module + 4 test files):**
  - **otto/v5_runner.py** (audit_loop integration at line ~8500)
  - Tests: test_v5_p1_hardening.py, test_runner.py
- **Status:** Central to v5 Layer 2 retry loop; live and active

### 1.6 spec_state.py ✅ ACTIVE (both paths)
- **Exports:** emit(), iter_events(), recover_mid_merge_state(), journal_path(), replay(), 14+ event functions (706 LOC, ~40 defs)
- **Callers (13 modules + 8 test files):**
  - **otto/build.py** (legacy: emit group progress events)
  - **otto/cli_run.py** (legacy: emit run.started)
  - **otto/runner.py** (legacy: emit events)
  - **otto/merge_queue.py** (emit merge.* events)
  - **otto/spec_amend.py** (reads events)
  - **otto/resume.py** (recovery from mid-merge)
  - **otto/mission_control/actions.py** (pause/resume control)
  - **otto/web/i2p_routes.py** (audit history view)
  - Tests: test_spec_state.py, test_runner.py, test_resume.py
- **Status:** Core journaling layer; LIVE in both legacy and v5 paths
- **Note:** No schema version bump needed — event shapes are append-only invariants

### 1.7 repair_gates.py ✅ ACTIVE (minimal)
- **Exports:** repair_gate_for_verdict(), NO_REPAIR, REPAIR_NOW, RepairGateDecision (93 LOC, 3 functions + 1 constant)
- **Callers (3 modules + 2 test files):**
  - **otto/audit_loop.py:35** (filters verdicts for repair eligibility in select_failing_features)
  - Tests: test_repair_gates.py, test_v5_p2_hardening.py
- **Status:** Lightweight verdict classifier; essential to audit_loop.py Layer 2 logic
- **No duplication:** focused single-purpose module

### 1.8 repair_evidence.py ✅ ACTIVE (minimal)
- **Exports:** RepairEvidence dataclass, repair_evidence_from_payload(), repair_evidence_payload_fields() (105 LOC, ~6 defs)
- **Callers (3 modules + 0 tests):**
  - **otto/repair_gates.py:52** (evidence analysis for gate decision)
  - **otto/audit.py** (legacy verdict routing)
- **Status:** Lightweight evidence extraction for verdict classification
- **No duplication:** minimal and focused

---

## 2. What's Dead Post-v5-Pivot

### 2.1 spec_compile.py — Legacy Compile Infrastructure
**Status:** DEPRECATED but not deleted (backward-compat during Phase B coexistence)

- **Lines 1-200:** Docstring + imports + schema v3 definition
- **Lines 63-86:** SCHEMA_VERSION=3, PROJECT_KINDS, BROWNFIELD_MODES, AUDIT_FIXTURE_KINDS constants
- **Lines 4001-4012:** `_load_kind_schema()`, `_validate_against_schema()` — schema validation (duplication: spec_compile_flat.py has no schema validation; flat uses JSON schema inline)
- **Lines 4053+:** `validate_spec()` — full Spec validation (permissive + amendment chain validation)
- **Lines 5182+:** `compile_spec()` — async agent loop with retry logic

**Why kept:** 
- cli_run.py still wires legacy `otto run` command to it (line 355)
- runner.py still uses Spec/Feature/Group types for backward compatibility
- Tests still exercise it (test_cli_run.py, test_spec_compile.py)
- No v5 replacement exists in v5_runner.py (flat compile only)

**Removal blocker:** Phase B coexistence requires both pipelines to work. Once `otto run` is fully deprecated and all live tests migrate to `otto v5 run`, spec_compile.py becomes removal candidate.

**Estimated savings if removed:** ~5480 LOC + 12 test files

### 2.2 build.py — Legacy Per-Slice Build
**Status:** DEAD for v5 i2p (replaced by v5_runner.py decomposition model)

- **Lines 1-100:** Group+slice dispatch, branch resolution, status enums
- **Lines 300-700:** `run_build()` orchestration — per-slice build agent dispatch
- **Lines 1200+:** `BuildAgentInput`, `BuildAgentOutput`, deterministic check execution
- **Lines 2000+:** `_build_agent_prompt()` — legacy per-slice agent framing

**Why dead for v5:**
- v5_runner.py (line ~1500+) implements its own group orchestration with journey-scoped decomposition
- v5 does NOT use Slices → Groups rename (build.py still uses Group for legacy slices)
- audit_loop.py does NOT call build agents directly; v5_runner.py owns the fix-agent callback

**Still used:**
- runner.py (legacy pipeline) imports and calls it
- cli_run.py (legacy `otto run`) calls it via runner.py
- Tests: 14 test files including test_build_*.py

**Removal blocker:** Legacy `otto run` command still uses it. Once deprecated, buildable removal candidate.

**Estimated savings if removed:** ~3818 LOC + 14 test files

### 2.3 audit.py — Legacy Audit Fix Loop
**Status:** PARTIALLY DEAD (legacy fix loop not used by v5; verdict/evidence types live)

- **Lines 1-150:** Docstring + imports + type definitions (FeatureAudit, AuditResult, etc.) ✅ LIVE
- **Lines 260-1000:** `run_audit()` orchestration with optional fix-agent loop
  - Lines 350-500: Legacy fix-agent dispatch (NOT used by v5_runner.py)
  - Lines 550-800: Verdict synthesis from checks
- **Lines 1100-1500:** Verdict/evidence payload types (LIVE, used by render.py, audit_loop.py)
- **Lines 1700+:** `_audit_prompt()`, `AuditAgentInput` (legacy, not used by v5)

**Why partially dead:**
- v5_runner.py does NOT call `run_audit()`
- audit_loop.py drives Layer 2 repair independently; does not use run_audit's fix loop
- v5_runner.py implements its own audit oracle (journey-scoped verification via lead_verify.py)

**Still used:**
- runner.py (legacy pipeline) calls run_audit() line ~800
- render.py (all paths) reads verdict/evidence types for proof packet
- Tests: test_audit.py, test_runner.py

**Removal blocker:** Verdict types are shared; audit.py would need refactoring to extract types into a separate module before deletion.

**Estimated savings if removed:** ~2000 LOC (legacy fix loop + prompt generation) + minor test impact

### 2.4 spec_warnings.py — Legacy Warning Infrastructure
**Status:** MOSTLY DEAD (only used by spec_compile.py for legacy path)

- **All 87 lines:** ValidationWarning dataclass + WarningCollector context manager
- **Usage:** spec_compile.py lines 4000+ emit warnings during parsing

**Why mostly dead:**
- spec_compile_flat.py does NOT import spec_warnings
- Flat spec has lint_warnings list for journey linting (lines 1560+) but no WarningCollector
- No v5 code uses WarningCollector

**Still used:**
- spec_compile.py only (legacy)
- Tests: test_spec_compile.py

**Removal blocker:** Removing spec_compile.py is the prerequisite; spec_warnings.py could be removed simultaneously.

**Estimated savings if removed:** 87 LOC + minimal test impact

### 2.5 spec_amend.py — Legacy Amendment Chain (PARTIAL)
**Status:** PARTIALLY DEAD

- **Lines 1-100:** Amendment chain walking, validation ✅ LIVE for legacy
- **Lines 200-400:** consume_amendment_request(), verify_amendment_chain() — legacy build.py integration
- **Lines 400-691:** Amendment reconstruction, intent patching

**Why partially dead:**
- v5_runner.py does NOT import or call any amendment functions
- Amendments were part of the legacy spec model for immutability tracking
- Flat spec (v5) has no amendment concept

**Still used:**
- build.py (legacy) line ~1400: consume_amendment_request()
- spec_review_routes.py (web UI): compute_invalidation() for amendment cost estimation
- Tests: test_spec_amend.py, test_audit.py

**Removal blocker:** Legacy build.py still uses it; depends on spec_compile.py removal.

**Estimated savings if removed:** ~400 LOC + test impact

---

## 3. Duplication Between spec_compile.py and spec_compile_flat.py

### 3.1 Schema Validation (AVOIDABLE)
| Aspect | spec_compile.py | spec_compile_flat.py | Duplication |
|--------|-----------------|----------------------|------------|
| Schema version constant | Line 63: v3 | Line 55: v4 | Two constants, different |
| Schema files used | spec_schemas/*.json | None (inline) | Flat embeds schemas in code |
| Validation function | _validate_against_schema() line 4012 | None | spec_compile uses external JSON schemas; flat doesn't |
| Field coercion | Extensive (lines 3700+) | Minimal (structured errors) | Different strategies |

**Duplication cost:** spec_compile.py's generic schema validation (lines 4001-4053, ~50 LOC) could be shared, but flat's inline approach is simpler for its single-pass parsing.

### 3.2 Shared Contracts (NO duplication)
- **journey_contracts.py:** Both import `normalize_journey_contracts()`, `VerificationContractError`
- **No duplication:** Contracts are agnostic to compile path

### 3.3 Prompt Building (DUPLICATION)
| Aspect | spec_compile.py | spec_compile_flat.py |
|--------|-----------------|----------------------|
| Prompt name | "compile-spec.md" (line 83) | "compile-spec-flat.md" (1140+) |
| Agent dispatch | Lines 5182-5400 | Lines 802-1100 |
| Retry loop | 3 attempts max (line 85) | 2 + bounded repair (lines 76-94) |

**Duplication cost:** Both orchestrate async agent loops with retries. No shared code; ~300 LOC overlap in structure.

---

## 4. Schema Versions: Kept vs Removable

### 4.1 Schema Files in spec_schemas/
All 5 JSON schema files are **still used** by spec_compile.py (legacy compile):

- **api.json** — 90 lines; referenced by spec_compile.py line 4001-4012
- **cli.json** — 85 lines; referenced by spec_compile.py for project_kind="cli"
- **library.json** — 80 lines; referenced for project_kind="library"
- **service.json** — 75 lines; referenced for project_kind="service"
- **webapp.json** — 95 lines; referenced for project_kind="webapp"

**No schema v4 files:** spec_compile_flat.py does not use external schemas; it validates using inline JSON schema construction (lines 657-750, `_json_schema_any_of()`).

**Removal blocker:** All 5 files must stay until spec_compile.py is removed.

---

## 5. Repair Gates: Active vs Orphaned

### 5.1 Repair Gate Functions (93 LOC total)

| Function | Lines | Used by | Status |
|----------|-------|---------|--------|
| `repair_gate_for_verdict()` | 50-69 | audit_loop.py line 35 (select_failing_features) | ✅ ACTIVE |
| `_non_repairable_reason_for_code()` | 89-93 | repair_gate_for_verdict() only | ✅ ACTIVE |
| `_typed_non_repairable_reason()` | 75-86 | repair_gate_for_verdict() only | ✅ ACTIVE |

**All functions:** Active. No orphaned gates.

### 5.2 Non-repairable Codes (Line 36-47)
All 9 non-repairable reason codes are still checked:
- "provider_auth_exhausted", "provider_auth_missing", "provider_permission_denied", "provider_quota_exhausted"
- "missing_toolchain", "clean_deploy_missing_toolchain"

**Status:** All live; used by v5_runner.py audit loop (audit_loop.py integration).

---

## 6. Critical Bugs Spotted

### 6.1 Schema Version Mismatch Risk
- **Issue:** spec_compile.py uses schema v3; spec_compile_flat.py uses schema v4
- **Risk:** If legacy `otto run` tries to load a flat spec (or vice versa), mismatch could cause silent failures
- **Mitigation:** CLI dispatch (resolve_pipeline_choice in cli_run.py) prevents cross-load; but no explicit version check at load time
- **Location:** spec_compile.py:4557 (load_spec) vs spec_compile_flat.py:1618 (load_flat_spec)
- **Severity:** LOW (dispatch prevents mixing), but could be hardened

### 6.2 Abandoned Amendment Tracking in Flat Spec
- **Issue:** spec_compile_flat.py does not implement amendments (no append_amendment equivalent)
- **Risk:** If a flat-spec run is resumed/edited and then run again, no change tracking exists
- **Location:** spec_compile.py has full amendment model (lines 4397-4445); flat has none
- **Severity:** LOW (current v5 runner does not support spec amendments; single-pass only)

### 6.3 Duplicate Warning Types
- **Issue:** spec_warnings.py defines WarningCode; audit.py redefines error signatures
- **Risk:** Minimal overlap; both coexist without collision
- **Severity:** INFORMATIONAL (no bug, just organization)

---

## 7. Estimated LOC Savings

### 7.1 Full Removal Scenario (Post-Phase-B)
Once `otto run` (legacy) is fully deprecated:

| Module | LOC | Notes |
|--------|-----|-------|
| spec_compile.py | -5480 | Entire module; remove with test_spec_compile.py, test_cli_run.py (~12 test files) |
| build.py | -3818 | Entire module; remove with test_build_*.py (~14 test files) |
| spec_warnings.py | -87 | Remove with spec_compile.py |
| spec_amend.py (partial) | -300 | Amendment chain only; keep 391 LOC for spec_review_routes.py |
| audit.py (partial) | -1500 | Legacy fix loop + prompt only; keep verdict/evidence types |
| runner.py | -1859 | Entire legacy pipeline orchestrator |
| **Total non-test savings** | **-13,044 LOC** | |
| **Test files impacted** | ~40-50 files | Mostly old pipeline tests; new v5 tests unaffected |

### 7.2 Partial Cleanup (NOW)
Remove dead code from active modules:

| Module | Dead code | Savings |
|--------|-----------|---------|
| audit.py | Legacy fix-agent loop (lines 260-1000) | ~500 LOC |
| spec_amend.py | Unused amendment features | ~50 LOC |
| Total immediate savings | | ~550 LOC |

**Low risk:** No calls to dead audit.py code from v5 path.

---

## 8. Recommendations

### 8.1 Immediate (safe, no breaking changes)
1. **Mark as deprecated:** Add runtime warnings to spec_compile.py when invoked via `otto run`
   - Already done at cli_run.py:644-650 ✅
2. **Add schema version check:** load_spec() and load_flat_spec() should validate version before parsing
   - Location: spec_compile.py:4557, spec_compile_flat.py:1618
3. **Remove dead audit.py code:** Delete legacy fix-agent loop (lines 260-1000)
   - Depends on: audit_loop.py being the sole audit orchestrator (confirmed ✅)
   - Savings: ~500 LOC

### 8.2 Phase B (after legacy deprecation sunsets)
1. **Delete spec_compile.py**, spec_warnings.py, runner.py entirely
2. **Refactor audit.py:** Extract verdict/evidence types into audit_types.py or verdicts.py
3. **Simplify build.py removal:** Delete entirely once legacy runner.py is gone
4. **Clean up spec_amend.py:** Keep only spec_review_routes.py integration (amendment cost estimation)
5. **Update imports:** 12 legacy test files → archive or rewrite for v5 stack

**Savings:** ~13,000 LOC (excluding tests)

### 8.3 Long-term (architecture)
1. **Unify schema model:** spec_compile_flat.py should use explicit v4 JSON schema files instead of inline schema construction (lines 657-750)
   - Benefit: Consistent schema management across compile paths
   - Cost: Minor refactor (~100 LOC)
2. **Centralize verdict types:** Move AuditResult, FeatureAudit, etc. to audit_types.py
   - Benefit: Shared across audit.py and audit_loop.py without circular imports
   - Cost: ~200 LOC refactor

---

## Appendix: Import Map

### spec_compile.py (legacy) imports
```
audit.py, audit_loop.py, build.py, 
cli_run.py, merge_queue.py, runner.py, 
seed.py, spec_amend.py
```

### spec_compile_flat.py (v5 i2p) imports
```
v5_runner.py, cli_v5.py, lead_verify.py,
v5_clean_verify.py, v5_verification_plan.py
```

**Zero cross-imports:** The two compile paths are cleanly separated.

---

## Files Referenced

- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/spec_compile.py` (5480 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/spec_compile_flat.py` (1643 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/build.py` (3818 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/audit.py` (3031 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/audit_loop.py` (691 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/spec_state.py` (706 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/repair_gates.py` (93 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/repair_evidence.py` (105 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/spec_warnings.py` (87 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/spec_amend.py` (691 LOC)
- `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/spec_schemas/` (5 JSON files)
