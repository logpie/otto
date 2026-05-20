# Critical Seam Fix Plan

Date: 2026-05-16
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`
Branch: `cc-i2p-2`

## Scope

Fix the three RED repros in `tests/test_critical_seam_repros.py` without a git
commit or branch change. Preserve unrelated worktree changes.

## Step 1 - Seam 2 UI Executor Runtime

Change `otto/journey_ui_executor.py` so Playwright execution does not call the
sync Playwright API in the pipeline asyncio loop. Prefer an async Playwright
port; if the surrounding synchronous preflight API makes that too invasive,
run the existing synchronous browser driver in a dedicated worker thread while
preserving the same DOM/network delta checks and artifacts.

Why: the current failure is not a product verdict; it is an executor runtime
fault that prevents every root-integration UI journey from being evaluated.

Verify:
`uv run pytest -q tests/test_critical_seam_repros.py::test_root_ui_executor_runtime_failure_enters_preflight_repair_and_working_control_passes`
must show the dead fixture failing as `ui_journey_failed` from `ui_executor`
and the working fixture passing from `ui_executor`.

## Step 2 - Seam 1 Upward Merge Refusal

Change `otto/v5_runner.py` so `_merge_child_branch()` does not terminally mark a
child `merge_blocked` after a late upward merge gate refusal without re-entry.
The refusal should be synthesized into a blocking oracle result in the same
child verify repair packet, then passed through `run_oracle_repair_agent()`.

Why: `_ensure_child_merge_ready()` has already established the child verify
repair unit. A later dirty/conflicted parent gate refusal is feedback for that
same bounded unit, not a reasonless terminal state.

Verify:
`uv run pytest -q tests/test_critical_seam_repros.py::test_child_verify_repair_pass_reenters_when_upward_merge_gate_refuses_dirty_parent`
must show at least two repair packet entries, structured merge/dirty reason in
the packet payload, and no reasonless terminal block.

## Step 3 - Seam 3 Root Propagation Refusal

Change `otto/v5_runner.py` so subtree-to-root propagation failure creates a
durable propagation repair packet, records the structured conflict reason, and
re-enters `run_oracle_repair_agent()` before terminally blocking.

Why: subtree integration green is not sufficient if root propagation fails;
that conflict must route through the same agent-native repair protocol.

Verify:
`uv run pytest -q tests/test_critical_seam_repros.py::test_subtree_integration_pass_reenters_when_root_propagation_conflicts`
must show a propagation repair packet whose latest/current/context payload
contains the conflict reason.

## Step 4 - No-Regress

Run the requested focused repros and regression batch, then ruff and
basedpyright on touched files.

Verify:
- `uv run python scripts/test_tiers.py smoke`
- requested no-regress pytest batch
- `uv run ruff check <touched files>`
- `uv run basedpyright --level error <touched files>`

## Plan Gate

The `/codex-gate` MCP tool required by project instructions is not available in
this session's active tools. This plan records the missing gate explicitly; the
substitute controls are RED/GREEN repros, focused protocol tests, requested
regression batch, ruff, and basedpyright.

## Verification Log

- RED before patch:
  - Seam 2 failed in 2.34s with Playwright sync API inside asyncio loop and
    `clean_deploy_smoke_error`.
  - Seam 1 failed in 0.95s with one repair packet, dirty parent merge refusal,
    and no recorded reason.
  - Seam 3 failed in 1.31s with zero repair packets after root propagation
    conflict.
- GREEN after patch:
  - Seam 2 focused repro: 1 passed in 125.23s.
  - Seam 1 focused repro: 1 passed in 1.18s.
  - Seam 3 focused repro: 1 passed in 1.48s.
  - Full `tests/test_critical_seam_repros.py`: 3 passed in 126.50s while
    local socket binds were available.
  - Final post-adjustment rerun: 2 passed, 1 skipped in 1.70s because the
    sandbox later denied the Seam 2 local socket bind.
- Required smoke: `UV_CACHE_DIR=/private/tmp/otto-uv-cache /usr/bin/time -p uv run python scripts/test_tiers.py smoke`
  passed after the final code change: 309 passed, 2560 deselected in 13.75s;
  wall 14.18s.
- Requested no-regress batch:
  full command reached 206 passed, 22 failed in 28.99s; every failure was
  `PermissionError: [Errno 1] Operation not permitted` from local
  `127.0.0.1:0` socket binds.
- Non-socket remainder of the requested batch:
  passed after the final code change: 199 passed, 1 deselected in 28.82s;
  wall 29.10s.
- Static checks:
  - `uv run ruff check otto/journey_ui_executor.py otto/v5_preflight.py otto/v5_runner.py`
    passed; wall 0.02s.
  - `uv run basedpyright --level error otto/journey_ui_executor.py otto/v5_preflight.py otto/v5_runner.py`
    passed with 0 errors, 0 warnings, 0 notes; wall 1.04s.
