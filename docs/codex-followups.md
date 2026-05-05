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
| A3 | **CheckKind has 3 unimplemented executors** | `CLIProbe`, `ImportCheck`, `TypeCheck` declared in union and listed in `DEFAULT_EVIDENCE_KINDS_PER_KIND` for cli/library projects, but `run_check` only dispatches 5/8 kinds — instances silently hit "unsupported check kind" | `checks.py:96-106,127-132`; zero hits for those kinds in checks.py |
| A4 | **`tests/integration/test_intent_to_proof.py` missing** | Promised real-Sonnet E2E test with concrete asserts (spec.json, proof-packet, audit=passed, ≥1 screenshot+video, OTTO_ALLOW_REAL_COST guard, 15min wall budget) — file does not exist | `ls tests/integration/` |
| A5 | **`i2p-e2e` tier missing** | `scripts/test_tiers.py` only defines `smoke|fast|default|full|integration|slow|web|browser-smoke|browser|prepush` | `test_tiers.py:23-104` |
| A6 | **Edit-and-recompile mid-build invalidation missing** | Plan said "edit spec mid-build → invalidates dependent in-flight slices wholesale". Reality: post-approval edits **blocked** in `spec_review_routes.py:103-153`; agent-driven amendments via `spec_amend.py` are scoped per-slice and don't cascade | `spec_review_routes.py:119-126`; no UI/API path where dependent slices get marked dirty |
| A7 | **No pause / abort verbs in MC** | Plan promised pause/resume + abort-a-slice. Reality: only Cancel + Resume exist (resume is just resume-from-checkpoint). No `pause` action handler. No per-slice cancellation API. | `grep '"pause"' otto/` returns nothing; `grep 'abort.*slice\|cancelSlice'` returns nothing |
| A8 | **Otto's default walkthrough produces no video/screenshots** | Plan promised "video capture, screenshot grid". Reality: `_synthesized_webapp_walkthrough` does Flask `test_client` GET only, saves rendered HTML body. Video/screenshots require the project to ship its own Playwright/Cypress `BrowserJourney` check (BYO). | `audit.py:390-510` |
| A9 | **Build agent NOT long-lived per slice** | Plan: "long-lived process while its slice is in flight". Reality: each retry constructs new `BuildAgentInput` and calls `build_agent(...)` which spawns a fresh subprocess via `run_agent_with_timeout`. Worktree+branch persist across retries, conversation does not. "Prompt-level reset" is implemented as fresh process. | `build.py:1574-1599, 2229-2294` |
| A10 | **Audit is multi-pass, not "one LLM pass at end"** | Plan: "one LLM pass at end on integrated product". Reality: `run_audit` itself loops up to `audit_retries+1` (default 2). On top of that, `audit_loop.repair_failing_features` (Layer 2) wraps run_audit. On top of THAT, `run_audit` has its own internal fix-agent slice-repair loop. Up to ~4 LLM judge calls per run. | `audit.py:585,677,805,870`; `audit_loop.py:211` |
| A11 | **Merge eligibility missing "base not stale" + "not superseded"** | Design doc lists both as core eligibility checks. Reality: only blocked-id check exists. Acceptable in single-worktree mode (acknowledged at `merge_queue.py:8-22`), but extension story is real follow-up for multi-worktree | `merge_queue.py:120-160` |
| A12 | **Merge-repair scope not enforced** | Plan: "edit scope = owned_paths + conflict regions" during repair. Reality: only enforced by prompt instruction; `detect_scope_violations` is NOT called on repair output before commit. | `merge_queue.py:586-647` |
| A13 | **No review gate between compile and build in `otto run`** | Plan Step 9 promised compile → review gate → build. Reality: compile → build directly; `--no-build` is the only review affordance | `otto run` flag list at `cli_run.py:271-355` |
| A14 | **Bench wall_s parity criterion failing but verdict says passed** | `bench-results/microfeed-i2p-20260505-001720`: `wall_s: 2123.0` vs `ceiling 1500.0` → fails parity, but verdict still emits `i2p_passed` | `bench_microfeed_i2p.py` parity logic |
| A15 | **CLAUDE.md stale** | Lists only `build/certify/improve/history/setup` — does NOT list `otto run` (the new entrypoint). | CLAUDE.md "Quick diagnosis" section |

### B. Cosmetic / framing — not material

| # | Gap | Note |
|---|-----|------|
| B1 | `compile_validator` symbol doesn't exist; it's `validate_spec` with broader-than-schema-only contract (folds in dep-cycle, vagueness, dup-id checks) | `spec_compile.py:2332` |
| B2 | File naming drift from plan: `otto/spec.py`→`spec_compile.py`, `otto/state.py`→`spec_state.py`, `state.jsonl`→`spec-state.jsonl`, `otto/certifier.py`→`audit.py`+`audit_loop.py`, `otto/merge.py`→`merge_queue.py` | Most are reasonable; just update plan/docs |
| B3 | "Screenshot grid" is `.thumbs` inline-block, not CSS grid | `render.py:422-423` |
| B4 | Promised test files renamed: `tests/test_web_mission_control.py`, `tests/browser/test_spec_review_unified.py` don't exist (related tests under different names) | |
| B5 | Lifecycle test split: passed-only + blocked-only across 2 tests instead of 1 combined fixture | `tests/test_render.py:362,416` |
| B6 | Stale doc-comment refs to deleted `oracles.py` | `checks.py:4`, `spec_compile.py:32,123` |
| B7 | Phase B "legacy still importable" mis-stated — actually stricter (raises on legacy import) | `pipeline.py:59` |
| B8 | "Long-lived agent process" framing oversells what's delivered (worktree+branch persist, conversation does not) | `build.py:1574-1599` |
| B9 | Per-slice worktree default = `lambda _s: project_dir` (single-worktree, Phase A simplification, documented) | `merge_queue.py:8-22` |
| B10 | Repair-time counter split: cost shared across build/audit/merge; repair-wall-time is build-only | `audit.py` and `merge_queue.py` don't call `charge_repair` |
| B11 | `scripts/bench_microfeed_real_webapp.py` (the bench's "adapt this" baseline) doesn't exist — comparison-vs-prior promise can't be substantiated | |
| B12 | Bench doesn't independently verify "≥1 screenshot + video" exist on disk | `bench_microfeed_i2p.py` |
| B13 | No CLI `otto amend` — `spec_amend.py` is library-only; only Mission Control `actions.py` wires the gate | |

## Recommended ordering for codex follow-ups

1. **Finish Slice→Group rename (A1)** — single biggest cleanup, mostly mechanical. Drop the dual JSON keys after one more bench cycle. Updates: `spec_compile.py`, `spec_state.py`, `merge_queue.py`, `build.py`, `audit.py`, render.py, all branch/commit-msg constants.
2. **Wire missing CheckKind executors (A3)** OR drop the unimplemented kinds from the union. Don't ship a typed enum where 3/8 silently fail.
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
