"""Post-big-refactor (2026-05-21): _ensure_child_merge_ready is a
no-op pass-through; the orchestrator no longer attempts upward merge
or dispatches child-verify repair at child-finish time. Integration
Lead is the single merge authority (see lead-integration.md Step 1)
and handles ALL merging + conflict resolution + journey verification.

The cross-feature-isolation partial cascade this function previously
handled via LAND-with-annotation is now resolved by integration
naturally: integration merges the sibling code in, so the cross-feature
test passes by construction. No need to second-guess the leaf verdict.

These tests assert the new no-op behavior. The function still exists
as a thin pass-through to preserve the dispatch.py call site signature
during incremental rollout; a follow-up commit will inline the
no-op and delete the function.
"""

from __future__ import annotations

import inspect

# Import v5_runner first to prime the circular-import on otto.v5.merge.
from otto import v5_runner  # noqa: F401
from otto.v5.merge import _ensure_child_merge_ready


def test_function_is_now_a_noop_passthrough() -> None:
    """The function should return the result unchanged. No merge attempt,
    no repair dispatch, no LAND-with-annotation. Integration Lead handles
    all of that."""
    src = inspect.getsource(_ensure_child_merge_ready)

    # The function must NOT call merge/repair APIs:
    assert "_run_child_verify_repair_packet" not in src, (
        "Child-verify repair packet dispatch was removed in the integration-"
        "as-single-merge-authority refactor. Integration Lead handles "
        "conflicts and re-verification — no need to dispatch a repair at "
        "child-finish time."
    )
    assert "_block_child_before_upward_merge" not in src, (
        "Block-and-LAND-with-annotation is no longer applied at "
        "child-finish; the integration Lead's merge resolves the cross-"
        "feature isolation that previously triggered this."
    )

    # The function must record reviewed-partial flags (a no-cost data
    # propagation that integration also reads) and pass through:
    assert "_record_reviewed_partial_if_present" in src
    assert "return result" in src


def test_emits_deferred_to_integration_event() -> None:
    """Telemetry event surfaces the new behavior for monitoring."""
    src = inspect.getsource(_ensure_child_merge_ready)
    assert '"event": "child_merge_deferred_to_integration"' in src, (
        "Should emit a telemetry event noting that merge handling is "
        "deferred to integration."
    )
