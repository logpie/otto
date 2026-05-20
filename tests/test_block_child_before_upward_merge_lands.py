"""Regression: `_block_child_before_upward_merge` must route through the
central terminal chokepoint, so child-verify failures LAND with annotation
(partial) instead of refusing as merge_blocked.

Discovered on iTracker cc-i2p-2 OPUS run (2026-05-20): 3 of 4 feature
children ended in `merge_blocked` because this helper bypassed the
chokepoint with a direct `set_verdict(..., "merge_blocked")` + assignment
to `result.verdict`. The chokepoint maps `origin="verification"` to
TerminalCause.VERIFICATION → LAND (partial), which is correct per the
locked one-hard-gate design. The direct bypass caused ~$120 of work to
be orphaned on side branches that never reached main.

Same anti-pattern shape as the foundation_contract_write_gate fix
(commit e8293e97c) — pre-commit/pre-merge refusal that needs to become
land-with-annotation.
"""
from __future__ import annotations

import inspect

from otto import v5_runner


def test_block_child_routes_through_chokepoint_helper():
    """The function should call `_record_task_merge_blocked_reason` (the
    chokepoint helper) — NOT `set_verdict(..., "merge_blocked")` directly
    and NOT assign `result.verdict = "merge_blocked"` directly. Source-level
    guard so the regression can't slip back."""
    src = inspect.getsource(v5_runner._block_child_before_upward_merge)

    # Must use the chokepoint helper
    assert "_record_task_merge_blocked_reason(" in src, (
        "_block_child_before_upward_merge must route terminal verdicts "
        "through the central chokepoint helper `_record_task_merge_blocked_reason`. "
        "The chokepoint maps origin→cause→TerminalAction and is the single "
        "place that decides LAND vs HONEST_TERMINAL per the locked design."
    )

    # Must NOT directly set merge_blocked
    assert 'set_verdict(project_dir, child_task_id, "merge_blocked"' not in src, (
        "Direct `set_verdict(..., \"merge_blocked\")` call detected — this "
        "bypasses the chokepoint. Use _record_task_merge_blocked_reason instead."
    )
    assert 'result.verdict = "merge_blocked"' not in src, (
        "Direct `result.verdict = \"merge_blocked\"` assignment detected — "
        "this bypasses the chokepoint. Use _record_task_merge_blocked_reason."
    )

    # Origin must be the chokepoint-recognized "verification" string
    assert 'origin="verification"' in src, (
        "Should pass `origin=\"verification\"` to the chokepoint helper so "
        "_cause_from_origin maps to TerminalCause.VERIFICATION (which routes "
        "to LAND per the locked design)."
    )


def test_function_still_emits_child_merge_blocked_event():
    """The downstream `child_merge_blocked` event is still useful telemetry
    even when the verdict is now `partial`. Don't drop it."""
    src = inspect.getsource(v5_runner._block_child_before_upward_merge)
    assert '"event": "child_merge_blocked"' in src, (
        "child_merge_blocked event was dropped — keep it for telemetry."
    )
