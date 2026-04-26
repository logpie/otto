"""W3-IMPORTANT-7 regression — internal mode flags must not leak into
the user-facing event log / activity feed.

Live W3 dogfood: the Recent Activity feed showed lines like
    info  web-as-user: legacy queue mode started  09:24:00 PM
That's an internal compatibility flag bleeding into operator-facing
copy. Operators reasonably read "legacy mode" as a deprecation warning.

Fix: legacy queue records keep `display_name = task.id` (not
"<task>: legacy queue mode"). The compat detail is still surfaced via
the side-panel `compat:` line and the `compatibility_warning` field on
the record's identity, so nothing observability-wise is lost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from otto.mission_control.adapters.queue import _legacy_queue_record
from otto.queue.schema import QueueTask
from tests._helpers import init_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path)


def test_legacy_queue_record_display_name_omits_internal_flag(repo: Path) -> None:
    """display_name → task.id only. The "legacy queue mode" string must
    not appear in display_name (which is what the activity feed renders
    as `<display_name> started`)."""
    task = QueueTask(
        id="my-task",
        command_argv=["build", "do thing"],
        resolved_intent="do thing",
        added_at="2026-04-23T00:00:00Z",
    )
    record = _legacy_queue_record(
        repo, task, {"status": "queued"}, datetime.now(timezone.utc)
    )

    assert record.display_name == "my-task"
    assert "legacy queue mode" not in record.display_name
    assert "legacy" not in record.display_name


def test_legacy_queue_record_keeps_compatibility_warning_in_identity(repo: Path) -> None:
    """The compatibility flag must still be tracked internally — the side
    panel and adapters depend on it. Only the operator-facing display
    name is cleaned up."""
    task = QueueTask(
        id="my-task",
        command_argv=["build", "do thing"],
        resolved_intent="do thing",
        added_at="2026-04-23T00:00:00Z",
    )
    record = _legacy_queue_record(
        repo, task, {"status": "queued"}, datetime.now(timezone.utc)
    )
    assert record.identity.get("compatibility_warning") == "legacy queue mode"


def test_event_message_built_from_display_name_is_clean(repo: Path) -> None:
    """Mirror service.py's event-message construction:
        message = f"{record.display_name or record.command or 'run'} started"
    The composed message must read like a user event, not an internal flag."""
    task = QueueTask(
        id="web-as-user",
        command_argv=["improve", "bugs", "edge cases"],
        resolved_intent="edge cases",
        added_at="2026-04-23T00:00:00Z",
    )
    record = _legacy_queue_record(
        repo, task, {"status": "queued"}, datetime.now(timezone.utc)
    )

    message = f"{record.display_name or record.command or 'run'} started"
    # The W3 leakage was exactly: "web-as-user: legacy queue mode started"
    assert "legacy queue mode" not in message, (
        f"event message leaks internal flag: {message!r}"
    )
    assert message == "web-as-user started", message
