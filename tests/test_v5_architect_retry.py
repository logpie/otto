"""Tests for architect-retry-on-preflight-failure machinery.

When scaffold preflight blocks an architect's self-declared pass, the
runner clears the verdict + stores a retry_reason so the next dispatch
picks it back up with the failure context attached.

These tests exercise the task_graph helpers and the cap-handling logic.
The full runner integration (mocked architect → preflight fail → retry
→ success) is more naturally validated by a live otto v5 run; here we
prove the building blocks behave as designed.
"""

from __future__ import annotations

from pathlib import Path

from otto.queue.task_graph import (
    clear_verdict_for_retry,
    get_retry_count,
    get_retry_reason,
    record_task,
    set_verdict,
)
from otto.v5_runner import MAX_ARCHITECT_RETRIES


def _seed_architect(project_dir: Path, task_id: str = "v5-arch") -> None:
    """Create an architect-shaped task with verdict=pass."""
    record_task(
        project_dir,
        task_id=task_id,
        intent="Architect a Linear-lite scaffold.",
        parent_task_id="root",
    )
    set_verdict(project_dir, task_id, "pass")


def test_clear_verdict_for_retry_resets_state(tmp_path: Path) -> None:
    _seed_architect(tmp_path)
    # Sanity precondition.
    from otto.queue.task_graph import get_task

    pre = get_task(tmp_path, "v5-arch") or {}
    assert pre.get("verdict") == "pass"
    assert pre.get("retry_count", 0) == 0
    assert get_retry_reason(tmp_path, "v5-arch") is None

    new_count = clear_verdict_for_retry(
        tmp_path, "v5-arch", "scaffold failed: tsc error"
    )

    post = get_task(tmp_path, "v5-arch") or {}
    assert post["verdict"] is None  # cleared, take_ready will re-pick-up
    assert post["completed_at"] is None
    assert post["retry_count"] == 1
    assert new_count == 1
    assert get_retry_reason(tmp_path, "v5-arch") == "scaffold failed: tsc error"
    assert get_retry_count(tmp_path, "v5-arch") == 1


def test_retry_count_increments_across_calls(tmp_path: Path) -> None:
    _seed_architect(tmp_path)
    assert clear_verdict_for_retry(tmp_path, "v5-arch", "fail 1") == 1
    # Simulate the agent finishing again (verdict back to pass) then
    # preflight invalidating it a second time.
    set_verdict(tmp_path, "v5-arch", "pass")
    assert clear_verdict_for_retry(tmp_path, "v5-arch", "fail 2") == 2
    set_verdict(tmp_path, "v5-arch", "pass")
    assert clear_verdict_for_retry(tmp_path, "v5-arch", "fail 3") == 3
    assert get_retry_count(tmp_path, "v5-arch") == 3
    # The most recent reason is what's stored.
    assert get_retry_reason(tmp_path, "v5-arch") == "fail 3"


def test_clear_verdict_unknown_task_returns_zero(tmp_path: Path) -> None:
    # Don't seed; task doesn't exist.
    assert clear_verdict_for_retry(tmp_path, "does-not-exist", "x") == 0


def test_get_retry_reason_returns_none_when_empty(tmp_path: Path) -> None:
    _seed_architect(tmp_path)
    assert get_retry_reason(tmp_path, "v5-arch") is None


def test_max_architect_retries_constant(tmp_path: Path) -> None:
    """Sanity: the cap is a small positive integer.

    Runner enforces ``retry_count < MAX_ARCHITECT_RETRIES`` before
    invalidating again, so the architect gets 1 original attempt + N
    retries = N+1 total. We don't want the cap to be 0 (no retries
    allowed) or unboundedly large.
    """
    assert isinstance(MAX_ARCHITECT_RETRIES, int)
    assert 1 <= MAX_ARCHITECT_RETRIES <= 5


def test_retry_state_independent_per_task(tmp_path: Path) -> None:
    """Two architect tasks (different subsystems) keep separate retry state."""
    record_task(tmp_path, task_id="v5-arch-fe", intent="Architect FE.", parent_task_id="root")
    record_task(tmp_path, task_id="v5-arch-be", intent="Architect BE.", parent_task_id="root")
    set_verdict(tmp_path, "v5-arch-fe", "pass")
    set_verdict(tmp_path, "v5-arch-be", "pass")

    assert clear_verdict_for_retry(tmp_path, "v5-arch-fe", "fe broken") == 1
    assert get_retry_count(tmp_path, "v5-arch-fe") == 1
    assert get_retry_count(tmp_path, "v5-arch-be") == 0
    assert get_retry_reason(tmp_path, "v5-arch-be") is None
