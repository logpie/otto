# Phase 3 — Sibling ownership overlap check (Lever A)

**Status:** Revised after Codex-gate (round 1 returned NEEDS REVISION). All 6 Codex findings folded in; the original plan moved the check to the wrong layer, duplicated existing logic, and conflicted with Phase 5 anti-cascade behavior.

**Goal:** Make sibling-feature merge conflicts structurally rare by surfacing path-overlap findings at the right layer (after the architect/CHARTER writes feature paths) and routing them through the existing plan-amendment repair path, with a graceful degrade into Phase 2c's integration-as-merger.

## What changed from round 2 (Codex feedback applied)

1. **Suppression of existing emitter on exhaustion.** `_foundation_isolation_feedback` (dispatch.py:473) will re-find `feature_owned_paths_overlap` on every dispatch loop iteration; without suppression, the degrade-with-annotation path is undone the next time around. Add explicit filter: if parent task has `sibling_overlap_attempts >= MAX` AND `decomposition_overlap_unresolved` set, skip emitting `feature_owned_paths_overlap` for any path listed in `decomposition_overlap_unresolved`. Add a test that feature dispatch still proceeds.
2. **Re-persist CHARTER after each plan-amendment attempt.** `_run_plan_amendment_repair_packet` only commits `CHARTER.md` (repair.py:1383) — the task graph stays stale unless `persist_feature_owned_paths_from_charter` re-runs. Loop becomes: amend → re-persist → re-check overlap.
3. **Glob/wildcard enforcement, not just docs.** Reject `*`, `?`, `[` in any owned_path segment as `unsupported_owned_path_glob`. Also reject any path segment that is `.` or `..`, POSIX absolute paths, Windows drive prefixes (`C:\...`), and UNC (`\\server\...`) paths. Raised as findings, not silently dropped.
4. **No `schema_version` bump.** The new top-level `parent` field on `integration_packet.json` is additive-optional under version 1; no strict consumer found that would break. Plan was overstating the change.
5. **K tied to `MAX_CONTRACT_AMENDMENT_ATTEMPTS`** (not `MAX_ARCHITECT_RETRIES`). Aliased as `MAX_SIBLING_OVERLAP_AMENDMENT_ATTEMPTS = MAX_CONTRACT_AMENDMENT_ATTEMPTS` for clarity.
6. **Honest note on HOIST / AUTO_LOADER applicability.** Both only work when foundation scaffold already exposes the extension point. If it doesn't, the plan-amendment must fall back to `CANONICAL_OWNER` or `RE_PARTITION`, or degrade to integration annotation. Document in the resolution-patterns block.

## What changed from round 1

| Round 1 (wrong) | Round 2 (revised) |
|---|---|
| Check fires at root decomposition time | **Fires at architect-CHARTER persist time** — root subtasks don't declare `owned_paths`; the architect derives them from the CHARTER (`persist_feature_owned_paths_from_charter`, dispatch.py:382) |
| Add a new `_compute_sibling_overlap_findings` helper | **Extract & reuse `_foundation_isolation_feedback`'s `feature_owned_paths_overlap` detection** (v5_runner.py:2742, finding at 2832). Duplicating it would invite drift. |
| K=2 architect re-emit, fresh decomposition | **Use existing plan-amendment repair path** (`_run_plan_amendment_repair_packet` at repair.py:1308). Phase 5 anti-cascade explicitly forbids fresh architect dispatch; we follow that pattern. |
| `DECLARE_SHARED` as a resolution option | **Drop it** — v5 has no first-class `shared_paths` partition contract. Replace with `CANONICAL_OWNER` (one feature owns, sibling imports). |
| Vague path-overlap semantics | **Tighten contract**: casefold for case-insensitive collisions, reject `.`/`..`/absolute, no glob expansion (current `_path_overlaps` is exact/prefix; conservative pattern overlap only if/when needed). |
| Annotation just "added to integration_packet" | **`_write_integration_packet` only writes children today** (dispatch.py:1785). Add a `parent` block with `decomposition_overlap_unresolved` field. Plumbing change required. |

## Behavior (revised)

```
Architect writes CHARTER and persists feature owned_paths
  ↓
persist_feature_owned_paths_from_charter() runs (dispatch.py:382)
  ↓
Sibling overlap check (REUSES _foundation_isolation_feedback's
  feature_owned_paths_overlap detection logic, extracted as pure helper)
  ↓
findings empty → proceed to feature dispatch
findings present →
  Attempt N=1:
    - emit structured feedback to _run_plan_amendment_repair_packet (existing path)
    - amendment commits CHARTER.md
    - RE-RUN persist_feature_owned_paths_from_charter (refresh task graph from CHARTER)
    - RE-CHECK overlap
  Attempt N=2 (final): same loop, last try
  After N=2 still failing →
    annotate parent task entry: decomposition_overlap_unresolved=[{path, claimants}, ...]
    set parent.sibling_overlap_attempts = MAX (suppression flag)
    proceed to feature dispatch with annotation
    integration_packet writes the annotation under top-level parent block
    integration Lead's Step 1 reads it, expects union on listed paths
```

**Suppression of existing emitter:** `_foundation_isolation_feedback` (dispatch.py:473) currently re-emits `feature_owned_paths_overlap` on every dispatch loop iteration. After exhaustion, the new check sets `parent.sibling_overlap_attempts >= MAX` AND `decomposition_overlap_unresolved`; the existing emitter is patched to skip paths already listed there. Otherwise the degrade-and-annotate path is undone next iteration.

## Touch points (revised)

| File | Function | Change |
|---|---|---|
| `otto/v5_runner.py` | NEW `_compute_sibling_path_overlap_findings` (pure) | Extract overlap-detection from `_foundation_isolation_feedback` (currently at v5_runner.py:2742, finding kind=`feature_owned_paths_overlap` at line 2832). Pure helper takes `[(task_id, owned_paths)]` and returns `[{kind, overlapping_path, claimants}]`. `_foundation_isolation_feedback` refactored to call the helper rather than duplicate. |
| `otto/v5_runner.py` | `_normalize_contract_path` / `_path_overlaps` | Tighten contract: casefold for case-insensitive comparison (gated by detecting `core.ignorecase` or always-on for safety), reject `.`/`..`/absolute paths at validation, document explicit non-glob semantics. Don't try to resolve symlinks. |
| `otto/v5/dispatch.py` | `_persist_feature_owned_paths_from_charter` call site (~line 382) | After persist, immediately run sibling-overlap check on the populated feature entries. On findings → dispatch via existing `_run_plan_amendment_repair_packet` with `kind=sibling_owned_path_overlap` feedback. |
| `otto/v5/dispatch.py` | NEW retry-budget gate | Read `parent_task.sibling_overlap_attempts` (default 0). Increment per plan-amendment dispatch. Cap: `MAX_SIBLING_OVERLAP_AMENDMENT_ATTEMPTS = MAX_CONTRACT_AMENDMENT_ATTEMPTS` (aliased — same constant). At exhaustion, set `decomposition_overlap_unresolved` on parent task entry and emit `sibling_overlap_unresolved_degrade` event. |
| `otto/v5_runner.py` | `_foundation_isolation_feedback` | Patch: skip emitting `feature_owned_paths_overlap` for paths already listed in parent's `decomposition_overlap_unresolved` (suppression after exhaustion). Without this, the degrade-and-annotate path is undone on the next dispatch-loop iteration. |
| `otto/v5/dispatch.py` | Plan-amendment loop | After `_run_plan_amendment_repair_packet` returns, call `persist_feature_owned_paths_from_charter` to refresh task graph from the (possibly amended) CHARTER, then re-check overlap. Without this the task graph is stale. |
| `otto/v5/dispatch.py` | `_write_integration_packet` (~line 1785) | Add `parent` top-level block with `{task_id, intent, decomposition_overlap_unresolved}`. Today only writes `children`. **Additive optional under schema_version=1** — no version bump needed (no strict consumer found that would break on an extra top-level key). |
| `otto/queue/task_graph.py` | `record_task` / `update_task_metadata` | Add `sibling_overlap_attempts: int` and `decomposition_overlap_unresolved: list[dict]` to parent task entry shape. |
| `otto/prompts/lead.md` | Hard Rule F-7 / Architect block | Add: "When a parent has ≥2 sibling features, you (the architect) declare each feature's `owned_paths` in the CHARTER. Two features MUST NOT both claim the same path. If two features need a shared resource: HOIST to foundation (foundation owns it, exposes contract); AUTO_LOADER (foundation provides dynamic discovery); CANONICAL_OWNER (one feature owns the file/module, sibling imports the public interface); RE_PARTITION (re-shape scopes so neither needs the path)." |
| `otto/prompts/lead-integration.md` | Step 1 | Add: "If integration_packet has `parent.decomposition_overlap_unresolved`, those paths are expected sibling conflicts. Use `git merge --no-commit` for affected branches; review and union both contributions before committing the merge." |
| `tests/test_v5_sibling_ownership_overlap.py` | NEW | Unit tests for the extracted pure helper (overlap shapes, N>2 claimants, same-feature dup-paths not counted, normalized-path comparisons, casefold, invalid input rejection). |
| `tests/test_v5_decomposed_child_lands_in_main.py` | Extend | Integration tests: (a) clean partition still passes; (b) overlap detected → plan-amendment fires; (c) K=2 exhausted → annotation in packet, integration receives it. |

## Tests to keep green (regression watch — Codex's list)

- `tests/test_makeitbuild_p2.py`
- `tests/test_makeitbuild_p5.py`
- `tests/test_foundation_seeded_feature_paths.py`
- `tests/test_no_architect_cascade.py` ← critical: we must NOT dispatch fresh architects
- `tests/test_v5_decomposed_child_lands_in_main.py` ← heavy edits in Phase 2c, fragile

## Structured feedback shape (revised resolution patterns)

```jsonc
{
  "kind": "sibling_owned_path_overlap",
  "step_id": "charter_partition_sibling_overlap",
  "message": "Features `bookmarks` and `tasks` both claim `frontend/src/App.tsx` in the CHARTER. Pick one of: HOIST / AUTO_LOADER / CANONICAL_OWNER / RE_PARTITION.",
  "parent_task_id": "root",
  "overlaps": [
    {"path": "frontend/src/App.tsx", "claiming_features": ["v5-aaa...", "v5-bbb..."]}
  ],
  "resolution_patterns": [
    {"id": "HOIST",          "summary": "Move the path to foundation's owned_paths. **Only valid if foundation scaffold already exposes the extension point (hook, callback registry, plugin interface).** If foundation has no such surface, prefer CANONICAL_OWNER or RE_PARTITION."},
    {"id": "AUTO_LOADER",    "summary": "Foundation provides runtime discovery (pkgutil iter_modules for Python; import.meta.glob for Vite). Features drop files; loader picks them up. **Only valid if foundation's auto-loader is already in place** — adding one mid-amend exceeds plan-amendment scope."},
    {"id": "CANONICAL_OWNER","summary": "One feature owns the file/module; sibling imports the public interface. Update CHARTER so only one feature lists the path; sibling references it as an import contract. Always available."},
    {"id": "RE_PARTITION",   "summary": "Re-shape feature scopes so neither needs the path. Often signals the partition itself is wrong. Always available."}
  ],
  "attempt": 1,
  "max_attempts": 2,
  "_written_at": "<iso timestamp>"
}
```

## Path-overlap contract

- **Normalization:** `_normalize_contract_path` strips leading `./`, normalizes backslashes, strips trailing slashes (existing behavior).
- **Case:** **casefold** both sides for comparison. macOS/Windows filesystems are case-insensitive; even on Linux, a `core.ignorecase` repo can introduce false negatives. False positives are safer than false negatives for a partition check.
- **Rejected inputs (validation, not comparison):** absolute paths (`/...`), parent traversal (`..` / `.`-only), empty strings. These yield a structured `invalid_owned_path` finding instead of being silently ignored.
- **Globs/wildcards:** **Rejected, not silently dropped.** If any owned_path segment contains `*`, `?`, or `[`, emit a structured `unsupported_owned_path_glob` finding (same severity as `invalid_owned_path`). The CHARTER must use literal paths until/unless glob support is added end-to-end. Document the limitation in the helper docstring and reference it in `lead-architect.md`.
- **Rejected platform-specific path shapes:** POSIX absolute (`/...`), Windows drive prefixes (`C:\...`), UNC (`\\server\...`), and any segment that is `.` or `..` after normalization. All raised as `invalid_owned_path`, not silently normalized away.
- **Symlinks:** logical-path only; do not resolve. Document.

## Graceful degrade — annotation flow

1. After 2 failed plan-amendment attempts, `update_task_metadata(parent_id, decomposition_overlap_unresolved=[{path, claimants}, ...], sibling_overlap_attempts=2)`.
2. `_write_integration_packet` reads parent task entry, includes a new top-level `parent` block:
   ```jsonc
   {
     "parent": {
       "task_id": "root",
       "intent": "...",
       "decomposition_overlap_unresolved": [
         {"path": "frontend/src/App.tsx", "claiming_features": ["v5-aaa", "v5-bbb"]}
       ]
     },
     "children": [...]  // existing
   }
   ```
3. `lead-integration.md` Step 1 reads `parent.decomposition_overlap_unresolved`; for each path, uses `git merge --no-commit <branch>`, manually unions both contributions before committing.
4. Suppression: subsequent dispatch-loop iterations check `sibling_overlap_attempts >= 2` before re-firing the overlap detection — don't re-trigger the same finding.

## Verify (per CLAUDE.md `Verify:` requirement)

1. **Unit (`tests/test_v5_sibling_ownership_overlap.py`):**
   - Exact-path overlap (`frontend/src/App.tsx` vs `frontend/src/App.tsx`) — flagged
   - Prefix overlap (`frontend/src/components` vs `frontend/src/components/Card.tsx`) — flagged
   - Case-collision (`FRONTEND/src/App.tsx` vs `frontend/src/App.tsx`) — flagged via casefold
   - Disjoint (`frontend/src/Bookmarks.tsx` vs `frontend/src/Tasks.tsx`) — not flagged
   - N=3 claimants on same path — flagged with all 3 listed
   - Same-feature duplicate path entries — not counted as sibling overlap
   - Absolute path `/etc/foo` — rejected as `invalid_owned_path`, not silently ignored
   - Glob path `frontend/src/**/*.tsx` — rejected as `unsupported_owned_path_glob`
   - Path with `..` segment (`features/../bookmarks.py`) — rejected as `invalid_owned_path`
   - Windows drive (`C:\src\App.tsx`) and UNC (`\\server\share`) — rejected as `invalid_owned_path`
   - Empty owned_paths for a feature — no finding (the feature owns nothing)
   - **Suppression-after-exhaustion:** if parent has `sibling_overlap_attempts >= MAX` and `decomposition_overlap_unresolved` includes a path, that path should be filtered out of new `_foundation_isolation_feedback` emissions on subsequent iterations.
2. **Integration (`tests/test_v5_decomposed_child_lands_in_main.py`):**
   - 3-feature clean partition (current happy path) — no findings, no plan-amendment, no annotation
   - 3-feature with declared overlap on `App.tsx` — plan-amendment fires; attempt 1 fixes; clean dispatch
   - 3-feature with overlap that plan-amendment fails to fix → attempt 2 → degrade → annotation in packet; sibling_overlap_attempts=2 prevents re-trigger
3. **Anti-cascade regression (`tests/test_no_architect_cascade.py`):** verify no fresh architect dispatch happens via the new code path.
4. **Existing must-stay-green:** run `test_makeitbuild_p2`, `test_makeitbuild_p5`, `test_foundation_seeded_feature_paths`, and existing Phase 2 tests after every code change.
5. **Live run (clean):** rerun multi-domain linkboard intent with `--tier modular`. Expected: no overlap findings (partition was clean in the most recent run), cost/duration comparable to $3.45/22.9min baseline.
6. **Live run (forced overlap):** craft an intent likely to provoke `theme.ts`-class overlap. Expected: attempt 1 of plan-amendment, finding is HOIST or CANONICAL_OWNER, retry succeeds, clean dispatch.

## Effort estimate (revised)

- **Code:**
  - Extract `_compute_sibling_path_overlap_findings` from `_foundation_isolation_feedback`: ~50 LOC + careful refactor of existing call site.
  - Path-overlap contract tightening (casefold, validation, docs): ~30 LOC.
  - Dispatch integration after CHARTER persist: ~80 LOC (the gate + plan-amendment dispatch + retry budget).
  - `_write_integration_packet` parent-block addition: ~30 LOC + downstream consumers.
  - Task graph field additions: ~20 LOC.
- **Tests:** ~300 LOC (unit + integration + anti-cascade verification).
- **Prompt edits:** ~50 lines in lead.md Architect block + lead-integration.md Step 1.
- **Live validation:** 2 multi-feature runs (~25-45 min each).
- **Total:** ~1.5 focused sessions (Codex review showed this is bigger than I scoped originally).

## Out of scope

- **Glob/wildcard pattern support in `owned_paths`** — defer until a real case demands it.
- **Lever B (auto-loader as foundation-required pattern)** — separate plan.
- **Lever C (child-finish sibling-territory write check)** — Phase A/B should make this rare; defer.

## Codex gate trail

- **Round 1**: NEEDS REVISION. 6 findings (wrong layer for check, duplicate of existing logic, retry conflicts with anti-cascade, annotation flow broken, path algorithm under-specified, DECLARE_SHARED vaporware).
- **Round 2**: NEEDS REVISION. 4 narrower must-fix items (suppress existing emitter on exhaustion, re-persist CHARTER after each amendment, enforce glob/path rejection, no schema version bump, K aliased to `MAX_CONTRACT_AMENDMENT_ATTEMPTS`).
- **Round 3**: **APPROVED** with 2 non-blocking implementation notes (below).

## Implementation notes from round 3 (non-blocking)

1. **Architect prompt change lives in `lead-architect.md`**, not mainly `lead.md`. The "file paths/globs" wording in the architect prompt must be tightened to "file paths only — no globs" alongside the rest of the partition guidance.
2. **Invalid/glob rejection as validation in the owned-path parser/helper**, NOT as a breaking behavior change in the global `_normalize_contract_path` (which has many permissive call sites). Add a new validator (`_validate_owned_path` or similar) that the sibling-overlap check calls; `_normalize_contract_path` stays as-is for non-validation use.
