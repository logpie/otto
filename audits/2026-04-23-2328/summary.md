# Test Suite Audit Summary

Branch: `fix/codex-provider-i2p`

Dirty-state note: the worktree already contained the stale Mission Control merge-row fix before this audit. This pass added test-suite cleanup on top of that work.

## What Changed

- Deprecated legacy Textual TUI tests from the default pytest run with a `tui` marker.
- Kept explicit TUI verification available via `pytest -m tui`.
- Moved shared Mission Control tests away from `otto.tui.*` compatibility imports and onto `otto.mission_control.*`.
- Removed real-time sleeps from high-cost queue runner and Mission Control cancel tests.
- Fixed the `otto_as_user` B2 verifier fallback so terminal queue state is honored before waiting up to 10 seconds for history.

## Results

- Before this audit: `908 passed in 167.92s`
- After excluding TUI and tightening sleeps: `890 passed, 18 deselected in 132.81s`
- Legacy TUI slice: `18 passed, 3 deselected in 7.62s`
- Focused changed tests: `131 passed, 1 deselected in 10.58s`

## Remaining Slow Areas

- Queue and atomic cancel heartbeat tests: around 2.3-2.5s each.
- Broad hardening and v3 pipeline tests: many medium-cost subprocess/git-agent simulations.
- Merge/integration tests: valuable but not unit-test cheap.

Recommendation: keep the current default as the safety-oriented non-TUI suite. If the desired developer loop is about one minute, add a second marker tier such as `slow`/`integration` and make the default `not tui and not slow and not integration`.
