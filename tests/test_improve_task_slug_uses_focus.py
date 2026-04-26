"""W3-IMPORTANT-1 regression — improve task id + resolved_intent derive
from `focus`, not from the project snapshot intent.

In the live W3 dogfood (see docs/mc-audit/live-findings.md), JobDialog
queued an improve task with focus="Make greet() handle empty/None name
by returning 'Hello, world!'…". The queued task came back as:

    id: web-as-user                          # ← project tempdir name!
    resolved_intent: '# web-as-user'
    focus: Make greet() handle empty/None …

The id should slugify the focus, and resolved_intent should be the focus
itself — the project snapshot intent (often the README) is not what
distinguishes one improve from another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otto.queue.enqueue import enqueue_task
from otto.queue.schema import load_queue
from tests._helpers import init_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path)


def test_improve_task_id_uses_focus_not_project_name(repo: Path) -> None:
    """Improve enqueued with focus → slug derives from focus."""
    focus = "Make greet handle empty or None name"
    result = enqueue_task(
        repo,
        command="improve",
        raw_args=["bugs", focus],
        intent="# proj\n\nproject snapshot",
        after=[],
        explicit_as=None,
        resumable=True,
        focus=focus,
    )
    # Slug must come from focus (lowercase, hyphenated), NOT from "# proj"
    assert "make-greet" in result.task.id
    assert result.task.id != "proj"
    # Round-trip through queue.yml
    persisted = load_queue(repo)
    assert persisted[0].id == result.task.id


def test_improve_task_resolved_intent_is_focus(repo: Path) -> None:
    """resolved_intent should be the focus, not the project snapshot."""
    focus = "Add empty-name handling"
    result = enqueue_task(
        repo,
        command="improve",
        raw_args=["bugs", focus],
        intent="# proj\n\nthis is the README of the project",
        after=[],
        explicit_as=None,
        resumable=True,
        focus=focus,
    )
    assert result.task.resolved_intent == focus
    # The "# proj" markdown heading must NOT leak into the stored intent.
    assert "# proj" not in (result.task.resolved_intent or "")


def test_improve_target_uses_target_for_slug(repo: Path) -> None:
    """`improve target` carries `target` not `focus` — slug from target."""
    target = "p95 latency under 100ms"
    result = enqueue_task(
        repo,
        command="improve",
        raw_args=["target", target],
        intent="# proj\n\nproject snapshot",
        after=[],
        explicit_as=None,
        resumable=True,
        target=target,
    )
    assert "p95-latency" in result.task.id or "latency" in result.task.id
    assert result.task.id != "proj"
    assert result.task.resolved_intent == target


def test_improve_without_focus_falls_back_to_intent(repo: Path) -> None:
    """When no focus/target is provided, fall back to legacy slug-from-intent."""
    result = enqueue_task(
        repo,
        command="improve",
        raw_args=["bugs"],
        intent="some snapshot intent",
        after=[],
        explicit_as=None,
        resumable=True,
        focus=None,
    )
    # Should slugify the snapshot intent (legacy behavior).
    assert "snapshot" in result.task.id or "intent" in result.task.id


def test_build_task_unaffected(repo: Path) -> None:
    """Build tasks must keep slugifying from intent (no focus arg in play)."""
    result = enqueue_task(
        repo,
        command="build",
        raw_args=["add CSV export"],
        intent="add CSV export",
        after=[],
        explicit_as=None,
        resumable=True,
    )
    assert "add-csv" in result.task.id
    assert result.task.resolved_intent == "add CSV export"
