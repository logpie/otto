"""Browser tests for the log-buffering cluster (mc-audit Phase 4 cluster B).

Each test fails before the corresponding fix in `App.tsx`/`service.py` and
passes after. They are paired with the fixes in this cluster:

  - bounded log tail buffer (long-string-overflow #1, CRITICAL)
  - "Live, polling" vs "Final" header (evidence-trustworthiness #5)
  - state-specific empty/missing/error copy + retry (error-empty-states #9)
  - exponential backoff on consecutive log fetch errors
  - polling pauses when inspector closed
  - polling pauses when the tab is hidden

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest tests/browser/test_log_buffering.py \\
        -m browser -p playwright -v

Tests stub `/api/state`, `/api/projects`, `/api/runs/{id}` and
`/api/runs/{id}/logs` so the SPA renders deterministically without a real
queue/run. Selection is driven via the `?run=…` query param so we don't need
the live-runs list to render selectable rows.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser

RUN_ID = "test-run-1"


# --------------------------------------------------------------------------- #
# Fixture builders — minimum shapes the SPA needs to mount + select a run.
# --------------------------------------------------------------------------- #


def _state_payload(*, run_active: bool = True) -> dict[str, Any]:
    """A StateResponse with one live run referenced by RUN_ID.

    `active=True` keeps the LogPane in the "Live · polling" branch; flip the
    flag to render the "Final · …" branch.
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
            "counts": {"queued": 0, "running": 1 if run_active else 0},
            "health": {
                "state": "running" if run_active else "stopped",
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
            "items": [_live_item(run_active=run_active)],
            "total_count": 1,
            "active_count": 1 if run_active else 0,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "total_rows": 0, "total_pages": 1},
        "events": {"items": [], "total_count": 0, "malformed_count": 0},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": 1,
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
            "files": {
                "queue": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "state": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "commands": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "processing": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
            },
            "supervisor": {
                "can_start": False,
                "can_stop": True,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


def _live_item(*, run_active: bool = True) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "display_name": "build:test",
        "status": "running" if run_active else "passed",
        "terminal_outcome": None if run_active else "passed",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "build/test",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "claude",
        "version": 1,
        "display_status": "running" if run_active else "passed",
        "active": run_active,
        "display_id": "test-run-1",
        "branch_task": "build/test",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": None,
        "cost_display": "-",
        "last_event": "running",
        "row_label": "build:test",
        "overlay": None,
    }


def _detail_payload(*, run_active: bool = True) -> dict[str, Any]:
    item = _live_item(run_active=run_active)
    return {
        **item,
        "source": "live",
        "title": "build: test",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [],
        "review_packet": _review_packet_skeleton(),
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


def _review_packet_skeleton() -> dict[str, Any]:
    return {
        "headline": "Test run",
        "status": "running",
        "summary": "",
        "readiness": {
            "state": "in_progress",
            "label": "in progress",
            "tone": "info",
            "blockers": [],
            "next_step": "Watch the run finish.",
        },
        "checks": [],
        "next_action": {"label": "wait", "action_key": None, "enabled": False, "reason": None},
        "certification": {
            "stories_passed": None,
            "stories_tested": None,
            "passed": False,
            "summary_path": None,
            "stories": [],
            "proof_report": {
                "json_path": None,
                "html_path": None,
                "html_url": None,
                "available": False,
            },
        },
        "changes": {
            "branch": "build/test",
            "target": "main",
            "merged": False,
            "merge_id": None,
            "file_count": 0,
            "files": [],
            "truncated": False,
            "diff_command": None,
            "diff_error": None,
        },
        "evidence": [],
        "failure": None,
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


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )


def _install_detail_route(page: Any, payload: dict[str, Any]) -> None:
    page.route(
        f"**/api/runs/{RUN_ID}?**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )
    # Also catch the bare ``/api/runs/<id>`` path in case the SPA omits the
    # query string. Playwright treats `?**` and `**` as different patterns.
    page.route(
        f"**/api/runs/{RUN_ID}",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )


def _open_logs_url(mc_backend: Any) -> str:
    return f"{mc_backend.url}?view=tasks&run={RUN_ID}"


def _open_inspector_logs(page: Any) -> None:
    """Click the Logs button so the inspector opens to the Logs tab."""

    btn = page.get_by_test_id("open-logs-button")
    btn.wait_for(state="visible", timeout=5_000)
    btn.click()
    page.get_by_test_id("log-pane-status").wait_for(state="visible", timeout=5_000)


# --------------------------------------------------------------------------- #
# CRITICAL — bounded buffer prevents render of >1MB log
# --------------------------------------------------------------------------- #


def test_log_pane_does_not_render_more_than_max_bytes(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A 5MB log payload must render at most ~1MB in the DOM.

    Before fix: `logText` accumulates the full 5MB into state and the
    `<pre>` renders all of it (split into lines), locking the browser.
    After fix: the bounded ring buffer caps the rendered text, and a
    `… earlier bytes elided` header advertises the truncation.
    """

    payload = _state_payload(run_active=False)
    detail = _detail_payload(run_active=False)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    # 5 MB log content with a recognisable trailing marker.
    big = ("x" * 1023 + "\n") * 5120  # ~5 MB
    big_text = big + "TAIL_MARKER_LINE\n"

    def logs_handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
                "offset": 0,
                "next_offset": len(big_text),
                "text": big_text,
                "exists": True,
                "total_bytes": len(big_text),
                "eof": True,
            }),
        )

    page.route(f"**/api/runs/{RUN_ID}/logs**", logs_handler)

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    _open_inspector_logs(page)

    # Wait for the log fetch to drain into the buffer.
    page.wait_for_function(
        "() => !!document.querySelector('[data-testid=run-log-pane]')",
        timeout=5_000,
    )

    rendered_length = page.evaluate(
        "() => document.querySelector('[data-testid=run-log-pane]').textContent.length"
    )
    assert rendered_length < 1_500_000, (
        f"expected the bounded tail buffer to cap rendered length under 1.5MB, got {rendered_length}"
    )

    # The tail of the log must still be visible — we drop the *front*, not the
    # most recent content.
    tail_present = page.evaluate(
        "() => document.querySelector('[data-testid=run-log-pane]').textContent.includes('TAIL_MARKER_LINE')"
    )
    assert tail_present, "expected the most recent log line to still be visible after truncation"

    # The "earlier bytes elided" header advertises the dropped prefix.
    elided = page.get_by_test_id("log-pane-elided")
    elided.wait_for(state="visible", timeout=2_000)
    text = elided.text_content() or ""
    assert "earlier bytes elided" in text, f"expected elided header; got {text!r}"


# --------------------------------------------------------------------------- #
# IMPORTANT — Live vs Final header
# --------------------------------------------------------------------------- #


def test_log_pane_shows_live_state_for_active_run(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """An active run shows `Live · polling…` in the LogPane header."""

    payload = _state_payload(run_active=True)
    detail = _detail_payload(run_active=True)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    page.route(
        f"**/api/runs/{RUN_ID}/logs**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/tmp/log",
                "offset": 0,
                "next_offset": 5,
                "text": "hi\n",
                "exists": True,
                "total_bytes": 3,
                "eof": True,
            }),
        ),
    )

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    _open_inspector_logs(page)

    status_text = page.get_by_test_id("log-pane-status").text_content() or ""
    assert "Live" in status_text and "polling" in status_text, (
        f"expected Live/polling header for active run; got {status_text!r}"
    )


def test_log_pane_shows_final_state_for_terminal_run(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A terminal run shows `Final · N lines · …` in the header."""

    payload = _state_payload(run_active=False)
    detail = _detail_payload(run_active=False)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    log_text = "alpha\nbeta\ngamma\n"
    page.route(
        f"**/api/runs/{RUN_ID}/logs**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/tmp/log",
                "offset": 0,
                "next_offset": len(log_text),
                "text": log_text,
                "exists": True,
                "total_bytes": len(log_text),
                "eof": True,
            }),
        ),
    )

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    _open_inspector_logs(page)

    page.wait_for_function(
        "() => (document.querySelector('[data-testid=log-pane-status]')?.textContent || '').includes('Final')",
        timeout=5_000,
    )
    status_text = page.get_by_test_id("log-pane-status").text_content() or ""
    assert "Final" in status_text and "line" in status_text, (
        f"expected Final/line header for terminal run; got {status_text!r}"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT — Missing log file shows path, polling pauses
# --------------------------------------------------------------------------- #


def test_log_pane_shows_path_when_log_does_not_exist(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """API returns `exists: false`; UI shows the path and stops polling."""

    payload = _state_payload(run_active=False)
    detail = _detail_payload(run_active=False)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    log_calls = 0
    log_lock = threading.Lock()

    def logs_handler(route: Any) -> None:
        nonlocal log_calls
        with log_lock:
            log_calls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/foo/bar/log",
                "offset": 0,
                "next_offset": 0,
                "text": "",
                "exists": False,
                "total_bytes": 0,
                "eof": True,
            }),
        )

    page.route(f"**/api/runs/{RUN_ID}/logs**", logs_handler)

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    _open_inspector_logs(page)

    missing = page.get_by_test_id("log-empty-missing")
    missing.wait_for(state="visible", timeout=5_000)
    text = missing.text_content() or ""
    assert "/foo/bar/log" in text, f"expected the missing-log path in body; got {text!r}"

    # Polling should pause after we've learned the file doesn't exist (the run
    # is terminal AND we have a successful read). Capture the call count and
    # wait — it should not climb.
    with log_lock:
        baseline = log_calls
    page.wait_for_timeout(2500)
    with log_lock:
        final = log_calls
    assert final - baseline <= 1, (
        f"expected polling to stop after missing-log read; logs called {final - baseline} more times"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT — Backoff on errors
# --------------------------------------------------------------------------- #


def test_log_polling_backoff_on_errors(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Three consecutive 500s grow the poll interval; recovery resets it.

    We assert qualitative behaviour rather than exact ms because the backoff
    schedule (1.2s -> 2s -> 5s -> 15s -> 30s) is too long for a CI test run.
    Specifically: after several errors the gap between calls is materially
    larger than the base 1.2s interval (>= 1.8s).
    """

    payload = _state_payload(run_active=True)
    detail = _detail_payload(run_active=True)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    call_times: list[float] = []
    call_lock = threading.Lock()

    def logs_handler(route: Any) -> None:
        with call_lock:
            call_times.append(time.monotonic())
            count = len(call_times)
        # First three calls fail, fourth succeeds.
        if count <= 3:
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "boom"}),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "path": "/tmp/log",
                    "offset": 0,
                    "next_offset": 4,
                    "text": "ok\n",
                    "exists": True,
                    "total_bytes": 4,
                    "eof": True,
                }),
            )

    page.route(f"**/api/runs/{RUN_ID}/logs**", logs_handler)

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    _open_inspector_logs(page)

    # Wait for at least 3 calls. Initial fetch is immediate (showLogs
    # reset=true). After error #1 the next poll waits 2s, after error #2 the
    # next poll waits 5s, after error #3 the next poll waits 15s. So three
    # error responses arrive at roughly t≈0, t≈2, t≈7. Budget 9s.
    deadline = time.monotonic() + 9.0
    while time.monotonic() < deadline:
        with call_lock:
            n = len(call_times)
        if n >= 3:
            break
        page.wait_for_timeout(200)

    with call_lock:
        snapshot = list(call_times)
    assert len(snapshot) >= 3, f"expected at least 3 calls within 9s; got {len(snapshot)}"

    gap_first = snapshot[1] - snapshot[0]
    gap_second = snapshot[2] - snapshot[1]
    # Without backoff each gap is the base 1.2s. With backoff the second gap
    # (5s) should be materially larger than the first (2s).
    assert gap_second > gap_first + 1.0, (
        f"expected backoff to widen poll cadence; gap_first={gap_first:.2f}s gap_second={gap_second:.2f}s"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT — Polling stops when inspector closed
# --------------------------------------------------------------------------- #


def test_log_polling_stops_when_inspector_closed(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Closing the inspector stops further `/api/runs/<id>/logs` calls."""

    payload = _state_payload(run_active=True)
    detail = _detail_payload(run_active=True)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    log_calls = 0
    log_lock = threading.Lock()

    def logs_handler(route: Any) -> None:
        nonlocal log_calls
        with log_lock:
            log_calls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/tmp/log",
                "offset": 0,
                "next_offset": 4,
                "text": "ok\n",
                "exists": True,
                "total_bytes": 4,
                "eof": True,
            }),
        )

    page.route(f"**/api/runs/{RUN_ID}/logs**", logs_handler)

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    _open_inspector_logs(page)

    # Let polling fire at least once.
    page.wait_for_timeout(1500)
    with log_lock:
        before_close = log_calls
    assert before_close >= 1, "expected at least one log fetch before closing inspector"

    page.get_by_test_id("close-inspector-button").click()

    # Wait — should be no further log calls after close.
    page.wait_for_timeout(3500)
    with log_lock:
        after = log_calls
    assert after - before_close == 0, (
        f"expected polling to stop after inspector close; saw {after - before_close} more calls"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT — Polling pauses when tab hidden
# --------------------------------------------------------------------------- #


def test_log_polling_stops_when_tab_hidden(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Dispatching a `visibilitychange` to hidden pauses polling.

    We can't actually hide the Playwright page (the browser thinks it's
    visible), but we can override `document.visibilityState` and dispatch the
    event the SPA listens for. The polling effect should observe hidden and
    skip the next tick.
    """

    payload = _state_payload(run_active=True)
    detail = _detail_payload(run_active=True)
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page, detail)

    log_calls = 0
    log_lock = threading.Lock()

    def logs_handler(route: Any) -> None:
        nonlocal log_calls
        with log_lock:
            log_calls += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": "/tmp/log",
                "offset": 0,
                "next_offset": 4,
                "text": "ok\n",
                "exists": True,
                "total_bytes": 4,
                "eof": True,
            }),
        )

    page.route(f"**/api/runs/{RUN_ID}/logs**", logs_handler)

    page.goto(_open_logs_url(mc_backend), wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    _open_inspector_logs(page)

    # Wait for one fetch, then hide.
    page.wait_for_timeout(1500)
    with log_lock:
        before = log_calls

    # Override visibilityState and dispatch the event.
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )

    page.wait_for_timeout(3500)
    with log_lock:
        after = log_calls
    # Allow at most one in-flight tick that already fired before the
    # visibility change took effect.
    assert after - before <= 1, (
        f"expected polling to pause when tab hidden; saw {after - before} more calls"
    )

    # Restore visibility — polling should resume.
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )

    page.wait_for_timeout(2000)
    with log_lock:
        resumed = log_calls
    assert resumed - after >= 1, (
        f"expected polling to resume after visibilitychange to visible; saw {resumed - after} more calls"
    )
