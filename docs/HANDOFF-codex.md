# Otto i2p redesign — handoff to Codex

Branch: `cc-i2p-2`. Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`. Base: `main`.

This document is for Codex picking up the work after Claude finished
the architectural redesign. Read it first.

## What's done

The full architectural redesign of the Otto pipeline shipped over ~70+
ticks, captured in `progress.md`. High-level phases:

- **Phase A0–A6**: vocabulary refactor, dataclasses, build/checks/state,
  merge, audit, render, Mission Control, spec review, brownfield compile.
- **Phase B**: `default_pipeline: i2p` flipped (`otto/config.py`). Real
  Bench A microfeed validated end-to-end ($4.44, audit verdict=passed,
  4/4 slices, evaluator_aggregate=passed).
- **Phase C** (legacy deletion): `otto/spec.py`, `otto/certifier/`,
  `otto/pipeline.py`, `otto/cli_merge.py`, `otto/merge/orchestrator.py`,
  `conflict_agent.py`, `edit_scope.py`, `stories.py` — all removed
  (~13,710 LOC prod + ~4,200 LOC tests). The `otto merge` CLI is gone;
  i2p uses `otto/merge_queue.py`.
- **Phase A0.4**: `capability_verdicts` JSON wire-key dropped — the
  canonical key is `feature_audits`.
- **`--resume`** implemented end-to-end: `otto/resume.py` (~340 LOC,
  ResumePlan + plan_resume + verify_spec_hash_matches + mid-merge
  recovery), wired into `runner.run_pipeline` via `resume_plan` kwarg.
  CLI flags: `--resume`, `--reset-budget`, `--force` on
  `otto build` / `otto certify`.
- **Mission Control redesign**: RunListLanding, RunDrawer (live polling
  via `useRunView.ts`), FeatureDrilldown, SpecReviewPage (react-markdown
  body + version history sidebar), SpecDiffPage (Wireframe 4d, inline
  LCS diff). Legacy `App.tsx` (-1,731 LOC) removed. typecheck+build
  green. 217.54 kB JS / 67.12 kB gzip.
- **Smoke 2** (real otto build through full i2p pipeline): clean,
  9m20s / $1.20. Audit correctly caught a real merge bug
  (`vite: command not found`) and produced verdict=blocked with
  per-Feature evidence + browser screenshots. See
  `docs/i2p-smoke-2-20260504-202757.md`.

**Cumulative diff vs main**: ~117 files changed,
**-24,050 net LOC** (~31,718 deletions, ~7,668 insertions).
Test sweep: **1585 passed, 0 failures**.

The architectural delivery is complete. What's left is gates +
hardening + bug hunt.

## Where things live

Every detail you might want is captured in repository docs. Read these
in order:

1. **`docs/intent-to-product-design.md`** — the architectural design
   doc that drove the redesign (4 stages × 1 artifact × 3 roles model).
2. **`progress.md`** — checkbox list of every step in every phase, with
   verification timestamps. Single source of truth for "what was done".
3. **`research.md`** — pre-redesign analysis of the two prior branches
   (codex-feats, codex-i2p) and how they collapse into one model.
4. **`plan.md`** — the 12-step implementation plan that drove the work.
5. **`docs/i2p-resume-design.md`** — `--resume` design rationale (438
   LOC). Includes the 3 user-decided open questions and the v1 answers:
   (a) cost-carry with `--reset-budget` escape, (b) `otto certify
   --resume` supported, (c) refuse-on-spec-hash-mismatch with `--force`.
6. **`docs/phase-b-summary.md`** + **`docs/phase-c-deletion-audit.md`**
   — Phase B/C cutover and deletion order rationale.
7. **`docs/i2p-smoke-2-20260504-202757.md`** — most recent end-to-end
   smoke run report; 3 honest anomalies surfaced and fixed.
8. **`docs/otto-wireframes.md`** — Mission Control redesign UI/UX
   intent, including the wireframes 4a–4d that the frontend
   implements.
9. **`docs/anti-drift-loops.md`**, **`docs/autonomous-loop.md`** —
   harness doctrine that drove the autonomous /loop-based delivery.
10. **`docs/rua/2026-05-04-172101/`** — Real-User-Audit pass evidence
    (16 chrome-devtools screenshots through every screen).
11. **`drift-log.md`**, **`review.md`**, **`loop-report.md`** — the
    drift sentinel + checkpoint review trail.

## Ground truth — repo state

- Branch `cc-i2p-2` is ahead of `main` by ~73 commits + 2 redesign
  commits (this commit train).
- Pre-existing CLAUDE.md "per-session layout" table is now accurate:
  `summary.json` IS emitted again (regression caught + fixed in the
  smoke 2 follow-ups).
- `bench-results/microfeed-i2p-20260505-001720/result.json` records
  the Bench A pass.
- The 3 user-approved decisions on `--resume`, `merge orchestrator`,
  and `capability_verdicts` removal are now baked in — they were the
  only architectural gates open at hand-off.

## Honest gap list — read this!

After the redesign was claimed "100% delivered", 5 parallel cross-check
audits surfaced **~15 material + ~13 cosmetic gaps**.
**A round-2 fix wave then closed most of them.** Current state:

- ✅ **Gaps fixed in code**: A1, A2, A3, A4, A5, A6, A7, A8, A9, A10,
  A11, A12, A13, A14, A15, B1, B3, B5, B6, B9, B12
- ✅ **Remaining items documented honestly with rationale**: B2, B4, B7,
  B8, B10, B11, B13
- ✅ **Former cosmetic deferrals closed**: B3 (.thumbs CSS grid) and
  B5 (combined landed+blocked render lifecycle fixture).

See **`docs/codex-followups.md`** for the full punch list with
file:line evidence + status notes.

Round-2 highlights (what changed since the first hand-off):

- **Slice→Group rename completed** in spec_compile, spec_state, build,
  audit, render, merge_queue, runner, resume, cli_run, spec_amend,
  spec_warnings, web/i2p_routes, frontend types, plus all tests. Parser
  keeps a one-cycle deprecation read fallback for legacy `"slices"` /
  `"cross_slice_checks"` JSON keys. Branch prefix `i2p/<id>` left
  opaque per A1 §6.
- **Group field renames**: `tasks → feature_ids`, `title → name`,
  `deps → dependencies`. Added optional `dispatch_plan` field with
  honest deferral docstring.
- **CheckKind executors**: `_run_cli_probe`, `_run_import_check`,
  `_run_type_check` wired into `run_check`. mypy/pyright/basedpyright
  via PATH; honest `tool_available=False` when absent.
- **Real-Codex E2E test** at `tests/integration/test_intent_to_proof.py`,
  gated by `OTTO_ALLOW_REAL_COST=1`, exposed as the `i2p-e2e` tier.
- **A6 mid-build edit invalidation**: new `compute_invalidation` diff,
  `editing_in_flight` lifecycle, `group.invalidated_by_spec_edit`
  events, runner re-dispatches invalidated Groups. Design at
  `docs/i2p-spec-edit-design.md`.
- **A7 pause + abort-a-Group verbs**: poll-flag-based pause/resume,
  per-Group abort handler + journal events + REST routes + frontend
  buttons.
- **A13 review gate**: opt-in `--review-gate` between compile and build
  with `--gate-timeout` (default 24h); `spec.review_pending` /
  `spec.review_approved` events.
- **A14 bench parity ladder**: emits `i2p_partial_wall_exceeded`
  instead of silently passing; per-criterion decomposition in
  `result.json`.
- **A15 CLAUDE.md** lists `otto run` and the proof-packet/spec-state
  artifacts.

**Test sweep: 1640/1640 passed.** Web typecheck + build clean.

### Round-3 fix wave (5 backend gaps, 2026-05-05)

Round-3 cross-check audits surfaced 5 narrow backend gaps below the
A-list. All closed in this wave (full table in `docs/codex-followups.md`):

- **R3-1 phase-map completeness**: `group.aborted_by_user → BLOCKED`
  added to `_PHASE_FOR_KIND` so aborted Groups round-trip honestly
  through `replay()`-derived RunState instead of stranding at BUILDING.
  Run-scoped events (`run.paused_by_user`, `spec.review_pending`, etc.)
  documented as intentionally not phase-affecting.
- **R3-2 spec-review preconditions**: `/edit` and `/approve` now both
  refuse with 409 when the underlying run is paused. `/approve` gained
  a lifecycle precondition (allowed only from `draft` or `approved`).
  Concurrent surgery via `editing_in_flight` / `amended` no longer
  rubber-stampable through `/approve`.
- **R3-3 resume composes with A6/A7**: `ResumePlan` gained
  `paused_by_user: bool` and `prior_invalidated_group_ids: frozenset[str]`
  populated by `plan_resume` from journal scans. Runner logs a warn-level
  line on resume when either is non-empty. Documented in
  `docs/i2p-resume-design.md` §7.7 / §7.8.
- **R3-4 schema bump v1 → v2**: `SCHEMA_VERSION = 2`. Legacy v1 keys
  still read with one advisory warning; v2 specs carrying leftover
  legacy keys emit louder warnings to time-bound the deprecation
  window. Auto-detect v1 when `schema_version` is absent and legacy
  keys are present.
- **R3-5 Event.feature_id**: optional `feature_id: str = ""` added to
  `Event`. Threaded through `emit()` + `iter_events()`. Mirrors
  `Evidence.feature_id`. Per-call-site wiring deferred — data-layer
  plumbing is in place.

**Prior Claude sweep: 1656/1656 passed** (16 new tests; 0 regressions).

Codex post-merge hardening on 2026-05-05 closed the former A8/A9/A10/A11
deferrals: synthesized webapp walkthroughs now attempt bundled Playwright
screenshot/DOM/video capture, build/fix retries reuse provider session ids,
the live runner no longer stacks the old run_audit fix loop on top of Layer 2,
and merge eligibility ignores superseded older BuildResult entries.

Honest deferrals you SHOULD know about:

- A8: screenshot/video capture now exists. Codex verified on 2026-05-05
  that Playwright Chromium launches in this worktree; the synthesized
  static-site detector now covers both generated output dirs and a plain
  root `index.html`.
- A9: session-pinned continuity is wired through `AgentOptions.resume`;
  this is not PID reuse, and providers that ignore resume will still
  behave as fresh subprocess calls.
- A11: "base not stale" is satisfied by merge-into-current-HEAD plus
  post-merge verification/rollback, not by a separate pre-rebase
  predicate. A future true rebase/multi-worktree executor can make that
  predicate explicit.
- B11: `scripts/bench_microfeed_real_webapp.py` (the bench's mono
  baseline source) lives only on the never-merged codex-i2p branch.
  Current bench uses a hard-coded 1500s ceiling rather than a re-run
  comparison.

Test-order flake to watch for: `test_autopilot.py::test_autopilot_full_executes_safe_recovery_once`
intermittently fails in the full sweep but passes in isolation —
pre-existing pattern (also affects test_cli_smoke / test_merge_orchestrator).
Re-run usually clears it.

## Files that need attention

These are the "honest gaps" Claude knows about but didn't close. None
block delivery. All are explicitly enumerated in `progress.md`.

- **`tests/_helpers.py`** — historical but live test helper.
  Cleanup-pass already done in `C.1f`; should be clean now but worth
  a re-grep for dead helpers if you go file-by-file.
- **Per-component frontend tests** — vitest+RTL infra never set up.
  This is its own ~½-day project. Not blocking anything but a real
  test-coverage gap on the new MC components.
- **A0.7** — retire user-facing `task` vocabulary. Tracked, deferred
  by design.
- **A6.6** — file-level "preserve" markers for brownfield compile.
  Deferred at design time.
- **Slice → Group rename cleanup** — canonical runtime/data fields use
  Group vocabulary. Remaining `slice` hits are mostly historical comments,
  test names, compatibility warnings, and read fallbacks for legacy
  `"slices"` proof/spec files. Dropping the fallback is intentionally held
  until after another bench cycle.
- **Type hygiene** — basedpyright surfaces a fair number of
  `reportUnknownVariableType` / `reportExplicitAny` warnings across
  `otto/cli.py`, `otto/cli_run.py`, `mission_control/service.py`. The
  redesign focused on shape, not type narrowing. Cleanup is welcome
  but not required.

## Known live areas where bugs could hide

The smoke proved the happy path. Less-tested paths:

1. **Brownfield compile** (`otto/spec_compile.py:_reconcile_brownfield`)
   — real Codex brownfield counter-repair run passed; see
   `docs/codex-handoff-results.md`.
2. **`--resume` on real interrupted runs** — real Codex kill/resume run
   passed after checkpoint and merge-skip fixes; see
   `docs/codex-handoff-results.md`.
3. **Multi-Group merge ordering with shared scaffolds** — tiny webapp and
   TODO-CLI runs landed multiple Groups in dependency order. TODO-CLI
   exposed dep-owned path expansion, now surfaced as `scope.warning`.
4. **MC live polling under high event volume** — synthetic backend stress
   read completed; frontend component harness remains deferred because
   vitest+RTL is not set up in this repo.
5. **Layer 2 audit→repair loop** (`otto/audit_loop.py`) — covered by
   runner tests plus real brownfield repair pass; repair re-audits and
   integrates successful fixes.
6. **Spec-edit during in-flight run** — unit/integration coverage exists
   for invalidation and route preconditions. A browser RUA for editing
   during a live paused build is still deferred.

## Process expectations

Per project `CLAUDE.md`:

- Adversarial review: run `/codex-gate` Plan Gate + Implementation Gate
  before merge (this is YOU now — in your `mcp__codex__codex` mode you
  can self-review iteratively).
- Logs first: when debugging, read `otto_logs/sessions/<id>/build/narrative.log`
  before guessing. Never claim "it's fixed" without a re-run.
- Verify before claiming: after every edit, grep for the pattern you
  changed across the entire `otto/` + `tests/` tree before moving on.
- Codex fixes Codex-found bugs: standard practice — when you find a
  bug while reviewing, you write the fix.
- Worktree discipline: stay on the active handoff worktree branch. For
  this Codex pass that is `codex-i2p-v2`; don't switch to `main` or
  run git writes against another branch without confirmation.

## What I want from you

See the prompt at the end of this conversation. In short:

1. Keep this branch self-contained and do not switch to `main`.
2. Hunt + fix bugs across the surfaces enumerated above ("known live
   areas where bugs could hide"). Real-cost runs are approved.
3. Run end-to-end tests against representative project types: a tiny
   webpage, a small CLI tool, a brownfield repo. Capture metrics
   (wall, cost, verdict) and write reports to `docs/`.

Stop conditions: 14d wall cap, catastrophic test red ≥2 sweeps, or
all enumerated areas have evidence of a real-cost run on `main`.
