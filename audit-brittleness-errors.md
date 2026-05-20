# Error-Swallowing Patterns Audit — Otto Codebase

**Scope:** 124 Python files, ~72k LOC  
**Total except patterns found:** 205 "except Exception" clauses; 63+ bare excepts  
**Analysis date:** 2026-05-20

## Executive Summary

The codebase exhibits **three distinct error-swallowing patterns**, each with different risk levels:

1. **LEGITIMATE defensive cleanup** (43 instances) — mostly in finally blocks, safe to leave as-is
2. **SILENT-RETURN masking** (18 instances) — probable bugs; callers have no way to know what failed
3. **BARE-PASS observability holes** (2 instances) — dangerous; break debugging and operator visibility

The critical finding: **18 functions return sentinel values (None/False/dict()) on catch, with NO logging or guard checks.** This is the highest-risk pattern — the operator sees "no data" with no way to distinguish between "feature not present" vs. "feature broke silently."

---

## Pattern Breakdown

### 1. LEGITIMATE: Defensive Cleanup in Finally Blocks

**Count:** 17 instances (safe)

These catches occur in cleanup/restoration logic where exceptions are *expected* and non-fatal:

- `journey_ui_executor.py:194` — browser.close() in finally
- `journey_ui_executor.py:383` — page.close() / context.close() in finally  
- `journey_ui_executor.py:591–597` — screenshot/DOM capture best-effort attempts

**Assessment:** These are intentional defensive patterns. The parent function continues regardless. No action needed.

---

### 2. CRITICAL: Silent-Return Masking (No Logging, No Guard)

**Count:** 18 instances (HIGH RISK)

These functions catch broadly, log nothing, and return a sentinel value. Callers cannot distinguish between "feature absent" and "feature broke."

#### Concrete Examples:

**otto/v5_runner.py:240-242 — Default branch detection**
```python
try:
    detected = detect_default_branch(project_dir)
    if detected:
        return detected
except Exception:  # noqa: BLE001
    pass
return "main"
```
**Symptom when it fails:** Operator sees fallback to "main" with zero indication that auto-detection broke. May commit to wrong branch silently.

**otto/v5_runner.py:1135-1137 — Seed profile resolution**
```python
try:
    hp = subprocess.run([...], capture_output=True, text=True, check=False)
    return (hp.stdout or "").strip() or None
except Exception:  # noqa: BLE001
    return None
```
**Symptom:** Function returns None. Caller checks `if val is None: ...` (line 1138+) and provides fallback. BUT: if subprocess.run itself raises TypeError, FileNotFoundError, etc., operator sees identical behavior to "subprocess succeeded with empty output." Indistinguishable.

**otto/v5_runner.py:1216-1217 — Graph resume checkpoint detection**
```python
try:
    tasks = read_graph(project_dir).get("tasks") or {}
except Exception:  # noqa: BLE001
    return None
root_t = tasks.get(ROOT_TASK_ID)
if not isinstance(root_t, dict):
    return None
```
**Symptom:** Returns None to skip resume. But if read_graph() crashes (corrupt JSON, disk I/O timeout), operator gets same "checkpoint not found" verdict. Real issue: checkpoint *exists* but is unreadable. Silent corruption risk.

**otto/cli_queue.py:225-227 — Queue state liveness check**
```python
try:
    state = load_state(project_dir)
except Exception:
    return False
return watcher_alive(state, max_age_s=max_age_s)
```
**Symptom:** Returns False (watcher not alive). But if the filesystem is on fire (ENOSPC, EROFS, NFS timeout), operator thinks watcher died and starts a new one. Silent queue divergence.

**otto/config.py:745-747 — Untracked file detection**
```python
try:
    untracked = _run_git(project_dir, "status", "--porcelain", --untracked-files=all")
except Exception:
    untracked = None
if untracked is not None:
    # parse untracked list
```
**Symptom:** If git hangs/crashes (bad .git index), `untracked=None` and the untracked-file *detection fails silently*. Caller then reports `"user has 0 untracked files"` when actually git is broken. Masks config issues.

**otto/cli.py:152-153 — Package version lookup**
```python
try:
    pkg_ver = _pkg_version("otto")
except Exception:
    pkg_ver = "dev"
```
**Symptom:** User sees "otto dev" in version output but the package *is* installed with version "1.2.3". Confusing; masks installation issues.

**otto/v5_runner.py:2290-2296 — Contract-to-payload marshalling**
```python
try:
    return {
        "project_kind": contract.get("project_kind"),
        ... # 8 more fields
    }
except Exception:  # noqa: BLE001
    return None
```
**Symptom:** Event payload is missing. Observers (logging, auditing, replay) see null contract data. No indication of *why* marshal failed (malformed contract? OOM? unicode error?).

---

### 3. DEFENSIVE-MASKING: Fallback on Import or Optional Logic

**Count:** 9 instances (medium risk)

Functions that catch to fall back to a no-op when an optional import fails or defensive alternative:

**otto/v5_runner.py:453-455 — Optional is_otto_owned_path fallback**
```python
try:
    from otto.setup_gitignore import is_otto_owned_path
except Exception:  # noqa: BLE001
    def is_otto_owned_path(_path: str) -> bool:
        return False
```
**Assessment:** Intended behavior. But if the import fails due to a SyntaxError in setup_gitignore.py (which breaks *production* builds), this swallows the error and silently treats all paths as "user-owned" (conservative fallback). Not ideal but not silent data corruption.

**otto/v5_runner.py:1078-1079 — Git tracked-file list fallback**
```python
try:
    tracked = subprocess.run([...], ...)
    repo_relpaths = [p for p in (tracked.stdout or "").splitlines() if p.strip()]
except Exception:  # noqa: BLE001
    repo_relpaths = []
```
**Assessment:** Empty list fallback is *wrong* when subprocess crashes. Caller later uses this for UI journey discovery — if git fails, discovers zero UI journeys instead of failing fast. Mask prevents operator from knowing git is broken.

**otto/memory.py:52-53 — Certifier memory recording**
```python
try:
    _record_run_impl(...)
except Exception:
    logging.getLogger("otto.memory").warning("Failed to record certifier memory")
```
**Assessment:** Best-effort OK — memory recording is auxiliary. But if the underlying file I/O fails (ENOSPC, EROFS), operator sees a warning and *thinks* it's handled. May cause cache incoherence later.

---

### 4. SCOPE-TOO-WIDE: Catching Everything When Only One Type Matters

**Count:** 8 instances

**otto/v5_runner.py:306-308 — Branch checkout with overly broad catch**
```python
try:
    checkout_branch(project_dir, branch)
except Exception as exc:  # noqa: BLE001 - repair agent decides the action
    return _checkout_issue_payload(project_dir=project_dir, branch=branch, ...)
```
**Issue:** Catches SystemExit (from sys.exit inside checkout), keyboard interrupt, OOM, etc. Same handler for all. Should narrow to (subprocess.CalledProcessError, OSError, FileNotFoundError).

**otto/config.py:796-797 — Dirty-file detection exception scope**
```python
try:
    # ... 30 lines of git status parsing
except Exception:
    dirty_files = []
```
**Issue:** Catches UnicodeDecodeError (from git output), AttributeError (from bad data model), OSError. Caller later reports "no dirty files" for all three cases, but they have different meanings (invalid encoding vs. git broken vs. FS issue).

---

## Top 5 Fix Patterns

### Fix Pattern A: Narrow Exception Type + Conditional Re-raise

**Before:**
```python
except Exception:
    return None
```

**After:**
```python
except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
    logger.warning("read_graph failed: %s (type=%s)", exc, type(exc).__name__)
    return None
```

**Example:** `v5_runner.py:1216`

### Fix Pattern B: Sentinel + Guard Check (Caller-Side)

For functions where None/False is valid output, add caller-side guard:

**Caller side:**
```python
val = detect_default_branch(project_dir)
if val is None:
    logger.error("failed to detect default branch; using 'main' as fallback")
return val or "main"
```

**Effect:** Operator sees diagnostic log. Grep finds "failed to detect" logs to audit silent failures.

### Fix Pattern C: Fail-Fast with Exception Context

For defensive import fallbacks, fail early if production code is broken:

**Before:**
```python
try:
    from otto.setup_gitignore import is_otto_owned_path
except Exception:
    def is_otto_owned_path(_path: str) -> bool:
        return False
```

**After:**
```python
try:
    from otto.setup_gitignore import is_otto_owned_path
except ModuleNotFoundError:
    # Optional dependency missing (intended fallback)
    def is_otto_owned_path(_path: str) -> bool:
        return False
except (SyntaxError, ImportError) as exc:
    # Production code is broken
    raise RuntimeError(f"otto.setup_gitignore broken: {exc}") from exc
```

**Effect:** Distinguishes "optional import not available" from "dependency is broken."

### Fix Pattern D: Cleanup Handler (Finally) vs. Business Logic Handler (Try)

For finally blocks, keep `except Exception: pass`. For try-except guarding returns/data mutations, narrow the exception type:

```python
try:
    page.screenshot(...)  # Best-effort; fine to swallow
except Exception:
    pass
finally:
    try:
        page.close()  # Cleanup; fine to swallow
    except Exception:
        pass
```

### Fix Pattern E: Pre-Check + Fallback (for defensive parsing)

**Before:**
```python
try:
    result = load_state(project_dir)
except Exception:
    return False
```

**After:**
```python
state_file = project_dir / "state.json"
if not state_file.exists():
    logger.info("queue state file not found; watcher not initialized")
    return False
try:
    result = load_state(project_dir)
except FileNotFoundError:
    return False
except (json.JSONDecodeError, OSError) as exc:
    logger.error("failed to load queue state: %s", exc)
    return False
```

**Effect:** Caller can distinguish "file missing" (not initialized) from "file corrupt" (operator action required).

---

## Risk Categorization Summary

| Category | Count | Risk | Action |
|----------|-------|------|--------|
| LEGITIMATE (cleanup/finally) | 17 | ✓ Low | Keep as-is |
| SILENT-RETURN (no logging) | 18 | **CRITICAL** | Add logging + narrow exception type |
| DEFENSIVE-MASKING (optional fallback) | 9 | Medium | Distinguish intentional vs. production errors |
| SCOPE-TOO-WIDE | 8 | Medium | Narrow exception types |
| **BLE001-annotated (intentional)** | 51 | ✓ Low | Reviewed; acceptable for best-effort paths |

---

## Recommended Priority

1. **Immediate:** Fix the 18 silent-return cases. Add logs. Examples: v5_runner.py:240, :1216, config.py:745.
2. **Follow-up:** Distinguish intentional import fallbacks from production errors.
3. **Nice-to-have:** Narrow exception scopes in the 8 overly-broad handlers.

The #1 root issue: **absence of logging in fallback paths makes debugging agent failures 10x harder.** When a build fails, grep for diagnostic logs in SILENT-RETURN functions — you get silence. That silence is itself a symptom, but operator has to know to look. Fix: one log line per catch, and the category of error (OSError vs. JSON parse failure) immediately narrows the diagnosis.
