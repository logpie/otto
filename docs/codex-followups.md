# Otto i2p — honest follow-up gap list

This is the consolidated output of 5 parallel cross-check audits run
after the redesign was claimed "100% delivered". Each gap below was
verified against actual code with file:line citations. Codex should
treat this as the authoritative todo list for hardening.

## Gaps by severity

### A. Material — real work, not just naming

| # | Area | Gap | Evidence |
|---|------|-----|----------|
| A1 | **Slice→Group rename half-done** | Dataclass renamed, but pervasive remnants live | `Spec.__init__` accepts `slices=`/`cross_slice_checks=` kwargs (`spec_compile.py:1535-1567`); `spec_to_dict` emits dual `"groups"`+`"slices"` keys (`:1734-1737`); `SliceState` class + `slice.*` event kinds + `RunState.slices` dict (`spec_state.py:128-222`); `slice_id` on MergeCandidate/MergeResult/SliceResult; `SliceStatus` enum; `branch=i2p/<id>`; commit msg `i2p({slice_id}):`; `total_passing_slices=` kwarg in audit.py. Comments claim "remove at A0.3.4" — A0.3.4 never shipped. |
| A2 | **Group field renames deferred** | Plan promised `feature_ids`/`name`/`dependencies`/`dispatch_plan`. Reality: `tasks/title/deps`; `dispatch_plan` **missing entirely**; `dependencies` is property alias for `deps`. | `spec_compile.py:215-250` + module docstring openly admits deferral |
| A3 | ~~**CheckKind has 3 unimplemented executors**~~ — **DONE.** `CLIProbe`, `ImportCheck`, `TypeCheck` are now wired through `run_check` (`_run_cli_probe` asserts exit-code + optional stdout/stderr substrings; `_run_import_check` runs `python -c "import <pkg>"` with optional `__version__` assertion; `_run_type_check` shells out to mypy/pyright/basedpyright when on PATH, returning honest `passed=False` with `tool_available=False` when absent). Raw-log filter and `__all__` updated. Tests cover happy + failure paths per kind (CLI exit/stdout, ImportCheck local-module + version mismatch, TypeCheck unavailable + monkeypatched success/failure). | `otto/checks.py:111-116,300-487`; `tests/test_checks.py` |
| A4 | ~~**`tests/integration/test_intent_to_proof.py` missing**~~ — **DONE.** Real-Sonnet E2E test landed at `tests/integration/test_intent_to_proof.py`. Drives `uv run otto build --provider claude` against a throwaway tmp project. Asserts spec.json (groups/project_kind/intent), no blocked groups, proof-packet.{html,json} present, verdict ∈ {passed, partial}, strict==passed for happy path. Screenshot assertion is lenient (≥0) and links to gap A8 since Otto's default walkthrough does not produce screenshots/video. Gated by `OTTO_ALLOW_REAL_COST=1`, 15min `--budget`. | filed |
| A5 | ~~**`i2p-e2e` tier missing**~~ — **DONE.** `scripts/test_tiers.py` now exposes `i2p-e2e` which runs only `tests/integration/test_intent_to_proof.py`. Help string + module docstring document the OTTO_ALLOW_REAL_COST gate. | `scripts/test_tiers.py` |
| A6 | **Edit-and-recompile mid-build invalidation missing** | Plan said "edit spec mid-build → invalidates dependent in-flight slices wholesale". Reality: post-approval edits **blocked** in `spec_review_routes.py:103-153`; agent-driven amendments via `spec_amend.py` are scoped per-slice and don't cascade | `spec_review_routes.py:119-126`; no UI/API path where dependent slices get marked dirty |
| A7 | **No pause / abort verbs in MC** | Plan promised pause/resume + abort-a-slice. Reality: only Cancel + Resume exist (resume is just resume-from-checkpoint). No `pause` action handler. No per-slice cancellation API. | `grep '"pause"' otto/` returns nothing; `grep 'abort.*slice\|cancelSlice'` returns nothing |
| A8 | **Otto's default walkthrough produces no video/screenshots** | Plan promised "video capture, screenshot grid". Reality: `_synthesized_webapp_walkthrough` does Flask `test_client` GET only, saves rendered HTML body. Video/screenshots require the project to ship its own Playwright/Cypress `BrowserJourney` check (BYO). | `audit.py:390-510` |
| A9 | **Build agent NOT long-lived per slice** | Plan: "long-lived process while its slice is in flight". Reality: each retry constructs new `BuildAgentInput` and calls `build_agent(...)` which spawns a fresh subprocess via `run_agent_with_timeout`. Worktree+branch persist across retries, conversation does not. "Prompt-level reset" is implemented as fresh process. | `build.py:1574-1599, 2229-2294` |
| A10 | **Audit is multi-pass, not "one LLM pass at end"** | Plan: "one LLM pass at end on integrated product". Reality: `run_audit` itself loops up to `audit_retries+1` (default 2). On top of that, `audit_loop.repair_failing_features` (Layer 2) wraps run_audit. On top of THAT, `run_audit` has its own internal fix-agent slice-repair loop. Up to ~4 LLM judge calls per run. | `audit.py:585,677,805,870`; `audit_loop.py:211` |
| A11 | **Merge eligibility missing "base not stale" + "not superseded"** | Design doc lists both as core eligibility checks. Reality: only blocked-id check exists. Acceptable in single-worktree mode (acknowledged at `merge_queue.py:8-22`), but extension story is real follow-up for multi-worktree | `merge_queue.py:120-160` |
| A12 | **Merge-repair scope not enforced** | Plan: "edit scope = owned_paths + conflict regions" during repair. Reality: only enforced by prompt instruction; `detect_scope_violations` is NOT called on repair output before commit. | `merge_queue.py:586-647` |
| A13 | **No review gate between compile and build in `otto run`** | Plan Step 9 promised compile → review gate → build. Reality: compile → build directly; `--no-build` is the only review affordance | `otto run` flag list at `cli_run.py:271-355` |
| A14 | ✅ **DONE 2026-05-04** — Bench parity verdict now honors all Step 11 criteria. `_verdict()` returns `i2p_partial_wall_exceeded` on wall-excess and emits per-criterion `summary.parity` decomposition into `result.json`. New unit suite `tests/test_bench_microfeed_i2p_parity.py` (9 tests, all green). Bench itself not re-run (real-cost). | `scripts/bench_microfeed_i2p.py` |
| A15 | ✅ **DONE 2026-05-04** — CLAUDE.md: `otto run` added to Quick diagnosis block; per-session layout table gained `proof-packet.html`, `proof-packet.json`, `spec/spec.json`, `spec-state.jsonl` rows; header line lists `otto run \| build \| certify \| improve`. | CLAUDE.md |

### B. Cosmetic / framing — not material

| # | Gap | Note |
|---|-----|------|
| B1 | ✅ **DONE 2026-05-04** — plan.md updated to document `validate_spec(spec) -> ValidationResult` as the shipping symbol; rationale recorded for why broader-than-schema-only is correct (cycle/vagueness/dup-id checks must run before Build). No alias added — "schema-only" was a premature constraint. | `plan.md:252` |
| B2 | ✅ **DONE 2026-05-04** — `plan.md` "Files" section now documents all renames (otto/spec.py→spec_compile.py, otto/state.py→spec_state.py, state.jsonl→spec-state.jsonl, otto/certifier.py→audit.py+audit_loop.py, otto/merge.py→merge_queue.py, plus the test file renames). | `plan.md` |
| B3 | "Screenshot grid" is `.thumbs` inline-block, not CSS grid | `render.py:422-423` |
| B4 | Promised test files renamed: `tests/test_web_mission_control.py`, `tests/browser/test_spec_review_unified.py` don't exist (related tests under different names) | |
| B5 | Lifecycle test split: passed-only + blocked-only across 2 tests instead of 1 combined fixture | `tests/test_render.py:362,416` |
| B6 | ✅ **DONE 2026-05-04** — `otto/checks.py:4` and `otto/spec_compile.py:32,123` no longer reference the deleted `codex-i2p/otto/oracles.py`. Comments rewired to point at the live `otto/checks.py` browser_journey executor; `oracles.py` now framed as historical context only. | `otto/checks.py`, `otto/spec_compile.py` |
| B7 | ✅ **DONE 2026-05-04** — `plan.md` Risk register row updated to reflect the actual hard-error behavior of Phase C; legacy entry-points raise RuntimeError (not just `DeprecationWarning`). | `plan.md` Risk register |
| B8 | ✅ **DONE 2026-05-04** — `plan.md` build module description now explicitly notes the "long-lived" framing only applies at worktree+branch level; each retry spawns a fresh subprocess with no conversation continuity. | `plan.md:266-275` |
| B9 | Per-slice worktree default = `lambda _s: project_dir` (single-worktree, Phase A simplification, documented) | `merge_queue.py:8-22` |
| B10 | Repair-time counter split: cost shared across build/audit/merge; repair-wall-time is build-only | `audit.py` and `merge_queue.py` don't call `charge_repair` |
| B11 | ✅ **DONE 2026-05-04** — `plan.md` Phase B parity gate now documents that `scripts/bench_microfeed_real_webapp.py` doesn't exist in cc-i2p-2 (lived on codex-i2p which was never merged); the parity ceiling is a hard-coded 1500s rather than a re-run mono comparison. To get a true comparison, port the script from codex-i2p first. | `plan.md` Phase B section |
| B12 | Bench doesn't independently verify "≥1 screenshot + video" exist on disk | `bench_microfeed_i2p.py` |
| B13 | No CLI `otto amend` — `spec_amend.py` is library-only; only Mission Control `actions.py` wires the gate | |

## Recommended ordering for codex follow-ups

1. **Finish Slice→Group rename (A1)** — single biggest cleanup, mostly mechanical. Drop the dual JSON keys after one more bench cycle. Updates: `spec_compile.py`, `spec_state.py`, `merge_queue.py`, `build.py`, `audit.py`, render.py, all branch/commit-msg constants.
2. ~~**Wire missing CheckKind executors (A3)**~~ — DONE. All 8 CheckKind variants now dispatch through `run_check`; no silent "unsupported check kind" fallthrough for in-union kinds.
3. **Write the missing E2E test (A4) + add `i2p-e2e` tier (A5)**. Highest-value real-cost gate; backstops every future change.
4. **Decide on review gate semantics (A13)** — either implement the gate or remove the promise from plan.md.
5. **Bench parity logic (A14)** — fix the wall_s logic to actually fail the verdict when ceiling is exceeded.
6. **CLAUDE.md update (A15)** — add `otto run` to the CLI surface table; add proof-packet to the layout.
7. **Mid-build edit-and-recompile (A6)** + abort-a-slice (A7) — design + implement OR explicitly defer with rationale.
8. **Group field renames (A2)** — finish the deferred rename, including `dispatch_plan` if still relevant.
9. **Audit retry layering audit (A10)** — document the 3-4 layers explicitly OR collapse them.
10. **Merge eligibility extensions (A11) + repair scope enforcement (A12)** — needed before multi-worktree mode lands.
11. Cosmetic items B1–B13 — clean up as you touch each file.

A8 (BYO video/screenshots) is a design call — the design doc should
clarify whether Otto-shipped capture is in scope, or document the BYO
contract explicitly.
