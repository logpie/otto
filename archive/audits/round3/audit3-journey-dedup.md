# Audit 3: Journey Executor Deduplication Plan

**Objective:** Consolidate ~140 LOC of verified duplication across API/UI executors, verdict logic, and runner detection without changing semantics.

---

## Item 1: Executor Result Wrapper Functions (~40 LOC saved)

**Duplication verified:** Both `journey_api_executor.py` and `journey_ui_executor.py` define wrapper pairs that immediately delegate to shared functions from `journey_executor_common.py`.

### API Executor wrappers (lines 635-664)

```python
def _pass_result(journey, *, detail, artifact_paths):
    return executor_pass_result(journey, source=API_EXECUTOR_SOURCE, detail=detail, artifact_paths=artifact_paths)

def _non_pass_result(journey, *, status, detail, proof_usable, artifact_paths):
    return executor_non_pass_result(journey, source=API_EXECUTOR_SOURCE, status=status, detail=detail, 
                                    proof_usable=proof_usable, artifact_paths=artifact_paths)
```

### UI Executor wrappers (lines 621-654)

```python
def _result_payload(journey, *, status, detail, proof_usable, artifact_paths):
    return executor_result_payload(journey, source=UI_EXECUTOR_SOURCE, status=status, detail=detail,
                                   proof_usable=proof_usable, artifact_paths=artifact_paths)

def _non_pass_result(journey, *, status, detail, proof_usable, artifact_paths):
    return executor_non_pass_result(journey, source=UI_EXECUTOR_SOURCE, status=status, detail=detail,
                                    proof_usable=proof_usable, artifact_paths=artifact_paths)
```

### Issue

These are pure pass-through wrappers adding only a `source=` constant. Each executor defines its own version with a different source constant (API_EXECUTOR_SOURCE vs UI_EXECUTOR_SOURCE).

### Solution

**Step 1:** Remove both `_pass_result()` and `_non_pass_result()` wrappers from both executors.

**Step 2:** Replace all callsites to use the shared functions directly with an import alias for clarity.

**API Executor patch:**

1. Remove lines 635-664 entirely
2. At line 21-26 (imports), add alias:
   ```python
   from otto.journey_executor_common import (
       ...
       executor_pass_result as _executor_pass_result,
       executor_non_pass_result as _executor_non_pass_result,
   )
   ```
3. Replace all `_pass_result(...)` with `_executor_pass_result(..., source=API_EXECUTOR_SOURCE, ...)`
4. Replace all `_non_pass_result(...)` with `_executor_non_pass_result(..., source=API_EXECUTOR_SOURCE, ...)`

**Call sites to update:**

- journey_api_executor.py:113: `_non_pass_result` → `_executor_non_pass_result`
- journey_api_executor.py:138: `_non_pass_result` → `_executor_non_pass_result`
- journey_api_executor.py:146: `_non_pass_result` → `_executor_non_pass_result`
- journey_api_executor.py:166,175,189,194,201,206,226,235,248,265,276,287: `_non_pass_result` calls (16 total)
- journey_api_executor.py:297: `_write_http_result` calls `_pass_result` indirectly (line 625)
- journey_api_executor.py:380-384: `_pass_result` / `_non_pass_result` (2 calls)
- journey_api_executor.py:467,491: `_pass_result` (2 calls)
- journey_api_executor.py:547-551: `_pass_result` (1 call)

**UI Executor patch:**

1. Remove `_result_payload()` (lines 621-636) — use `executor_result_payload` directly
2. Keep `_non_pass_result()` but change to call shared function directly (no wrapper needed)
3. Update `_finalize_journey()` at line 608 to call `executor_result_payload` directly instead of `_result_payload`

**Call sites in UI Executor to update:**

- journey_ui_executor.py:200,206: `_non_pass_result` (2 calls)
- journey_ui_executor.py:250-259, 266-289, 304-314, 319-329, 342-352, 354-364, 366-376: `_non_pass_result` (13+ calls via _finalize_journey)
- journey_ui_executor.py:608: `_result_payload()` → `executor_result_payload()`
- journey_ui_executor.py:659-665: `_non_pass_result` in _infra_results (1 call)

### Before/After Snapshot

**Before (API):**
```python
# Line 113
return _non_pass_result(journey, status="unverified", detail=f"...", proof_usable=False, artifact_paths=[])

# Line 649-664
def _non_pass_result(journey, *, status, detail, proof_usable, artifact_paths):
    return executor_non_pass_result(journey, source=API_EXECUTOR_SOURCE, status=status, ...)
```

**After (API):**
```python
# Line 113 (same place, simpler)
return executor_non_pass_result(journey, source=API_EXECUTOR_SOURCE, status="unverified", 
                                detail=f"...", proof_usable=False, artifact_paths=[])

# Lines 649-664 deleted
```

---

## Item 2: Consolidate Verdict Aggregation (~60 LOC saved)

**Duplication verified:** Both `lead_verify.py` (lines 149-154) and `v5_clean_verify.py` (lines 977-982) call `resolve_journey_verdicts()` identically, then aggregate results identically.

### Current patterns

**lead_verify.py:149-157:**
```python
journey_results = resolve_journey_verdicts(
    journeys=journeys_in_scope,
    execution_scope=execution_scope,
    executor_results=api_executor_results,
    registered_executor_levels={"ui", "api"},
)
api_ids = {str(journey.get("id") or "") for journey in api_journeys}
api_results = [item for item in journey_results if str(item.get("id") or "") in api_ids]
api_failed = failed_journey_ids(api_results)
api_passed = sum(1 for item in api_results if item.get("passed") is True)
```

**v5_clean_verify.py:977-982:**
```python
verdicts = resolve_journey_verdicts(
    journeys=journeys,
    execution_scope=journey_scope,
    executor_results=probe.executor_results,
    registered_executor_levels={"ui", "api"},
)
```

### Issue

The verdict resolution is the canonical source of truth, but callers independently post-process results. If verdict resolution policy changes, both sites must track the change or diverge.

### Solution

**No consolidation required.** The audit identified this as a "silent divergence" risk, but inspection reveals:
- `lead_verify.py` calls resolve once, then post-filters for API results only
- `v5_clean_verify.py` calls resolve once for all journeys
- The logic is NOT duplicated; they serve different purposes (one filters API-only, one returns all)

**Action:** Document in journey_verdict_sink.py that `resolve_journey_verdicts()` is the canonical policy and both callers depend on it. Add a comment to both callers:

```python
# Lead verification filters API-only results for legacy reporting, but
# all verdicts come through journey_verdict_sink.resolve_journey_verdicts().
```

**Recommendation:** SKIP this consolidation. Both callers use the same function correctly. The "drift risk" is overblown — changing the policy in one place changes it for both.

---

## Item 3: Executor Summary Writing (~20 LOC potential)

**Duplication verified:** Both executors write nearly identical summary JSON structures.

### API Executor (lines 82-92)

```python
summary_path = run_dir / "api-executor-summary.json"
write_json_atomic(summary_path, {
    "_written_at": iso_timestamp(),
    "source": API_EXECUTOR_SOURCE,
    "executor_results": executor_results,
}, trailing_newline=True)
artifact_paths.append(summary_path)
```

### UI Executor (lines 677-688)

```python
summary_path = run_dir / "executor-results.json"  # Different filename!
write_json_atomic(summary_path, {
    "_written_at": iso_timestamp(),
    "source": UI_EXECUTOR_SOURCE,
    "infra_error": infra_error,
    "executor_results": executor_results,
    "artifact_paths": [str(path) for path in artifact_paths],
}, trailing_newline=True)
return summary_path
```

### Issue

The filenames differ (`api-executor-summary.json` vs `executor-results.json`) and UI includes extra fields. Cannot consolidate without breaking downstream readers.

### Recommendation

**SKIP consolidation.** The schemas have diverged enough (filename, UI-specific fields like `infra_error` and `artifact_paths` at top level) that a shared function would require parameterization and lose clarity. Each executor owns its own summary format.

---

## Item 4: Browser Runner Detection Duplication (~15 LOC saved)

**Status:** The audit claimed duplication at `lead_verify.py:315-329` and `v5_clean_verify.py:320-334`, but inspection shows `v5_clean_verify.py` does NOT have a `_detect_browser_runner()` function (grep returns zero matches).

### Current state

Only `lead_verify.py:315-329` contains:

```python
def _detect_browser_runner(project_dir: Path) -> str | None:
    """Detect a browser journey runner. Returns a shell command or None."""
    runner = project_dir / "tests" / "run_browser_journey.py"
    if runner.is_file():
        return f"python3 {runner}"
    if (project_dir / "package.json").is_file():
        try:
            pkg = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            if isinstance(scripts, dict) and "browser" in scripts:
                return "npm run browser"
        except (OSError, json.JSONDecodeError):
            pass
    return None
```

### Recommendation

**SKIP consolidation.** No duplicate exists in the current codebase. The audit's claim was based on stale code or a commit that was already cleaned up.

---

## Item 5: _fail_closed() Metadata Loss (Bug Fix, Priority: MEDIUM)

**Bug verified:** journey_verdict_sink.py:125-133 constructs a verdict dict without preserving original journey metadata.

### Current implementation

```python
def _fail_closed(jid: str, *, source: str, detail: str) -> dict[str, Any]:
    return {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": False,
        "status": "unverified",
    }
```

### Problem

If a downstream system (e.g., proof packet renderer) expects `feature_id` or `covers_primary_actions` to link journeys back to features, this function silently loses that metadata.

### Call sites

- journey_verdict_sink.py:35 (line 35): applicability fails
- journey_verdict_sink.py:49 (line 49): missing verification_level
- journey_verdict_sink.py:60 (line 60): no executor result
- journey_verdict_sink.py:66 (line 66): no executor registered

All sites have the journey object available. Changing signature is safe.

### Solution

**Change `_fail_closed()` to accept journey object and preserve top-level metadata:**

```python
def _fail_closed(journey: dict[str, Any], *, source: str, detail: str) -> dict[str, Any]:
    jid = str(journey.get("id") or "").strip() or "<unnamed>"
    result = {
        "id": jid,
        "passed": False,
        "detail": detail,
        "source": source,
        "proof": False,
        "status": "unverified",
    }
    # Preserve metadata keys that downstream systems may depend on
    for key in ("feature_id", "covers_primary_actions", "group_id", "verification_level"):
        if key in journey:
            result[key] = journey[key]
    return result
```

**Update call sites:**

- Line 35: `_fail_closed(journey, source=..., detail=...)`
- Line 49: `_fail_closed(journey, source=..., detail=...)`
- Line 60: `_fail_closed(journey, source=..., detail=...)`
- Line 66: `_fail_closed(journey, source=..., detail=...)`

---

## Implementation Order & Risk Assessment

| Item | Complexity | Risk | LOC Saved | Effort |
|------|-----------|------|-----------|--------|
| 1. Remove executor wrappers | High | Very Low | 40 | ~30 min |
| 2. Verdict aggregation | None | N/A | 0 | 5 min (docs) |
| 3. Summary writing | None | N/A | 0 | Skip |
| 4. Browser runner | None | N/A | 0 | Skip |
| 5. Fix _fail_closed metadata | Medium | Low | 0 | ~20 min |

### Phase 1 (Low Risk) → Execute First
1. Fix `_fail_closed()` to preserve metadata (Item 5)
2. Add docs comment to journey_verdict_sink.py (Item 2)

### Phase 2 (Medium Risk, Higher Effort) → Execute After Phase 1 Verification
1. Remove executor wrappers in API executor (Item 1a)
2. Verify no test failures
3. Remove/simplify executor wrappers in UI executor (Item 1b)

---

## Summary

- **Confirmed duplications:** 2 (executor wrappers, missing metadata in _fail_closed)
- **False positives in audit:** 3 (verdict aggregation is intentional, summary writing is schema-diverged, browser runner duplication doesn't exist)
- **Total LOC consolidation possible:** ~40 LOC (from wrapper removal)
- **Total bugs fixed:** 1 (metadata loss in _fail_closed)
- **Zero behavior changes if executed correctly.**
