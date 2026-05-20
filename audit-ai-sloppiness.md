# Otto AI-Sloppiness Audit

**Audit Date:** 2026-05-20  
**Scope:** `otto/` codebase (~72k LOC, 120 Python files)  
**Goal:** Identify patterns that invite repeated AI bugs and propose consolidations.

---

## 1. Top 5 Copy-Paste Twin Pairs

### 1.1 `_handle_mechanical_preflight_blocker` ↔ `_handle_mechanical_merge_blocker`

**Files:** `otto/v5/preflight_oracle.py:213` + `otto/v5/preflight_oracle.py:287`

**Pattern:** Both functions parse dirty-paths from status/detail strings, check if they're committable, and optionally commit before retry. Shared logic:
- `_status_lines_from_detail()` → `_porcelain_paths()` → `_dirty_paths_are_runner_committable()`
- `_commit_runner_output_paths()` + same emit pattern
- Return tuple `(verdict, payload_or_detail)`

**Shared Core:** ~70% identical. Extract `_attempt_mechanical_commit()` helper with signature:
```python
def _attempt_mechanical_commit(
    *,
    detail_or_payload: dict | str,
    project_dir: Path,
    kind_name: str,
    on_event: Any = None,
) -> tuple[str, dict | str]:
```

Both callers would instantiate with their own verdict/return types.

---

### 1.2 `_repair_subtree_propagation_once` ↔ `_repair_child_upward_merge_gate_once` ↔ `_repair_child_stale_target_gate_once`

**Files:** `otto/v5/repair.py:860` + `otto/v5/repair.py:1656` + `otto/v5/repair.py:1764`

**Pattern:** All three functions follow identical structure:
1. Build a `RepairPacket` via `_v5r._build_repair_packet()` (50+ lines of context setup)
2. Call `_run_child_verify_repair_packet()` or equivalent
3. Check `repair.verdict != "pass"` and record terminal if failed
4. Return `(bool, str)` tuple

**Diff:** Only phase/scope/feedback details vary. The packet-building boilerplate is ~250 LOC spread across three functions with minor tweaks per phase.

**Deduplication Sketch:**
```python
async def _repair_with_gate_packet(
    *,
    phase: str,  # "subtree_propagation" | "upward_merge_gate" | "stale_target_gate"
    verify_scope: str,  # "subtree" | "whole"
    config: dict,
    gate_feedback: dict,  # phase-specific
    origin: str,
    on_event: Any,
    # ... shared params
) -> tuple[bool, str]:
    # DRY build + run logic
    # Delegate phase-specific feedback assembly to callers
```

**Highest-leverage:** This is a **fragmentation risk**. Each phase adds 80–150 LOC of near-identical repair boilerplate. AI will copy-paste the wrong feedback structure into a new phase.

---

### 1.3 `_schedule_foundation_contract_amendment` ↔ `_schedule_smoke_repair_needed`

**Files:** `otto/v5/repair.py:213` + `otto/v5/repair.py:430`

**Pattern:** Both enqueue subtasks with identical structure:
- Compute `attempt_count` via `_increment_contract_amendment_attempt()`
- Build multi-line intent string
- Call `enqueue_subtask()` with nearly identical payload
- `record_task()` + `update_task_metadata()` + `set_contract_amendment_blocked()`
- Emit event with task/amendment IDs

**Diff:** Intent template and `contract_amendment` vs `integration_smoke_repair` routing.

**Duplication:** ~85% identical, ~65 LOC per function.

**Extraction:** Create `_enqueue_repair_subtask(phase, intent_template, **kwargs)` wrapper around the standard enqueue+record+metadata pattern.

---

### 1.4 Timestamp formatting: 4+ variants

**Pattern Locations:**
- `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` — 15+ sites (v5_runner.py, v5/repair.py, journal.py)
- `datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")` — 5+ sites (observability.py, logstream.py, mission_control/*)
- `datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")` — 3+ sites (supervisor.py, autopilot.py, events.py)
- `.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")` — serializers.py:643

**Issue:** AI agents add timestamps inconsistently. Some omit microsecond stripping, some use wrong tz kwarg syntax, some forget the Z-replace.

**Consolidation:** Centralize in `otto/observability.py`:
```python
def iso_now() -> str:
    """UTC ISO 8601 timestamp with Z suffix, no microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
```
Replace all 20+ call sites with `iso_now()`.

---

### 1.5 JSON reading: 6 different function names, inconsistent error handling

**Pattern Locations:**
- `_read_json_object()` — `otto/v5_context_slicer.py:544`, `otto/mission_control/service.py:3892`
- `_read_json()` — `otto/web/run_view_routes.py:337`, `otto/queue/artifacts.py:402`, `otto/queue/runner.py:368`, `otto/runs/atomic_repair.py:184`
- `_read_json_artifact()` — `otto/v5/preflight_oracle.py:383` (with `max_chars` cap)
- `_read_json_dict()` — `otto/runs/lifecycle.py:321`
- `read_jsonl_rows()` — `otto/runs/registry.py:492` (special case)
- `_read_json_file()` — `otto/mission_control/actions.py:894`

**Inconsistencies:**
- Some return `dict | None`, others assume valid and raise on OSError/JSONDecodeError
- Some log.warning on error, others silent
- `_read_json_artifact()` alone enforces size limits (AI risk: forgetting bounds on untrusted input)

**Consolidation:** Single `paths.py` utility:
```python
def read_json_safe(
    path: Path,
    *,
    default: Any = None,
    max_bytes: int | None = None,
    strict: bool = False,  # if True, raise instead of default
) -> dict[str, Any] | None:
    """Safe JSON read with optional size limit and consistent logging."""
```

---

## 2. Top 5 Long Functions (Intrinsic vs Accidental Length)

### 2.1 `_carry_prior_repair_packets` (400 LOC)

**File:** `otto/v5/repair.py:1203`

**Verdict:** **Intrinsic—but could split phases.** This is a single coherent state machine:
1. Scan prior sessions for repair packets (lines 1224–1240)
2. Skip self-copies (lines 1242–1246)
3. Load and rewrite packet JSON (lines 1248–1263)
4. Clear bookkeeping (lines 1264–1270)
5. Write to new location (lines 1272–1278)
6. Archive events (lines 1287–1299)

The 400 LOC is dense iteration over multiple packets + error handling per step. Splitting would fragment the "per-unit" loop logic. **Keep as-is**, but add section markers in comments.

---

### 2.2 `_conflict_packet_for_refusal` (327 LOC)

**File:** `otto/v5/repair.py:1515` (approximate offset from grep)

**Verdict:** **Accidental—clear split candidate.** This function:
1. Reads unmerged git paths + merge status
2. Extracts conflict context (base/ours/theirs diffs)
3. Classifies conflict type (structural, semantic, etc.)
4. Assembles response

Extractors like `_classify_conflict_type()`, `_extract_merge_diffs()` would drop LOC by ~40%.

---

### 2.3 `_stale_target_gate_feedback` (163 LOC)

**File:** `otto/v5/repair.py:1717`

**Verdict:** **Accidental—parameterizable.** This function builds a feedback dict by computing 7 different git refs and histories. The repetitive `_git_capture()` calls + field assembly could become:
```python
def _build_git_context(
    project_dir: Path,
    source_branch: str,
    parent_integration_branch: str,
) -> dict[str, str]:
    """Capture base/source/target refs and histories once."""
```

Reduces `_stale_target_gate_feedback()` by ~80 LOC.

---

### 2.4 `_read_latest_conflict_packet` (156 LOC)

**File:** `otto/v5/repair.py` (approximate)

**Verdict:** **Accidental—should use a helper.** Reads JSON, validates schema, extracts fields. Extract a `ConflictPacket` dataclass + `ConflictPacket.load()` method. Loses ~60 LOC.

---

### 2.5 `_schedule_smoke_repair_needed` (115 LOC)

**File:** `otto/v5/repair.py:430`

**Verdict:** **Accidental—is a thin wrapper.** 80% of this function is the shared subtask-enqueue pattern. Extract to `_enqueue_repair_subtask()` (see section 1.3). Loses ~65 LOC.

---

## 3. Module-Level Mutable State

**Full list:**

| File | Line | Name | Type | Risk |
|------|------|------|------|------|
| `otto/cli_improve.py` | 29 | `_VERDICT_GLYPHS` | dict (constant) | LOW—read-only, dict literal |
| `otto/spec_state.py` | 228 | `_PHASE_FOR_KIND` | dict (constant) | LOW—read-only, dict literal |
| `otto/config.py` | 30, 116, 136 | `DEFAULTS`, `DEFAULT_CONFIG`, `PROVIDER_AGENT_MODEL_DEFAULTS` | dicts (constants) | LOW—module config, not mutated |
| `otto/v5_capability_inventory.py` | 51, 78, 253 | `_KNOWN_CONFIGS`, `_KNOWN_ENTRYPOINTS`, `_INI_TOOL_HINTS` | dicts (constants) | LOW—read-only catalogs |
| `otto/journey_api_executor.py` | 587 | `PROBE_EXECUTORS` | dict (built once at import) | MEDIUM—initialized per-module, AI could mutate thinking it's local |
| `otto/journey_contracts.py` | 31 | `PROJECT_KINDS_WITH_API_JOURNEYS` | dict (constant) | LOW—read-only lookup table |
| `otto/v5_provider_fallback.py` | 49 | `_PATTERNS_BY_REASON` | dict (constant) | LOW—read-only patterns |
| `otto/defaults.py` | 120 | `_DOTTED_TO_FIELD` | dict (constant) | LOW—config field mapper |
| `otto/journey_ui_executor.py` | 951 | `_SEMANTIC_ROLE_SYNONYMS` | dict (sets) | MEDIUM—set values mutable, should be frozenset |
| `otto/journey_scope_policy.py` | 18, 27, 33 | `APPLICABILITY_POLICY`, `_NODE_KIND_FOR_SCOPE`, `_INFER_FROM_LEAD_SHAPE` | dicts (constants) | LOW—read-only policy tables |
| `otto/browser_testing.py` | 30, 34 | `_EXECUTABLE_TOOL_FAMILIES`, `_PYTHON_MODULE_TOOL_FAMILIES` | dicts (constants) | LOW—read-only tool registry |
| `otto/v5/merge.py` | 1042 | `_ORIGIN_CAUSE_MAP` | dict (constant) | LOW—read-only terminal cause classifier |
| `otto/verification/schema.py` | 29 | `_VERIFICATION_POLICY_ALIASES` | dict (constant) | LOW—read-only aliases |

**Verdict:** No critical module-level mutables. Two candidates for defensive hardening:
- `_SEMANTIC_ROLE_SYNONYMS`: Change `set[str]` values to `frozenset[str]` to prevent accidental mutation.
- `PROBE_EXECUTORS`: Document as "built once, do not mutate" in a module-level comment.

---

## 4. Inconsistent Abstractions (Top 3)

### 4.1 Timestamp formatting (covered in section 1.4)

### 4.2 Slug/path normalization: 4 different strategies

**Pattern Locations:**
- `safe_slug(text, max_len=64)` — `otto/safe_slug.py` (primary, used in repair.py)
- `_normalize_contract_path()` — called ~40 times in repair.py, normalizes git paths for contract matching
- `_normalize_session_id()` or similar — doesn't exist; session IDs are hand-validated
- `urlsplit()` + `quote()` — in `otto/mission_control/service.py:1210` for branch names

**Issue:** AI agents use wrong slugifier for the context. Example: using `safe_slug()` on a file path when `_normalize_contract_path()` is needed, or vice versa.

**Consolidation:** Create `otto/path_utils.py` with three clear, named functions:
```python
def normalize_contract_path(path: str) -> str:
    """Git path → contract identifier (strip leading /, lowercase, etc.)."""

def slug_for_file_id(text: str, max_len: int = 64) -> str:
    """Human-readable slug for file/report names."""

def slug_for_branch_name(text: str) -> str:
    """Git branch-safe slug (RFC 1123 subset)."""
```

---

### 4.3 Defensive isinstance chains: 19 locations

**Example from `otto/v5/repair.py:60–85`:**
```python
child = tasks.get(child_task_id) if isinstance(tasks, dict) else None
child_owned_paths = _v5r._task_owned_paths(child) if isinstance(child, dict) else []
# ... 6 more nested isinstance checks
```

**Issue:** Each guard is a sign the contract is unclear. Does `tasks.get()` ever return non-dict? Is `child` guaranteed dict if tasks is dict?

**Root Cause:** No schema validation for task graph JSON. AI agents add defensive checks instead of failing fast.

**Fix:** Schema validation at load time (Pydantic or JSON schema) in `otto/queue/task_graph.py`. Once validated, remove isinstance guards and trust the schema.

---

### 4.4 Result/feedback assembly: 3+ incompatible patterns

**Patterns:**
- `dict(gate_feedback or {})` + `.setdefault()` — `otto/v5/repair.py:1672–1678`
- `{**base_dict, **updates}` — `otto/v5/repair.py:98–102`
- `dict.fromkeys(deduped_list)` → iterate → extract — `otto/v5/repair.py:452`

**Issue:** AI extends feedback dicts inconsistently, sometimes missing the "right" pattern.

**Consolidation:** Create lightweight feedback builder:
```python
class FeedbackBuilder:
    def __init__(self, kind: str, message: str):
        self.data = {"kind": kind, "message": message, "_written_at": iso_now()}
    
    def add_context(self, key: str, value: Any) -> Self:
        self.data[key] = value
        return self
    
    def build(self) -> dict[str, Any]:
        return self.data
```

Replace 20+ feedback-dict assembly sites with `.add_context()` chains.

---

## 5. Names-That-Lie (Top 5)

### 5.1 `_repair_stale_target_and_retry_merge` (226 LOC)

**File:** `otto/v5/repair.py:1828`

**Actual Behavior:** Re-enters repair loop, optionally runs smoke preflight, handles foundation contract writes, may emit union incomplete events, *then* retries merge. Also records terminal blocks if repair/preflight fails.

**Problem:** Name says "repair + merge", but the function does: repair → smoke-test → union-check → merge → terminal-record. It's 4 phases, not 2.

**Better Name:** `_resolve_stale_target_with_remediation_and_retry()` or `_repair_and_merge_child_with_full_validation()`

### 5.2 `_carry_prior_repair_packets`

**File:** `otto/v5/repair.py:1203`

**Actual Behavior:** Copies prior repair packets to a new session's location AND clears bookkeeping (attempt_history, current_state). The clearing is load-bearing—without it, the resumed agent would replay old attempts.

**Problem:** Name says "carry", but it's "copy + reset".

**Better Name:** `_resume_repair_packets_for_new_session()` or `_carry_and_reset_repair_packets()`

### 5.3 `_foundation_contract_for_feedback_path`

**File:** `otto/v5/repair.py:52`

**Actual Behavior:** Takes a `feedback` dict, extracts all paths/missing items, searches for a matching foundation contract that *overlaps* but *doesn't conflict* with child ownership, and returns the contract metadata.

**Problem:** Name says "for feedback path" (sounds like "given a path, return feedback"), but it actually "searches for a contract matching feedback".

**Better Name:** `_find_overlapping_foundation_contract()` or `_resolve_contract_from_integration_feedback()`

### 5.4 `_stale_target_gate_feedback`

**File:** `otto/v5/repair.py:1717`

**Actual Behavior:** Builds a feedback dict capturing the git state *before* a stale-target repair is attempted. It's a diagnostic snapshot, not feedback on the repair outcome.

**Problem:** Name is ambiguous. Is this feedback *about* stale targets, or feedback *for handling* stale targets?

**Better Name:** `_pre_stale_target_repair_diagnostic()` or `_stale_target_diagnostic_context()`

### 5.5 `_handle_mechanical_preflight_blocker`

**File:** `otto/v5/preflight_oracle.py:213`

**Actual Behavior:** If the blocker is a known mechanical issue (dirty paths, port conflicts), commit/cleanup and return `"retry"`. Otherwise return `"repair"` to escalate to the agent.

**Problem:** "Handle" suggests the function *resolves* the blocker, but it either retries or delegates. The name doesn't convey the retry-vs-escalate decision logic.

**Better Name:** `_attempt_mechanical_resolution()` or `_classify_and_retry_mechanical_blocker()`

---

## 6. Overall Verdict: Convergence Status

### Summary
The codebase still **needs more refactoring** at the abstraction level. While individual functions are often correct, the **repeated patterns** (copy-paste repair phases, timestamp formats, JSON readers) create compound AI risk: each new feature adds another variant, narrowing the common path.

### Evidence of Non-Convergence
1. **Timestamp formats:** 4+ variants across 20 call sites. Each new event log adds another chance to pick the wrong one.
2. **Copy-paste repair phases:** 3 nearly identical `_repair_*_once()` functions. The next phase (e.g., `_repair_union_gate_once`) will likely copy one of these, drifting from the others.
3. **JSON readers:** 6 different function names for essentially the same operation. New modules will define a 7th.
4. **Sprawling parameters:** `_repair_stale_target_and_retry_merge(19 params)` and `_repair_child_stale_target_gate_once(14 params)` are signatures that should never have been written. They should have triggered a refactor *before* reaching production.

### Highest-Leverage Fixes (in order)
1. **Timestamp centralization** (`iso_now()`): Trivial to implement, eliminates 4 variants across 20 sites. **Cost:** 10 min. **Risk reduction:** HIGH (logging invariant).
2. **Repair-phase abstraction** (`_repair_with_gate_packet()`): Consolidates `_repair_subtree_propagation_once`, `_repair_child_upward_merge_gate_once`, `_repair_child_stale_target_gate_once`. Reduces copy-paste risk for future phases. **Cost:** 2–3 hours. **Risk reduction:** HIGH (phase logic correctness).
3. **Subtask-enqueue abstraction** (`_enqueue_repair_subtask()`): Consolidates `_schedule_foundation_contract_amendment`, `_schedule_smoke_repair_needed`, and future subtask enqueuers. **Cost:** 1 hour. **Risk reduction:** MEDIUM (subtask metadata correctness).
4. **JSON reader consolidation** (`read_json_safe()`): Centralizes 6 readers. **Cost:** 1.5 hours. **Risk reduction:** MEDIUM (size limits, error handling consistency).
5. **Schema validation at load** (task graph, feedback structs): Remove 19+ isinstance guards. Shift validation from runtime to parse time. **Cost:** 4–6 hours. **Risk reduction:** MEDIUM (defensive coding overhead).

### Recommendation
**Do NOT** attempt all five in parallel. Prioritize by ROI:
- **Phase 1 (30 min):** Centralize timestamps (`iso_now()`).
- **Phase 2 (3 hours):** Consolidate repair-phase abstraction. Codex-gate the design before implementation.
- **Phase 3 (deferred):** Subtask enqueue + JSON readers (lower urgency, can be part of next maintenance cycle).

After Phase 2, the codebase should have a clear "repair loop protocol" that future agents can extend safely.

---

**End of Audit**
