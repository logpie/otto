"""Regression: when child-verify repair passes its oracle but the child's
own verdict.json wasn't lifted (cross-feature isolation pattern), the
upward merge LANDS with annotation instead of blocking.

The bug class: Feature B builds its router fully but its own tests need
Feature A's auth endpoint that isn't in B's isolated worktree. B honestly
reports `verdict: partial`. The child-verify repair runs against the
integrated worktree (where A IS present) and the oracle passes. But B's
verdict.json still says partial — B never updated it. Pre-fix:
_ensure_child_merge_ready blocked the upward merge. Post-fix: it
LANDS with annotation (landed_with_annotation=True) so aggregation
counts the child as satisfactory_terminal.

Same shape as commit e8293e97c (foundation_contract_write_gate moving
from refuse to LAND-annotate) and the broader chokepoint pattern.
"""

from __future__ import annotations

import inspect

# Import v5_runner first to break the circular-import on otto.v5.merge
# (v5_runner imports merge symbols, so this primes the cycle).
from otto import v5_runner  # noqa: F401
from otto.v5.merge import _ensure_child_merge_ready


def test_repair_pass_with_unlifted_verdict_lands_with_annotation() -> None:
    """Source-level guard: the post-repair-pass path must mark
    landed_with_annotation=True instead of blocking."""
    src = inspect.getsource(_ensure_child_merge_ready)

    # The function must include the LAND-with-annotation path:
    assert "landed_with_annotation" in src and "True" in src, (
        "_ensure_child_merge_ready should LAND with annotation when "
        "repair.verdict == PASS but the child's verdict.json wasn't lifted "
        "to a mergeable verdict."
    )
    assert "child_verify_repair_resolved_isolation" in src, (
        "Annotation origin should be child_verify_repair_resolved_isolation "
        "so the proof packet records why the partial verdict landed."
    )
    assert "set_verdict_and_metadata" in src, (
        "Should use set_verdict_and_metadata to atomically write the "
        "annotation alongside the verdict."
    )


def test_block_path_still_fires_when_repair_oracle_fails() -> None:
    """The fail-closed path (repair.verdict != PASS) still blocks the
    upward merge — only the repair-PASS-but-unlifted case lands now."""
    src = inspect.getsource(_ensure_child_merge_ready)
    # The original block path on repair fail is preserved:
    assert "Child verify/repair oracle did not pass" in src
    # And the helper call that routes through the chokepoint:
    assert "_block_child_before_upward_merge" in src


def test_emits_child_landed_with_annotation_event() -> None:
    """Event surface for the new landing path so monitoring can
    distinguish 'landed with annotation due to isolation gap' from
    'merge_blocked'."""
    src = inspect.getsource(_ensure_child_merge_ready)
    assert '"event": "child_landed_with_annotation"' in src
    assert '"origin": "child_verify_repair_resolved_isolation"' in src
