"""Browser test for toast pause-on-hover + manual dismiss.

Cluster: mc-audit microinteractions I8 (IMPORTANT).

Problem: Toasts auto-dismiss after 3.2s. There was no way to pause a toast
to read it, and no way to dismiss it manually — operators glancing away
missed the message entirely.

Fix:
  1. ``onMouseEnter`` cancels the auto-dismiss timer so the toast stays
     visible while hovered.
  2. ``onMouseLeave`` restarts the timer so the toast resumes its
     countdown after the cursor leaves.
  3. A ``×`` close button (``data-testid=toast-close``) inside the toast
     dismisses it immediately.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_toast_pause_and_dismiss.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _state() -> dict[str, Any]:
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
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
            "target": "main",
        },
        "live": {
            "items": [],
            "total_count": 0,
            "active_count": 0,
            "refresh_interval_s": 5.0,
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
                "can_start": False,
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
    page.route(
        "**/api/projects",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "launcher_enabled": False,
                "projects_root": "",
                "current": None,
                "projects": [],
            }),
        ),
    )


def _install_state_route(page: Any) -> None:
    body = json.dumps(_state())
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _hydrate(mc_backend: Any, page: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)


def _trigger_watcher_start_error(page: Any) -> None:
    """Easiest path to a real toast: set up a queued task so Start watcher
    is enabled, stub /api/watcher/start with a 500 — the SPA's error path
    calls showToast(...). Avoids any reliance on direct DOM manipulation.

    BUT — the simpler path is just to call the SPA's exposed test hook.
    Mission Control doesn't expose one, so we go through the real API
    error route instead by installing a state with queued > 0.
    """
    raise NotImplementedError  # not used; we use the direct showToast hook below


def _emit_toast_via_button(page: Any) -> None:
    """The most reliable way to summon a real toast in tests is to click
    the Refresh button (always available), but Refresh doesn't toast on
    success. Instead we use the watcher Stop / Start error path: make the
    watcher action POST fail and the SPA renders an error toast.

    To keep this test self-contained we install a minimal queued payload
    and a failing /api/watcher/start handler.
    """

    raise NotImplementedError


# --------------------------------------------------------------------------- #
# Tests — we drive a real toast via the watcher-start failure path so the
# entire showToast → setTimeout → mouseenter/mouseleave wiring is exercised.
# --------------------------------------------------------------------------- #


def _install_watcher_start_failure(page: Any) -> dict[str, int]:
    counter: dict[str, int] = {"count": 0}

    def handler(route: Any) -> None:
        counter["count"] += 1
        route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps({
                "ok": False,
                "message": "watcher subprocess refused to start",
                "severity": "error",
            }),
        )

    page.route("**/api/watcher/start", handler)
    return counter


def _state_with_queued() -> dict[str, Any]:
    payload = _state()
    payload["watcher"]["counts"]["queued"] = 1
    payload["runtime"]["queue_tasks"] = 1
    payload["runtime"]["supervisor"]["can_start"] = True
    return payload


def _install_state_with_queued(page: Any) -> None:
    body = json.dumps(_state_with_queued())
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _summon_error_toast(page: Any) -> None:
    btn = page.get_by_test_id("start-watcher-button")
    btn.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=start-watcher-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    btn.click()
    # Wait for the toast to materialise.
    page.wait_for_selector('#toast', timeout=5_000)


def test_toast_pauses_on_hover_and_dismisses_on_leave(
    mc_backend: Any, page: Any
) -> None:
    """Hover keeps the toast visible past the 3.2s auto-dismiss window;
    leaving the toast restarts the timer so it eventually disappears."""

    _install_projects_route(page)
    _install_state_with_queued(page)
    _install_watcher_start_failure(page)

    _hydrate(mc_backend, page)
    _summon_error_toast(page)

    toast = page.locator("#toast")
    toast.wait_for(state="visible", timeout=5_000)

    # Hover the toast — pause the auto-dismiss timer. React 17+ uses
    # delegated events at the root and derives mouseenter from native
    # mouseover events with relatedTarget outside the element. Playwright's
    # hover() positions the cursor over the element which dispatches the
    # full native mouseover/mouseenter sequence the SPA actually listens
    # for.
    toast.hover()

    # Wait 4 seconds — past the normal 3.2s auto-dismiss. The toast must
    # still be visible because the timer was paused on mouseenter.
    page.wait_for_timeout(4_000)
    assert page.locator("#toast").count() == 1, (
        "toast was dismissed during hover — pause-on-hover is not working"
    )

    # Move the cursor far from the toast (top-left corner) to fire mouseout
    # / mouseleave. A subsequent body.hover() also works but the explicit
    # mouse.move avoids accidentally hovering another element that could
    # itself trigger UI side-effects (e.g. a sidebar button).
    page.mouse.move(0, 0)

    # After mouseleave we get a fresh full-duration timer (~3.2s). Wait
    # up to 5s for the toast to vanish.
    page.wait_for_function(
        "() => !document.querySelector('#toast')",
        timeout=5_000,
    )


def test_toast_close_button_dismisses_immediately(
    mc_backend: Any, page: Any
) -> None:
    """Clicking the toast's ✕ close-button dismisses it immediately,
    well before the 3.2s auto-dismiss window."""

    _install_projects_route(page)
    _install_state_with_queued(page)
    _install_watcher_start_failure(page)

    _hydrate(mc_backend, page)
    _summon_error_toast(page)

    close_btn = page.get_by_test_id("toast-close")
    close_btn.wait_for(state="visible", timeout=2_000)
    close_btn.click()

    # The toast vanishes within a few hundred ms of the click.
    page.wait_for_function(
        "() => !document.querySelector('#toast')",
        timeout=1_500,
    )


def test_toast_does_not_cover_topbar_actions(mc_backend: Any, page: Any) -> None:
    """Transient errors must not block project, queue-runner, or New job controls."""

    _install_projects_route(page)
    _install_state_with_queued(page)
    _install_watcher_start_failure(page)

    _hydrate(mc_backend, page)
    _summon_error_toast(page)

    toast_box = page.locator("#toast").bounding_box()
    actions_box = page.locator(".topbar-actions").bounding_box()
    assert toast_box is not None and actions_box is not None

    overlaps = not (
        toast_box["x"] + toast_box["width"] <= actions_box["x"]
        or actions_box["x"] + actions_box["width"] <= toast_box["x"]
        or toast_box["y"] + toast_box["height"] <= actions_box["y"]
        or actions_box["y"] + actions_box["height"] <= toast_box["y"]
    )
    assert not overlaps, f"toast must not overlap topbar actions; toast={toast_box!r}, actions={actions_box!r}"
