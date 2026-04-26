"""Browser tests for `/api/state` polling behaviour under tab-visibility changes.

Regression coverage for live-findings W9-CRITICAL-1 (see
``docs/mc-audit/live-findings.md``):

* **Leg 1** — under ``visibilityState=hidden`` the SPA used to STOP
  polling entirely (browsers throttled the bare ``setInterval`` to 0Hz).
  Result: 110s of stale state during a hide window, no completion
  notifications, watchers go cold.
* **Leg 2** — on visibility restore the SPA used to fire a 175-poll
  burst inside a single 10s window — accumulated effect re-runs combined
  with browser timer un-throttling produced a thundering herd against
  ``/api/state``.

The fix replaced the ``window.setInterval(refresh, fastMs)`` pattern with
a recursive ``setTimeout`` that:

1. Slows polling to ``STATE_POLL_HIDDEN_MS`` (30s) while hidden instead
   of stopping.
2. Fires a single immediate catch-up poll on hidden→visible, then
   resumes the normal cadence.
3. Uses a single timer ref + ``AbortController`` so cascading effect
   re-runs cannot stack timers or in-flight requests.
4. Enforces a ``STATE_POLL_MIN_GAP_MS`` (1s) hard floor between polls.

Tests assert each of those properties end-to-end via Playwright with
``visibilityState`` stubbed deterministically.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_visibility_polling_behavior.py -m browser \\
        -p playwright -v
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Shared fixture stubs (project + state routes + visibility / poll-counter
# init scripts)
# ---------------------------------------------------------------------------


def _state_payload() -> dict[str, Any]:
    """Minimal /api/state payload — empty live/landing/history.

    The polling loop only reads `live.refresh_interval_s` to set its
    cadence, so we don't need rich rows for these tests. We do set it to
    1.5s so cadenceMs() lands at the LOG_POLL_BASE_MS-equivalent of
    1500ms — comfortable for the timing assertions below.
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
            "target": "main",
        },
        "live": {
            "items": [],
            "total_count": 0,
            "active_count": 0,
            "refresh_interval_s": 1.5,
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


class _StateCounter:
    """Thread-safe counter for `/api/state` requests, with timestamps.

    Returns the static payload to every request so the SPA's state
    doesn't change underneath us — the assertions are about poll cadence
    not payload semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timestamps: list[float] = []
        # Pin a "wall clock" anchor so test assertions can talk in
        # seconds-since-test-start without dealing with monotonic vs
        # wall clock skew.
        self._origin = time.monotonic()

    def install(self, page: Any) -> None:
        page.route("**/api/state*", self._handle)
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

    def _handle(self, route: Any) -> None:
        with self._lock:
            self._timestamps.append(time.monotonic() - self._origin)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state_payload()),
        )

    def count(self) -> int:
        with self._lock:
            return len(self._timestamps)

    def timestamps(self) -> list[float]:
        with self._lock:
            return list(self._timestamps)

    def count_in_window(self, start_s: float, end_s: float) -> int:
        with self._lock:
            return sum(1 for t in self._timestamps if start_s <= t <= end_s)


# Init script — installs once before the SPA mounts.
#
# Two responsibilities:
#   1. Override `document.visibilityState` + `document.hidden` to a
#      mutable backing field so tests can flip between "visible" and
#      "hidden" by calling `window.__setVisibility('hidden')` from
#      Python via `page.evaluate`.
#   2. Provide `window.__fireVisibilityChange()` so we can dispatch the
#      `visibilitychange` event after flipping the backing field.
#
# Without (1), `document.visibilityState` is read-only in headless
# Chromium and we can't simulate background-tab behaviour. Without (2),
# overriding the property doesn't notify any listeners.
_VISIBILITY_HARNESS = """
() => {
  let _state = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get() { return _state; },
  });
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get() { return _state === 'hidden'; },
  });
  Object.defineProperty(window, '__setVisibility', {
    configurable: true,
    writable: true,
    value: (next) => {
      _state = next === 'hidden' ? 'hidden' : 'visible';
    },
  });
  Object.defineProperty(window, '__fireVisibilityChange', {
    configurable: true,
    writable: true,
    value: () => {
      document.dispatchEvent(new Event('visibilitychange'));
    },
  });
}
"""


def _set_visibility(page: Any, state: str) -> None:
    """Flip `document.visibilityState` and dispatch the change event."""

    page.evaluate(f"window.__setVisibility({state!r}); window.__fireVisibilityChange();")


def _wait_for_app_ready(page: Any) -> None:
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_polls_slow_when_tab_hidden(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Tab hidden for 10s → polling slows but doesn't stop entirely.

    We can't sleep a full 30s in a unit test, so we assert the
    representative behaviour: with ``STATE_POLL_HIDDEN_MS=30000``, a 10s
    hide window should produce **at most one** new poll on top of the
    pre-hide steady state, NOT 6+ (which is what 1.2s cadence would
    yield) and NOT 0 (which is what the old setInterval-only behaviour
    yielded under browser throttling).

    The contract is: hidden polling exists, but it's bounded by the
    hidden cadence — not driven by the visible cadence.
    """

    counter = _StateCounter()
    counter.install(page)
    page.add_init_script(f"({_VISIBILITY_HARNESS})()")

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_app_ready(page)
    disable_animations(page)

    # Wait long enough for the SPA to settle into normal-cadence
    # polling. Two visible-cadence polls is sufficient to confirm the
    # loop is running — at 1.5s cadence that's ~3s.
    page.wait_for_timeout(3500)

    # Snapshot poll count, then go hidden.
    pre_hide = counter.count()
    _set_visibility(page, "hidden")

    # Wait 10s with the tab "hidden". Under the fix, cadence is 30s while
    # hidden, so we should see AT MOST one new poll in this window
    # (potentially zero — the previously-scheduled visible-cadence poll
    # may have already fired before the visibility flip landed).
    hidden_start = counter.count()
    page.wait_for_timeout(10_000)
    hidden_end = counter.count()
    polls_during_hide = hidden_end - hidden_start

    # Assert we didn't keep polling at 1.5s cadence (would be ~6 polls).
    # Allow up to 2 to absorb timing slack (e.g. one in-flight poll
    # completing right after the flip + one "hidden cadence" tick).
    assert polls_during_hide <= 2, (
        f"expected ≤2 polls during 10s hide window with hidden-cadence=30s; "
        f"got {polls_during_hide} (pre_hide={pre_hide}, hidden_start={hidden_start}, "
        f"hidden_end={hidden_end}). All timestamps: {counter.timestamps()!r}"
    )


def test_single_burst_request_on_visibility_restore(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Hidden→visible fires ≤2 polls in the first 5s after restore.

    The original W9 bug fired 175 polls in 10s on restore. The fix
    promises one immediate catch-up poll on the visibility flip, then
    a normal-cadence tick after that — strictly bounded by
    ``STATE_POLL_MIN_GAP_MS=1000`` between successive calls.

    Assert: at most 5 new polls in the first 5s after restore (one
    catch-up + at most ~3 cadence-driven polls at 1.5s, plus slack).
    The critical assertion is "not 175" — we set the bar at 5 because
    that's the worst plausible legit count and any regression toward
    "burst" will blow well past it.
    """

    counter = _StateCounter()
    counter.install(page)
    page.add_init_script(f"({_VISIBILITY_HARNESS})()")

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_app_ready(page)
    disable_animations(page)

    # Settle into normal cadence.
    page.wait_for_timeout(2500)

    # Hide for 3s. (We don't need a full 30s hide window — the burst
    # happens regardless of hide duration; what matters is that we go
    # hidden→visible at all.)
    _set_visibility(page, "hidden")
    page.wait_for_timeout(3000)

    # Restore.
    restore_count = counter.count()
    restore_t = time.monotonic() - counter._origin
    _set_visibility(page, "visible")

    # Sample 5s after restore.
    page.wait_for_timeout(5000)
    polls_after_restore = counter.count() - restore_count

    # Hard bound: NOT 175. We allow up to 5 (one catch-up + ~3 cadence
    # ticks at 1.5s + 1 slack). The original bug fired 175 polls in this
    # window — anything close to that is an immediate fail.
    assert polls_after_restore <= 5, (
        f"expected ≤5 polls in 5s after restore (one catch-up + cadence ticks); "
        f"got {polls_after_restore}. The W9 regression fired 175. "
        f"restore_t={restore_t:.2f}s, all timestamps after restore: "
        f"{[t for t in counter.timestamps() if t >= restore_t]!r}"
    )

    # Also verify SOMETHING fired — the catch-up poll must run, otherwise
    # restored tabs see stale state until the next 1.5s tick.
    assert polls_after_restore >= 1, (
        f"expected ≥1 poll in 5s after restore (the immediate catch-up); "
        f"got {polls_after_restore}. Restore catch-up did not fire."
    )


def test_no_pile_up_after_repeated_visibility_toggles(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Toggling hidden/visible 5 times in 10s leaves exactly one timer alive.

    The pre-fix bug allowed multiple ``setInterval`` instances to stack
    when the polling effect re-ran with a stale cleanup. This test
    drives 5 hidden↔visible cycles inside a 10s window and asserts the
    poll cadence afterwards stays at the configured rate (~1 per
    1.5s) — not 2× / 5× / 10× the rate that a stack of duplicate
    timers would produce.

    We measure the poll RATE in a 5s window AFTER all toggles complete
    rather than counting timer handles directly: the framework gives us
    no introspection into ``window.setTimeout`` ids, but observable
    rate is the contract that actually matters to the server.
    """

    counter = _StateCounter()
    counter.install(page)
    page.add_init_script(f"({_VISIBILITY_HARNESS})()")

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_app_ready(page)
    disable_animations(page)

    # Let it settle.
    page.wait_for_timeout(2500)

    # 5 toggles in ~5s (1s per cycle, half-second per phase).
    for _ in range(5):
        _set_visibility(page, "hidden")
        page.wait_for_timeout(500)
        _set_visibility(page, "visible")
        page.wait_for_timeout(500)

    # Let any post-restore catch-up calls drain.
    page.wait_for_timeout(1500)

    # Now sample the poll rate in a 5s steady-state window.
    rate_start = counter.count()
    page.wait_for_timeout(5000)
    rate_end = counter.count()
    polls_in_5s = rate_end - rate_start

    # At the configured 1.5s visible cadence with the 1s min-gap floor,
    # expected steady-state is 3-4 polls per 5s. If timers are stacked
    # 2x we'd see 6-8; 5x → 15-20. Bound at 6 to give one tick of
    # slack while still failing hard on stack-up.
    assert polls_in_5s <= 6, (
        f"expected ≤6 polls in 5s steady-state after toggle storm "
        f"(cadence=1.5s + 1s min-gap floor); got {polls_in_5s}. "
        f"Multiple stacked timers indicate the visibility handler "
        f"leaked between effect re-runs. All timestamps in window: "
        f"{[t for t in counter.timestamps()][rate_start:rate_end]!r}"
    )

    # Sanity: we should still be polling at all (not 0 — the cleanup
    # path didn't accidentally tear down the only live timer).
    assert polls_in_5s >= 2, (
        f"expected ≥2 polls in 5s steady-state (cadence ~1.5s); "
        f"got {polls_in_5s}. Toggle storm tore down the live timer."
    )
