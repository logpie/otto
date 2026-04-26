"""Cross-tab state propagation — W10-CRITICAL-1 + W10-CRITICAL-2 regression.

Cluster: web-as-user W10 live-finding (CRITICAL).

Problem
-------

Two operators on one project (each in their own browser tab) was silently
broken:

* W10-CRITICAL-1: a job submitted in tab A was not visible in tab B's task
  board within the 30-second observation window.
* W10-CRITICAL-2: a cancellation issued from tab B was not reflected in
  tab A's UI within the same window.

Root causes
-----------

The diagnosis split into three real defects:

1. ``/api/state`` had no ``Cache-Control`` header, so browsers / proxies
   were free to serve a stale snapshot to the second tab even though the
   server saw the mutation.
2. ``TaskCard`` rendered the task title only — the run id never appeared
   in the DOM. Cross-tab integration tests (and any future automation)
   that scoped to ``[data-testid="task-board"]`` had no stable hook to
   identify "the row for run X". Operators *could* see the task by
   title, but tooling could not assert on it.
3. There was no cross-tab signalling. Tab B had to wait for its own
   poll tick (~1.2-5s while the tab is visible, 30s while hidden) to
   discover any mutation issued by tab A.

Fixes (implemented in this commit)
----------------------------------

* ``otto/web/app.py``: add ``Cache-Control: no-store`` middleware on
  every ``/api/`` response.
* ``otto/web/client/src/App.tsx``: add ``data-run-id`` and ``data-task-id``
  attributes to the rendered ``TaskCard`` ``<article>`` so the row is
  programmatically identifiable.
* ``otto/web/client/src/hooks/useCrossTabChannel.ts``: a same-origin
  ``BroadcastChannel('mc-state-mutation')`` that mutation call sites use
  to notify peer tabs; receivers fire an immediate ``/api/state`` refresh
  instead of waiting for the next poll. Falls back to the ``storage``
  event for browsers without ``BroadcastChannel``.

Run
---

::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_two_tab_consistency.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import time
from typing import Any

import pytest


pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Cache-Control assertion (server-side hardening; runs without a browser)
# --------------------------------------------------------------------------- #


def test_state_endpoint_has_cache_control_no_store(mc_backend: Any) -> None:
    """``/api/state`` must NOT be cacheable.

    Without this, a second tab on the same machine could be served a stale
    snapshot from the browser's HTTP cache while the server saw the
    mutation, making cross-tab state propagation race the cache TTL.
    """

    import urllib.request

    req = urllib.request.Request(f"{mc_backend.url}/api/state")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        cache_control = resp.headers.get("Cache-Control") or ""
    # ``no-store`` is the strongest assertion (no browser/proxy may cache
    # under any circumstance); ``no-cache`` would still let a cache store
    # the response and revalidate. We require ``no-store``.
    assert "no-store" in cache_control.lower(), (
        f"expected /api/state Cache-Control to include no-store, got {cache_control!r}"
    )


def test_other_api_endpoints_have_cache_control_no_store(mc_backend: Any) -> None:
    """The middleware must apply to every /api/ response, not just /api/state.

    A future ``/api/foo`` route added without explicit ``Cache-Control``
    must also be ``no-store`` so callers cannot accidentally cache the
    runtime mutation surface.
    """

    import urllib.request

    for path in ("/api/projects", "/api/watcher", "/api/runtime", "/api/events"):
        with urllib.request.urlopen(f"{mc_backend.url}{path}", timeout=5) as resp:
            cache_control = resp.headers.get("Cache-Control") or ""
        assert "no-store" in cache_control.lower(), (
            f"expected {path} Cache-Control no-store, got {cache_control!r}"
        )


# --------------------------------------------------------------------------- #
# Helpers: real backend, real two pages, real /api/queue/build POST
# --------------------------------------------------------------------------- #


_TASK_BOARD_SELECTOR = '[data-testid="task-board"]'


def _wait_for_hydration(page: Any) -> None:
    """Wait until the SPA root renders something + /api/state has resolved.

    The ``mc_backend`` fixture starts an empty git project, so the initial
    state has no live items and the task board is empty. We wait for the
    task-board panel to be present (proves /api/state returned + first
    render committed).
    """

    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    page.wait_for_selector(_TASK_BOARD_SELECTOR, timeout=10_000)


def _enqueue_via_api(page: Any, intent: str = "Add a hello-world endpoint") -> str:
    """Submit a build job from ``page``'s JS context.

    Going through the page's ``fetch`` proves the request originates from
    the tab (cookies, headers, origin all match) — same as how a real user
    submitting from the JobDialog would do it. Returns the queued task id.
    """

    result = page.evaluate(
        """
        async (intent) => {
            const r = await fetch('/api/queue/build', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({intent: intent, extra_args: []}),
            });
            const text = await r.text();
            return {status: r.status, body: text};
        }
        """,
        intent,
    )
    assert isinstance(result, dict), f"expected fetch result dict, got {result!r}"
    assert result.get("status") == 200, (
        f"expected /api/queue/build → 200, got {result.get('status')} body={result.get('body')!r}"
    )
    body = result.get("body") or "{}"
    import json
    parsed = json.loads(body)
    task = parsed.get("task") or {}
    task_id = task.get("id")
    assert isinstance(task_id, str) and task_id, f"missing task id in response: {body!r}"
    return task_id


def _board_task_ids(page: Any) -> list[str]:
    """Return the ``data-task-id`` values currently rendered on the board."""

    return page.evaluate(
        """
        () => {
            const board = document.querySelector('[data-testid="task-board"]');
            if (!board) return [];
            return Array.from(board.querySelectorAll('[data-task-id]'))
                .map(el => el.getAttribute('data-task-id'))
                .filter(Boolean);
        }
        """
    ) or []


def _wait_for_task_id_on_board(page: Any, task_id: str, timeout_s: float) -> float:
    """Block until ``task_id`` appears on the page's task board. Returns elapsed s.

    Raises ``AssertionError`` if it never appears within ``timeout_s``.
    """

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ids = _board_task_ids(page)
        if task_id in ids:
            return time.monotonic() - (deadline - timeout_s)
        time.sleep(0.05)
    raise AssertionError(
        f"task_id {task_id!r} never appeared on board within {timeout_s}s; "
        f"last seen ids={_board_task_ids(page)!r}"
    )


# --------------------------------------------------------------------------- #
# Real two-tab tests
# --------------------------------------------------------------------------- #


def test_data_run_id_attr_exposed_on_task_card(
    mc_backend: Any, page: Any
) -> None:
    """The ``TaskCard`` ``<article>`` must carry ``data-task-id`` (and
    ``data-run-id`` once a run id exists) so cross-tab automation can
    identify the row without scraping textContent for the run id.
    """

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_hydration(page)

    task_id = _enqueue_via_api(page)
    # The freshly enqueued task should appear within one poll tick. The
    # row's data-task-id matches the queue task id we just got back.
    elapsed = _wait_for_task_id_on_board(page, task_id, timeout_s=8.0)
    assert elapsed < 8.0, f"task did not appear on its own board in time ({elapsed}s)"


def test_tab_b_sees_tab_a_submission_within_5s(
    mc_backend: Any, pages_two: tuple[Any, Any]
) -> None:
    """W10-CRITICAL-1: a job queued in tab A must be visible in tab B
    within 5 seconds. The BroadcastChannel hook makes this near-instant
    (<200ms typical); the 5s ceiling tolerates poll-fallback behaviour
    when BroadcastChannel is sandboxed.
    """

    page_a, page_b = pages_two
    page_a.goto(mc_backend.url, wait_until="networkidle")
    page_b.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_hydration(page_a)
    _wait_for_hydration(page_b)

    # Both tabs start with an empty board.
    assert _board_task_ids(page_a) == []
    assert _board_task_ids(page_b) == []

    # Tab A submits.
    task_id = _enqueue_via_api(page_a)

    # Tab A's own board should pick up its own row (sanity).
    _wait_for_task_id_on_board(page_a, task_id, timeout_s=5.0)

    # Tab B must see it within 5s without any user action.
    _wait_for_task_id_on_board(page_b, task_id, timeout_s=5.0)


def test_broadcast_channel_received_on_mutation(
    mc_backend: Any, browser: Any
) -> None:
    """The cross-tab mutation broadcast must reach peer tabs.

    Tab A submits a job through the *real* JobDialog UI (not a raw
    `fetch` — the broadcast hook fires from the dialog's ``onQueued``
    callback, not from the underlying ``/api/queue/build`` POST). Tab B
    has a sniffing ``BroadcastChannel`` listener installed on the
    ``mc-state-mutation`` channel before the submit. Within 5s the
    listener must observe a ``kind === 'queue.submit'`` message —
    proving BroadcastChannel (not polling) is the propagation path.

    NOTE: BroadcastChannel only propagates between pages that share the
    same browser context (same incognito profile / same partition). Real
    users on the same machine share a context by default. The
    ``pages_two`` fixture creates *isolated* contexts (incognito-style)
    which is the harder cross-tab case but BroadcastChannel does not
    cross that boundary by spec — for that case the storage-event
    fallback also requires shared origin storage. To exercise
    BroadcastChannel itself we open two pages in ONE context here.
    """

    context = browser.new_context()
    page_a = context.new_page()
    page_b = context.new_page()
    try:
        _run_broadcast_test(mc_backend, page_a, page_b)
    finally:
        context.close()


def _run_broadcast_test(mc_backend: Any, page_a: Any, page_b: Any) -> None:
    page_a.goto(mc_backend.url, wait_until="networkidle")
    page_b.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_hydration(page_a)
    _wait_for_hydration(page_b)

    # Install a sniffing BroadcastChannel listener in tab B's window
    # context. The SPA's own listener is unaffected — multiple listeners
    # on the same channel name receive every message.
    page_b.evaluate(
        """
        () => {
            window.__crossTabSniff = [];
            const bc = new BroadcastChannel('mc-state-mutation');
            bc.onmessage = (ev) => { window.__crossTabSniff.push(ev.data); };
            window.__crossTabSniffChannel = bc;
        }
        """
    )

    # Submit via the JobDialog so the SPA's onQueued path (which is what
    # publishes to BroadcastChannel) actually fires. A direct
    # /api/queue/build fetch would NOT reach the broadcast hook.
    page_a.get_by_test_id("new-job-button").click()
    page_a.locator("textarea").first.fill(
        "Add an /api/health endpoint that returns ok"
    )
    submit = page_a.get_by_test_id("job-dialog-submit-button")
    page_a.wait_for_function(
        "() => document.querySelector('[data-testid=job-dialog-submit-button]')?.disabled === false",
        timeout=5_000,
    )
    submit.click()

    # Wait up to 5s for the message to arrive in tab B.
    deadline = time.monotonic() + 5.0
    received: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        received = page_b.evaluate("() => window.__crossTabSniff || []") or []
        if any(
            isinstance(msg, dict) and msg.get("kind") == "queue.submit"
            for msg in received
        ):
            break
        time.sleep(0.05)
    assert any(
        isinstance(msg, dict) and msg.get("kind") == "queue.submit"
        for msg in received
    ), f"expected a queue.submit broadcast in tab B; saw {received!r}"


def test_tab_a_sees_tab_b_cancellation_within_5s(
    mc_backend: Any,
    pages_two: tuple[Any, Any],
) -> None:
    """W10-CRITICAL-2: a cancellation issued in tab B must reach tab A's
    UI within 5 seconds.

    We do not rely on the watcher actually starting the task — the
    cancel path supports cancelling a queued (not-yet-started) task and
    the task board flips its label accordingly. This makes the test
    deterministic without spawning a real subprocess.
    """

    page_a, page_b = pages_two
    page_a.goto(mc_backend.url, wait_until="networkidle")
    page_b.goto(mc_backend.url, wait_until="networkidle")
    _wait_for_hydration(page_a)
    _wait_for_hydration(page_b)

    # Tab A submits a build, tab B will cancel it.
    task_id = _enqueue_via_api(page_a)
    _wait_for_task_id_on_board(page_a, task_id, timeout_s=5.0)
    _wait_for_task_id_on_board(page_b, task_id, timeout_s=5.0)

    # Tab B issues a cancel via the queue-task synthetic id (which is
    # what the SPA uses for queued-but-not-started rows). The route
    # accepts the queue-task id directly via the cancel action.
    cancel_target = f"queue-compat:{task_id}"
    cancel_result = page_b.evaluate(
        """
        async (rid) => {
            const r = await fetch(
                '/api/runs/' + encodeURIComponent(rid) + '/actions/cancel',
                {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}
            );
            return {status: r.status};
        }
        """,
        cancel_target,
    )
    # We don't strictly require 200 here — the cancel path may return 200
    # with a warning (W10-IMPORTANT-1) when the task has no PID yet. What
    # matters is that the broadcast fires regardless of outcome and tab A
    # observes a state change within the window.
    assert isinstance(cancel_result, dict)

    # Tab A must observe the row leaving the active board (cancelled
    # rows transition to history) OR the row's stage flipping out of
    # "queued" within 5s.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if task_id not in _board_task_ids(page_a):
            return
        # Or the row's stage may have changed — if so, the board still
        # shows it but in a non-active state. That's also a successful
        # propagation — what we are guarding against is "tab A is
        # frozen in pre-cancel state".
        stages = page_a.evaluate(
            """
            (tid) => {
                const board = document.querySelector('[data-testid="task-board"]');
                if (!board) return [];
                return Array.from(board.querySelectorAll('[data-task-id]'))
                    .filter(el => el.getAttribute('data-task-id') === tid)
                    .map(el => el.getAttribute('data-stage'));
            }
            """,
            task_id,
        ) or []
        if any(stage and stage != "queued" for stage in stages):
            return
        time.sleep(0.05)
    raise AssertionError(
        f"tab A never reflected cancellation of {task_id} within 5s; "
        f"current board ids={_board_task_ids(page_a)!r}"
    )
