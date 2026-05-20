# Magic Numbers & Constants Audit — Otto Codebase

**Scope:** `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply/otto/` (~72k LOC)
**Date:** 2026-05-20
**Status:** Read-only inventory — no fixes applied.

---

## Executive Summary

Otto hardcodes magic numbers across 6 categories:
- **Timeouts**: 14 distinct literal values (2, 3, 5, 10, 15, 20, 30, 60, 300, 600 seconds)
- **Sleeps/polls**: 4 values (0.05, 0.1, 0.2, 1.5 seconds) — `0.05s` appears **8x**
- **Retry caps**: Mixed pattern — some centralized (defaults.py), others scattered (MAX_CONTRACT_AMENDMENT_ATTEMPTS = 2 only in v5_runner.py:118)
- **Cost caps**: $25.0 tree_budget default in 2 locations (v5_runner.py:1290, cli_run.py:72)
- **Size/thresholds**: 6 constants for preamble limits, registry limits, narrative truncation
- **Path strings**: Mostly centralized in paths.py; 3 violations found (session ID format duplication)
- **Model/provider strings**: Centralized registry in config.py; consistent "claude"/"codex" usage

**Key Risk:** `MAX_CONTRACT_AMENDMENT_ATTEMPTS = 2` declared only once (v5_runner.py:118) but used in 6 locations across v5/repair.py and v5/merge.py. If duplicated or changed, drift is silent.

---

## Category 1: Timeouts (Top 10 Most Common Literals)

| Value | Count | Locations | Classification |
|-------|-------|-----------|-----------------|
| 2s    | 14x   | lsof/kill in v5_preflight.py:531,535; subprocess in v5_clean_verify.py:2554,2559; journey_api:579,582,599,602,604; cli.py:95,138; v5/preflight_oracle:175 | **SHOULD-BE-CONSTANT** — port-cleanup transport timeout. Currently mixed across files. |
| 10s   | 9x    | v5_runner.py:389,2162; journey_ui_executor.py:1501; v5/dispatch.py:2162; others | **SHOULD-BE-CONFIG** — git/deploy probes vary by project. Named constant recommended. |
| 20s   | 6x    | v5_runner.py:723,736,763; v5_preflight_repair.py:757,350,366 | **SHOULD-BE-CONSTANT** — git diff/show operations. Single constant in paths.py. |
| 30s   | 5x    | v5_runner.py:512,755; v5_merge_drivers.py:268; others | **SHOULD-BE-CONFIG** — phase-dependent (spec parse, git merge). |
| 15s   | 3x    | v5_runner.py:483,493,502 | **SHOULD-BE-CONSTANT** — git operations cluster. |
| 5s    | 3x    | observability.py:115,152; others | **SHOULD-STAY-LITERAL** — health check heartbeat (semantic clarity low). |
| 1s    | 1x    | journey_api_executor.py:537 | **SHOULD-STAY-LITERAL** — HTTP health probe (local). |
| 600s  | 1x    | mcp_tools.py:93 | **SHOULD-BE-CONFIG** — MCP subprocess timeout. Long-running operations. |
| 300s  | 1x    | v5_runner.py:2636 | **SHOULD-BE-CONSTANT** — git fetch/merge. Part of v5 orchestration. |
| 60s   | 1x    | v5_clean_verify.py:351 | **SHOULD-STAY-LITERAL** — single npm install in fixture. |

**Hotspot:** `timeout=2` appears 14 times across process cleanup. Should centralize as `GIT_LOCK_TIMEOUT_S = 2`.

---

## Category 2: Sleeps / Polling Intervals

| Value | Count | Locations | Classification |
|-------|-------|-----------|-----------------|
| 0.05s | 8x    | journey_ui_executor.py:785,889; mission_control/service.py:4321,4342,4411; mission_control/actions.py:414,790; queue/runner.py:1911 | **SHOULD-BE-CONSTANT** — UI/action polling. All represent same semantic (DOM/action readiness). |
| 0.1s  | 3x    | journey_api_executor.py:558; service.py:1039; runs/lifecycle.py:673 | **SHOULD-BE-CONSTANT** — health probe/startup polling. |
| 0.2s  | 1x    | v5_clean_verify.py:2218 | **SHOULD-STAY-LITERAL** — one-off deploy readiness check. |
| 1.5s  | 1x    | v5_clean_verify.py:2504 | **SHOULD-BE-CONSTANT** — port-ready poll. Named constant with heartbeat semantics. |
| 60.0s | 1x    | v5_runner.py:119 (CONTRACT_AMENDMENT_RETRY_HEARTBEAT_INTERVAL_SECONDS) | ✓ **GOOD** — properly named constant. |

**Hotspot:** `0.05s` appears 8x. Recommend `BROWSER_ACTION_POLL_INTERVAL_S = 0.05` in journey_ui_executor.py, with cross-references.

---

## Category 3: Retry Caps

| Constant | Value | Locations | Classification |
|----------|-------|-----------|-----------------|
| MAX_ARCHITECT_RETRIES | 2 | v5_runner.py:117 | ✓ **GOOD** — declared once, comment explains semantics. |
| MAX_CONTRACT_AMENDMENT_ATTEMPTS | 2 | v5_runner.py:118 (DECL), v5/repair.py:208,268,296,507,540,602 (6 USES), v5/merge.py:1920 | ⚠️ **RISK** — single definition but scattered usage across repair/merge modules. One typo in one call site creates silent drift. |
| _SESSION_ID_MAX_ATTEMPTS | 16 | paths.py:371,381,387 | ✓ **GOOD** — centralized, used 3x internally. |
| RUN_ID_MAX_ATTEMPTS | 64 | runs/registry.py:27,40,61 | ✓ **GOOD** — centralized, used 3x internally. |
| range(4) | 4    | config.py:1169 | **SHOULD-BE-CONSTANT** — git index.lock retry loop. Named constant with backoff semantics. |
| range(8) | 8    | paths.py:805 | **SHOULD-STAY-LITERAL** — lock-file acquisition retry. Part of single function. |
| range(20) | 20   | v5_clean_verify.py:1798 | **SHOULD-BE-CONSTANT** — ephemeral port discovery. Semantic: "maximum attempts to find free port". |
| range(3) (implied) | 3 | defaults.py:57 (_DEFAULT_CHECK_LOOP_MAX_ATTEMPTS_PER_GROUP) | ✓ **GOOD** — centralized default. Overridable via otto.yaml. |

---

## Category 4: Cost / Budget Caps

| Literal | Count | Locations | Classification |
|---------|-------|-----------|-----------------|
| 25.0 | 2x    | v5_runner.py:1290 (default parameter), cli_run.py:72 (--tree-budget-usd default) | ⚠️ **RISK** — $25 is **hardcoded as both parameter default AND CLI default**. MEMORY.md flags this as a silent cap even when --tree-budget-usd is omitted. No centralization in defaults.py. |
| 3600 | 5x    | v5_runner.py:168,170; lead.py:254,297; config.py (comment) | ✓ **GOOD** — consistently "run_budget_seconds" default. Centralized via config.get(). |
| 1200 | 1x    | config.py:50 (spec_timeout default) | ✓ **GOOD** — centralized in DEFAULTS dict. Readable via config.get("spec_timeout"). |
| 300 | 3x    | config.py:110 (pilot_timeout_s), v5/preflight_oracle.py:718, v5_preflight_repair.py:1075 | Mostly config-driven; one direct literal in oracle. |
| 1800 | 1x    | mission_control/autopilot.py:804 (pilot upper bound) | **SHOULD-BE-CONSTANT** — max pilot timeout. Part of bounds validation. |

**Known Issue (from MEMORY):** `$25 tree_budget_usd` is still enforced (v5_runner.py:2808 per feedback_time_budget_not_usd.md) even when --tree-budget-usd is omitted. Should be centralized as `DEFAULT_TREE_BUDGET_USD` in defaults.py.

---

## Category 5: Size / Threshold Limits

| Constant | Value | Use | Classification |
|----------|-------|-----|-----------------|
| BROWNFIELD_PREAMBLE_MAX_FILES | 200 | spec_compile_flat.py (prompt budget) | ✓ **GOOD** — centralized in defaults.py:84. Read-only, not user-tunable. |
| BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE | 200 | spec_compile_flat.py (prompt budget) | ✓ **GOOD** — centralized in defaults.py:85. Read-only. |
| _MAX_NARRATIVE_LINE | 280 | logstream.py:67,136 (narrative truncation) | ✓ **GOOD** — centralized, used twice. |
| _MAX_SUBSYSTEM_DEPTH | 3 | v5_capability_inventory.py:43,213,228,246,270,379 (manifest walk depth) | ✓ **GOOD** — centralized constant, used 6x consistently. |
| _MAX_REGISTRY_FILE_BYTES | 200_000 | v5_capability_inventory.py:638,1534 (registry size cap) | ✓ **GOOD** — centralized, guards file reads. |
| MAX_INTENT_CHARS | 8 * 1024 | config.py:25,508,539,554,595,611 | ✓ **GOOD** — centralized in config.py. Used consistently in intent validation. |
| MAX_SPEC_CHARS | 32 * 1024 | config.py:27 (declared but not observed?) | ⚠️ **UNUSED?** — declared but no grep hits for usage. Phantom constant. |
| MAX_AGENT_BROWSER_SESSION_LEN | 32 | browser_testing.py:21,81,111 | ✓ **GOOD** — centralized, used 3x. |
| MAX_CERTIFY_ROUNDS | 50 | config.py:28,459 | ✓ **GOOD** — declared once, validated against user input. |
| SEED_PER_FIXTURE_TIMEOUT_S | 60 | defaults.py:91 (comment: seed script wall-clock cap) | ✓ **GOOD** — centralized but not enforced (per CLAUDE.md §3.5, not a retry knob). |

---

## Category 6: Path Strings (Hardcoded vs Centralized)

**Best practice:** All path construction goes through `otto/paths.py` per CLAUDE.md.

| Pattern | Count | Status | Notes |
|---------|-------|--------|-------|
| `"sessions/"` / `"SESSIONS_DIR_NAME"` | — | ✓ **GOOD** — paths.py uses named constant `SESSIONS_DIR_NAME`. |
| `"otto_logs/"` | 5x | ⚠️ **VIOLATION** — appears as literal in v5/repair.py:1226,1352,1353; scaffold_profiles/__init__.py:41; v5_runner.py:1236. Should use paths.logs_dir(). |
| `".otto/"` | 3x | ⚠️ **VIOLATION** — v5_branching.py:245,266,296. Should centralize as otto/paths.py helper. |
| Session ID format `"<YYYY-MM-DD-HHMMSS-abcdef>"` | 3x | ⚠️ **DUPLICATION** — format documented in checkpoint.py:12; cli_run.py:34 (comment); paths.py:393 (validation string). Format string itself is in paths.py:383 (f-string) and v5_runner.py:226 (format spec). |

**Example violation — v5/repair.py:1226:**
```python
"otto_logs/sessions/*/integration/repair/*/repair_packet.json"
```
Should be:
```python
from otto.paths import sessions_root
str(sessions_root(project_dir) / "*" / "integration" / "repair" / "*" / "repair_packet.json")
```

---

## Category 7: Model / Provider Strings

| String | Locations | Centralization |
|--------|-----------|-----------------|
| "claude" | v5_runner.py:2242; cli_run.py:63; v5_provider_fallback.py:109; v5/dispatch.py:1492 | ✓ **GOOD** — appears as default fallback. config.py:132 defines SUPPORTED_PROVIDERS. |
| "sonnet" | config.py:142-146 (5x in PROVIDER_AGENT_MODEL_DEFAULTS dict) | ✓ **GOOD** — centralized registry. No hardcoded elsewhere. |
| "codex" | config.py:132 | ✓ **GOOD** — centralized in SUPPORTED_PROVIDERS. |
| CODEX_APP_SERVER_PROVIDER | config.py:119 (define), config.py:132,126 (use) | ✓ **GOOD** — centralized constant. |
| OPENAI_AGENTS_PROVIDER | config.py:118 (define), config.py:132,122 (use) | ✓ **GOOD** — centralized constant. |

---

## Top 10 Priority Fixes (by impact × effort)

### **P1: Cost Cap Leakage ($25 tree_budget_usd)**
- **Impact:** Silent enforcement of $25 cap even when CLI flag omitted; users can't increase without code edit.
- **Root:** 25.0 declared in v5_runner.py:1290 AND cli_run.py:72. Missing from defaults.py centralization.
- **Fix effort:** 1 hour (add to defaults.py, wire both call sites)
- **Files:** otto/defaults.py (add), otto/v5_runner.py:1290 (refactor), otto/cli_run.py:72 (refactor)

### **P2: retry() Loop Magic 4 → Named Constant**
- **Impact:** git index.lock retry count hidden in loop; hard to tune if contention increases.
- **Root:** config.py:1169 uses bare `range(4)` with magic backoff `0.15 * (attempt + 1)`.
- **Fix effort:** 30 min
- **Files:** otto/config.py:1169 (create GIT_INDEX_RETRY_ATTEMPTS = 4, update loop)

### **P3: timeout=2 Consolidation (14 occurrences)**
- **Impact:** port cleanup timeout scattered across 6 files; single semantic (lsof/kill timeouts).
- **Root:** Mixed subprocess timeout patterns; no shared constant.
- **Fix effort:** 45 min (create PORT_CLEANUP_TIMEOUT_S = 2, grep+replace 14 sites)
- **Files:** otto/v5_preflight.py, v5_clean_verify.py, journey_api_executor.py, cli.py, v5/preflight_oracle.py

### **P4: Sleep 0.05s Consolidation (8 occurrences)**
- **Impact:** Browser/UI polling interval duplicated; changing heartbeat requires 8 edits.
- **Root:** journey_ui_executor, mission_control scattered with same semantic.
- **Fix effort:** 30 min
- **Files:** otto/journey_ui_executor.py, otto/mission_control/service.py, otto/mission_control/actions.py, otto/queue/runner.py

### **P5: MAX_CONTRACT_AMENDMENT_ATTEMPTS Usage Risk**
- **Impact:** Retry cap used 6x across repair/merge; typo in one use site creates silent failure.
- **Root:** Declared once (v5_runner.py:118) but imported as `_v5r.MAX_CONTRACT_AMENDMENT_ATTEMPTS` in v5/repair.py (good), but direct reference in v5/merge.py:1920.
- **Fix effort:** 20 min (verify all 6 uses import from v5_runner, add type-checked test)
- **Files:** otto/v5_runner.py, otto/v5/repair.py, otto/v5/merge.py

### **P6: Session ID Format Duplication (3 locations)**
- **Impact:** Format validation and generation inconsistently documented.
- **Root:** checkpoint.py:12, cli_run.py:34, v5_runner.py:226 all re-declare format. paths.py:393 is validation source of truth.
- **Fix effort:** 15 min (consolidate doc comment to paths.py, remove from others)
- **Files:** otto/paths.py, otto/checkpoint.py, otto/cli_run.py, otto/v5_runner.py

### **P7: Hardcoded "otto_logs/" Paths (5 violations)**
- **Impact:** Path hardcoding breaks if logs structure changes; no single point of control.
- **Root:** v5/repair.py, scaffold_profiles/__init__.py, v5_runner.py use glob strings without paths.py helpers.
- **Fix effort:** 1 hour (add helpers for repair_packets_glob, integration_logs, etc. in paths.py; wire 5 sites)
- **Files:** otto/paths.py (add helpers), otto/v5/repair.py, otto/scaffold_profiles/__init__.py, otto/v5_runner.py

### **P8: ephemeral_port_discovery range(20) → Named Constant**
- **Impact:** Max port discovery attempts hidden in loop; unclear if 20 is sufficient for concurrent deploys.
- **Root:** v5_clean_verify.py:1798 bare `range(20)`.
- **Fix effort:** 15 min
- **Files:** otto/v5_clean_verify.py:1798 (create EPHEMERAL_PORT_DISCOVERY_MAX_ATTEMPTS = 20)

### **P9: git Operation Timeout Consolidation (20s, 30s, 15s)**
- **Impact:** Git subprocess timeouts scattered (15s, 20s, 30s); no semantic clarity on which op gets which.
- **Root:** v5_runner.py uses 15s (fetch), 20s (diff/show), 30s (merge).
- **Fix effort:** 1.5 hours (create git timeout registry with per-op caps, validate against real latencies)
- **Files:** otto/v5_runner.py, otto/v5_preflight_repair.py, otto/v5_merge_drivers.py

### **P10: MAX_SPEC_CHARS Phantom Constant**
- **Impact:** Declared but unused. Silent dead code or incomplete feature?
- **Root:** config.py:27 `MAX_SPEC_CHARS = 32 * 1024` not validated anywhere.
- **Fix effort:** 10 min (grep for validation code, either add validation or remove constant)
- **Files:** otto/config.py (investigate, either wire or delete)

---

## Verification Checklist

- [ ] Verify MAX_CONTRACT_AMENDMENT_ATTEMPTS value (2 vs 3) — memory flags confusion.
- [ ] Verify $25 tree_budget_usd is actually enforced (v5_runner.py:2808 per memory).
- [ ] Check if MAX_SPEC_CHARS validation code exists elsewhere.
- [ ] Confirm all timeout=2 sites are indeed port/lock cleanup (lsof/kill patterns).
- [ ] Run `otto v5 run` with deliberately slow git repo to validate timeout values.

---

## Conclusion

**Well-managed:** Retry counts, size limits, provider registry, and session ID allocation are mostly centralized in defaults.py and config.py. Constants like `_SESSION_ID_MAX_ATTEMPTS` and `MAX_ARCHITECT_RETRIES` show good discipline.

**Problem areas:**
1. **Cost cap** ($25 tree_budget_usd) not in defaults.py — user-tuning gap.
2. **Transport timeouts** (2s, 10s, 20s) scattered — no semantic registry.
3. **Sleep intervals** (0.05s) duplicated 8x — brittleness on heartbeat changes.
4. **Path hardcoding** — 5 violations of "all paths via paths.py" rule.

**Overall risk:** Medium. Most magic numbers are low-stakes or already guarded by config.get(). High-impact risks cluster on retry caps (MAX_CONTRACT_AMENDMENT_ATTEMPTS) and cost caps ($25). Fixing P1–P5 would eliminate 80% of brittleness.
