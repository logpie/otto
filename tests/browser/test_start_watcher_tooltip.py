"""Queue-runner control regressions.

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

The redesigned top bar now renders one queue-runner control. When it is
running, the only visible control is the stop/pause action; there is no
second disabled "Start" chip to confuse the operator.
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


def _state_with_dead_watcher_reported_running() -> dict[str, Any]:
    """State from a stale supervisor record: health says running, process is gone."""

    payload = _state_with_watcher_running()
    payload["watcher"]["alive"] = False
    payload["watcher"]["watcher"] = {"pid": 12345, "started_at": "2026-04-23T10:00:00Z"}
    payload["watcher"]["health"]["watcher_process_alive"] = False
    payload["watcher"]["health"]["lock_process_alive"] = False
    payload["watcher"]["health"]["heartbeat_age_s"] = 45
    payload["watcher"]["health"]["next_action"] = "Watcher process is missing; inspect Health."
    payload["runtime"]["supervisor"]["can_start"] = False
    payload["runtime"]["supervisor"]["can_stop"] = False
    payload["runtime"]["supervisor"]["matches_blocking_pid"] = False
    payload["runtime"]["supervisor"]["stop_target_pid"] = None
    return payload


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


def test_running_queue_renders_one_stop_control_not_duplicate_start(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Running queue state should not render both Start and Stop controls."""

    _install_route(page, "**/api/projects", _projects_payload())
    _install_route(page, "**/api/state*", _state_with_watcher_running())

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    page.wait_for_selector('[data-testid="stop-watcher-button"]', timeout=10_000)

    assert page.locator('[data-testid="start-watcher-button"]').count() == 0
    assert page.locator(".topbar .topbar-watcher").count() == 1


def test_running_queue_control_uses_queue_language(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visible topbar copy should describe the user-facing queue runner."""

    _install_route(page, "**/api/projects", _projects_payload())
    _install_route(page, "**/api/state*", _state_with_watcher_running())

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    button = page.get_by_test_id("stop-watcher-button")
    button.wait_for(state="visible", timeout=10_000)

    title = button.get_attribute("title") or ""
    text = button.text_content() or ""
    git_status = page.locator(".topbar .topbar-status").first
    git_title = git_status.get_attribute("title") or ""
    git_text = git_status.text_content() or ""

    assert "Queue running" in text
    assert "ago" not in text
    assert "Pause queue processing" in title
    assert "Last heartbeat 5s ago" in title
    assert "Watcher" not in text
    assert "watcher" not in title.lower()
    assert "Git clean" in git_text
    assert "Git working tree is clean" in git_title


def test_dead_watcher_is_surfaced_as_stale_not_running(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """If health says running but the process is dead, the topbar and focus state must say stale."""

    _install_route(page, "**/api/projects", _projects_payload())
    _install_route(page, "**/api/state*", _state_with_dead_watcher_reported_running())

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    assert page.locator('[data-testid="stop-watcher-button"]').count() == 0
    button = page.get_by_test_id("start-watcher-button")
    button.wait_for(state="visible", timeout=10_000)
    text = button.text_content() or ""
    title = button.get_attribute("title") or ""
    aria = button.get_attribute("aria-label") or ""

    assert "stale" in text.lower(), f"expected stale label; got {text!r}"
    assert "stale" in title.lower(), f"expected stale title; got {title!r}"
    assert "stale" in aria.lower(), f"expected stale aria label; got {aria!r}"
