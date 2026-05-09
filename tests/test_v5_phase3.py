"""Phase 3 smoke tests — review affordance backend.

Tests the v5_review module + the watcher pause integration. UI tests are
out of scope (the React tree-view component is for an MC contributor; the
review modal can be driven via CLI).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from otto.queue.subtask import enqueue_subtask, read_pending
from otto.v5_review import (
    approve,
    cancel,
    edit,
    list_pending_review,
    mark_pending_review,
    replace,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "otto_logs").mkdir()
    return tmp_path


class TestMarkPendingReview:
    def test_mark_flips_approved_to_pending(self, project: Path) -> None:
        a = enqueue_subtask(
            project_dir=project, parent_task_id="root",
            parent_session_dir=project, intent="A",
        )
        b = enqueue_subtask(
            project_dir=project, parent_task_id="root",
            parent_session_dir=project, intent="B",
        )
        # Default state is approved; flip to pending_review.
        n = mark_pending_review(project, parent_task_id="root")
        assert n == 2
        pending = list_pending_review(project)
        assert {p["task_id"] for p in pending} == {a, b}

    def test_mark_only_targets_specified_parent(self, project: Path) -> None:
        a = enqueue_subtask(
            project_dir=project, parent_task_id="root",
            parent_session_dir=project, intent="A",
        )
        b = enqueue_subtask(
            project_dir=project, parent_task_id="other",
            parent_session_dir=project, intent="B",
        )
        n = mark_pending_review(project, parent_task_id="root")
        assert n == 1
        pending_root = list_pending_review(project, parent_task_id="root")
        pending_other = list_pending_review(project, parent_task_id="other")
        assert len(pending_root) == 1
        assert len(pending_other) == 0  # 'other's child stayed approved


class TestApprove:
    def test_approve_all_under_parent(self, project: Path) -> None:
        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="A")
        b = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="B")
        mark_pending_review(project, parent_task_id="root")
        n = approve(project, parent_task_id="root")
        assert n == 2
        assert list_pending_review(project) == []

    def test_approve_specific_task(self, project: Path) -> None:
        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="A")
        b = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="B")
        mark_pending_review(project, parent_task_id="root")
        n = approve(project, task_ids=[a])
        assert n == 1
        # Only a is approved; b still pending.
        pending = list_pending_review(project)
        assert {p["task_id"] for p in pending} == {b}


class TestCancel:
    def test_cancel_marks_state(self, project: Path) -> None:
        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="A")
        mark_pending_review(project, parent_task_id="root")
        n = cancel(project, task_ids=[a])
        assert n == 1
        # Cancelled tasks no longer in pending_review list.
        assert list_pending_review(project) == []
        # And in the raw queue, state is "cancelled".
        all_tasks = read_pending(project)
        a_entry = next(t for t in all_tasks if t["task_id"] == a)
        assert a_entry["review_state"] == "cancelled"


class TestEdit:
    def test_edit_changes_intent(self, project: Path) -> None:
        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="original")
        mark_pending_review(project, parent_task_id="root")
        ok = edit(project, task_id=a, new_intent="revised")
        assert ok is True
        pending = list_pending_review(project)
        assert pending[0]["intent"] == "revised"

    def test_edit_rejects_empty_intent(self, project: Path) -> None:
        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="x")
        mark_pending_review(project, parent_task_id="root")
        with pytest.raises(ValueError):
            edit(project, task_id=a, new_intent="   ")

    def test_edit_returns_false_for_unknown_task(self, project: Path) -> None:
        ok = edit(project, task_id="v5-no-such-task", new_intent="anything")
        assert ok is False


class TestReplace:
    def test_replace_cancels_existing_and_appends_new(self, project: Path) -> None:
        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="orig-A")
        b = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="orig-B")
        mark_pending_review(project, parent_task_id="root")

        cancelled, new_ids = replace(
            project,
            parent_task_id="root",
            new_intents=["new1", "new2", "new3"],
        )
        assert set(cancelled) == {a, b}
        assert len(new_ids) == 3

        # New tasks are in approved state by default (created via enqueue_subtask).
        all_tasks = read_pending(project)
        new_entries = [t for t in all_tasks if t["task_id"] in new_ids]
        assert len(new_entries) == 3
        for entry in new_entries:
            assert entry["intent"] in {"new1", "new2", "new3"}
            assert entry["review_state"] == "approved"


class TestWaitForReviewTimeout:
    @pytest.mark.asyncio
    async def test_wait_auto_approves_on_timeout(self, project: Path) -> None:
        """Per plan-v5 §13: every layer terminates. Review timeout auto-approves."""
        from otto.v5_runner import _wait_for_review

        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="A")
        mark_pending_review(project, parent_task_id="root")
        # Start with very short timeout.
        events: list[dict] = []
        await _wait_for_review(
            project,
            parent_task_id="root",
            poll_interval_s=0.05,
            timeout_s=0.1,
            on_event=lambda e: events.append(e),
        )
        # After timeout, no tasks remain in pending_review state.
        assert list_pending_review(project) == []
        # Event log shows the auto-approve.
        assert any(e.get("event") == "review_timeout_auto_approve" for e in events)

    @pytest.mark.asyncio
    async def test_wait_completes_when_user_approves(self, project: Path) -> None:
        """Wait returns cleanly when user approves via the CLI/API."""
        from otto.v5_runner import _wait_for_review

        a = enqueue_subtask(project_dir=project, parent_task_id="root",
                            parent_session_dir=project, intent="A")
        mark_pending_review(project, parent_task_id="root")

        # Spawn the waiter; have a sibling approve after a small delay.
        async def approver() -> None:
            await asyncio.sleep(0.05)
            approve(project, parent_task_id="root")

        await asyncio.gather(
            _wait_for_review(
                project,
                parent_task_id="root",
                poll_interval_s=0.02,
                timeout_s=2.0,
            ),
            approver(),
        )
        assert list_pending_review(project) == []
