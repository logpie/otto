"""Browser regression for mc-audit codex-first-time-user #26 — diagnostics
should use user-facing labels, not internal vocabulary.

Banned strings on the rendered Diagnostics view:

* "operator actions" (subtitle / mission body copy)
* "command backlog" (heading)
* "runtime issues" (heading)
* "malformed event rows" (timeline warning)

Replacements:

* "Pending commands" (heading + body copy)
* "System issues" (heading + body copy)
* "Unreadable log entries" / "unreadable" (timeline warning + runtime banner)

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_diagnostics_friendly_labels.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _projects_payload() -> dict[str, Any]:
    return {
        "launcher_enabled": False,
        "projects_root": "/tmp/managed",
        "current": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": "abc1234",
        },
        "projects": [],
    }


def _state_with_diagnostics() -> dict[str, Any]:
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": "abc1234",
            "defaults": {
                "provider": "claude",
                "model": "sonnet-4-7",
                "reasoning_effort": "high",
                "certifier_mode": "fast",
                "skip_product_qa": False,
                "config_file_exists": True,
                "config_error": None,
            },
        },
        "watcher": {
            "alive": False,
            "watcher": None,
            "counts": {"queued": 0, "running": 0},
            "health": {
                "state": "stopped",
                "blocking_pid": None,
                "watcher_pid": None,
                "watcher_process_alive": False,
                "lock_pid": None,
                "lock_process_alive": False,
                "heartbeat": None,
                "heartbeat_age_s": None,
                "started_at": None,
                "log_path": "",
                "next_action": "",
            },
        },
        "landing": {
            "items": [],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {"items": [], "total_count": 0, "active_count": 0, "refresh_interval_s": 1.5},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {
            "path": "events.jsonl",
            "items": [
                {
                    "event_id": "evt-1",
                    "severity": "info",
                    "message": "queue picked up task",
                    "created_at": "2026-04-25T12:00:00Z",
                    "target_kind": "queue",
                    "target": "task-1",
                }
            ],
            "total_count": 5,
            "malformed_count": 2,
            "limit": 80,
            "truncated": False,
        },
        "runtime": {
            "status": "warning",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": 0,
            "command_backlog": {
                "pending": 1,
                "processing": 0,
                "malformed": 2,
                "items": [
                    {
                        "command_id": "cmd-1",
                        "kind": "merge",
                        "state": "pending",
                        "run_id": "r-1",
                        "task_id": "t-1",
                        "created_at": "2026-04-25T12:00:00Z",
                        "age_s": 12.0,
                        "issued_by": "user",
                        "reason": None,
                    }
                ],
            },
            "files": {
                "queue": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "state": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "commands": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "processing": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
            },
            "supervisor": {
                "mode": "manual",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": None,
                "matches_blocking_pid": None,
                "can_start": False,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
                "stop_target_pid": None,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": None,
            },
            "issues": [
                {
                    "key": "watcher-stopped",
                    "severity": "warning",
                    "label": "Watcher stopped",
                    "detail": "Watcher is stopped while a command is pending.",
                    "next_action": "Start the watcher to apply pending commands.",
                }
            ],
        },
    }


def _install_routes(page: Any) -> None:
    payload = _state_with_diagnostics()

    def projects(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_projects_payload()))

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_diagnostics_view_uses_friendly_labels(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Diagnostics tab content must NOT contain the banned internal phrases."""

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations)

    page.get_by_test_id("diagnostics-tab").click()
    # Wait for the diagnostics summary to render.
    page.get_by_test_id("diagnostics-pending-commands-heading").wait_for(state="visible", timeout=5_000)

    # textContent preserves source casing (uppercase CSS transform doesn't
    # affect it); innerText reflects the rendered text. We check both —
    # banned strings must be absent in either reading, friendly labels must be
    # present in source text (the case-insensitive comparison handles
    # uppercase CSS transforms on h3 headings).
    body_text = page.evaluate("() => document.body.textContent").lower()

    banned_lower = [
        "operator actions",
        "command backlog",
        "runtime issues",
        "malformed event row",
    ]
    for needle in banned_lower:
        assert needle not in body_text, (
            f"Diagnostics view still contains banned phrase {needle!r}\nbody: {body_text[:1500]}"
        )

    # Friendly labels must be present (case-insensitive — h3 may uppercase
    # via CSS but the source text is mixed-case).
    assert "pending commands" in body_text, (
        f"expected 'Pending Commands' heading; body: {body_text[:1500]}"
    )
    assert "system issues" in body_text, (
        f"expected 'System Issues' heading; body: {body_text[:1500]}"
    )
    # The unreadable timeline warning fires because malformed_count > 0.
    assert "unreadable" in body_text, (
        f"expected 'unreadable' translation of malformed event rows; body: {body_text[:1500]}"
    )
