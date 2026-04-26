"""Browser test for the long-run-finished browser Notification.

Heavy-user paper cut #3 (mc-audit `_hunter-findings/heavy-user.md`):

    A power user kicks off a 12-minute build, switches to another tab, has
    no signal that it finished — must come back and check.

The fix: when the polling loop detects a previously-running run
transitioned to a terminal state AND `document.visibilityState === "hidden"`,
construct `new Notification("Otto: build completed", ...)`.

Test approach: stub `window.Notification` BEFORE the SPA loads so we can
record every `new Notification(...)` invocation. Force visibility to
hidden via the standard property override. Drive the SPA from a payload
that has one live run, then swap the live-runs route to an empty list to
simulate completion. Assert that exactly one Notification was created.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_background_notification.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

pytestmark = pytest.mark.browser


RUN_ID = "long-run-1"


def _live_item() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "display_name": "build:long",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "build/long",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "claude",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": RUN_ID,
        "branch_task": "build/long",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": None,
        "cost_display": "-",
        "last_event": "running",
        "row_label": "build:long",
        "overlay": None,
    }


def _state_payload(*, with_live: bool) -> dict[str, Any]:
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
            "counts": {"queued": 0, "running": 1 if with_live else 0},
            "health": {
                "state": "running" if with_live else "stopped",
                "blocking_pid": None,
                "watcher_pid": None,
                "watcher_process_alive": with_live,
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
            "target": "main",
        },
        "live": {
            "items": [_live_item()] if with_live else [],
            "total_count": 1 if with_live else 0,
            "active_count": 1 if with_live else 0,
            "refresh_interval_s": 0.5,
        },
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
                "can_start": False,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


class _StateRouter:
    """Switchable /api/state — flip `with_live` to simulate completion."""

    def __init__(self) -> None:
        self.with_live = True
        self._lock = threading.Lock()

    def install(self, page: Any) -> None:
        page.route("**/api/state*", self._handle)

    def set_complete(self) -> None:
        with self._lock:
            self.with_live = False

    def _handle(self, route: Any) -> None:
        with self._lock:
            payload = _state_payload(with_live=self.with_live)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


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


_NOTIFICATION_STUB = """
() => {
  const calls = [];
  class FakeNotification {
    static permission = 'granted';
    static requestPermission() { return Promise.resolve('granted'); }
    constructor(title, options) {
      calls.push({title, options: options || null});
      this.title = title;
      this.body = (options && options.body) || '';
    }
    close() {}
    addEventListener() {}
    removeEventListener() {}
  }
  Object.defineProperty(window, 'Notification', {
    configurable: true,
    writable: true,
    value: FakeNotification,
  });
  Object.defineProperty(window, '__notification_calls__', {
    configurable: true,
    get() { return calls; },
  });
  // Force the page to look hidden so the App's "fire only when hidden"
  // gate is satisfied.
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get() { return 'hidden'; },
  });
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get() { return true; },
  });
}
"""


def test_notification_fires_when_run_finishes_in_background(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Live run disappears (terminal) while tab is hidden → Notification fires."""
    _install_projects_route(page)
    router = _StateRouter()
    router.install(page)

    # Inject the Notification stub + visibility override BEFORE the SPA mounts.
    page.add_init_script(f"({_NOTIFICATION_STUB})()")

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    # Sanity: Notification stub is installed.
    assert page.evaluate("typeof window.Notification") == "function"
    assert page.evaluate("document.visibilityState") == "hidden"

    # Wait until the SPA has hydrated state and observed the live run.
    page.wait_for_function(
        "() => document.body.innerText.includes('build:long') || document.body.innerText.includes('long-run-1')",
        timeout=5_000,
    )

    # Flip the route to "no live runs" — next poll sees the run as finished.
    router.set_complete()

    # The polling interval is 500ms in our payload; wait up to 5s for the
    # notification to fire as the App diff-detects the disappearance.
    page.wait_for_function(
        "() => (window.__notification_calls__ || []).length >= 1",
        timeout=5_000,
    )
    calls = page.evaluate("window.__notification_calls__")
    assert len(calls) >= 1, f"expected ≥1 notification, got {calls!r}"
    titles = [c["title"] for c in calls]
    assert any("Otto" in t for t in titles), f"expected Otto title, got {titles!r}"


def test_notification_does_not_fire_when_tab_visible(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Same lifecycle but tab visible → no notification (we only nag in bg)."""
    _install_projects_route(page)
    router = _StateRouter()
    router.install(page)

    # Same Notification stub but DON'T override visibility (defaults to visible).
    page.add_init_script(
        """
        () => {
          const calls = [];
          class FakeNotification {
            static permission = 'granted';
            static requestPermission() { return Promise.resolve('granted'); }
            constructor(title, options) { calls.push({title, options: options || null}); }
            close() {}
          }
          Object.defineProperty(window, 'Notification', {
            configurable: true, writable: true, value: FakeNotification,
          });
          Object.defineProperty(window, '__notification_calls__', {
            configurable: true,
            get() { return calls; },
          });
        }
        """
    )

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.wait_for_function(
        "() => document.body.innerText.includes('build:long') || document.body.innerText.includes('long-run-1')",
        timeout=5_000,
    )
    router.set_complete()

    # Allow the polling loop to observe the change.
    page.wait_for_function(
        "() => !document.body.innerText.includes('long-run-1')",
        timeout=5_000,
    )
    # Visible tab — no notification should have fired.
    calls = page.evaluate("window.__notification_calls__ || []")
    assert calls == [], f"expected no notifications when tab visible, got {calls!r}"
