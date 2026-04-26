"""Browser regression for mc-audit codex-first-time-user #27 — Recent
Activity subtitle uses user-facing copy.

Banned: "queue, watcher, land, and run outcomes" (four Otto terms in one
sentence).
Replacement: "Recent job activity, approvals, merges, and errors appear
here."

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_recent_activity_copy.py \\
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


def _state() -> dict[str, Any]:
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
        "history": {
            "items": [
                {
                    "run_id": "run-1",
                    "domain": "build",
                    "run_type": "build",
                    "command": "build",
                    "status": "completed",
                    "terminal_outcome": "success",
                    "queue_task_id": "task-1",
                    "merge_id": None,
                    "branch": "feature/x",
                    "worktree": None,
                    "summary": "build",
                    "intent": "build",
                    "completed_at_display": "2026-04-25 11:30",
                    "outcome_display": "success",
                    "duration_s": 60,
                    "duration_display": "1m",
                    "cost_usd": 0.05,
                    "cost_display": "$0.05",
                    "resumable": False,
                    "adapter_key": "build",
                }
            ],
            "page": 0,
            "page_size": 25,
            "total_rows": 1,
            "total_pages": 1,
        },
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 80, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": 0,
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
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
            "issues": [],
        },
    }


def _install_routes(page: Any) -> None:
    payload = _state()

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


def test_recent_activity_subtitle_is_user_facing(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The subtitle must use the friendly copy and avoid the four-jargon sentence."""

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations)

    sub = page.get_by_test_id("activity-subtitle")
    sub.wait_for(state="visible", timeout=5_000)
    text = (sub.text_content() or "").strip()

    expected = "Recent job activity, approvals, merges, and errors appear here."
    assert text == expected, f"expected friendly copy; got {text!r}"

    # Banned legacy copy must be absent from the activity panel scope.
    panel_text = page.locator(".activity-panel").first.text_content() or ""
    assert "queue, watcher, land" not in panel_text, (
        f"activity panel still contains the legacy jargon-heavy subtitle; got {panel_text!r}"
    )
