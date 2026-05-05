# Otto redesign — progress

Live checklist tracked by the drift-sentinel loop. Source of truth for
"where are we right now."

Conventions:
- `[ ]` = not started
- `[~]` = in progress
- `[✓]` = complete + verified
- `[!]` = blocked / failed
- Each item ends with `· verified <timestamp>` once Loop 2 confirms.

Last loop-1 sentinel run: tick 4 (2026-05-04T18:13Z)
Last loop-2 gate run: (none)
Last E2E sweep: i2p-smoke-2 webapp counter (2026-05-04 20:27) — verdict=blocked (honest, real merge bug surfaced), $1.21, ~560s, full pipeline compile→build→merge→audit→render with no crash. Report: `docs/i2p-smoke-2-20260504-202757.md`. Session: `/tmp/otto-i2p-smoke-2-20260504-202757/otto_logs/sessions/2026-05-05-032804-b07585/`.
Prior E2E sweep: tick 10/11 cli — verdict=blocked (honest), $1.52, 586s. Smoke contract PASS.
Prior E2E sweep: tick 5/6 webapp — verdict=blocked (honest), $0.55, 212s. Smoke contract PASS.

### Codex remaining-gap pass (2026-05-05, branch codex-i2p-v2)
- [✓] **A8 — bundled screenshot/video capture** — `_synthesized_webapp_walkthrough` now runs Playwright for default webapp walkthroughs after Flask/static discovery. Artifacts: `screenshot-home.png`, `dom-home.html`, `walkthrough.webm` when video is produced, `browser-capture.log`, and conservative `walkthrough.jsonl`. Missing Playwright/browser binary is logged honestly and falls back to HTML evidence. · verified 2026-05-05
- [✓] **A9 — provider session continuity** — `BuildAgentOutput.session_id` and `BuildAgentInput.agent_session_id` now thread through build retries, merge repair, audit compatibility repair, and Layer 2 repair; `default_build_agent` maps it to `AgentOptions.resume`. · verified 2026-05-05
- [✓] **A10 — live retry-layer collapse** — `run_pipeline` calls `run_audit(..., fix_agent=None)` and reserves the supplied fix agent for `repair_failing_features`; direct `run_audit` callers retain the old compatibility loop. · verified 2026-05-05
- [✓] **A11 — superseded merge eligibility** — merge queue now computes latest Group/Component result per id, ignores older PASSING entries superseded by later results, and uses the latest result's branch/worktree for the candidate. Base freshness remains the merge-into-current-HEAD + verification/rollback strategy. · verified 2026-05-05
- [✓] **A3.2 — proof templates** — added `otto/web/templates/proof-packet.html.j2` and `feature-proof.html.j2`; the proof packet and per-Feature proof renderers now load those templates through dependency-free placeholder substitution. · verified 2026-05-05
- [✓] **B5 — combined lifecycle render fixture** — `tests/test_render.py` now has one render-run fixture with a landed Group and blocked Group, asserting HTML + JSON lifecycle state together. · verified 2026-05-05
- [✓] **Regression sweep for this pass** — `uv run pytest -q tests/test_audit.py tests/test_build.py tests/test_merge_queue.py tests/test_runner.py tests/test_runner_layer2_fix.py tests/test_audit_loop_repair.py tests/test_render.py tests/test_render_per_feature.py tests/test_a1a_dataclasses.py -k 'not test_autopilot_full_executes_safe_recovery_once'` -> 294 passed. `uv run python scripts/test_tiers.py fast` -> 1391 passed, 531 deselected.

### i2p-smoke-2 anomaly fixes (2026-05-04)
- [✓] **Issue 1 — `--yes` rejected with stale `otto run` hint** — `otto/cli.py` build-path: `--yes` removed from i2p ignored-flag list (silently accepted; the i2p path has no interactive spec-approval step, so it's a definitional no-op). Stale "pass them to `otto run`" hint reworded to drop the bogus subcommand reference (build + certify paths). No tests assert on the message text. · verified 2026-05-04
- [✓] **Issue 2 — `--budget` semantics** — no code change. `otto/cli.py:1006,1173` already declare `--budget` help as "Total wall-clock budget in seconds, must be > 0"; CLAUDE.md doesn't claim USD. Confirmed via `otto build --help` output. · verified 2026-05-04
- [✓] **Issue 3 — missing `summary.json` at session root** — real regression. `otto.runner.run_pipeline` is intentionally headless and only emits `proof-packet.{html,json}`, but `summary.json` is consumed by `otto/resume.py:_read_prior_accounting`, `otto/runs/history.py`, `otto/runs/atomic_repair.py`, and Mission Control. Added `_persist_session_summary` helper in `otto/cli_run.py` that calls existing `_write_session_summary` from `otto.runs.lifecycle` after every i2p completion; wired into greenfield tail of `orchestrate_run` and into `_drive_brownfield_pipeline` (covering certify + improve). Threaded `command="build|certify|improve"` through call sites. Failures are logged + non-fatal (bookkeeping must not mask verdict). Full sweep `tests/ -q --ignore=integration --ignore=browser`: 1585 passed. · verified 2026-05-04

### codex-followups gap-fixes (2026-05-04, branch cc-i2p-2)
- [✓] **A6 — Mid-build spec edit invalidation** — landed end-to-end. New lifecycle state `editing_in_flight` (`otto/web/spec_review_routes.py:52`); the runner sets it before `run_build` and reverts to `approved` after re-dispatch (`otto/runner.py` build-phase block + `_set_lifecycle_best_effort`). `compute_invalidation(old_spec, new_spec) -> InvalidationPlan` in `otto/spec_amend.py` returns direct (name / feature_ids / dependencies / owned_paths / checks deltas) + cascading (Groups whose deps include an invalidated id) Group invalidations, plus `removed_group_ids` / `added_group_ids` informational sets. POST `/api/specs/<id>/edit` accepts edits during `editing_in_flight` and emits `group.invalidated_by_spec_edit` events per affected Group. New event kind in `otto/spec_state.py:96` plus an `INVALIDATED` phase. Build loop's `_run_slice` checks the journal between attempts and aborts in place with `BLOCKED + "invalidated by spec edit: <reason>"` (helper `_spec_edit_invalidation_reason`); the worktree is left unmerged for forensic value. Runner re-dispatches once via `_redispatch_invalidated_groups`: scans the journal, re-loads the post-edit Spec from `<session>/spec/spec.json`, runs a second `run_build` over the invalidated subset (skip_components covers everything else), and merges the GroupResult lists with cost/wall accumulation. Tests: 8 new `compute_invalidation` cases in `tests/test_spec_amend.py` (no-op, name change + cascade, feature_ids change, dependency change, unrelated group untouched, removed group, added group, owned_paths change); runner integration test in `tests/test_runner.py::test_spec_edit_invalidation_redispatches_affected_groups` exercises the full re-dispatch with stubbed agents asserting two build calls + lifecycle revert + cost accumulation; new spec-review-route tests cover the `editing_in_flight` accept path and the still-blocked `approved` path. Design note: `docs/i2p-spec-edit-design.md` (records the resolved ambiguities: tier-1 stays locked; ANY round-tripping edit accepted; component invalidation, multi-edit-per-run, and worktree GC explicitly deferred). Full sweep `tests/ -q --ignore=integration --ignore=browser`: 1640 passed. · verified 2026-05-04
- [✓] **A13 — Review gate between compile and build** — landed in `otto/runner.py:run_pipeline` (review-gate poll loop between compile and seed) + `otto/cli_run.py` + `otto/cli.py` (new `--review-gate` / `--auto-approve` / `--gate-timeout` flags on `otto run` and `otto build --i2p`). Two new event kinds added to `otto/spec_state.py`: `spec.review_pending` (runner emits when gate engages) and `spec.review_approved` (resume signal the runner polls for). `otto/web/spec_review_routes.py` `POST /api/specs/<id>/approve` now additionally emits `spec.review_approved` on every call (idempotent at the lifecycle layer; the journal accepts repeats so a transient runner restart can re-read the signal). `--review-gate` is OFF by default to preserve CI/script automation; `--auto-approve` is the explicit-opt-out flag. CLI banner prints `http://localhost:<OTTO_WEB_PORT|8765>/runs/<session>/spec/review` so the operator knows where to approve. Resume path bypasses the gate (operator already approved on the prior session). Gate timeout (default 24h) halts the run with verdict=BLOCKED honestly when no approval lands. Tests: `tests/test_runner.py` (5 new — pause-until-approved, timeout, off-by-default, announce callback, resume bypass), `tests/test_cli_run.py` (5 new — flag wiring, default omitted, mutual-exclusion error, `otto build --i2p --review-gate`, help-text exposure), `tests/test_spec_review_routes.py` (1 new — POST /approve emits spec.review_approved). Full sweep `tests/ -q --ignore=integration --ignore=browser`: 1640 passed. · verified 2026-05-04
- [✓] **A14 — Bench parity verdict** — `scripts/bench_microfeed_i2p.py` `_verdict()` now honors all plan.md Step 11 parity criteria. Wall excess no longer silently emits `i2p_passed`; it returns `i2p_partial_wall_exceeded` and writes a per-criterion `summary.parity` decomposition into `result.json`. Functional fails (audit/hidden/browser/blocked) outrank wall-excess; quality-low outranks wall-excess. New unit suite `tests/test_bench_microfeed_i2p_parity.py` (9 tests) locks the ladder; bench itself was not re-run (real-cost). · verified 2026-05-04
- [✓] **A7 — Pause + Resume + Abort-a-Group verbs** — Mission Control now exposes the verbs the plan promised. **Pause:** `otto/mission_control/actions.py:execute_pause_run/_resume_run` append `run.paused_by_user` / `run.resumed_by_user` events to `<session>/spec-state.jsonl`; `otto/runner.py:_wait_while_paused` polls the journal at every `_phase()` boundary (1s cadence; tunable via `runner.PAUSE_POLL_INTERVAL_S`) and sleeps until cleared. SIGSTOP rejected on portability + IO-safety grounds — freezing async I/O mid-flight risks corrupted tempfiles + held fd locks; the journal poll-flag gives clean phase boundaries. Existing Cancel (SIGTERM) is unchanged. **Abort-a-Group:** `execute_abort_group(session_dir, group_id, reason)` appends `group.aborted_by_user`; `otto/build.py:_run_slice` checks `is_group_aborted_by_user` at the top of each retry attempt and exits with `status=BLOCKED, failure_narrative="aborted_by_user"`. The merge queue pre-populates `blocked_ids` with `aborted_group_ids(session_dir)` so aborted groups never become merge candidates. Component abort intentionally not exposed (Group is the user-facing dispatch unit per research §3). **Routes:** `POST /api/run-view/{sid}/actions/{pause,resume}` and `POST /api/run-view/{sid}/groups/{gid}/abort` (mounted from `otto/web/run_view_routes.py`). **UI:** `RunDrawer` shows Pause+Resume in a run-action-bar while non-terminal; `GroupList` renders per-Group Abort buttons (hidden on landed/blocked/failed_scope groups); `RunViewPage` wires `onAfterAction={reload}` so the next poll picks up state immediately. Three new event kinds added to `otto/spec_state.py` + helper predicates `is_run_paused_by_user` / `is_group_aborted_by_user` / `aborted_group_ids`. Tests: 7 unit tests in `tests/test_mission_control_actions.py` (event emission, idempotence, missing-session guards, pause→resume flip), 2 integration-ish tests in `tests/test_runner.py` (real `run_build` with a 2-group spec where the aborted group is BLOCKED + the live group runs to PASSING; `_wait_while_paused` blocks until a resume event lands). Full sweep `tests/ -q --ignore=integration --ignore=browser`: 1640 passed; `npm run web:typecheck && npm run web:build` green. · verified 2026-05-04
- [✓] **A15 — CLAUDE.md stale CLI surface** — added `otto run` to the Quick diagnosis block; added `proof-packet.html`, `proof-packet.json`, `spec/spec.json`, `spec-state.jsonl` rows to the per-session layout table; header line now lists `otto run` alongside `build|certify|improve`. · verified 2026-05-04
- [✓] **B1 — `compile_validator` plan-vs-reality drift** — `plan.md:252` now documents `validate_spec(spec) -> ValidationResult` as the shipping symbol with rationale for why it is broader than schema-only (dep-cycle/vagueness/dup-id checks must run before Build to avoid corrupting the merge queue). No alias added — the "schema-only" framing was a premature constraint. · verified 2026-05-04
- [✓] **B6 — stale `oracles.py` doc-comment refs** — `otto/checks.py:4` and `otto/spec_compile.py:32,123` no longer reference the deleted `codex-i2p/otto/oracles.py`. Comments now point at the live `otto/checks.py` browser_journey executor and frame the prototype `oracles.py` as historical context only. 1608 tests green post-fix. · verified 2026-05-04
- [✓] **A1 — Slice→Group rename across runtime + serialized form** — `otto/spec_state.py` renamed `SliceState`→`GroupState`, `slice_id`→`group_id`, `RunState.slices`→`groups`, all `slice.*` event kinds → `group.*`. `otto/spec_compile.py` dropped `slices=`/`cross_slice_checks=` kwargs from `Spec.__init__`, removed the back-compat `Spec.slices`/`Spec.cross_slice_checks` properties, and `spec_to_dict` now emits ONLY `groups`/`cross_group_checks` (one-cycle deprecation read fallback for legacy keys preserved with deprecation warnings via new `spec.deprecated.*` warning codes). `otto/build.py`, `otto/audit.py`, `otto/render.py`, `otto/merge_queue.py`, `otto/resume.py`, `otto/cli_run.py`, `otto/runner.py`, `otto/spec_amend.py`: `SliceResult`→`GroupResult`, `SliceStatus`→`GroupStatus`, `SliceVerdict`→`GroupVerdict`, `SlicePacket`→`GroupPacket`, `slice_id` field→`group_id`, `slice_results`→`group_results`, `total_passing_slices`→`total_passing_groups`, `slice_verdicts`→`group_verdicts` (Python field; JSON wire key also updated to `group_verdicts`/`group_id` since tests live in repo), `passing_slice_ids`→`passing_group_ids`, `_merge_slice_branch`→`_merge_group_branch`, `BuildAgentInput.slice`→`group`, `branch_for_slice`→`branch_for_group`, etc. Commit messages and event payloads use `group_id`. Branch naming `i2p/<id>` left as opaque prefix per scope. `otto/web/` left untouched per scope; tests of web routes that read `spec_data["slices"]` still fail (4 web-only failures, see "Open gaps" below). 1604 / 1608 non-integration tests pass. · verified 2026-05-04
- [✓] **A2 — Group field renames** — `otto/spec_compile.py:Group` renamed `tasks`→`feature_ids`, `title`→`name`; dropped the `dependencies` property alias and renamed `deps`→`dependencies` directly. Added optional `dispatch_plan: str = ""` field with docstring documenting deferral honestly (plan.md only lists it as a placeholder; shape is intentionally underspecified, persisted only when non-empty). `_coerce_slice_id` renamed `_coerce_group_id`; `_SLICE_ID_RE` → `_GROUP_ID_RE`. Validator messages updated (`group id …`, `feature_ids field empty`, `multi-group spec declares no cross_group_checks`). All consumers updated: `otto/build.py:_component_as_slice`, `otto/merge_queue.py:_component_as_merge_slice`, `otto/spec_compile.py:render_spec_md` and the spec-md round-trip parser. Test fixtures and helpers updated across `tests/test_spec_compile.py`, `tests/test_spec_amend.py`, `tests/test_audit.py`, `tests/test_build*.py`, etc. The compile-spec prompt example in `otto/prompts/compile-spec.md` now emits `groups`/`name`/`feature_ids`/`dependencies` JSON keys. · verified 2026-05-04

#### Open gaps from A1/A2

- [✓] **A1 web-surface propagation (cc-i2p-2 follow-up, 2026-05-04)** — `otto/web/i2p_routes.py` migrated to canonical `groups`/`group_id`/`group_count`. Reads `spec_data["groups"]`, calls `replay(group_ids=...)`, emits `group_count` (was `slice_count`), `landed_count` from `landed_group_ids` (was `landed_slice_ids`), `blocked_count` from `blocked_group_ids` (was `blocked_slice_ids`). State serialiser emits `groups: [{group_id, phase, ...}]` (was `slices: [{slice_id, ...}]`). Embedded HTML/JS shell + CSS classes renamed `slice`→`group`. `otto/web/client/src/types.ts` comment updated. Test assertions in `tests/test_web_i2p_routes.py` updated to match (`s["group_count"]`, `ss["group_id"]`). `npm run web:typecheck && npm run web:build` clean. `tests/test_web_i2p_routes.py` 17/17 green; full sweep 1608/1608. · verified 2026-05-04
- Legacy-name back-compat surface preserved on read in the parser (one cycle): `"slices"` → `"groups"`, `"cross_slice_checks"` → `"cross_group_checks"`, inner `tasks/title/deps` → `feature_ids/name/dependencies`. Each emits a deprecation warning via `spec.deprecated.*` codes. Drop these reads in a future cycle.
- `BuildBudget.per_slice_*` legacy kwargs and property aliases in `otto/build.py` were preserved as-is (caller back-compat is broader than A1's stated scope); the canonical field names are `per_group_*`.

## i2p `--resume` (2026-05-04, branch cc-i2p-2)
[✓] **i2p `--resume` implementation** — landed per
`docs/i2p-resume-design.md`. New `otto/resume.py` derives a
`ResumePlan` from the paused session's `spec-state.jsonl` (replay) +
`summary.json` (cost-carry). The runner accepts `resume_plan=` and
threads it into `run_build` (skip-already-landed Components via
synthesised PASSING SliceResult/ComponentResult entries) and into the
audit phase (short-circuit when journal recorded `audit.finished` with
non-empty verdict). Cost-carry is enforced by charging
`prior_cost_usd` to the shared `BuildBudget` on entry; `--reset-budget`
zeroes it. Spec-edit policy v1: refuse on hash mismatch with `--force`
escape (logs `spec.regenerated` event). `otto build|certify|run
--resume` flags wired in `otto/cli.py` + `otto/cli_run.py`. Mid-merge
git recovery probes the project worktree at resume entry. Tests:
`tests/test_resume.py` (11 unit tests covering classification,
spec-hash check, mid-merge detection), `tests/test_cli_run.py`
(4 new flag-propagation tests), `tests/test_runner.py` (2 new tests
for skip-components plumbing + audit short-circuit). Full sweep
(1585 tests) passes.

**E2E SWEEPS PAUSED** (user directive 2026-05-04): prioritize implementation
over E2E. Resume only at major implementation milestones (A1a complete, A2
complete, A3 complete, A4 complete, before Phase B cutover, before Phase C
deletion). Until then the loop builds new design code; E2E validates after
each milestone instead of every 5 ticks.

**STRATEGY SHIFT** (user directive 2026-05-04): jump ahead from incremental
A0 vocabulary cleanup to A1a (Feature dataclass + new design data model).
Add Feature/Component/Guardrail/Component dataclasses ALONGSIDE the existing
Slice/Group structures. Vocab cleanup of legacy continues opportunistically
when files are touched, but is no longer the bottleneck.
Current phase: **A0.3 (Slice→Group rename; tick 7 resumes — Spec.slices field rename + call-site propagation)**

**New sequence (per plan reviewer's recommendation):**
A0 → A1a → A1.5-types → A1b → A1c → A1.5-seed → A2 → A3 → A4 → A5 → A6 → B → C

**Phase reasoning:**
- A0 is rename + JSON shims (5-7 days, not 1-2 — most code already exists)
- A1 split into A1a (dataclasses) / A1b (build+checks) / A1c (merge)
- A1.5-types between A1a and A1b: lock RunView TS interface upfront
- A1.5-seed between A1c and A2: pre-Audit fixture seeding for multi-user products

---

## Pre-flight

Before any phase begins, these must hold:

- [ ] `research.md` reviewed by user, signed off
- [ ] `plan.md` reviewed by user, signed off
- [ ] All review-walkthrough-* agent reports read and any blocking
      changes folded into research.md / plan.md
- [ ] `docs/otto-wireframes.md` reviewed by user, signed off
- [ ] `progress.md` (this file) initialized with phase checklists
- [ ] `drift-log.md` initialized
- [ ] `review.md` initialized

---

## Phase A0 — Vocabulary refactor + defaults.py

**Goal:** zero hits for retired words; `otto/defaults.py` owns all
numeric knobs.

### Steps

- [✓] A0.1 — Inventory current vocabulary debt · verified 2026-05-04T17:55Z
  - [✓] Vocab grep run; baselines:
    - `otto/`: 1672 hits across 67 files
    - `tests/`: 700 hits
    - `docs/` (excluding signed-off): 265 hits
    - per-term in otto/: slice=520, capability=23, capability_verdict=0,
      certifier=214, story=753, stories_passed=95, stories_tested=110
  - [✓] Magic-number grep run; baseline: 23 hits outside `otto/defaults.py` + `otto/prompts/`
  - [✓] Baseline recorded above

- [✓] A0.2 — Create `otto/defaults.py` and wire it · verified 2026-05-04T18:08Z
  - [✓] Define schema: `retries`, `budgets`, `audit`, `agents` · 2026-05-04T18:01Z
  - [✓] Read from `otto.yaml` if present; fall back to baked-in defaults · 2026-05-04T18:01Z
  - [✓] Wire `BuildBudget` defaults through `defaults.py` via
        `field(default_factory=...)` · 37 build tests passing 2026-05-04T18:08Z
  - [✓] Test: `tests/test_defaults.py` covers override precedence
        (CLI > otto.yaml > baked-in) · 11 tests passing 2026-05-04T18:01Z
  - 23 magic-number hits remaining are transport-layer subprocess/network
    timeouts in legacy modules (Phase C deletion targets); not
    configurable budgets. Documented in drift-log.md as info severity.
    Future scans treat 23 as legacy floor; only halt on increase.

- [~] A0.3 — Rename `slice` / `Slice` → `group` / `Group` (in progress)
  - [~] Python: dataclass + variables + JSON keys + prompt files
    - [✓] `class Slice` → `class Group` in spec_compile.py with
          backward-compat alias `Slice = Group` · 33+37+11+11=92 tests
          passing 2026-05-04T18:13Z; slice-hits 520→517
    - [✓] Spec.slices field → Spec.groups with backward-compat property
          + custom __init__ accepting both `slices=` and `groups=` kwargs
          + JSON serialization emits both keys + parse_spec reads either
          · 81 tests passing 2026-05-04T20:23Z; slice-hits flat at 517
          (back-compat aliases preserve refs; will drop after external
          callers migrate in tick 8+)
    - [✓] Spec.cross_slice_checks → Spec.cross_group_checks with
          back-compat property + __init__ kwarg alias + JSON dual-write/dual-read
          · audit.py call sites migrated · 92 tests passing 2026-05-04T20:50Z
    - [ ] Propagate Group through call sites (otto/build.py, otto/audit.py,
          otto/render.py, otto/cli_run.py, otto/merge_queue.py)
    - [✓] Update prompt files (otto/prompts/*.md) — only
          otto/prompts/compile-spec.md contained legacy `slice` /
          `slices` prose; renamed to `group` / `groups` while
          preserving the wire-format `<spec_json>` JSON example block
          (which still emits `"slices":` under dual-write back-compat).
          New tests/test_prompt_group_vocabulary.py (18 tests) lints
          every prompt file: prose outside fenced code blocks must use
          `group`; the legacy `slices` JSON key in compile-spec.md's
          example is asserted to still be present until the wire
          cutover. 166 prompt+spec tests passing 2026-05-04T22:30Z.
    - [✓] Remove `Slice = Group` alias once vocab scan = 0 · all
          otto/ + tests/ identifier-position uses migrated to `Group`;
          remaining `\bSlice\b` hits are docstrings/comments documenting
          historical vocabulary or string-literal slug-validation inputs
          (e.g. `"Auth Slice"`); 1655 tests passing 2026-05-04
          (alias deleted at otto/spec_compile.py:256)
  - [ ] Schema migration: old `proof-packet.json` files (v2 with `slices[]`)
        still readable; new files emit `groups[]` (v3)
  - [ ] Frontend types and field names

- [x] A0.4 — Rename `capability` / `capability_verdict` → `feature` / `feature_verdict`
  - [x] otto/audit.py: `CapabilityVerdict` dataclass → `FeatureAudit`.
        Name disambiguates from TS-layer `FeatureVerdict` Literal (verdict
        outcome string) — Python dataclass carries name+status+detail+evidence_refs.
        Legacy `CapabilityVerdict = FeatureAudit` alias dropped post-cutover.
  - [x] `AuditResult` and `AuditAgentOutput` carry `feature_audits` only.
        The mirrored `capability_verdicts` field + `__post_init__` mirror
        logic was removed after the back-compat window.
  - [x] Propagated to otto/render.py and scripts/bench_todo_cli_i2p.py.
        `ProofPacket` carries `feature_audits` only; `render_json` emits
        only the canonical key. Bench script reads `feature_audits`
        directly (legacy `.get("capability_verdicts")` fallback removed).
  - [x] Audit prompts updated: `_audit_prompt` requests `feature_audits`;
        `_parse_audit_output` reads `feature_audits` only. The legacy
        wire-key parser branch and back-compat note in the prompt were
        removed. Coverage in `tests/test_a0_4_propagation.py` (rewritten
        to assert canonical-only) and `tests/test_audit*.py`.
  - [x] Back-compat removal (post-cutover, ~2 weeks stable): all
        production references to `capability_verdicts` /
        `CapabilityVerdict` removed from `otto/` and `scripts/`. The
        obsolete `tests/test_audit_vocab_renames.py` (which pinned the
        alias contract) was deleted. Only historical comments mention
        the old name. 1568/1568 tests green.

- [✗] A0.5 — Rename `certifier` → `audit` — **SUPERSEDED by Phase C deletion** (tick 60)
  - The new-stack `otto/audit.py` + `otto/audit_loop.py` modules already
    exist and are populated. Renaming `otto/certifier/` is wasted work
    because that directory is slated for deletion in Phase C
    (see `docs/phase-c-deletion-audit.md`). Leave certifier in place
    until Phase C deletes it; new code uses `otto/audit*.py` directly.

- [✗] A0.6 — Retire `story` / `stories_passed` / `stories_tested` —
      **SUPERSEDED by Phase C deletion** (tick 60)
  - Story/stories vocabulary lives in `otto/certifier/__init__.py`
    (legacy) and `tests/test_certifier_stories.py`. Both delete in
    Phase C. The new stack uses `Feature` and `feature_audits`
    everywhere.

- [✗] A0.7 — Retire `task` from user-facing surfaces — **DEFERRED** (tick 60)
  - Most legacy `task` references are in v3 pipeline modules (slated
    for Phase C). Surfaces in the new stack already use `step` /
    `todo_item` per progress.md guidance. Mark as deferred until a
    user surface concretely needs the rename.

### Exit criteria (Loop 2 gate)

- [ ] Grep: zero hits for retired vocabulary across `otto/`, `tests/`,
      `docs/` (excluding `docs/otto-redesign-conversation.md` and
      `docs/legacy/*` if any)
- [ ] Grep: zero magic-number occurrences outside `otto/defaults.py`
      and `tests/`
- [ ] Full test suite green
- [ ] `npm run web:typecheck && npm run web:build` green
- [ ] Bench A (greenfield e2e tier) runs to completion with no
      behavioral regression vs pre-A0 baseline

---

## Phase A1a — Dataclasses (2 days)

**Goal:** Refactor existing `otto/spec_compile.py` (`Slice` → `Group`,
add Feature/Guardrail/Component dataclasses + shared_paths +
audit_fixtures).

### Steps

- [✓] A1a.1 — Rename `Slice` → `Group` (done at A0.3 with backward-compat alias)
- [✓] A1a.2 — Add `Feature` dataclass (id, name, description,
      acceptance_detail, evidence_kinds[], group_id, verdict?,
      evidence_completeness, coverage_confidence, multi_actor_required,
      audit_pre_merge) · 2026-05-04T tick 12
- [✓] A1a.3 — Add `Guardrail` dataclass (id, text, applies_to) · tick 12
- [✓] A1a.4 — Add `Component` dataclass (research §2.6) — id, name,
      description, owned_paths, dependencies, checks, consumed_by · tick 12
- [✓] A1a.5 — Add `Spec.shared_paths: list[str]` (research §2.6) · tick 12
- [✓] A1a.7 — Add `Spec.audit_fixtures[]` (research audit-honesty) ·
      `AuditFixture` dataclass with kind + payload · tick 12
- [✓] A1a.8 — `Finding` dataclass + `FINDING_SEVERITIES = (critical,
      important, polish)` for severity ladder (research §4) · tick 12
- [✓] A1a.9 — Spec.__init__ accepts new kwargs (features, components,
      guardrails, shared_paths, audit_fixtures) · tick 12
- [✓] A1a.10 — Unit tests: 19 new tests in tests/test_a1a_dataclasses.py
      covering construction, defaults, scoping, id stability · tick 12
- [✓] A1a.11 — JSON round-trip: spec_to_dict emits features /
      components / guardrails / shared_paths / audit_fixtures keys;
      parse_spec permissively reads them (defaults to [] if absent
      → legacy spec compat); 6 new round-trip tests pass · tick 13
      (117/117 total)
- [✓] A1a.6 — Per-`project_kind` structure schemas (research §2.7):
      webapp/api/library/cli JSON schemas exist on disk;
      `DEFAULT_EVIDENCE_KINDS_PER_KIND` constant + `default_evidence_kinds_for()`
      helper added to spec_compile.py; 5 new tests pass · tick 14
      (122/122 total)
- [✓] A1a.12 — `parse_spec_md(md_text, base=None) -> tuple[Spec, list[str]]`
      with id stability across rename + mechanical-field preservation
      via base · tick 33 (123/123 total)
- [✓] A1a.13 — `render_spec_md(spec) -> str` user-facing prose with
      `<!-- group: id -->` / `<!-- feature: id | evidence: ... -->`
      metadata comments · tick 32

### Exit criteria (Loop 2 gate)

- [ ] `pytest tests/test_spec.py` green
- [ ] Round-trip property test passes
- [ ] JSON back-compat: existing session dirs with `"slices"` keys
      still load correctly
- [ ] Compile produces specs with Feature ids stable from `name` slug

---

## Phase A1.5-types — RunView TypeScript contract (1 day, between A1a and A1b)

**Goal:** Lock the API contract before backend implementation continues.

### Steps

- [✓] Define `otto/web/client/src/types/run.ts` with full `RunView`,
      `FeatureView`, `GroupView`, `ComponentView`, `GuardrailView`,
      `StageView`, `EvidenceRef`, `RunMeta`, `FindingView` interfaces
      · tick 15 2026-05-04T21:14Z
- [✓] Include `null` semantics for in-flight fields · verdict, finished_at,
      duration_s, cost_usd at stage level all nullable for active runs
- [✓] Reflect review-walkthrough findings: `evidence_completeness`
      (full/proxy_only/partial), `coverage_confidence` (high/medium/low),
      `multi_actor_required`, severity-tagged `FindingView`,
      Component/Guardrail first-class types
- [ ] Stub `otto/mission_control/run_view.py:build_run_view`
      returning the correct shape (deferred to A4 / when backend wires up)

### Exit criteria

- [✓] `npm run web:typecheck` green · tick 15
- [ ] Stub returns valid `RunView` for fixture session dir (deferred — A4)

---

## Phase A1b — Build + Checks (2 days)

**Goal:** Refactor `otto/build.py` + `otto/checks.py` to dispatch by
Group/Component, thread `feature_id` through Evidence, add new Check
kinds.

### Steps

- [✓] A1b.1 — `build_groups` is the canonical dispatch entry. The
      historical name was `run_build` (no `build_slices` ever shipped);
      `build_groups` and `build_slices` are now module-level aliases
      pointing at `run_build`, both re-exported via `__all__`. Verified
      by `tests/test_build_renames.py::test_build_groups_is_canonical_dispatch_entry`.
- [✓] A1b.2 — `BuildBudget` canonical fields are now
      `per_group_retries_hard_cap` / `per_group_wall_s` /
      `per_group_cost_usd`; default factories routed through
      `otto/defaults.py` (`budgets.per_group_cost_usd`,
      `retries.check_loop.*`). Legacy `per_slice_*` names remain
      accepted as constructor kwargs and as attribute reads/writes via
      `@property` aliases that proxy to the canonical fields. Passing
      both legacy and canonical for the same field raises `TypeError`.
      Internal callers in `otto/build.py` updated to canonical names.
      Verified by `tests/test_build_renames.py` (10 tests).
- [✓] A1b.3 — **Component dispatch** — Components run alongside Groups
      in same parallel build phase · `run_build` now iterates
      `ready_slices` + `ready_components` in one loop, builds each
      Component on its own branch, populates `BuildResult.component_results`,
      and propagates dep-blocked Components as BLOCKED with attempts=0.
      Build agent reuses existing `BuildAgentCallable` via a synthetic
      Slice adapter (`_component_as_slice`).
- [✓] A1b.4 — **Shared-paths handling** — Groups may freely edit
      shared_paths; merge queue serializes lands · `detect_scope_violations`
      now treats `Spec.shared_paths` as globally writeable (in addition to
      legacy `shared_scaffold`); Component owned_paths participate in the
      peer-vs-dep partition. shared_paths overrides peer ownership.
- [✓] A1b.5 — `otto/checks.py:Evidence` gets `feature_id` field.
      Verified in the current build/audit/render path: check evidence carries
      `feature_id`, audit/render prefer Feature ids over display names, and
      scoped Layer 2 re-audit uses Feature ids as the stable join key.
- [x] A1b.6 — Add `CLIProbe`, `ImportCheck`, `TypeCheck` kinds
      (research §2.7) · dataclasses live in `spec_compile.py`; executors
      `_run_cli_probe`, `_run_import_check`, `_run_type_check` wired in
      `otto/checks.py` with happy + failure tests in `tests/test_checks.py`
      (closes codex-followups gap A3 — full sweep `pytest tests/ -q
      --ignore=tests/integration --ignore=tests/browser` 1599 passed).
- [x] A1b.7 — Walkthrough actions emit `action_kind` discriminator +
      per-kind fields (research §2.7)

### Exit criteria

- [ ] `pytest tests/test_build.py tests/test_checks.py` green
- [ ] Greenfield Run produces multi-Group artifacts
- [ ] Component dispatch verified
- [ ] All four `project_kind` variants compile + dispatch successfully

---

## Phase A1c — Merge (2 days)

**Goal:** Eligibility-gated FIFO supports Groups + Components +
shared_paths.

### Steps

- [x] A1c.1 — Eligibility logic uses `Group.dependencies`
      (legacy `Group.deps` aliased; `shared_paths_set` helper retained
      for the in-progress shared_paths rule)
- [x] A1c.2 — Component dependencies threaded into eligibility ordering
      (Group<->Component cross-deps resolved via union of landed ids)
- [x] A1c.3 — Per-Component conflict repair (Components have agents
      like Groups) · `run_merge_queue` now iterates `eligible_candidates`
      (Groups) + `eligible_components` (Components) in one FIFO with a
      shared `landed_ids` / `blocked_ids` set. Components flow through
      the same `_process_candidate` path via a `_component_as_merge_slice`
      adapter that surfaces `Component.owned_paths` / `dependencies` /
      `checks` verbatim, so the existing conflict-repair flow re-invokes
      the build agent on the Component branch and is pinned to the
      Component's owned_paths through `BuildAgentInput.slice`. Tests:
      `tests/test_merge_component_repair.py` (5 tests).
- [x] A1c.4 — Stories module renamed: `otto/merge/stories.py` →
      `otto/merge/features.py` with backward-compat reader · The legacy
      import path remains as a shim that re-exports every symbol from
      `otto.merge.features` and emits a `DeprecationWarning` on import.
      Internal callers (`otto/merge/orchestrator.py`) now import from
      the new path.

### Exit criteria

- [ ] `pytest tests/test_merge.py` green
- [ ] Integration test: two Groups + one Component land in dep order
- [ ] Conflict repair on shared_paths works

---

## Phase A1.5-seed — Seed stage (1 day)

**Goal:** Apply pre-existing fixtures before Audit, for multi-user
products.

### Steps

- [x] New file: `otto/seed.py` — `seed_fixtures(spec, session_dir)`
- [x] Reads `Spec.audit_fixtures[]`, applies to live product
- [x] Idempotent on rerun
- [x] Failed seed = blocked Run, not silent proceed-with-empty-state
- [x] Per-fixture-kind handlers: user, channel, follow, data

### Exit criteria

- [x] `pytest tests/test_seed.py` green
- [ ] Integration: Run with `audit_fixtures` declares pre-existing test
      users before audit walks

---

## Phase A1 (LEGACY-FRAME — superseded by A1a/A1b/A1c above)

This section is preserved only for cross-reference with older plan
versions. Do not work from it; use the split sub-phases above.

### Steps

- [ ] A1.1 — `otto/spec.py`
  - [ ] `Spec`, `Feature`, `Group`, `Guardrail` dataclasses
  - [ ] `compile_spec(intent, project_kind, base=None) -> Spec`
  - [ ] `validate_spec(spec) -> ValidationResult`
  - [ ] `parse_spec_md(md_text, base=None) -> Spec | ParseError`
  - [ ] `render_spec_md(spec) -> str`
  - [ ] `apply_user_edits(spec, edits) -> Spec`
  - [ ] Round-trip property test: `parse_spec_md(render_spec_md(s)) == s`

- [ ] A1.2 — `otto/checks.py`
  - [ ] `Check` base + kinds
  - [ ] `run_check(check, project_dir, *, feature_id) -> Evidence`
  - [ ] Evidence carries `feature_id`

- [ ] A1.3 — `otto/state.py`
  - [ ] Append-only `state.jsonl` event log
  - [ ] `replay(session_dir) -> RunState`
  - [ ] All event kinds defined

- [ ] A1.4 — `otto/build.py`
  - [ ] `build_groups(spec, session_dir, *, defaults) -> BuildResult`
  - [ ] Per-Group worktree + branch + long-lived agent
  - [ ] Check loop bounded by `defaults.retries.check_loop`
  - [ ] `owned_paths` write-scope enforced at commit time

- [ ] A1.5 — `otto/merge.py`
  - [ ] Eligibility-gated FIFO merge queue per `(project, target_branch)`
  - [ ] Conflict repair in Group's worktree
  - [ ] Post-land verification

- [~] A1.6 — `otto/runner.py`
  - [✓] Top-level Run orchestrator: `run_pipeline(intent, project_dir,
        session_dir, *, project_kind, brownfield, base_url, config,
        build_agent, audit_agent, fix_agent, ...) -> RunResult` drives
        compile → seed → build → merge → audit → repair (Layer 2)
        → render. `RunResult` dataclass exposes per-phase results +
        wall_s/cost_usd/verdict/halted_reason. `cli_run.orchestrate_run`
        refactored to delegate the chain (compile stays inside the
        project lock, then `run_pipeline(spec=...)` drives the rest).
        9 new tests in `tests/test_runner.py`; all 25 existing
        `test_cli_run.py` tests still green.
  - [✓] Seed stage (`otto/seed.py`) + Layer 2 retry
        (`otto/audit_loop.py:repair_failing_features`) wired into the
        live pipeline for the first time. Seed failure halts pre-audit
        with `verdict=BLOCKED` (auditing a half-seeded product produces
        meaningless verdicts). Layer 2 fires only when the audit verdict
        is non-PASS AND a fix_agent is wired.
  - [ ] Budget enforcement (shared BuildBudget threaded across phases —
        already done; honest `cost_usd` aggregation in `RunResult`).
        TODO: dedicated wall-clock budget cap per phase.
  - [ ] Resume from `state.jsonl`. Design written 2026-05-04: `docs/i2p-resume-design.md` (reuse `paused` pointer + `spec_state.replay()`; add `otto/resume.py:plan_resume`; refuse spec-hash mismatch unless `--force`).
  - [✓] Layer 2 wired: `_make_layer2_fix_agent` now constructs a real
        `BuildAgentInput` with `feature_id=failing.feature_id` (new
        field) so `_build_agent_prompt` emits a "FIX ONLY THIS FEATURE"
        preamble naming the Feature and threading the audit detail.
        The bridge invokes the build agent, translates
        `BuildAgentOutput` → `RepairAttempt`, and turns agent crashes
        into honest `succeeded=False` repairs (no longer a no-op stub).
        4 new tests in `tests/test_runner_layer2_fix.py`.
  - [✓] `orchestrate_certify` / `orchestrate_improve` now delegate the
        audit → render chain to `runner.run_pipeline(brownfield=True,
        spec=spec)`. CLI plumbing (lock acquisition, brownfield compile
        with its own console heading, intent resolution, exit codes)
        stays in `cli_run.py` via two small helpers
        (`_brownfield_compile_locked`, `_drive_brownfield_pipeline`),
        but the audit-budget construction, fix_agent wiring,
        cost/wall-time aggregation, and render call all flow through
        `run_pipeline`. Both flows reuse `_print_run_result` for the
        post-run summary. Phase headings (`Audit phase`, `Render phase`,
        plus the certify/improve-specific overrides) are preserved via
        a small `_make_phase_callback` shim around `_PHASE_HEADINGS`.
        ~140 lines of duplicated inline-chain code removed; 34/34 tests
        pass in `tests/test_cli_run.py` + `tests/test_runner.py`.

- [ ] A1.7 — `otto/cli.py` `otto run` integration
  - [ ] Routes through new stack
  - [ ] CLI flags for budget/retry overrides

### Exit criteria (Loop 2 gate)

- [ ] All A1.* unit tests green
- [✓] Integration: `tests/integration/test_intent_to_proof.py` written
      (gap A4) — drives real `otto build --provider codex` against a
      tmp project; asserts spec.json shape (groups/project_kind/intent),
      no blocked groups, proof-packet.{html,json} on disk, audit verdict
      ∈ {passed, partial} (strict==passed for the happy path), and at
      least one screenshot artifact under `audit/` now that A8 is closed.
      Gated behind `OTTO_ALLOW_REAL_COST=1`, 15min wall budget. New
      `i2p-e2e` tier in `scripts/test_tiers.py` (gap A5) runs only this
      file. Collection verified: `uv run pytest
      tests/integration/test_intent_to_proof.py --collect-only -q` →
      1 test collected; default suite still skips it correctly.
- [ ] Resume test: kill mid-Build, resume, complete
- [ ] Greenfield Bench A passes for ≥1 fixture intent

---

## Phase A2 — Audit Feature-tagging

**Goal:** every walkthrough action tagged with `feature_ids[]`;
`feature-verdicts.json` correct.

### Steps

- [ ] A2.1 — `otto/audit.py`
  - [ ] `audit_run(spec, session_dir, *, scope=ALL_FEATURES) -> AuditResult`
  - [✓] Walkthrough Feature-tag coverage validator wired into
        `run_audit` via `_validate_walkthrough_jsonl(walk_log_dir, spec)`.
        Reads `walkthrough.jsonl`, parses each line via
        `parse_walkthrough_entry`, runs `validate_walkthrough_coverage`,
        attaches result to `AuditResult.walkthrough_coverage`. Below-threshold
        coverage logs a WARNING. 8 new tests · tick 58.
  - [✓] Coverage cap on verdict (deferred from tick 58): when
        `walkthrough_coverage["meets_threshold"]` is False, force the
        post-judge verdict to at least PARTIAL via `_strictest`
        (BLOCKED stays BLOCKED). Cap reason narrated under
        `[walkthrough coverage cap]` and recorded in new
        `AuditResult.verdict_cap_reasons: list[str]` for render
        surfacing. Honesty contract: audit cannot certify Features it
        did not observe. 5 new tests in
        `tests/test_audit_coverage_cap.py` cover the five cases
        (full/below/vacuous/no-jsonl/blocked-not-downgraded).
  - [✓] Parsed walkthrough entries plumbed through to
        `AuditResult.walkthrough_entries` (single read pass —
        `_validate_walkthrough_jsonl` now returns
        `(entries, coverage)`). `compose_proof_packet` reads the field
        and feeds it into `build_feature_proof_blocks`, so per-Feature
        proof blocks carry their walkthrough trace. 7 new tests in
        `tests/test_audit_walkthrough_entries.py` cover the helper
        signature, the field default, and the end-to-end render flow.
        Closes the A3.1 gap surfaced by the A3 sub-agent (tick 60).
  - [✓] Untagged actions outside "exploration" allowlist rejected by parser.
        `_validate_walkthrough_jsonl` now keeps permissive coverage stats but
        only strict entries enter proof evidence; strict parse errors cap
        `walkthrough_coverage.meets_threshold` to false.

- [~] A2.2 — `otto/audit_loop.py`
  - [✓] `repair_failing_features(...)` — Layer 2 loop (orchestration
        + caps wired in earlier tick).
  - [✓] Live build-agent dispatch: `runner._make_layer2_fix_agent`
        now adapts a `BuildAgentCallable` to the `FixAgentCallable`
        contract by constructing a `BuildAgentInput` with the new
        `feature_id` field set to the failing feature's id. The build
        prompt's Layer 2 preamble names the feature and surfaces the
        audit detail so the agent fixes only the failing feature, not
        the whole group. Tests: `tests/test_runner_layer2_fix.py`.
  - [✓] Re-audit narrows to affected Features. `runner.run_pipeline` passes
        the failing Feature ids into `run_audit(feature_scope_ids=...)`; the
        audit prompt names the scoped ids, and the returned FeatureAudit list
        is filtered by Feature id with display-name fallback for old judges.
  - [✓] Caps respected from `defaults`
        (`retries.audit_loop.max_repair_attempts_per_run`,
        `max_audit_passes_per_run`).

- [✓] A2.3 — Audit prompt rewrite — `otto/prompts/audit-feature-tagging.md`
        loaded by `_audit_prompt` and embedded into every audit-agent
        rendering. `default_audit_agent` inherits the contract via
        `_audit_prompt`. 5 new tests in
        tests/test_audit_prompt_feature_tagging.py pin the markers
        (feature_ids[], action_kind, ≥90%, per-kind examples,
        `feature_audits` wire-format).
  - [✓] Explicit Feature-tagging requirement (contract markdown reads
        "every walkthrough action carries `feature_ids: list[str]`")
  - [✓] Examples of tagged vs exploration actions (per-kind JSONL
        blocks: webapp, api, library, cli)
  - [✓] Failure-mode handling instructions (verdict honesty rules:
        zero walkthrough lines → `missing`; proxy-only / low-confidence
        flags; severity ladder)

### Exit criteria (Loop 2 gate)

- [ ] Walkthrough.jsonl tagging coverage ≥ 95% on real audit pass
      (greenfield fixture)
- [ ] Audit loop unit tests green
- [ ] Failing Feature → re-audit narrows → other Features unaffected
- [ ] Greenfield Bench A: all Features get verdicts; per-Feature
      evidence refs resolve

---

## Phase A3 — Render — per-Feature Proof

**Goal:** `proof-packet.html` + per-Feature pages render from any
session dir; deterministic; re-runnable.

### Steps

- [✓] A3.1 — `otto/render.py`
  - [✓] Pure deterministic function (`render_html` + `render_json` —
        no IO, no time)
  - [✓] Reads spec + audit + group logs + state (compose pulls
        FeatureAudit per Feature, maps name→id, builds blocks via
        `build_feature_proof_blocks`)
  - [✓] Writes whole-product packet with `<h2>Features</h2>` block
        (one `<section class="feature-proof">` per Feature, ordered
        by spec) — emitted before per-Group dispatch details for primacy
        (research §3 atomic units).
        8 new tests in `tests/test_render_per_feature.py` cover the
        section, JSON `features[]` array, multi-Feature cross-link,
        per-Feature finding filter, escape, and legacy-pass-through.
        19/19 render tests green. [✓] Walkthrough-entry plumbing closed
        in A2.1: `AuditResult.walkthrough_entries` flows from the
        single-pass `_validate_walkthrough_jsonl` tuple return into
        `build_feature_proof_blocks`, so per-Feature blocks now carry
        verdicts + findings + walkthrough trace.

- [✓] A3.2 — Templates · verified 2026-05-05
  - [✓] `otto/web/templates/proof-packet.html.j2`
  - [✓] `otto/web/templates/feature-proof.html.j2`
  - [✓] `render_html` and `feature_proof_block_to_html` load these
        repo-owned templates via dependency-free placeholder substitution;
        `tests/test_render.py::test_proof_templates_exist_and_are_used`
        verifies both files are used.

- [✓] A3.3 — `otto render <session-id>` CLI
  - [✓] Re-renders without LLM cost by loading `proof-packet.json` and
        regenerating `proof-packet.html`.
  - [✓] Idempotent by default: JSON is left untouched unless
        `--rewrite-json` is passed.

### Exit criteria (Loop 2 gate)

- [ ] Determinism test: render twice, byte-identical output
- [ ] Snapshot test: golden HTML for fixture spec passes
- [ ] All per-Feature pages contain ≥1 evidence ref
- [✓] Multi-Feature evidence cross-linked (no duplication) — verified by
      `test_multi_feature_walkthrough_entry_appears_in_each_feature`
- [✓] Old session dir (pre-A3) renders successfully — verified by
      `test_render_html_no_features_section_for_legacy_packet`

---

## Phase A4 — MC redesign

**Goal:** `otto web` shows new RunsView + RunDrawer; legacy panel
untouched.

### Steps

- [ ] A4.1 — `otto/mission_control/run_view.py`
  - [ ] `build_run_view(session_dir, *, live_state=None) -> RunView`
  - [ ] Pure function, reads Proof + state.jsonl

- [ ] A4.2 — Frontend `otto/web/client/src/components/run/`
  - [ ] `RunsView.tsx`, `RunDrawer.tsx`
  - [ ] `VerdictHeader`, `FeatureList`, `GroupList`, `StageTimeline`,
        `Guardrails`, `RunMetadata`
  - [ ] Primitives: `MetricChip`, `EvidenceLink`
  - [✓] Live polling — `useRunView` refetches every 3s while the run is
        in-flight (queued/compiling/awaiting_spec_review/building/auditing/
        rendering/landing) and stops at terminal status (passed/partial/
        blocked/landed/aborted/failed). Mid-poll fetch failures keep the
        last successful snapshot; cleanup clears the interval on unmount.
        Data flows through to RunDrawer + FeatureDrilldown via shared hook.

- [ ] A4.3 — Types `otto/web/client/src/types/run.ts`
  - [ ] `RunView`, `Feature`, `Group`, `Guardrail`, `Stage`, `EvidenceRef`

- [ ] A4.4 — Routing in `App.tsx`
  - [ ] New runs → new drawer
  - [ ] Legacy runs → legacy panel (unchanged)

### Exit criteria (Loop 2 gate)

- [ ] Backend tests pass
- [ ] Typecheck + build green
- [x] RUA pass: ≥3 fixture sessions through every screen with screenshots (docs/rua/2026-05-04-172101/, 16 screenshots — passed/partial/blocked, RunDrawer + FeatureDrilldown + SpecReviewPage edit/cancel/save/approve)
- [ ] Regression: legacy `otto build` runs render unchanged (snapshot)

---

## Phase A5 — Spec review screen + hybrid plan ownership [✓ APPROVED tick 37]

**Goal:** spec-review gate works through MC; user can edit, approve,
recompile.

### Steps

- [ ] A5.1 — Backend
  - [✓] `otto/web/spec_review_routes.py` — GET markdown, POST edit
        (parse_spec_md round-trip + version archive), POST approve
        (lifecycle flip, idempotent); install_spec_review_routes
        wired into otto/web/app.py · tick 35; 9 route tests green
  - [✓] State events: `spec.review.opened`, `spec.edited`, `spec.approved`
        wired into spec-review routes (with dedupe on opened + idempotent
        approve); `spec.regenerated` reserved for compile-agent
        recompile path (lands when spec_compile gains a regen entry
        point) · tick 36; 5 new event tests (14/14 spec-review total)

- [ ] A5.2 — Frontend
  - [✓] Skeleton: `useSpecMd` hook + `SpecReviewPage` (read-only +
        edit-toggle + Save/Approve stubs) + `?view=spec-review&spec=<id>`
        URL routing · tick 34. typecheck + build green.
  - [✓] Full markdown rendering of the spec body via `react-markdown`
        (^9.1.0, repo-root dep). Replaced the raw `<pre>` with a
        `.spec-markdown` container; default plugins only, no GFM /
        rehype add-ons (KISS). `.spec-markdown` styles in styles.css
        match surrounding page weight.
  - [✓] Spec history widget (wireframe A5.2): on-mount fetch of
        `/api/specs/<sid>/versions`, sticky right-column aside listing
        v1..vN with `?view=spec-diff&from=<v>&to=<latest>` links.
        Empty/single-version state shows "No prior versions yet".
        Re-fires after Save via `data.updated_at` dependency.
        typecheck + build green; `tests/test_spec_review_routes.py`
        19/19 still green. No React component tests (no vitest+RTL
        harness — documented gap).
  - [ ] `SpecReview.tsx` (Markdown view + Form view) — full styling
  - [ ] Add Feature modal with Otto-suggestion micro-compile
  - [✓] Spec diff view (vN → v(N+1)) — wireframe 4d.
        Backend: `GET /api/specs/<sid>/versions` lists archived
        `spec-v<N>.json` integers; `GET /api/specs/<sid>/diff?from=N&to=M`
        returns paired markdown + parsed JSON, 404 on missing version.
        Frontend: `SpecDiffPage` mounted at
        `?view=spec-diff&session=<sid>` with from/to dropdowns and an
        inline LCS line diff (no extra deps; reuses
        `.diff-pane`/`.diff-add`/`.diff-del`). 5 new backend tests
        (19/19 spec-review). typecheck + build green. Frontend
        component tests skipped — no vitest+RTL harness in repo
        (honest gap, route is keyboard/URL-accessible).

- [ ] A5.3 — Round-trip
  - [ ] Edit operations preserve Feature id stability
  - [ ] Spec versioned (`spec-v1.{md,json}`, `spec-v2.{md,json}`, ...)

### Exit criteria (Loop 2 gate)

- [ ] Pause Run at gate, edit via API, approve, Build proceeds with
      edited Spec — integration test
- [ ] Browser RUA: spec review flow against an in-flight pause
- [ ] Recompile preserves user-added Features when compatible

---

## Phase A6 — Brownfield compile mode [✓ APPROVED tick 46]

**Goal:** `otto run "add image upload"` against existing project
produces deltas only. Plus: `compile_spec(intent="", brownfield=True)`
on a no-prior-spec existing project emits a baseline AS-IS spec
that `otto certify`/`otto improve` (post-B.1/B.2 cutover) can drive
their audit/repair flows from.

### Design (built tick 40 — to be implemented tick 41+)

**Two modes conflated under "brownfield":**
1. **Brownfield-fresh** (no prior spec). Compile reads project tree;
   emits an AS-IS spec describing existing Features. Required by
   B.1/B.2 cutovers. Lower complexity.
2. **Brownfield-additive** (prior spec + intent). Compile reads tree
   + loads base spec; emits delta — new/changed Features, existing
   Groups carry forward. Higher complexity; matches research §9.4.

**API decision:** single `compile_spec(intent, project_dir, run_dir,
config, *, project_kind, brownfield: bool = False, base_spec: Spec |
None = None)`. `brownfield=False` is the existing greenfield path
(unchanged). `brownfield=True` switches to the brownfield prompt
template; `base_spec` is consulted for additive mode.

**Prompt template:** new `otto/prompts/compile-spec-brownfield.md`.
Content sketch:
- Project preamble: top-level dir tree (depth=2, max=200 entries),
  README.md first 200 lines, package.json/pyproject.toml manifest,
  prior `spec.json` summary if `base_spec` provided.
- Instructions: agent uses Read/Glob/Grep to dive deeper. Emits
  Features that REFLECT the project (not the intent). Intent is a
  scope hint, not a derivation source.
- Diff rule: if `base_spec` provided, only emit new + changed
  Features. Carry forward existing Groups verbatim by id.
- "Leave it alone" markers: deferred to tick 43 (out of A6.1 scope).

**Project preamble generator** (Python helper, not LLM):
- New helper `otto.spec_compile.build_project_preamble(project_dir)
  -> str`. Reads file tree (using `git ls-files` for tracked files
  only, falling back to `Path.glob("**")` capped at 200 entries),
  reads README.md / pyproject.toml / package.json / Cargo.toml /
  go.mod (whichever exist, capped at 200 lines each), formats as
  fenced-code preamble for the prompt.
- Determines project_kind heuristically if not specified:
  presence of pyproject.toml → library/cli/api candidate; package.json
  → webapp candidate; tests/ folder + cli entry_point → cli; etc.
  Final decision still surfaced to user via spec-review gate.

**Out-of-scope detection** (research §9.5b): if intent text contains
"browser", "kernel", "compiler", "OS-level" etc., emit a warning
before LLM cost. v1: simple keyword match. v2: LLM-based classifier.

### Steps

- [✓] A6.1 — `build_project_preamble(project_dir) -> str` helper
      (file tree + README + first manifest, capped via
      `BROWNFIELD_PREAMBLE_MAX_FILES=200` / `MAX_LINES_PER_FILE=200`
      in defaults.py). git-tracked when available, glob fallback,
      common-ignore filter; deterministic. 11 new tests pass · tick 41
- [✓] A6.2 — `otto/prompts/compile-spec-brownfield.md` prompt template
      with `{project_preamble}` interpolation; greenfield prompt
      unchanged. Anti-derivation rules ("read; don't invent"; intent
      is scope hint not source); empty-project bootstrap branch;
      per-Feature evidence_kinds guidance per project_kind. 2 new
      render_prompt tests pass · tick 42 (13/13 brownfield total)
- [✓] A6.3 — `compile_spec(brownfield=True, base_spec=None)` wires
      preamble + brownfield prompt + same parsing pipeline as greenfield;
      base_spec emits a warning + is ignored until A6.4. Greenfield
      path entirely unchanged. 4 new tests pass · tick 43
- [✓] A6.4 — Additive mode (`_reconcile_brownfield(new_spec, base_spec)`):
      Group ids carry forward (warning on title conflict); Feature
      audit/coverage state preserved on matching ids; Components
      union-by-id; Guardrails union-deduped by text; intent +
      intent_hash from base; mechanical/historical fields (structure,
      shared_paths, non_goals, done_means, amendments, audit_fixtures,
      cross_group_checks, shared_scaffold) preserved from base.
      4 new reconciliation tests (8/8 brownfield-compile total) · tick 44
- [✓] A6.5 — Out-of-scope keyword guard before LLM call (research
      §9.5b). `OUT_OF_SCOPE_KEYWORDS` (13 multi-token phrases) +
      `detect_out_of_scope_intent(intent)` helper + `compile_spec`
      pre-LLM check raising `SpecValidationError`. User override via
      literal `override-scope` token in intent (proof packet will
      mark verdict suggestive). Greenfield + brownfield share the
      check. 22 new tests · tick 45 (43/43 A6 total)
- [ ] A6.6 — File-level "preserve" markers (mechanism TBD; deferred
      until A6.4 lands; likely `.otto/preserve` file pattern).
- [✓] A6.7 — Integration test (`tests/integration/test_brownfield_compile_real.py`)
      builds a realistic CLI fixture in tmp_path, runs full
      compile_spec(brownfield=True) plumbing (preamble + prompt +
      parsing + reconciliation) with stubbed agent. Empty-base + base
      additive paths both verified. 2 new tests · tick 46
      (45/45 A6 total)

### Exit criteria

- [ ] `otto run --brownfield` (or auto-detect via project state)
      compiles against an existing project and emits a Spec with
      ≥1 Feature reflecting actual project state.
- [ ] Bench C (brownfield add-feature) passes — measures delta
      mode (additive) against a fixture project + intent. Defer until
      A6.1-A6.5 land.
- [ ] B.1 (`otto certify`) and B.2 (`otto improve`) cutovers unblock.

---

## Phase B — Cutover

**Goal:** legacy CLI commands route through new stack.

### Cutover catalog (built tick 38; entry point → new-stack equivalent)

| Legacy entry point          | Current backing                                            | New-stack equivalent                                                       | Cutover notes                                                                                                                |
|-----------------------------|------------------------------------------------------------|----------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `otto build`                | `otto.pipeline.build_agentic_v3` + `run_certify_fix_loop`  | `compile_spec` → `build.run_build` → `merge_queue` → `audit_loop` → `render` (same chain as `otto run`) | Largest blast radius. Wraps spec gate, build, certify, fix in one loop. Cut last.                              |
| `otto certify`              | `otto.certifier.run_agentic_certifier`                     | `audit_loop` + `render`                                                    | Read-mostly (no merge queue). Smallest blast radius; cut FIRST as a smoke proof.                                              |
| `otto improve`              | `otto.cli_improve` → certify+build feedback loop           | `audit_loop` (multi-round) + `build.run_build` for fixes                   | Mid blast radius. Logically a long-running loop of certify→build→certify; can wrap the same pieces.                           |
| `otto run`                  | `otto.cli_run` (new stack — already routes correctly)      | n/a — already on new stack ✓                                               | Reference implementation; the others should converge here.                                                                    |
| `otto history` / `pow` / `setup` / `cleanup` / `merge` / `queue` / `dashboard` / `web` | `otto.cli_logs`, `cli_pow`, `cli_setup`, `cli_cleanup`, `cli_merge`, `cli_queue`, `cli.dashboard`, `cli.web_command` | No cutover required — utility commands; `web` mounts both `/api/runs` (legacy) and `/api/run-view` (new) | Read-only or auxiliary; defer to Phase C deletion of any legacy view.    |
| `/api/runs/<id>/...` artifact routes | legacy MC inspector            | `/api/run-view/<id>` (RunView JSON) + `/api/specs/<id>/markdown` (SpecMdView) | Both currently coexist. Phase B leaves both; Phase C deletes the legacy `/api/runs/*` body once MC default switches over.    |

Cutover order — REVISED tick 39 after design-gap discovery:

**Critical finding (tick 39):** `run_audit(spec, ..., build_result, merge_result, ...)` REQUIRES populated `BuildResult` and `MergeQueueResult` from the new-stack chain. Legacy `otto certify` runs without these (no spec, no build phase). Direct cutover would require either:
(a) synthesizing fake BuildResult/MergeQueueResult — bandaid, violates anti-slop rule, OR
(b) brownfield-compiling a spec on the fly + running a no-op build → A6 dependency.

This means **B.1 (`otto certify`) and B.2 (`otto improve`) BLOCK on A6 (brownfield compile)** because they're called on projects without an existing spec/build cycle. `otto build` does NOT block — it always compiles a spec first, which is exactly what `otto run` already does end-to-end.

**Revised order:**
1. **B.0 — opt-in `otto build --i2p`** (new step): add a flag that routes `otto build` through the existing `otto.cli_run.run_command` body without removing the legacy v3 stack. Lets users dogfood the new chain on real projects without breaking anyone. Smallest-possible safe move.
2. **B.3 — flip default** to the new stack once bench evidence + dogfood shows feature parity. Add a `--legacy` escape hatch for one cycle. Removes `otto.pipeline.build_agentic_v3` callers.
3. **A6 — brownfield compile** (was deferred): implement `compile_spec(intent, *, project_dir, brownfield=True)` that reads the existing project to seed groups + features. Required by B.1/B.2.
4. **B.1 — `otto certify` cutover** (after A6): brownfield-compile a spec, run `audit_loop + render`. Becomes "audit a project that already exists".
5. **B.2 — `otto improve` cutover** (after A6 + B.1): wrap multi-round `audit_loop` with the `cli_improve` retry pattern.
6. **B.4 — DeprecationWarnings** on any legacy module functions slated for Phase C deletion.

### Steps

- [✓] B.0 — `otto build --i2p` opt-in flag routes through new stack
      via extracted `otto.cli_run.orchestrate_run`; legacy default
      untouched · tick 39; 4 new tests (16/16 cli_run + cli_smoke pass)
- [✓] B.3 (PREP) — `default_pipeline` config field added (default still
      `"legacy"`); `--legacy` flag added to `otto build`/`certify`/
      `improve bugs`; `resolve_pipeline_choice(i2p, legacy, project_dir)`
      helper centralizes dispatch logic. Mutual-exclusion with `--i2p`
      raises `click.UsageError`. Actual default flip awaits bench
      validation. 9 new tests · tick 50 (38/38 cli + deprecation +
      pipeline-choice total).
- [✓] B.1 — `otto certify --i2p` routes through brownfield-compile +
      `run_audit` (with placeholder BuildResult/MergeQueueResult, no
      build phase, fix_agent=None) + `render_run`. Legacy default
      unchanged. `orchestrate_certify` extracted as reusable helper
      in cli_run.py. 4 new tests · tick 47 (20/20 cli total).
      Follow-up tick: `orchestrate_certify` now delegates the audit
      + render chain to `runner.run_pipeline(brownfield=True,
      fix_agent=None, spec=...)`; only the brownfield-compile (which
      lives inside the project lock and prints its own heading) plus
      CLI plumbing stay in cli_run.py.
- [✓] B.2 — `otto improve bugs --i2p` routes through brownfield-compile +
      `run_audit` (with `fix_agent=default_build_agent` enabling repair
      loop, `--rounds` mapped to `AuditBudget.audit_retries`) + `render_run`.
      Legacy default unchanged. `orchestrate_improve` extracted in
      cli_run.py. 4 new tests · tick 48; `feature` and `target`
      subcommands wired with the same pattern · tick 51. 6 additional
      tests (43/43 cli + deprecation + pipeline-choice total).
      Follow-up tick: `orchestrate_improve` now delegates the audit
      + render chain to `runner.run_pipeline(brownfield=True,
      fix_agent=default_build_agent, audit_budget=..., spec=...)`,
      sharing `_brownfield_compile_locked` / `_drive_brownfield_pipeline`
      with `orchestrate_certify`. The duplicated inline chain (run_audit
      placeholder BuildResult/MergeQueueResult + render_run) is gone.
- [✓] B.4 — DeprecationWarnings on legacy paths slated for Phase C
      deletion. `build_agentic_v3` and `run_agentic_certifier` emit
      `DeprecationWarning` on each call (not at module import) naming
      Phase C and the `--i2p` migration path. 3 new tests · tick 49.
      NOTE: `_run_improve` not warned (private helper; user-facing
      surface is `otto improve bugs --i2p` which already warns about
      ignored flags).

### Exit criteria

- [ ] Bench B (Microfeed parity) passes (criteria in research §12.7)
- [ ] All four legacy commands route through new stack with passing fixture tests
- [ ] Legacy `otto.pipeline.build_agentic_v3` and `otto.certifier.run_agentic_certifier` reachable only through deprecation shim

---

## Phase C — Deletion

> Audit: see [`docs/phase-c-deletion-audit.md`](docs/phase-c-deletion-audit.md)
> for module line counts, caller surface, MC route enumeration, and the
> proposed deletion order. Re-run the audit before landing the deletion
> PR — line counts shift as Phase B work continues.

**Goal:** delete every module/component listed in research §13.

### Steps

- [ ] C.1 — Delete legacy backend modules
  - [✓] C.1a — Gut `otto/cli_improve.py` legacy bodies (`_run_improve`,
        `_run_improve_locked`, `_apply_improver_agent_aliases`,
        `_exit_for_lock_busy`, `_create_improve_branch`,
        `_resolve_improve_certifier_mode`,
        `_resolve_feature_certifier_mode`). The click subcommands stay;
        `--legacy` is now a hard error pointing at `--i2p`. Tick 63.
        Removed test files: `test_improvement_report_splits_pass_warn_fail.py`,
        `test_improve_writes_build_journal_single_round.py`,
        `test_improve_phase_writes_to_improve_dir.py`. Legacy improve
        hardening tests in `test_hardening.py` deleted in place.
  - [✓] C.1b — Gut `otto/certifier/__init__.py` (tick 64). Reduced from
        **4,456 lines → ~80 lines** (pure shim re-exporting `contracts.py`
        + `report.py`, plus a hard-error stub for `run_agentic_certifier`
        so legacy lazy imports in `otto/pipeline.py` and
        `otto/merge/orchestrator.py` (Phase C.3 deletion targets) fail
        loudly instead of with `ImportError`).
        `otto/certifier/contracts.py` (292 lines) and
        `otto/certifier/report.py` (40 lines) **kept** — referenced by
        `otto/merge/orchestrator.py` (slated for C.3) and
        `tests/test_merge_orchestrator.py` / `tests/test_hardening.py`.
        The new stack (`otto/audit.py`, `otto/render.py`,
        `otto/audit_loop.py`) does not import them.
        `otto/cli.py::_certify_locked` deleted (-229 lines);
        `otto certify --legacy` now hard-errors via
        `_exit_legacy_certify_removed` (sibling to
        `_exit_legacy_build_removed` from C.3).
        `tests/test_certifier_stories.py` deleted (-1,839 lines);
        `tests/test_proof_provenance.py`'s certifier-coupled
        `test_visual_evidence_manifest_written_at_capture` removed
        (-76 lines). `tests/test_legacy_deprecation.py` updated:
        the run_agentic_certifier deprecation test became a
        hard-error assertion (call → `RuntimeError` naming Phase C.2).
  - [✓] C.1b cleanup — Phase C cleanup pass (tick 64 follow-up). Pruned
        the 23 orphaned tests that exercised legacy certifier internals
        deleted in W8-A:
        `TestProofOfWorkRendering` (13 tests, -525 lines —
        `_build_pow_report_data` / `_render_pow_html` /
        `_render_pow_markdown` / `_write_pow_report` / `_intent_excerpt`),
        `TestSpecTimeoutTolerance` (3 tests, -64 lines —
        `run_agentic_certifier`),
        `TestCertifyPassesConfig` (2 tests, -65 lines —
        `run_agentic_certifier`),
        `TestCertifierStoryDedup` (2 tests, -67 lines —
        `run_agentic_certifier`),
        and the two `test_standalone_certifier_target_*` standalone
        functions (-62 lines — `run_agentic_certifier`) in
        `tests/test_hardening.py`; plus `TestStandaloneCertifierPrompt`
        (3 tests, -63 lines — `_render_certifier_prompt`) in
        `tests/test_spec.py`. Cross-cutting parser/marker/resume tests
        retained.
        `tests/test_cli_run.py::test_certify_without_i2p_uses_legacy_path`
        rewritten as `test_certify_without_i2p_hard_errors_after_phase_c2`
        (mirrors the build-side `_after_phase_c3` pattern from C.1c).
        Targeted suite: 135 passed.
  - [✓] C.1c — Gut `otto/pipeline.py` (Phase C.3, this tick). Reduced
        from **2,875 lines → 61 lines** (thin shim that re-exports
        shared lifecycle helpers from the new `otto/runs/lifecycle.py`
        module and hard-errors on access to `build_agentic_v3`,
        `run_certify_fix_loop`, `BuildResult`, `InfraFailureError`).
        Shared run-lifecycle helpers (process cleanup, atomic
        publisher / heartbeat / cancel ack, session summary writer,
        terminal history append, runtime metadata) moved to
        `otto/runs/lifecycle.py` (~600 lines) — not deleted, since
        `otto/agent.py`, `otto/cli.py:_run_spec_phase`, and the test
        suite still need them. `_build_locked` body (-654 lines) gutted
        in `otto/cli.py`; `--legacy` route now exits via
        `_exit_legacy_build_removed`. Removed tests:
        `tests/test_v3_pipeline.py` (-1,354 lines, deleted),
        `tests/test_build_fallback_to_intent_md.py` (-85 lines,
        deleted), 18 v3-only classes/functions in
        `tests/test_hardening.py` (-2,732 lines pruned in place; file
        is now 1,953 lines vs 4,685). Re-pointed
        `tests/test_run_history.py`, `tests/test_token_usage_phase_logs.py`,
        `tests/test_agent.py` to import the shared helpers from
        `otto.runs.lifecycle`. `tests/test_legacy_deprecation.py`
        already updated by W8-A to assert the hard-error contract.
        Final pipeline.py shim deletion tracked separately — keeps
        existing while `otto/merge/orchestrator.py`'s lazy import of
        `run_agentic_certifier` survives.
  - [x] C.1d — Delete `otto/spec.py` (legacy markdown spec gate).
        Inlined the small `write_spec_review_decision` helper into
        `otto/mission_control/actions.py` as `_write_spec_review_decision`
        (single consumer, ~12 LOC), then deleted `otto/spec.py` and the
        legacy `tests/test_spec.py`. Sidecar `review-decision.json` is
        currently informational — no consumer reads it; queue resume
        works off the checkpoint. New web spec-review flow lives in
        `otto/web/spec_review_routes.py` on top of `spec_compile`.
        Verified: `grep -rn 'from otto\.spec\b'` returns zero matches.
        `tests/test_mission_control_actions.py` + spec test sweep
        (138 tests) green; broad sweep (1652 tests) green modulo a
        pre-existing test-ordering flake unrelated to this change.
  - [✓] C.1f — Phase C cleanup pass (W8-B follow-up, tick 66).
        Removed orphaned references left by C.3:
        (1) Deleted `otto/cli.py:_run_spec_phase` (-450 LOC) — its only
            caller was the gutted `_build_locked` stub.
        (2) Deleted `tests/integration/test_resume_flow.py` (-47 LOC)
            — single test depended on `from otto.pipeline import
            build_agentic_v3` (now hard-error stub).
        (3) Surgically removed
            `tests/integration/test_build_flow.py::test_build_agentic_v3_dedupes_repeated_certify_round_markers`
            and pruned the resulting unused imports; the canonical+mirror
            manifest test in the same file is unchanged.
        (4) Annotated the lazy `from otto.certifier import
            run_agentic_certifier` import at
            `otto/merge/orchestrator.py:1730` with a 10-line comment
            explaining the C.2/C.3 status and why a substitution into
            `otto.audit.run_audit` is a structural rewrite rather than a
            cleanup. The orchestrator stays reachable via `otto/cli_merge.py`;
            tests in `tests/test_merge_orchestrator.py` exercise the path
            through monkeypatched stubs.

            **Post-compact finding (W10-D, this tick):** Re-audited the
            reachability claim. The lazy import is **NOT dead** — call
            chain confirmed: `otto/cli.py:1289 register_merge_command`
            → `otto/cli_merge.py:206 await run_merge(...)` →
            `otto.merge.orchestrator.run_merge` (line ~1080-1100) →
            `_run_post_merge_verification` (line 1654) → unconditional
            `await run_agentic_certifier(...)` at line 1763 (unless
            `--no-certify` set). **Real `otto merge` invocations
            without `--no-certify` will fail with the Phase C.2
            RuntimeError stub.** Tests pass only because they
            monkeypatch `otto.certifier.run_agentic_certifier`,
            masking the production hard-error.

            **Decision required (user input):** (a) delete `otto merge`
            CLI + orchestrator entirely (Phase C.3 expansion); (b) wrap
            `_run_post_merge_verification` in a `--no-certify`
            short-circuit at `run_merge` level so the orchestrator
            never reaches the stub; or (c) do the structural rewrite
            to call `otto.audit.run_audit` (incompatible call shapes
            — `intent + stories + merge_context` vs `Spec + BuildResult
            + MergeQueueResult`). i2p stack is unaffected — uses
            `otto/merge_queue.py`, not the legacy orchestrator.

            **Resolution (W10-E, this tick):** Option (a) executed in
            C.1g below — the lazy import (and everything around it) is
            now deleted, not annotated.
        Verification: `uv run pytest tests/test_merge_orchestrator.py
        -q` → 49 passed; `tests/integration/test_build_flow.py` → 1
        passed (the canonical+mirror manifest test).
  - [✓] C.1f cleanup — Dead helper sweep (this tick). Deleted the
        residual `write_test_pow_report` helper in `tests/_helpers.py`
        (-72 lines) — its body imported `_build_pow_report_data` and
        `_write_pow_report` from `otto.certifier`, both removed in
        Phase C.2 (tick 64), so the helper could no longer execute.
        Grep confirmed zero callers across `otto/` and `tests/` (only
        historical comments in `otto/certifier/__init__.py:36` and
        `tests/test_hardening.py:576-577` reference the deleted
        symbols by name). Also dropped the now-unused `Any` import.
        Verification: `uv run pytest tests/ -q --ignore=tests/integration
        --ignore=tests/browser --tb=no` → 1700 passed.
  - [✓] C.1g — Delete legacy `otto merge` CLI + orchestrator
        (W10-E, this tick). Option (a) of the C.1f decision tree.
        i2p stack uses `otto/merge_queue.py`; the legacy multi-mode
        merge orchestrator was reachable only through `otto merge`,
        and any real invocation without `--no-certify` would crash
        on the C.2 `run_agentic_certifier` stub.
        Production deletions:
        (1) `otto/merge/orchestrator.py` (-2382 LOC)
        (2) `otto/cli_merge.py` (-359 LOC)
        (3) `otto/merge/conflict_agent.py` (-312 LOC,
            production callers: only the orchestrator)
        (4) `otto/merge/edit_scope.py` (-230 LOC,
            production callers: only conflict_agent)
        (5) `otto/merge/stories.py` (-30 LOC, deprecated re-export
            shim used only by the orchestrator)
        (6) `otto/cli.py` — dropped the `register_merge_command`
            registration block (-3 LOC)
        Helpers preserved (in active use by i2p stack /
        mission_control / test suite): `otto/merge/git_ops.py`,
        `otto/merge/state.py`, `otto/merge/features.py`,
        `otto/merge/verification.py`, `otto/merge/__init__.py`.
        Test deletions (1996 + 260 + 46 + 267 + 109 + 127 + 77 +
        142 = 3024 LOC, 8 files):
        `tests/test_merge_orchestrator.py`,
        `tests/test_cli_merge.py`,
        `tests/test_registry_gc.py`,
        `tests/test_merge_conflict_agent.py`,
        `tests/merge/test_conflict_agent_scope.py`,
        `tests/merge/test_edit_scope.py`,
        `tests/integration/test_merge_flow.py`,
        `tests/integration/test_conflict_scope_flow.py`.
        Test rewrites (3 files):
        `tests/test_cli_smoke.py` — `merge --help` test inverted
        to assert `No such command 'merge'` (Click exits non-zero).
        `tests/test_run_history.py` — dropped the
        `_append_merge_history` import + the merge slice of the
        terminal-history multi-domain test (other 4 domains
        retained).
        `tests/conftest.py` — removed `tests/test_merge_orchestrator.py`
        from `HEAVY_TEST_FILES`.
        Doc/comment updates: `otto/certifier/__init__.py` and
        `tests/test_legacy_deprecation.py` no longer claim a lazy
        import lives in `otto/merge/orchestrator.py` (the file is
        gone). The C.2 hard-error stub stays for `otto/pipeline.py`'s
        lazy import (still alive).
        **Total: -3334 production LOC + -3024 test LOC = -6358 LOC.**
        Verification: full unit sweep
        `uv run pytest tests/ -q --ignore=tests/integration
        --ignore=tests/browser --tb=short -x` → **1568 passed** in
        128s; `uv run otto --help | grep -i merge` → exit 1 (no
        match, command removed).
  - [✓] C.1e — Delete legacy `/api/runs/<run_id>/...` MC routes from
        `otto/web/app.py` (audit-doc step 4). Tick 65. 11 GET/POST routes
        removed: `run_detail`, `run_logs`, `run_artifacts`,
        `run_artifact_content`, `run_artifact_raw`, `run_proof_report`,
        `run_proof_asset`, `run_legacy_proof_evidence_asset`,
        `run_legacy_proof_file`, `run_diff`, `run_action`. Frontend
        default switched simultaneously: `main.tsx` now renders
        `<RunListLanding/>` (lists sessions from `/api/run-view`) when
        no `?view=` param is set; legacy `App.tsx` deleted (1731 lines).
        New component: `otto/web/client/src/components/run/RunListLanding.tsx`.
        Removed test files: `test_web_landing.py`,
        `test_web_mission_control.py`, `test_web_review_packet.py`
        (55 legacy tests). Surgically dropped 1 test in
        `test_web_events_history.py` and 1 legacy `/api/runs/<id>` call
        in `test_web_queue_actions.py`. Targeted suite green: 120
        tests across `tests/test_run_view*.py`,
        `tests/test_spec_review_routes.py`, and `tests/test_web_*.py`.
- [ ] C.2 — Delete legacy frontend components
- [ ] C.3 — Remove `domain` field from `HistoryRow`
- [ ] C.4 — Single-PR diff with full test suite + RUA pass

### Exit criteria

- [ ] All Layer-1 to Layer-5 evaluations from research §12 green
- [ ] Sign-off criteria from research §4.8 met

---

## Drift counters (loop-1 maintained)

Updated by drift sentinel each tick. If any goes nonzero outside the
permitted allowlist, drift sentinel halts and writes to `drift-log.md`.

| Counter | Target | Last value | Last checked |
|---|---|---|---|
| Retired-vocab hits | 0 (post-A0) | 1669 in otto/ (down from 1672; A0 active) | 2026-05-04T18:13Z |
| Magic-number hits outside defaults.py | 23 (legacy floor) | 23 (unchanged; transport timeouts in Phase-C-doomed modules) | 2026-05-04T18:08Z |
| Out-of-scope file edits in current phase | 0 | 0 | 2026-05-04T17:55Z |
| Failing unit tests | 0 | (not run this tick) | (never) |
| Frontend typecheck errors | 0 | (not run this tick) | (never) |

---

## Cost & wall tracking (informational only — no caps)

User has explicitly removed cost constraints. Track spend per phase
for retrospective only. No phase blocks on cost.

| Phase | Spent so far | Wall (real time) |
|---|---|---|
| A0 | (n/a) | (n/a) |
| A1a | (n/a) | (n/a) |
| A1.5-types | (n/a) | (n/a) |
| A1b | (n/a) | (n/a) |
| A1c | (n/a) | (n/a) |
| A1.5-seed | (n/a) | (n/a) |
| A2 | (n/a) | (n/a) |
| A3 | (n/a) | (n/a) |
| A4 | (n/a) | (n/a) |
| A5 | (n/a) | (n/a) |
| A6 | (n/a) | (n/a) |
| B | (n/a) | (n/a) |
| C | (n/a) | (n/a) |
