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
- **CLAUDE.md** — `proof-packet.{html,json}` not in the layout table
  (only in narrative). Doc nit.
- **Type hygiene** — basedpyright surfaces a fair number of
  `reportUnknownVariableType` / `reportExplicitAny` warnings across
  `otto/cli.py`, `otto/cli_run.py`, `mission_control/service.py`. The
  redesign focused on shape, not type narrowing. Cleanup is welcome
  but not required.

## Known live areas where bugs could hide

The smoke proved the happy path. Less-tested paths:

1. **Brownfield compile** (`otto/spec_compile.py:_reconcile_brownfield`)
   — only smoke-tested via `tests/integration/test_brownfield_compile_real.py`,
   no real-cost validation against a non-trivial existing repo.
2. **`--resume` on real interrupted runs** — unit tests cover
   `plan_resume` classification; no real-cost test of "kill mid-build,
   restart, complete". Worth a real run.
3. **Multi-Group merge ordering with shared scaffolds** — i2p
   merge_queue logic has unit coverage but the dep-graph + shared_paths
   interaction is subtle. Real bench has only run on 1- and 4-Group
   intents.
4. **MC live polling under high event volume** — useRunView polls
   every 3s; no stress test of long runs (50+ events/sec backend).
5. **Layer 2 audit→repair loop** (`otto/audit_loop.py`) — unit tests
   cover the orchestrator but real fix-cycle behavior on a failing
   feature has not been smoked end-to-end.
6. **Spec-edit during in-flight run** — research.md design says
   recompile invalidates dependent in-flight slices wholesale; not
   tested live.

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
- Worktree discipline: stay on `cc-i2p-2`. Don't switch to `main`,
  don't run git writes against another branch without confirmation.

## What I want from you

See the prompt at the end of this conversation. In short:

1. Merge `cc-i2p-2` into `main` cleanly (rebase or merge — your call,
   document the choice).
2. Hunt + fix bugs across the surfaces enumerated above ("known live
   areas where bugs could hide"). Real-cost runs are approved.
3. Run end-to-end tests against representative project types: a tiny
   webpage, a small CLI tool, a brownfield repo. Capture metrics
   (wall, cost, verdict) and write reports to `docs/`.

Stop conditions: 14d wall cap, catastrophic test red ≥2 sweeps, or
all enumerated areas have evidence of a real-cost run on `main`.
