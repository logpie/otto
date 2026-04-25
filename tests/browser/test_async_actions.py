"""Browser tests for the async-action discipline cluster (mc-audit Phase 4).

Each test fails before the corresponding fix in `App.tsx`/`styles.css` and
passes after. They are paired with the fixes in the cluster:

  - watcher start/stop synchronous in-flight latch (microinteractions C2,
    state-management #10, first-time-user #14)
  - shared <Spinner /> visual loading indicator (microinteractions C3)
  - global `:active` pressed-state CSS rule (microinteractions C1)
  - refresh-button disable during refresh (microinteractions I2)
  - search-input debounce (microinteractions I3)
  - JobDialog inline validation hint (microinteractions I1)

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest tests/browser/test_async_actions.py \\
        -m browser -p playwright -v

The bundle build is required for the fixes to be visible in the served SPA.
After implementation, run once with the full bundle build (omit the env var)
to confirm the live bundle has them.

Tests rely on Playwright's `page.route` to inject synthetic responses for the
canStartWatcher gate (`/api/state` with queued > 0) and to delay the actual
mutation endpoints so the in-flight UI is observable. Real network responses
would either resolve too fast (under 5 ms locally) or require seeding the real
queue from Python — both unnecessary for what we are testing.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Synthetic state payload — minimum shape that satisfies App.tsx invariants
# (StateResponse) AND keeps `canStartWatcher(data) === true`. Mirrors the
# subset of fields the components read; everything else is null/empty so the
# UI renders the launcher-equivalent of an idle project with one queued task.
# --------------------------------------------------------------------------- #


def _state_with_queued_task() -> dict[str, Any]:
    """A StateResponse fixture with one queued task and a stopped watcher.

    `canStartWatcher(data)` requires `runtime.supervisor.can_start === true`
    AND (queued > 0 OR command_backlog.pending > 0). We satisfy both so the
    Start watcher button is enabled.
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
            "counts": {"queued": 1, "running": 0},
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
        "live": {"items": [], "refresh_interval_s": 1.5},
        "history": {"items": [], "total_rows": 0, "total_pages": 1},
        "events": {"items": [], "total_count": 0, "malformed_count": 0},
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
                "can_start": True,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    """Stub `/api/state*` to return the synthetic payload on every poll."""

    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/state*", handler)


def _install_projects_route(page: Any) -> None:
    """Stub `/api/projects` so the SPA is not pulled into launcher mode."""

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


# --------------------------------------------------------------------------- #
# CRITICAL — Start watcher latch
# --------------------------------------------------------------------------- #


def test_start_watcher_disables_button_during_post(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Two rapid clicks on Start watcher fire exactly one POST.

    Before fix: the button stays enabled during the POST; both clicks reach
    the network layer, producing two `/api/watcher/start` requests.
    After fix: `useInFlight` synchronously latches; the second click is
    coalesced into the in-flight promise.
    """

    payload = _state_with_queued_task()
    _install_projects_route(page)
    _install_state_route(page, payload)

    # Hold the watcher start response for 500ms so the button must remain
    # disabled across two clicks. Track every call.
    post_count = 0
    post_lock = threading.Lock()

    def watcher_start_handler(route: Any) -> None:
        nonlocal post_count
        with post_lock:
            post_count += 1
        time.sleep(0.5)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "watcher started", "refresh": True}),
        )

    page.route("**/api/watcher/start", watcher_start_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    button = page.get_by_test_id("start-watcher-button")
    button.wait_for(state="visible")
    # Sanity: queued=1 enables the button before we click.
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=start-watcher-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )

    # Dispatch three clicks back-to-back via JS, bypassing Playwright's
    # actionability auto-wait. This simulates a real fast double-click and
    # the rapid-double-fire race the hunters flagged. With Playwright's
    # native `click()` we'd implicitly wait for re-enable between clicks,
    # which would mask the bug we're testing.
    page.evaluate(
        """() => {
            const btn = document.querySelector('[data-testid=start-watcher-button]');
            btn.click();
            btn.click();
            btn.click();
        }"""
    )

    # Wait for the holds to drain.
    page.wait_for_timeout(1200)

    assert post_count == 1, f"expected exactly one /api/watcher/start POST, got {post_count}"


def test_start_watcher_shows_spinner_and_starting_label(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """While the POST is in flight the button shows a spinner and 'Starting…'."""

    payload = _state_with_queued_task()
    _install_projects_route(page)
    _install_state_route(page, payload)

    def watcher_start_handler(route: Any) -> None:
        time.sleep(0.5)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "watcher started", "refresh": True}),
        )

    page.route("**/api/watcher/start", watcher_start_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=start-watcher-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    # Click and immediately snapshot — we want to capture the pending-state
    # render before the 500ms response delay completes and the button reverts.
    snapshot = page.evaluate(
        """async () => {
            const btn = document.querySelector('[data-testid=start-watcher-button]');
            btn.click();
            // Yield a microtask + a paint so React commits the pending state.
            await new Promise(r => requestAnimationFrame(() => r(null)));
            await new Promise(r => requestAnimationFrame(() => r(null)));
            return {
                disabled: btn.disabled,
                text: btn.textContent,
                hasSpinner: !!btn.querySelector('[data-testid=mc-spinner]'),
            };
        }"""
    )
    assert snapshot["disabled"], f"button should be disabled while pending: {snapshot!r}"
    assert snapshot["hasSpinner"], f"spinner should render in pending state: {snapshot!r}"
    assert "Starting" in (snapshot["text"] or ""), (
        f"expected 'Starting' label in pending state; got {snapshot!r}"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT — Refresh button disables during refresh
# --------------------------------------------------------------------------- #


def test_refresh_button_disables_during_refresh(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Toolbar Refresh button must be disabled while the manual refresh is in flight."""

    payload = _state_with_queued_task()
    _install_projects_route(page)

    # Slow `/api/state` only on the manual-refresh wave; first poll resolves
    # fast so the SPA hydrates.
    request_count = 0
    request_lock = threading.Lock()

    def state_handler(route: Any) -> None:
        nonlocal request_count
        with request_lock:
            request_count += 1
            this_request = request_count
        if this_request >= 2:
            time.sleep(0.5)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/state*", state_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    refresh_btn = page.get_by_test_id("toolbar-refresh-button")
    refresh_btn.wait_for(state="visible")

    # Click + immediate snapshot — capture the pending render frame before
    # the 500ms response lets `useInFlight` clear pending.
    snapshot = page.evaluate(
        """async () => {
            const btn = document.querySelector('[data-testid=toolbar-refresh-button]');
            btn.click();
            await new Promise(r => requestAnimationFrame(() => r(null)));
            await new Promise(r => requestAnimationFrame(() => r(null)));
            return {
                disabled: btn.disabled,
                hasSpinner: !!btn.querySelector('[data-testid=mc-spinner]'),
                text: btn.textContent,
            };
        }"""
    )
    assert snapshot["disabled"], f"Refresh button should be disabled while in flight: {snapshot!r}"
    assert snapshot["hasSpinner"], f"Spinner should render in pending state: {snapshot!r}"
    assert "Refreshing" in (snapshot["text"] or ""), (
        f"expected 'Refreshing' label while pending; got {snapshot!r}"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT — Search input debounce
# --------------------------------------------------------------------------- #


def test_search_input_debounces(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Five rapid keystrokes within 50ms produce ≤2 `/api/state` requests.

    Without debounce: each keystroke dirties `filters`, which is in
    `refresh`'s deps, so each char triggers a state fetch. Six chars in 50ms
    can stack five extra requests within the 200ms debounce window.
    With debounce: the search box updates locally, the committed query lands
    once after the user stops typing.
    """

    payload = _state_with_queued_task()
    _install_projects_route(page)

    state_requests: list[float] = []

    def state_handler(route: Any) -> None:
        state_requests.append(time.monotonic())
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/state*", state_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    # Wait for the initial poll to settle.
    page.wait_for_timeout(300)
    initial_count = len(state_requests)

    search = page.get_by_test_id("filter-search-input")
    search.wait_for(state="visible")
    search.click()
    # Type five chars rapidly — Playwright type with delay=0 fires individual
    # input events.
    search.type("hello", delay=10)

    # Within 250ms after typing, we should not yet see a settled `state`
    # request because the debounce window is 200ms.
    # After 350ms we expect exactly ONE additional state request (the
    # debounce-flushed one). The polling interval is ~1.5s so polling cannot
    # add another request within this window.
    page.wait_for_timeout(400)
    final_count = len(state_requests)
    additional = final_count - initial_count
    # Polling could add at most 0 requests in 700ms total, so ≤2 (debounce
    # commit + maybe one poll race). Without debounce we would see ~5+.
    assert additional <= 2, (
        f"expected ≤2 additional /api/state requests after typing 5 chars "
        f"(debounce + maybe one poll), got {additional}"
    )


# --------------------------------------------------------------------------- #
# CRITICAL — Global :active rule
# --------------------------------------------------------------------------- #


def test_active_state_rule_present_in_stylesheet(mc_backend: Any, page: Any) -> None:
    """A `button:active` rule must exist in the loaded stylesheet.

    We look for the rule textually in any stylesheet served to the page —
    the simplest, most resilient assertion. Computed-style on `:active` is
    awkward to test in Playwright because the pseudo-class is only set
    during an actual mousedown, so we assert the rule's existence instead.
    """

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)

    # Walk every CSSStyleSheet attached to the page; find at least one rule
    # whose selector text includes ':active' targeting buttons or [role=button].
    found = page.evaluate(
        """() => {
            for (const sheet of Array.from(document.styleSheets)) {
                let rules;
                try { rules = sheet.cssRules; } catch { continue; }
                if (!rules) continue;
                for (const rule of Array.from(rules)) {
                    const text = rule.cssText || "";
                    if (text.includes(':active') && (text.includes('button') || text.includes('[role="button"]'))) {
                        return text;
                    }
                }
            }
            return null;
        }"""
    )
    assert found, "expected at least one button:active rule in the stylesheet"


# --------------------------------------------------------------------------- #
# IMPORTANT — JobDialog inline validation hint
# --------------------------------------------------------------------------- #


def test_job_dialog_shows_inline_validation_hint_when_intent_empty(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Opening JobDialog with an empty intent shows an inline hint.

    The hint must be visible to keyboard/screen-reader users, not hidden in
    a `title=` tooltip. We assert the helper element is present and contains
    the expected copy.
    """

    payload = _state_with_queued_task()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    hint = page.get_by_test_id("job-dialog-validation-hint")
    hint.wait_for(state="visible", timeout=2_000)
    text = hint.text_content() or ""
    assert "Describe the requested outcome" in text, (
        f"expected validation hint to mention the missing intent; got: {text!r}"
    )


def test_job_dialog_hint_clears_when_intent_typed(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Typing an intent removes the validation hint and enables submit."""

    payload = _state_with_queued_task()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    hint = page.get_by_test_id("job-dialog-validation-hint")
    hint.wait_for(state="visible")

    # Type intent — the hint should disappear and submit should enable.
    page.locator("textarea").first.fill("Add a checkout button to the storefront")

    # Hint should no longer be visible.
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=job-dialog-validation-hint]')",
        timeout=2_000,
    )
    submit = page.get_by_test_id("job-dialog-submit-button")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=job-dialog-submit-button]')?.disabled === false",
        timeout=2_000,
    )
    assert submit.is_visible()
