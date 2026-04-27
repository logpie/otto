"""Browser regressions for queue-runner controls.

Source: live W11 dogfood — after a silently-rejected enqueue (W11-CRITICAL-1)
the user clicked Start watcher, but the button stayed disabled (no queued
work) with no feedback. The fix in ``otto/web/client/src/App.tsx``:

* The MissionFocus start-watcher button reads its title from the
  supervisor's ``start_blocked_reason`` / ``next_action`` so the
  disabled state is self-explanatory.
* ``runWatcherAction`` wraps the POST in try/catch and emits an error
  toast on rejection so a server-side failure (or transient network
  error) never silently disappears.

See ``docs/mc-audit/live-findings.md`` (search "W11-IMPORTANT-3").

Invariants pinned by this test:

1. When the queue runner is already running, the top bar renders the single
   pause/stop control instead of a duplicate disabled Start control.
2. When the watcher start POST fails with 4xx/5xx, an error toast appears
   (``data-testid="toast"`` rendered with class containing "error").

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_start_watcher_button_feedback.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _base_state() -> dict[str, Any]:
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": "abc1234",
            "defaults": {
                "provider": "claude",
                "model": None,
                "reasoning_effort": None,
                "certifier_mode": "fast",
                "skip_product_qa": False,
                "config_file_exists": False,
                "config_error": None,
            },
        },
        "watcher": {
            "alive": False,
            "watcher": None,
            "counts": {"queued": 0, "running": 0, "done": 0},
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
        "live": {
            "items": [],
            "total_count": 0,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
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
                "mode": "stopped",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": None,
                "matches_blocking_pid": False,
                "can_start": True,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_target_pid": None,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": None,
            },
            "issues": [],
        },
    }


def _install_projects_route(page: Any) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "launcher_enabled": False,
                "projects_root": "",
                "current": None,
                "projects": [],
            }),
        )

    page.route("**/api/projects", handler)


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/state*", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_start_watcher_failure_shows_error_toast(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A failed POST /api/watcher/start must surface an error toast.

    Reproduces W11-IMPORTANT-3 — historically the rejection went through
    `void runWatcherAction("start")` and was swallowed as an unhandled
    promise rejection.
    """

    payload = _base_state()
    # Make the watcher controls button startable so we can click it.
    payload["watcher"]["counts"]["queued"] = 1
    payload["runtime"]["supervisor"]["can_start"] = True

    _install_projects_route(page)
    _install_state_route(page, payload)

    # Reject the start POST.
    def fail_start(route: Any) -> None:
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"detail": "watcher already running"}),
        )

    page.route("**/api/watcher/start", fail_start)

    _hydrate(mc_backend, page, disable_animations)

    # Sidebar start-watcher button.
    btn = page.get_by_test_id("start-watcher-button")
    btn.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=start-watcher-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    btn.click()

    # An error toast must appear. Toasts use id="toast" and a class like
    # `visible toast-error` for the error severity.
    page.wait_for_selector("#toast", timeout=5_000)
    toast_locator = page.locator("#toast")
    classes = toast_locator.get_attribute("class") or ""
    text = toast_locator.text_content() or ""
    assert "toast-error" in classes, (
        f"expected an error-severity toast (class~='toast-error'), got class={classes!r}"
    )
    assert "watcher" in text.lower(), (
        f"expected toast mentioning watcher failure, got: {text!r}"
    )


def test_running_queue_uses_single_pause_control(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """When the queue runner is active, show one pause control."""

    payload = _base_state()
    payload["watcher"]["counts"]["queued"] = 1
    payload["runtime"]["supervisor"]["can_start"] = False
    payload["runtime"]["supervisor"]["start_blocked_reason"] = (
        "Watcher is already running on this project."
    )
    payload["watcher"]["alive"] = True
    payload["watcher"]["health"]["state"] = "running"
    payload["watcher"]["health"]["next_action"] = (
        "Watcher is already running on this project."
    )

    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    pause_btn = page.get_by_test_id("stop-watcher-button")
    pause_btn.wait_for(state="visible", timeout=5_000)
    title = pause_btn.get_attribute("title") or ""
    text = pause_btn.text_content() or ""
    assert page.locator('[data-testid="start-watcher-button"]').count() == 0
    assert "Queue running" in text
    assert "Pause queue processing" in title
