# Test Suite Audit Findings

## Fixed

- IMPORTANT: Legacy Textual TUI tests still ran in the default suite even though Mission Control is moving to web. Marked TUI-specific tests with `@pytest.mark.tui` and excluded them from default pytest via `addopts = ["-m", "not tui"]`. They still run explicitly with `-m tui`.
- IMPORTANT: Shared Mission Control tests imported through `otto.tui.*` compatibility aliases. Updated non-UI tests to import `otto.mission_control.*` directly so default coverage tracks the active web/client-neutral core instead of the deprecated TUI surface.
- IMPORTANT: `scripts/otto_as_user.py::resolve_queue_task_verify_status` waited up to 10 seconds for terminal history before honoring an already-terminal queue state. This made `test_verify_b2_falls_back_to_queue_terminal_status_without_stringifying_none` spend 10 seconds on a fallback path. The resolver now returns terminal queue state immediately while still waiting for history when the state is nonterminal.
- IMPORTANT: Queue runner tests used fixed sleeps between ticks, including 6 seconds in the timeout test and multiple 1.0-2.0 second waits. Replaced those sleeps with bounded polling helpers and direct timestamp aging where the behavior under test is timeout handling.
- IMPORTANT: Mission Control cancel fallback tests waited through the real four-second minimum fallback window. They now use a fake clock in the tests that do not need to measure real elapsed time; the dedicated minimum-window test still asserts the four-second contract.
- IMPORTANT: Abandoned live-run rows from interrupted merge/certify flows could remain visible as running forever. Fixed in the previous patch by treating stale dead-writer records as inactive and cleanupable; retained this in the audit because the test suite now covers it.

## Deferred

- NOTE: The remaining default suite is still dominated by broad integration-style tests in `tests/test_hardening.py`, `tests/test_v3_pipeline.py`, merge tests, queue runner subprocess tests, and `tests/integration/`. They are not redundant, but they should be split into explicit `integration` or `slow` tiers if the default target must return in about one minute.
- NOTE: `tests/test_hardening.py` is over 4,000 lines and mixes unrelated regression areas. Splitting by subsystem would make targeted runs easier and reduce accidental broad-test edits.
- NOTE: Legacy TUI production code and the `textual` runtime dependency remain present. This audit only moved TUI tests out of the default suite; deleting or fully optionalizing TUI code is a separate product decision.
