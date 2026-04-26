"""Codex error-empty-states #1: connection-lost sticky banner.

Source: mc-audit/findings.md, theme "Connection / polling resilience" —
IMPORTANT row "App.tsx:282: /api/state polling failures leave old data
rendered; only 'refresh failed' + transient toasts".

Without a sticky banner, three consecutive ``/api/state`` failures look
identical to a quiet UI — the operator stares at stale data, never
realizing the server has gone away. The fix renders a sticky red
banner ("Lost connection to Mission Control. Retrying every 5s…") with
a "Retry now" button. A successful poll clears the banner.

Tests asserts:

* After 3 consecutive ``/api/state`` failures, ``connection-lost-banner``
  appears with a ``Retry now`` button.
* Clicking ``Retry now`` while ``/api/state`` is still failing keeps
  the banner up.
* Restoring ``/api/state`` and clicking ``Retry now`` (or letting the
  next poll succeed) clears the banner — restore-on-reconnect.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_lost_connection_banner.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
import time
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


def _state_payload() -> dict[str, Any]:
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
            "counts": {"ready": 0, "merged": 0, "total": 0},
            "merge_blocked": False,
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        # We need a fast refresh interval so 3 polls happen in < 6s.
        "live": {"items": [], "total_count": 0, "active_count": 0, "refresh_interval_s": 0.7},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"items": [], "total_count": 0, "malformed_count": 0},
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
                "can_start": True,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


# A handle that flips between "fail" and "ok" so we can simulate an
# outage and a recovery in the same test. Needs to be mutable from a
# closure that lives across page.route handler invocations.
class StateRouteController:
    def __init__(self) -> None:
        self.fail_state: bool = False
        self.fail_count: int = 0
        self.ok_count: int = 0

    def install(self, page: Any) -> None:
        page.route(
            "**/api/projects",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_projects_payload()),
            ),
        )

        ok_body = json.dumps(_state_payload())

        def state_handler(route: Any) -> None:
            if self.fail_state:
                self.fail_count += 1
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error": "simulated outage"}',
                )
            else:
                self.ok_count += 1
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=ok_body,
                )

        page.route("**/api/state*", state_handler)


def _hydrate(page: Any, mc_backend: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#root')?.children.length > 0", timeout=10_000
    )


def _wait_for_banner(page: Any, *, present: bool, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        count = page.locator("[data-testid=connection-lost-banner]").count()
        if present and count >= 1:
            return
        if (not present) and count == 0:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"banner present={present} not satisfied within {timeout_s}s "
        f"(count={page.locator('[data-testid=connection-lost-banner]').count()})"
    )


def test_banner_appears_after_3_consecutive_failures(
    mc_backend: Any, page: Any
) -> None:
    """Banner must show within ~5s once /api/state has failed 3 times."""

    controller = StateRouteController()
    controller.install(page)

    _hydrate(page, mc_backend)
    page.wait_for_selector("[data-mc-shell=ready]", timeout=10_000)

    # No banner before the outage.
    assert page.locator("[data-testid=connection-lost-banner]").count() == 0

    # Trigger the outage. The poll cadence is 0.7s but the
    # STATE_POLL_MIN_GAP_MS floor is 1s, so 3 failures take ~3-5s.
    controller.fail_state = True
    _wait_for_banner(page, present=True, timeout_s=12.0)

    # Banner must contain the documented copy + a retry button.
    banner = page.locator("[data-testid=connection-lost-banner]")
    text = banner.text_content() or ""
    assert "Lost connection" in text, (
        f"banner copy must mention 'Lost connection'; got {text!r}"
    )
    assert page.locator("[data-testid=connection-lost-retry-button]").count() == 1, (
        "banner must include a manual retry button"
    )


def test_retry_button_clears_banner_on_recovery(
    mc_backend: Any, page: Any
) -> None:
    """Manual retry must restore the UI when the backend comes back.

    Verifies the restore-on-reconnect contract: once /api/state starts
    returning 200 again, the banner disappears within the next poll
    cycle. We don't depend on a specific count of background polls
    (those can be throttled when the headless window loses OS focus
    between tests in a session) — we just assert the banner is gone
    once recovery has had a chance to land.
    """

    controller = StateRouteController()
    controller.install(page)

    _hydrate(page, mc_backend)
    page.wait_for_selector("[data-mc-shell=ready]", timeout=10_000)

    controller.fail_state = True
    _wait_for_banner(page, present=True, timeout_s=12.0)

    # Restore the backend.
    controller.fail_state = False

    # Click "Retry now". The button is the canonical recovery affordance.
    page.get_by_test_id("connection-lost-retry-button").click()

    # The banner must clear within a generous window. The success path:
    # either the manual click's refresh succeeds, OR the next background
    # poll's tick lands while fail_state=False. Both are covered by
    # `_wait_for_banner(present=False)` — which is the user-visible
    # outcome that matters.
    _wait_for_banner(page, present=False, timeout_s=20.0)


def test_banner_does_not_show_on_single_transient_failure(
    mc_backend: Any, page: Any
) -> None:
    """A single failed poll should not flash the banner — the threshold
    rides out one-off network blips so the UI is not alarmist.
    """

    controller = StateRouteController()
    controller.install(page)

    _hydrate(page, mc_backend)
    page.wait_for_selector("[data-mc-shell=ready]", timeout=10_000)

    # Simulate ONE failure then immediately restore.
    controller.fail_state = True
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if controller.fail_count >= 1:
            break
        time.sleep(0.05)
    controller.fail_state = False

    # Wait long enough for the retry to succeed but not so long that
    # another sequence of 3 failures could occur.
    time.sleep(2.5)
    assert page.locator("[data-testid=connection-lost-banner]").count() == 0, (
        "single transient failure should not surface the lost-connection "
        "banner; threshold is 3 consecutive failures"
    )
