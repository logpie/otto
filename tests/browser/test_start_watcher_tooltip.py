"""W3-IMPORTANT-6 regression — Start watcher tooltip stays consistent
with its own action when disabled.

Live W3 dogfood: with the watcher already running, the sidebar showed
two buttons:
  - Start watcher (disabled), title="Stop watcher to pause queue dispatch."
  - Stop watcher (enabled), title="Stop watcher to pause queue dispatch."

The disabled Start button's tooltip described the OPPOSITE action. A
human (or automation) reading "Start watcher → Stop watcher to pause…"
reasonably concludes the disabled control is the one to enact.

Fix: Start watcher gets a Start-specific tooltip — "Watcher already
running." when the watcher is alive — instead of falling back to the
shared next_action that was authored for the Stop button.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _state_with_watcher_running() -> dict[str, Any]:
    """/api/state payload where the watcher is alive — exactly the live W3
    scenario where the Start button is disabled with a contradictory tooltip.
    """
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
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
            "alive": True,
            "watcher": {"pid": 12345, "started_at": "2026-04-23T10:00:00Z"},
            "counts": {"queued": 1, "running": 0},
            "health": {
                "state": "running",
                "blocking_pid": 12345,
                "watcher_pid": 12345,
                "watcher_process_alive": True,
                "lock_pid": 12345,
                "lock_process_alive": True,
                "heartbeat": "2026-04-25T11:59:55Z",
                "heartbeat_age_s": 5,
                "started_at": "2026-04-23T10:00:00Z",
                "log_path": "",
                # The shared next_action is authored for the *Stop* control.
                # The Start button must NOT echo this tooltip when disabled.
                "next_action": "Stop watcher to pause queue dispatch.",
            },
        },
        "landing": {
            "items": [],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
            "collisions": [],
        },
        "live": {"items": [], "total_count": 0, "active_count": 0, "refresh_interval_s": 1.5},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 80, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 1,
            "state_tasks": 1,
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
                "supervised_pid": 12345,
                "matches_blocking_pid": True,
                # Cannot start because watcher is already running.
                "can_start": False,
                "can_stop": True,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
                "stop_target_pid": 12345,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": None,
            },
            "issues": [],
        },
    }


def _projects_payload() -> dict[str, Any]:
    return {
        "launcher_enabled": False,
        "projects_root": "/tmp/managed",
        "current": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
            "dirty": False,
            "head_sha": "abc1234",
        },
        "projects": [],
    }


def _install_route(page: Any, url_glob: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload)

    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route(url_glob, handler)


def test_start_watcher_disabled_tooltip_does_not_say_stop(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The contradictory "Stop watcher to pause queue dispatch." MUST NOT
    appear as the Start watcher button's tooltip when the watcher is
    already running."""
    _install_route(page, "**/api/projects", _projects_payload())
    _install_route(page, "**/api/state*", _state_with_watcher_running())

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    # Wait for the Start watcher button itself to render.
    page.wait_for_selector('[data-testid="start-watcher-button"]', timeout=10_000)

    title = page.evaluate(
        """() => document.querySelector('[data-testid=\"start-watcher-button\"]')?.getAttribute('title')"""
    )
    disabled = page.evaluate(
        """() => document.querySelector('[data-testid=\"start-watcher-button\"]')?.disabled"""
    )

    assert disabled is True, (
        f"Start watcher button must be disabled when watcher is running. "
        f"title={title!r}"
    )
    assert title is not None, "Start watcher button missing"
    assert "Stop watcher" not in title, (
        f"Start watcher tooltip must not describe the Stop action. Got: {title!r}"
    )


def test_start_watcher_disabled_tooltip_says_already_running(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Positive form: the Start watcher tooltip should explain WHY it's
    disabled (watcher already running), so operators don't waste time
    trying to click it."""
    _install_route(page, "**/api/projects", _projects_payload())
    _install_route(page, "**/api/state*", _state_with_watcher_running())

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    page.wait_for_selector('[data-testid="start-watcher-button"]', timeout=10_000)

    title = page.evaluate(
        """() => document.querySelector('[data-testid=\"start-watcher-button\"]')?.getAttribute('title') || ''"""
    )

    # Accept any of these phrasings — the contract is "tooltip explains
    # the disabled state from the Start button's perspective".
    assert any(phrase in title.lower() for phrase in ("already running", "already started", "running")), (
        f"Start watcher tooltip should indicate the watcher is already "
        f"running, got: {title!r}"
    )
