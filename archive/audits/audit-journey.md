# Journey/Lead Executor Family Audit

**Scope:** otto/journey_api_executor.py (936 LOC), otto/journey_ui_executor.py (1537 LOC), otto/journey_executor_common.py (72 LOC), otto/journey_contracts.py (759 LOC), otto/journey_scope_policy.py (72 LOC), otto/journey_verdict_sink.py (144 LOC), otto/lead.py (1439 LOC), otto/lead_verify.py (444 LOC), plus audit.py (3031 LOC) and audit_loop.py (691 LOC).

---

## Module Dependency Graph

```
┌─ journey_executor_common.py (5 public, 0 private)
│   ├─ artifact_run_name(), journey_id()
│   ├─ executor_result_payload(), executor_pass_result(), executor_non_pass_result()
│   └─ USED BY: journey_api_executor, journey_ui_executor

├─ journey_scope_policy.py (4 public, 0 private)
│   ├─ ExecutionScope, VerificationLevel (type aliases)
│   ├─ applicability_for(), infer_execution_scope(), node_kind_for_scope()
│   ├─ validate_policy_exhaustive()
│   └─ USED BY: lead.py, lead_verify.py, v5_clean_verify.py, v5_verification_plan.py, journey_verdict_sink.py, mcp_tools.py, v5_runner.py

├─ journey_contracts.py (5 public, 14 private) ← LARGEST AT 759 LOC
│   ├─ VerificationContractError (exception), VerificationLevel, ProbeKind (types)
│   ├─ normalize_journey_contracts() [main entry]
│   ├─ assign_verification_level(), synthesize_ui_pass_model()
│   ├─ validate_ui_pass_model(), validate_api_pass_model()
│   ├─ 14 private helpers (_normalize_one_journey, _cold_start_state_ids, etc.)
│   └─ USED BY: journey_api_executor, journey_ui_executor, spec_compile.py, spec_compile_flat.py, v5_verification_plan.py

├─ journey_verdict_sink.py (2 public, 4 private) — VERDICT LOGIC
│   ├─ resolve_journey_verdicts() [fail-closed verdict resolution]
│   ├─ failed_journey_ids()
│   ├─ 4 private: _normalize_executor_verdict(), _fail_closed(), _not_applicable(), _index_results()
│   └─ USED BY: lead_verify.py, v5_clean_verify.py, v5_verification_plan.py

├─ journey_api_executor.py (1 public, 26 private) ← 936 LOC
│   ├─ APIJourneyExecutorRun (dataclass)
│   ├─ run_api_journey_executor() [main entry]
│   ├─ 26 private helpers for HTTP, CLI, library, service health probes
│   └─ USED BY: lead_verify.py:run_verify_for_lead()

├─ journey_ui_executor.py (1 public, 45 private) ← LARGEST AT 1537 LOC
│   ├─ UIJourneyExecutorRun (dataclass)
│   ├─ run_ui_journey_executor() [main entry]
│   ├─ 45 private helpers for Playwright-based DOM/network/observable testing
│   └─ USED BY: v5_clean_verify.py:run_ui_journey_executor()

├─ lead.py (1 public, 36 private) ← 1439 LOC, ORCHESTRATOR
│   ├─ LeadKind, LeadVerdict, LeadResult (types/dataclass)
│   ├─ run_lead() [main v5 build primitive]
│   ├─ 36 private helpers (prompt rendering, verdict reading/recovery, sanitization)
│   └─ COMPLEX: verdict recovery, intent sanitization, prompt interpolation

├─ lead_verify.py (1 public, 8 private) ← 444 LOC, VERIFICATION GATE
│   ├─ VerifyVerdict type
│   ├─ run_verify_for_lead() [runs native tests + API/browser journeys]
│   ├─ Layers: native tests → browser journeys → API journeys
│   ├─ IMPORTS: run_api_journey_executor, resolve_journey_verdicts, applicability_for
│   └─ SPLIT: _run_native_tests(), _run_browser_journey() (isolated executors)

├─ audit.py (4 public, 42 private) ← 3031 LOC, LEGACY/AUDIT AGENT
│   ├─ AuditVerdict, AuditResult, FeatureAudit, AuditAgentInput/Output (types)
│   ├─ run_audit() [standalone audit orchestrator]
│   ├─ default_audit_agent() [LLM judge for spec compliance]
│   ├─ no_op_walkthrough(), default_walkthrough_from_spec()
│   ├─ 42 private helpers (heavy lifting for proof, quality scoring)
│   └─ LEGACY: pre-v5 architecture; coexists for Phase A compatibility

└─ audit_loop.py (5 public, 7 private) ← 691 LOC, LAYER 2 REPAIR
    ├─ select_failing_features(), features_to_repair()
    ├─ can_run_another_audit_pass(), repair_failing_features()
    ├─ group_for_feature()
    ├─ 7 private helpers (cost calc, repair gating)
    └─ ROLE: Feature-level Layer 2 repair loop (after audit verdicts)
```

### Cross-Module Call Sites
- **lead_verify.py** calls **journey_api_executor.run_api_journey_executor()**
- **lead_verify.py** calls **journey_verdict_sink.resolve_journey_verdicts()**
- **lead.py** imports **journey_scope_policy** for **infer_execution_scope()**
- **journey_verdict_sink.py** imports **journey_scope_policy.applicability_for()**
- **Both executors (api/ui)** import **journey_contracts** validators
- **v5_clean_verify.py** mirrors **lead_verify.py** logic (separate implementation)

---

## API ↔ UI Executor Diff Summary

### **Structural Similarity (Copy-Paste Pattern)**

Both executors follow the same pattern:
1. Entry point: `run_*_journey_executor(journeys, project_dir, artifact_dir, ...)`
2. Dataclass result: `APIJourneyExecutorRun` / `UIJourneyExecutorRun`
3. Main loop: iterate journeys → run individual journey → collect results → write summary
4. Helper utilities: result builders (`_pass_result`, `_non_pass_result`, etc.)
5. Contract validation upfront via **journey_contracts.validate_ui_pass_model()** / **validate_api_pass_model()**

### **Execution Logic (Where They Diverge)**

| Aspect | API Executor | UI Executor |
|--------|--------------|------------|
| **Technology** | urllib, subprocess, direct Python import | Playwright browser automation |
| **Probe Types** | 4 hardcoded: http_api, cli_command, library_call, service_health | Single declarative Playwright loop |
| **Setup** | None | Runs pass_model.setup (precondition phase) |
| **Action Loop** | N/A | Iterates pass_model.actions, waits for observables |
| **Network Tracking** | Event collection in http_api only | Global _NetworkEvent list + expect_response() |
| **DOM Assertions** | N/A | _assert_dom_observable() with signature fingerprinting |
| **Error Handling** | Try/except at probe level; urllib.HTTPError handling | Try/except + Playwright timeout handling |
| **Infra Errors** | Returns unverified on import/exec failures | Returns unverified + infra_error flag on Playwright unavailable |
| **Post-Journey** | Writes http-events.json or CLI logs | Writes screenshot.png, dom.html, network.jsonl, console-errors.jsonl |

### **Result Schema Differences**

Both use `executor_result_payload()` from journey_executor_common, but UI adds:
- `infra_error` flag (returned separately in UIJourneyExecutorRun)
- Screenshot/DOM/network artifacts (journey_dir subdirs)
- Console error capture (populated during browser session)

### **Copy-Paste Duplication Level**

**High structural duplication:**
- Both have identical `_pass_result()`, `_non_pass_result()` (lines 635-665 in api vs 621-639 in ui)
- Result payload construction: identical pattern via shared `executor_result_payload()`
- Summary writing: both write to `{executor}-summary.json` with identical structure
- Both use same timestamp/slug helpers from journey_executor_common

**Could consolidate via base class:**
```python
class JourneyExecutorBase:
    def __init__(self, executor_source: str):
        self.executor_source = executor_source
        self.executor_results = []
        self.artifact_paths = []
    
    def _pass_result(self, journey, detail, artifact_paths):
        return executor_pass_result(journey, source=self.executor_source, ...)
    
    def _write_summary(self, run_dir, summary_text):
        # Shared logic
```

**Estimated savings:** 50-80 LOC (mostly boilerplate around result builders).

---

## Centralization Candidates

### **1. Journey Verdict Logic (SPREAD ACROSS 4 FILES)**

**Current state:**
- `journey_verdict_sink.py` (144 LOC): resolve_journey_verdicts(), failed_journey_ids()
- `lead_verify.py` (lines 149-154, 175-184): calls sink + aggregates verdicts
- `v5_clean_verify.py` (parallel implementation, ~100 LOC): duplicates all verdict logic
- `v5_verification_plan.py` (lines 19-37): similar verdict aggregation

**Problem:** The verdict resolution policy (applicability + fail-closed logic) is duplicated across v5_clean_verify.py and implicitly in lead_verify.py. Changing the policy requires touching 3 places.

**Recommendation:**
- Keep `journey_verdict_sink.resolve_journey_verdicts()` as the canonical source of truth.
- Refactor `v5_clean_verify.py` to import and use it.
- Create a `JourneyVerificationResult` dataclass to standardize the output:
  ```python
  @dataclass
  class JourneyVerificationResult:
      verdicts: list[dict[str, Any]]
      failed_ids: list[str]
      passed_count: int
      summary: str
  ```
- Estimated savings: 40-60 LOC (v5_clean_verify would shrink by ~40 LOC).

### **2. Executor Result Builders (PARTIALLY SHARED)**

**Current state:**
- `journey_executor_common.py`: executor_result_payload(), executor_pass_result(), executor_non_pass_result()
- Both executors redefine `_pass_result()` and `_non_pass_result()` (wrapper pairs)

**Problem:** The executors add a wrapper layer that just calls the common functions. Unnecessary indirection.

**Recommendation:**
- Remove executor-local `_pass_result()` and `_non_pass_result()` wrapper functions
- Call `executor_pass_result()` and `executor_non_pass_result()` directly (or inline via local import alias)
- Estimated savings: 20 LOC per executor (40 LOC total).

### **3. Result Payload Schema (DUPLICATED)**

Both executors write nearly identical summary JSON:
```json
{
  "_written_at": "ISO_TIMESTAMP",
  "source": "api_executor" | "ui_executor",
  "executor_results": [...]  // identical structure
}
```

The only difference is the source string. Could consolidate:
```python
# journey_executor_common.py
def write_executor_summary(run_dir, executor_results, executor_source):
    write_json_atomic(run_dir / f"{executor_source}-summary.json", {...})
```

Estimated savings: 20 LOC.

### **4. Setup/Action Precondition Validation (DUPLICATED LOGIC)**

`journey_ui_executor._run_setup()` (lines 386-422) mirrors contract validation from `journey_contracts.validate_ui_pass_model()` — both check that setup steps are "executable" (have route or UI locator).

The validation is in contracts at lines 208-220, but the UI executor still re-checks at lines 406-410 in _run_setup(). This is defensive but redundant.

**Recommendation:** Trust journey_contracts validation (it already runs pre-execution in _run_one_journey line 237). Remove redundant checks in _run_setup() (they fire after validation already passed).

Estimated savings: 10 LOC.

---

## Dead Modules / Dead Functions

### **Zero Dead Exports**
All public functions are imported:
- `run_api_journey_executor`: called by lead_verify.py:125
- `run_ui_journey_executor`: called by v5_clean_verify.py:968 (4 times)
- `run_verify_for_lead`: called by mcp_tools.py:437
- `run_lead`: called by v5_runner.py (core)
- All journey_scope_policy exports: used by 6+ modules
- All journey_contracts exports: used by spec_compile.py, spec_compile_flat.py, v5_verification_plan.py

### **Potentially Unused Private Functions**

**journey_ui_executor.py:**
- `_DOMObservableState` dataclass: defined line 76, only used in _assert_dom_observable (line 788) — **utilized**
- `_NetworkEvent` dataclass: defined line 57, only used in _finalize_journey (line 597) — **utilized**
- All 45 private funcs are called from _run_one_journey or _finalize_journey; none are dead.

**journey_api_executor.py:**
- All 26 private funcs are in PROBE_EXECUTORS dict (line 586) or called from probes — none are dead.

**lead.py:**
- `_is_runtime_hint_label()` (lines 464-513): complex token-set logic, called by _sanitize_runtime_invariant_lines (line 521) — **utilized but could simplify**
- `_verdict_recovery_warning()` / recovery functions: all called in _read_agent_verdict (lines 756-872) — **utilized** but this is complex verdict recovery code (130+ LOC) that could be moved to separate module

**lead_verify.py:**
- All 8 private functions are called within the module or by run_verify_for_lead — none dead.

### **Verdict Recovery Code (Complex, Isolated)**

**lead.py lines 756-1147:** Contains 10+ functions (350+ LOC) dedicated to verdict recovery:
- `_read_agent_verdict()` → delegates to 4 rescue strategies
- `_rescue_verdict_from_write_tool_inputs()`
- `_rescue_verdict_from_messages()`
- `_extract_verdict_payload_from_write_input()`
- `_rewrite_canonical_verdict()`
- `_canonicalize_verdict_payload()` (80+ LOC)
- `_verdict_token_to_canonical()`, `_noncanonical_verdict()`, etc.

**Verdict recovery is not dead**, but could be extracted to a separate `otto/verdict_recovery.py` module for clarity. This would:
- Reduce lead.py from 1439 → ~1100 LOC
- Make verdict schema migration/recovery testable independently
- Estimated savings (via clarity, not LOC count): improves maintainability

---

## Critical Bugs Spotted

### **1. v5_clean_verify.py Duplicates lead_verify.py Logic Without Sync Mechanism**

**Location:** /otto/v5_clean_verify.py (lines 48-154 mirrors lead_verify.py lines 83-154)

**Issue:** When verdict resolution policy changes in journey_verdict_sink.py, v5_clean_verify.py must be updated manually. No shared code path.

```python
# lead_verify.py:149-154
journey_results = resolve_journey_verdicts(
    journeys=journeys_in_scope,
    execution_scope=execution_scope,
    executor_results=api_executor_results,
)

# v5_clean_verify.py:83-88 (parallel, independent)
journey_results = resolve_journey_verdicts(
    journeys=journeys_in_scope,
    execution_scope=execution_scope,
    executor_results=api_executor_results,
)
```

**Impact:** If a verdict edge case is fixed in one path, the other path remains broken. Example: if `applicability_for()` policy changes, both callers must update independently.

**Fix:** Extract verdict aggregation to shared function in journey_verdict_sink.py that both callers use.

**Severity:** **MEDIUM** — both callsites exist, but silent divergence over time is a maintainability trap.

---

### **2. journey_contracts.validate_ui_pass_model() Requires Journeys to Pre-Exist**

**Location:** journey_contracts.py lines 378-401

**Issue:** The validator raises VerificationContractError if pass_model is missing, but _run_one_journey in journey_ui_executor.py (line 237) **already validated** it:

```python
# journey_ui_executor.py:237
validate_ui_pass_model(journey, path=f"...")
pass_model = journey["pass_model"]  # Assumes it exists after validation
if not isinstance(pass_model, dict):
    raise VerificationContractError(...)  # Redundant check
```

The contract validator should guarantee pass_model exists and is a dict after validation succeeds. The redundant isinstance() check (lines 238-239) is a code smell indicating uncertain trust in the validator.

**Fix:** Document in journey_contracts.py that validate_ui_pass_model() guarantees pass_model is present and dict-typed. Remove isinstance() check in _run_one_journey.

**Severity:** **LOW** — defensive programming, but indicates unclear contract.

---

### **3. lead_verify.py and v5_clean_verify.py Both Detect Browser Runners (Duplicate Logic)**

**Locations:**
- lead_verify.py:315-329 (_detect_browser_runner)
- v5_clean_verify.py:320-334 (_detect_browser_runner — identical copy)

**Issue:** Runner detection code is duplicated. If a new Playwright runner convention emerges (e.g., `tests/e2e.ts`), both files must be updated.

**Fix:** Move to journey_executor_common.py or new otto/journey_runners.py.

**Severity:** **LOW** — code duplication, not a correctness bug.

---

### **4. journey_verdict_sink._fail_closed() Doesn't Preserve Original Journey Metadata**

**Location:** journey_verdict_sink.py:125-133

**Issue:** When a journey's verdict is downgraded to "unverified" via _fail_closed(), the response object loses the original journey metadata (e.g., feature_id, covers_primary_actions) that might be needed downstream:

```python
def _fail_closed(jid: str, *, source: str, detail: str) -> dict[str, Any]:
    return {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": False,
        "status": "unverified",
        # Missing: feature_id, covers_primary_actions, etc. from original journey
    }
```

**Impact:** If a downstream system (e.g., proof packet renderer) tries to link journeys back to features, it can only use the id string, not structured metadata.

**Fix:** Pass the original journey object to _fail_closed() and merge metadata into result:

```python
def _fail_closed(journey: dict[str, Any], *, source: str, detail: str) -> dict[str, Any]:
    return {
        "id": journey.get("id"),
        "feature_id": journey.get("feature_id"),  # Preserved
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": False,
        "status": "unverified",
    }
```

**Severity:** **LOW-MEDIUM** — potential for data loss in downstream systems that expect journey metadata.

---

## Estimated LOC Savings

| Consolidation | LOC Saved | Effort | Risk |
|---|---|---|---|
| Remove executor wrapper functions (_pass_result, _non_pass_result) | 40 | Low | Very Low |
| Extract verdict aggregation to shared function | 60 | Medium | Low |
| Consolidate executor summary writing | 20 | Low | Very Low |
| Move verdict recovery to separate module | 0* | Medium | Low |
| Remove redundant pass_model isinstance() check | 5 | Very Low | Very Low |
| Deduplicate browser runner detection | 15 | Low | Very Low |
| **Total** | **~140 LOC** | — | — |

*Verdict recovery extraction doesn't save LOC directly; it improves clarity and testability.

---

## Summary

### **Key Findings**

1. **API/UI Executors are structurally similar but with divergent logic** — copy-paste pattern for boilerplate (result builders, summary writing) but fundamentally different probe implementations. Consolidation via base class is feasible for ~40-50 LOC savings.

2. **Verdict resolution logic is split across 3+ files** — journey_verdict_sink.py is the canonical source, but v5_clean_verify.py and lead_verify.py independently apply the same logic. Silent divergence risk.

3. **Verdict recovery in lead.py is complex (350+ LOC)** — not dead code, but isolated enough to extract into separate module for clarity and testability.

4. **No truly dead modules or exports** — all public APIs are called. Private functions are fully utilized (checked via call graph).

5. **Two moderate bugs found:**
   - Duplicate verdict aggregation across v5_clean_verify.py and lead_verify.py without sync mechanism
   - Redundant pass_model validation in journey_ui_executor after contracts already validated

### **Refactoring Priority**

1. **High impact / Low effort:** Remove executor wrapper functions (40 LOC, trivial change)
2. **High impact / Medium effort:** Extract shared verdict aggregation (60 LOC saved + prevents future bugs)
3. **Medium impact / Low effort:** Consolidate browser runner detection (15 LOC)
4. **Polish / Medium effort:** Extract verdict recovery to separate module (structural improvement)

**Total opportunity: ~140 LOC consolidation + improved maintainability without touching core logic.**
