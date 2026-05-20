# Plan: Overnight iTracker Correctness Bugs

Date: 2026-05-15
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`
Branch: `cc-i2p-2`

## Objective

Fix three confirmed correctness bugs without changing branch or committing:

- Bug A: progress-making preflight repair sequences must not hit the old flat
  total-attempt cap.
- Bug B1: deprecation detection must distinguish real product warning emissions
  from zeroed/filtered prose and third-party-only warnings, and runner downgrades
  must write a concrete `failure_reason`.
- Bug B2: Step 0b branch-ancestry reconciliation must not convert
  verification-blocked children to `pass`.

## Owned Files

- `otto/v5_preflight_repair.py`
- `otto/v5_verification_plan.py`
- `otto/lead.py`
- `otto/v5_runner.py`
- `otto/queue/task_graph.py`
- `tests/smoke/test_preflight_repair_fixtures.py`
- `tests/test_v5_verification_plan.py`
- `tests/test_v5_step0b_recovery.py`

## Plan Gate

1. Current worktree/branch verified with `pwd && git branch --show-current`:
   `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`, `cc-i2p-2`.
2. Existing patterns:
   - Preflight repair already logs timestamped JSONL attempts and has
     per-kind/repeated-fingerprint caps.
   - Verification plan already writes structured checks and `runner_checks`.
   - Task graph verdict is the durable source for aggregation.
3. Riskiest assumptions:
   - Progress definition for Bug A must avoid infinite retries. Use changed
     fingerprint/kind, fewer unsatisfied ports, or a repaired outcome to reset
     consecutive no-progress; keep repeated fingerprint and per-kind caps; add a
     generous absolute ceiling.
   - Bug B1 must not become a one-sentence carve-out. Use emitted-warning-line
     parsing plus zero/filtered/prose summaries and third-party path filtering.
   - Bug B2 needs a durable enough marker for why a child is merge_blocked. Use
     graph-visible block provenance when available, and conservative inference
     from summary/verdict runner checks when only legacy state exists.
4. Simpler alternatives rejected:
   - Raising `max_total_attempts` only would still punish progress eventually and
     would not distinguish real no-progress loops.
   - Suppressing all deprecations would violate the hardening rule; product-path
     `DeprecationWarning:` emissions still fail.
   - Removing reconciliation entirely would regress legitimate Step 0b recovery.

## Steps

1. Add RED tests.
   Verify: run each new focused test against current production code and capture
   failures for A, B1, and B2 before production patches.

2. Fix Bug A in `PreflightRepairController`.
   Verify: progressing repaired sequence clears old cap; repeated identical
   fingerprint still escalates.

3. Fix Bug B1 in verification and lead downgrade propagation.
   Verify: filtered/zeroed line and third-party-only warnings pass; product
   warning fails; `run_lead` summary has non-empty `failure_reason` on runner
   downgrade.

4. Fix Bug B2 in recovered-child reconciliation.
   Verify: branch-ancestry-only recovery leaves verification-blocked child
   blocked, but a merge-blocked child with ancestry still upgrades.

5. Run required validation.
   Verify:
   - `uv run python scripts/test_tiers.py smoke`
   - hardening/leaf/smoke/guardrail batch plus new tests
   - `uv run ruff check` on touched files
   - `uv run basedpyright --level error` on touched files

## Implementation Gate Trail

- RED proof before implementation:
  `uv run pytest -q tests/smoke/test_preflight_repair_fixtures.py::test_progressing_preflight_repairs_do_not_hit_old_total_cap tests/test_v5_verification_plan.py::test_deprecation_detection_filters_prose_dependencies_and_records_downgrade_reason tests/test_v5_step0b_recovery.py::test_reconcile_does_not_upgrade_verification_blocked_child_by_ancestry`
  produced 3 failures: old `total_attempt_cap`, false deprecation/filter +
  empty downgrade reason, and ancestry-only reconciliation count `2 != 1`.
- GREEN proof after implementation:
  same command passed, `3 passed in 0.75s`.
- Requested regression batch:
  `uv run pytest -q tests/test_v5_p0_hardening.py tests/test_v5_p1_hardening.py tests/test_v5_p2_hardening.py tests/test_v5_pass4_hardening.py tests/test_v5_leaf_runtime_invariants.py tests/test_brittleness_guardrail.py tests/test_v5_integration_worktree.py tests/smoke tests/test_merge_queue.py tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py`
  passed, `210 passed in 21.34s`.
- Required smoke tier:
  `uv run python scripts/test_tiers.py smoke` passed,
  `307 passed, 2473 deselected in 13.52s`.
- Touched-file lint:
  `uv run ruff check otto/v5_preflight_repair.py otto/v5_verification_plan.py otto/lead.py otto/v5_runner.py otto/queue/task_graph.py tests/smoke/test_preflight_repair_fixtures.py tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py`
  passed.
- Touched-file type check:
  `uv run basedpyright --level error otto/v5_preflight_repair.py otto/v5_verification_plan.py otto/lead.py otto/v5_runner.py otto/queue/task_graph.py tests/smoke/test_preflight_repair_fixtures.py tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py`
  passed, `0 errors, 0 warnings, 0 notes`.
