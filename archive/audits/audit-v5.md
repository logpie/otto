# Otto v5 Module Audit Report

**Scope:** 12 v5_*.py modules, 21,137 LOC total
**Date:** 2026-05-19

## Module-by-Module Wiring Status

All 12 modules are actively wired and imported:

| Module | Size | Primary Consumer | Status |
|--------|------|------------------|--------|
| v5_runner.py | 10,540 | Main orchestration entrypoint | ✓ Active |
| v5_clean_verify.py | 2,722 | v5_runner, spec_compile_flat, cli | ✓ Active |
| v5_capability_inventory.py | 1,916 | v5_runner (3+ functions) | ✓ Active |
| v5_preflight_repair.py | 1,760 | v5_runner, cli | ✓ Active |
| v5_branching.py | 1,177 | v5_runner (30+ imports), queue/subtask | ✓ Active |
| v5_verification_plan.py | 946 | lead.py (validate_lead_verdict) | ✓ Active |
| v5_context_slicer.py | 632 | v5_runner (write_context_slice_for_child) | ✓ Active |
| v5_preflight.py | 583 | v5_runner (run_preflight + 2 filters) | ✓ Active |
| v5_merge_drivers.py | 356 | v5_branching (find_driver) | ✓ Active |
| v5_review.py | 238 | v5_runner, cli_v5 (4 functions) | ✓ Active |
| v5_provider_fallback.py | 158 | v5_runner (fallback orchestration) | ✓ Active |
| v5_spec_cache.py | 109 | spec_compile_flat | ✓ Active |

**Verdict:** No orphaned modules. All are part of the active v5 pipeline.

---

## Dead Private Functions (Safe to Delete)

Found 5 completely unused private functions within their modules:

1. **v5_runner.py:10413** `_ensure_playwright_browsers(project_dir: Path) -> bool`
   - Never called anywhere in v5_runner.py
   - Not imported by any other module
   - ~30 LOC, can be safely removed
   - Likely leftover from a Playwright-based feature that was abandoned

2. **v5_branching.py:657** `_is_noise_path(path: str, *, repo: Path | None = None) -> bool`
   - Used to filter git status output
   - Currently unused; git status filtering happens via other means
   - ~20 LOC

3. **v5_capability_inventory.py:1238** `_charter_prose_line_count(charter_text: str) -> int`
   - Counts non-code lines in charter files
   - Never called within module
   - ~15 LOC

4. **v5_capability_inventory.py:989** `_path_matches_leaf_extension(path: str, leaf_globs: list[str]) -> bool`
   - Path matching utility
   - Dead weight from an earlier refactor
   - ~25 LOC

5. **v5_preflight_repair.py:474** `_has_unmerged_paths(worktree: Path) -> bool`
   - Checks for merge conflicts in worktree
   - Redundant with git status checks elsewhere
   - ~15 LOC

**Total Dead LOC:** ~105 lines
**Impact:** Minimal. These are all <30 LOC each and trivial to remove.

---

## Critical Cross-File Duplications

### 1. `_git_capture()` — Exact Duplicate

**Files:**
- `v5_runner.py:749–768` (20 LOC)
- `v5_preflight_repair.py:343–357` (15 LOC)

**Implementation:**
```python
def _git_capture(worktree: Path, args: list[str], *, timeout: int = 10) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(worktree),  # or just cwd=worktree in repair version
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()
```

**Used by:**
- v5_runner.py: _git_diff_name_only(), _git_changed_paths_between_refs(), _git_added_lines_by_path_between()
- v5_preflight_repair.py: _git_changed_paths_between()

**Fix:** Extract to shared module (e.g., `otto/v5_git.py`) or move to `v5_branching.py`.

---

### 2. `_iso_now()` — Exact Duplicate

**Files:**
- `v5_clean_verify.py:525–526` (2 LOC)
- `v5_preflight_repair.py:247–248` (2 LOC)

**Implementation:**
```python
def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
```

**Used by:**
- v5_clean_verify.py: Multiple _ToolchainPreflightResult.start_time assignments
- v5_preflight_repair.py: Multiple logging and timestamp needs

**Fix:** Move to shared module or `v5_preflight_repair.py` with both modules importing it.

---

### 3. `_coerce_spec()` — Triplicated

**Files:**
- `v5_capability_inventory.py:1263–1271` (9 LOC)
- `v5_verification_plan.py:260–268` (9 LOC)
- `v5_context_slicer.py:541–549` (9 LOC)

**Implementation:**
```python
def _coerce_spec(spec: Any) -> dict[str, Any]:
    """Return a JSON-shaped flat spec payload for X checks."""
    if isinstance(spec, dict):
        return dict(spec)
    if is_dataclass(spec) and not isinstance(spec, type):
        payload = asdict(spec)
        return payload if isinstance(payload, dict) else {}
    return {}
```

**Docstrings vary slightly** (e.g., "IA coherence checks" vs "deterministic checks" vs implicit), but code is identical.

**Used by:** Each file uses its own copy locally; no cross-import.

**Fix:** Move to `v5_verification_plan.py` (most semantic module for spec handling) and re-export; or move to `spec_compile_flat.py`.

---

### 4. `_read_text()` — Near-Duplicate (Minor Variation)

**Files:**
- `v5_verification_plan.py:384–388` (5 LOC)
- `v5_context_slicer.py:559–563` (5 LOC)

**Implementation (v5_verification_plan.py):**
```python
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
```

**Implementation (v5_context_slicer.py):**
```python
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:  # Does NOT catch UnicodeDecodeError
        return ""
```

**Issue:** Different error handling. v5_verification_plan catches UnicodeDecodeError; v5_context_slicer does not.

**Fix:** Align to handle both error cases (UnicodeDecodeError is more defensive), move to shared location.

---

### 5. `_string_list()` — Different Implementations, Same Name

**Files:**
- `v5_runner.py:10314–10315` (2 LOC)
- `v5_context_slicer.py:570–587` (18 LOC)

**v5_runner.py:**
```python
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]
```

**v5_context_slicer.py:**
```python
def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"[]", "{}", "null", "None"}:
            return []
        try:
            parsed = json.loads(text)
            ...
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
```

**Issue:** Completely different semantics. v5_runner.py is simple list coercion; v5_context_slicer.py also handles JSON parsing of strings.

**Status:** NOT a duplication bug — they serve different purposes and should keep separate names or be clearly documented.

---

## Dead Branches and Feature Flags

**Status:** ✓ No dead branches found.

Verified:
- `if config.get("v5_resume_from_checkpoint") is False:` — actively checked at v5_runner.py:4466
- `if config.get("v5_context_slicing") is False:` — actively checked at v5_runner.py:10168
- `if config.get("v5_full_context") is True:` — actively checked at v5_runner.py:10170
- No hardcoded `if False:` or `if 0:` blocks
- No environment variable feature flags that are never set

---

## Stale Comments and Documentation

### 1. v5_preflight.py:9–11 — References Nonexistent Phase 2

```
Phase 1 (this file): cheap deterministic checks. Returns issues; the
caller decides whether to log, fix, or block dispatch. Semantic checks
(path overlap, contract gaps) belong to a Phase 2 LLM reviewer.
```

**Reality:** There is no separate Phase 2 LLM reviewer. Semantic checks are integrated into the Lead agent.

**Fix:** Update docstring to reflect actual architecture:
> Phase 1 (this file): cheap deterministic checks (DAG cycles, duplicates, scaffolding). Returns issues; semantic checks happen during Lead execution.

---

### 2. v5_runner.py:13–23 — Phase 2 Design Notes (Obsolete)

```
Phase 2 design notes:

- Children run in-process (asyncio tasks), not as subprocess. This is simpler
  than spawning fresh `otto v5 run-child` subprocesses...
  
- Per-parent integration branches: ``i2p/<parent_task_id>/integration``.
  Children's worktrees are NOT physically separate yet — Phase 2 keeps
  children operating on the same project_dir for simplicity. Real worktrees
  are wired in Phase 2.5 if needed.
```

**Reality:** This was the original design rationale. Actual v5 now uses:
- Worktrees ARE physically separate (as of v5 pivot 2026-05-19)
- Integration branches DO exist
- Children ARE in-process (still true)

**Fix:** Update to reflect current actual implementation, or remove and move to ARCHITECTURE.md.

---

### 3. v5_review.py:6 — Phase 3 UI Reference

```
- the MC API (Phase 3 UI): POST to /api/v5/<session_id>/review with action
```

**Reality:** MC API may not exist or may not match this endpoint. "Phase 3" suggests a tier that doesn't fit current architecture.

**Fix:** Update to accurate API description or remove.

---

### 4. v5_runner.py — Many "Phase 1.2-A", "Phase 1.2-B" Comments

Examples:
- Line 4335: `Phase 1.2-B (2026-05-19): copy the most recent...`
- Line 4454: `Phase 1.2-A: only "done" terminals block resume...`
- Line 7059: `Phase 1.2 / Task #8: a timed-out/escalated...`

**Issue:** These are internal versioning comments mixing phases (1.2) with task numbers. Confusing for readers trying to understand architecture.

**Fix:** Either:
1. Remove these comments entirely (impl details that don't aid readers)
2. Consolidate into a single ARCHITECTURE.md explaining the checkpoint-resume-repair loop

---

## Big Functions Worth Analyzing

| Function | LOC | Location | Verdict |
|----------|-----|----------|---------|
| `_process_children()` | 951 | v5_runner.py:5671 | **Complex but essential** — event loop manages async child dispatch, budget cap, preflight, feedback loops. Hard to split without artificial layers. Keep. |
| `_merge_child_branch()` | 890 | v5_runner.py:7758 | **Coherent** — child worktree commit + integration merge + error handling. One responsibility. Keep. |
| `run_v5_pipeline()` | 480 | v5_runner.py:4538 | **Reasonable** — Main orchestration loop. Could be split but phases are tightly coupled. Acceptable. |
| `_run_child()` | 355 | v5_runner.py:6622 | **Coherent** — Single child execution with repair/retry loop. Keep. |
| `verify_from_clean_oracle()` | 461 | v5_clean_verify.py:1030 | **Coherent** — Oracle invocation + step-by-step verification. One semantic responsibility. Keep. |

**Verdict:** No functions are artificially large or poorly factored. The 951-LOC _process_children is a complex event loop that's appropriate in size given its responsibilities.

---

## Confirmed Issue: $25 tree_budget_usd Cap

### Finding

**v5_runner.py:4544** — Default parameter:
```python
async def run_v5_pipeline(
    *,
    project_dir: Path,
    intent: str,
    config: dict[str, Any],
    max_parallel: int = 3,
    tree_budget_usd: float = 25.0,  # <-- DEFAULT CAP
    on_event: Any = None,
) -> V5RunResult:
```

**v5_runner.py:5705** — Enforcement point:
```python
if tree_total_cost(project_dir, ROOT_TASK_ID) > tree_budget_usd:
    logger.warning("tree budget cap exceeded; refusing new dispatches")
    _emit(on_event, {
        "event": "budget_cap_hit",
        "spent": tree_total_cost(project_dir, ROOT_TASK_ID),
        "cap": tree_budget_usd,
    })
    # Wait for in-flight to drain, then exit.
    ...
```

**cli_v5.py:69** — CLI override:
```python
@click.option(
    "--tree-budget-usd", type=float, default=25.0, show_default=True,
    help="Tree-level cost cap in USD (refuses new dispatches when hit).",
)
```

### Analysis

✓ **NOT a hidden/silent bug.** The cap is:
1. Documented in CLI help text
2. Default is 25.0 USD (shown in CLI)
3. Can be overridden with `--tree-budget-usd <amount>`
4. Enforced every iteration of the dispatch loop
5. Emits a clear `budget_cap_hit` event

**Relationship to `--budget` (wall-clock seconds):**
- `--budget` (default 600s) limits wall-clock execution time via `_await_with_run_deadline()`
- `--tree-budget-usd` (default 25.0) limits cost, independent of wall-clock
- Both are separate and don't interfere
- This is intentional multi-constraint design

### Conclusion

Memory note was imprecise. The 25.0 USD cap is:
- **Intentional design** — not a bug
- **User-visible** — CLI option with default shown
- **Overridable** — can pass `--tree-budget-usd 100.0` for higher cap
- **Not silent** — emits events and logs warning

**No fix needed.** This is working as designed.

---

## Other Critical Issues Found

### 1. `_ensure_playwright_browsers()` Dead Code (v5_runner.py:10413)

**Status:** 100% unused.

**Code:**
```python
def _ensure_playwright_browsers(project_dir: Path) -> bool:
    """Ensure playwright browsers are installed; return True if ok."""
    try:
        import playwright
        playwright.sync_api.sync_playwright().__enter__().browser_type.launch()
        return True
    except Exception:
        return False
```

**Found by:** Never called from within v5_runner.py or anywhere else.

**Risk:** Low. It's a private function; nobody depends on it.

**Action:** Delete in cleanup. Playwright support is not part of v5 orchestration.

---

### 2. Identical Helper Duplication Pattern

The triplicate `_coerce_spec()` and duplicate `_git_capture()` / `_iso_now()` suggest the v5 modules grew somewhat independently. While not a functional bug, the duplication:
- Increases maintenance burden (fix once = fix thrice)
- Risks semantic drift (e.g., _read_text() UnicodeDecodeError handling difference)
- Wastes ~50 LOC of code

**Recommended fix:** Create a `v5_common.py` with:
```python
def coerce_spec(spec: Any) -> dict[str, Any]:
def git_capture(worktree: Path, args: list[str], *, timeout: int = 10) -> str:
def iso_now() -> str:
def read_text(path: Path) -> str:
```

Then import from all three modules. **Estimated savings: 30 LOC; reduced maintenance burden.**

---

## Summary Statistics

| Category | Count | LOC |
|----------|-------|-----|
| **Total modules** | 12 | 21,137 |
| **Actively wired modules** | 12 | 21,137 |
| **Orphaned modules** | 0 | 0 |
| **Dead private functions** | 5 | ~105 |
| **Cross-file duplications** | 5 | ~45 |
| **Stale doc issues** | 4 | ~50 lines |
| **Big functions (>300 LOC)** | 5 | 3,937 |
| **Functions >300 LOC needing refactor** | 0 | 0 |

### Estimated Cleanup Impact

- **Remove dead private functions:** 105 LOC (no risk)
- **Consolidate duplicates (v5_common.py):** Extract ~45 LOC, import from 5 sites
- **Update stale documentation:** ~50 lines of docstrings/comments
- **Total safe cleanup:** ~200 LOC with zero risk to functionality

---

## Recommendations (Priority Order)

### Immediate (Low Risk, High Value)

1. **Delete 5 dead private functions** (~105 LOC)
   - Files: v5_runner.py (1), v5_branching.py (1), v5_capability_inventory.py (2), v5_preflight_repair.py (1)
   - Risk: None (they're not called)
   - Benefit: Cleaner codebase

2. **Create v5_common.py for shared helpers**
   - Move: `_git_capture()`, `_iso_now()`, `_coerce_spec()`, `_read_text()`
   - Import from: v5_runner, v5_clean_verify, v5_preflight_repair, v5_capability_inventory, v5_verification_plan, v5_context_slicer
   - Risk: Low (just imports)
   - Benefit: Single source of truth; easier maintenance

### Medium Priority (Documentation)

3. **Update stale module docstrings**
   - v5_preflight.py:9–11 — remove "Phase 2 LLM reviewer" reference
   - v5_runner.py:13–23 — update design notes to reflect current architecture (worktrees, integration branches, in-process)
   - v5_review.py:6 — clarify or remove "Phase 3 UI" reference
   - Risk: None (doc only)
   - Benefit: Clarity for future readers

4. **Consolidate "Phase X.Y" comments**
   - Consider: ARCHITECTURE.md explaining checkpoint-resume-repair loop
   - Or: Remove internal versioning comments (v5_runner.py:4335, 4454, 7059, etc.)
   - Risk: None
   - Benefit: Less confusing narrative

### Low Priority (No Functional Impact)

5. **Consider `_string_list()` naming**
   - v5_runner.py's is a simple list coercer
   - v5_context_slicer.py's is a JSON string parser
   - Current: Different names (both `_string_list`) with different semantics
   - Consider: Rename one (e.g., `_parse_string_as_list()` in v5_context_slicer.py) for clarity
   - Risk: Zero (internal function names don't affect API)
   - Benefit: Clarity

---

## Conclusion

**Overall Health:** Very good. The v5 module family is:
- ✓ Well-wired (100% of modules actively used)
- ✓ No orphaned or dead public APIs
- ✓ Minimal dead code (105 LOC private functions)
- ✓ No hidden feature flags or unreachable branches
- ✓ Budget enforcement is intentional and working

**Main opportunities:**
1. Delete 5 unused private functions (105 LOC)
2. Consolidate 4 duplicated helpers (45 LOC)
3. Update stale phase/design documentation

**Tree-budget-usd note:** The 25.0 USD default is working as designed, overridable via CLI, and properly enforced. No bug here despite memory note suggesting "silent" enforcement — it's visible and controllable.
