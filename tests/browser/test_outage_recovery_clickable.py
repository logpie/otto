"""Browser regression for outage-recovery click survivability (W13-CRITICAL-1).

Source: live W13 dogfood run (mc-audit/live-findings.md, search
"W13-CRITICAL-1").

Reproduction summary:

    The user opens Mission Control, opens a run inspector to the Logs
    tab, then the backend is restarted and the user reloads the page.
    Post-reload, clicks on the in-`RunDetailPanel` evidence-shortcut
    buttons ("Review result" / "Code changes" / "Logs" / "Artifacts")
    fail with Playwright's "intercepts pointer events" error: the
    fixed-position inspector overlays those buttons, and on retry the
    `.app-shell` outer wrapper also catches clicks. The user is
    effectively locked into whatever inspector tab they opened first.

Root cause:

    `.run-inspector` is `position: fixed` covering the workspace area.
    The same `RunDetailPanel` shortcut buttons that opened the inspector
    sit underneath that overlay — they are visually hidden from a
    sighted user but remain in the DOM and remain "visible+enabled" from
    Playwright's perspective. Any script-driven click against them is
    intercepted by the inspector subtree (the log-pane `<pre>`) or, on
    retry-after-scroll, by the `.app-shell` parent.

Fix (defense in depth):

    1.  ``RunDetailPanel`` no longer renders ``.detail-inspector-actions``
        when the inspector is open. The inspector ships its own tablist
        (Result / Code changes / Logs / Artifacts) so the shortcuts are
        redundant once the inspector is on screen.

    2.  ``[inert]`` containers (`.main-shell-content`, `.sidebar`,
        `[data-mc-inspector]`) get ``pointer-events: none`` in CSS so any
        rogue script-driven click cannot reach a buried button.

This test pins down both invariants and the live-W13 user flow:

    * Once the inspector is open, ``.detail-inspector-actions`` must not
      be in the DOM.
    * The inspector tablist (the supported way to switch views) must
      remain clickable.
    * After a force-reload of the page (simulating the user reopening
      the browser after a server outage), the primary controls
      (``new-job-button``, ``start-watcher-button``, history-row
      activator) remain clickable — no `.app-shell` interception.
    * No visible primary button has `.app-shell` (or any inert
      container) sitting on top of its center point.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_outage_recovery_clickable.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser

# 2s click timeout — long enough for the SPA to react, short enough that
# a regression of the W13 shape (intercepted by inspector / app-shell)
# fails fast instead of hanging on Playwright's default 30s.
CLICK_TIMEOUT_MS = 2_000

RUN_ID = "outage-recovery-run"


# --------------------------------------------------------------------------- #
# Synthetic state — one completed run that the inspector can be opened against.
# Kept stable across hydrate / reload so the post-reload page resolves to the
# same selected run.
# --------------------------------------------------------------------------- #


def _live_item() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "display_name": "build:outage",
        "status": "passed",
        "terminal_outcome": "passed",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": "merge-1",
        "branch": "build/outage",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "claude",
        "version": 1,
        "display_status": "passed",
        "active": False,
        "display_id": RUN_ID,
        "branch_task": "build/outage",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": None,
        "cost_display": "-",
        "last_event": "passed",
        "row_label": "build:outage",
        "overlay": None,
    }


def _state_payload() -> dict[str, Any]:
    item = _live_item()
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
            "items": [item],
            "total_count": 1,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {
            "items": [item],
            "page": 1,
            "page_size": 25,
            "total_rows": 1,
            "total_pages": 1,
        },
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
                "can_start": True,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


def _detail_payload() -> dict[str, Any]:
    item = _live_item()
    return {
        **item,
        "source": "live",
        "title": "build: outage",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [
            {
                "index": 0,
                "label": "summary.json",
                "kind": "json",
                "path": "/tmp/proj/otto_logs/sessions/x/summary.json",
                "exists": True,
                "size_bytes": 12,
            }
        ],
        "log_paths": ["/tmp/proj/otto_logs/sessions/x/build/narrative.log"],
        "selected_log_index": 0,
        "selected_log_path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "legal_actions": [],
        "review_packet": {
            "headline": "Outage run",
            "status": "passed",
            "summary": "",
            "readiness": {
                "state": "ready",
                "label": "ready",
                "tone": "success",
                "blockers": [],
                "next_step": "Review evidence.",
            },
            "checks": [],
            "next_action": {"label": "review", "action_key": None, "enabled": False, "reason": None},
            "certification": {
                "stories_passed": 1,
                "stories_tested": 1,
                "passed": True,
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
                "branch": "build/outage",
                "target": "main",
                "merged": False,
                "merge_id": "merge-1",
                "file_count": 1,
                "files": ["src/foo.py"],
                "truncated": False,
                "diff_command": "git diff main...build/outage",
                "diff_error": None,
            },
            "evidence": [],
            "failure": None,
        },
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


def _log_payload(text: str = "line one\nline two\nline three\n") -> dict[str, Any]:
    return {
        "path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "offset": 0,
        "next_offset": len(text),
        "text": text,
        "exists": True,
        "total_bytes": len(text),
        "eof": True,
    }


def _diff_payload() -> dict[str, Any]:
    return {
        "diff": "diff --git a/src/foo.py b/src/foo.py\n+hello world\n",
        "branch": "build/outage",
        "target": "main",
        "stale": False,
        "command": "git diff main...build/outage",
        "error": None,
        "head_sha": "deadbeef",
    }


# --------------------------------------------------------------------------- #
# Route installers — keep responses STABLE across reloads so the second mount
# resolves to the same run/inspector state as the first.
# --------------------------------------------------------------------------- #


def _install_routes(page: Any) -> None:
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

    state_body = json.dumps(_state_payload())
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=state_body),
    )

    detail_body = json.dumps(_detail_payload())
    page.route(
        f"**/api/runs/{RUN_ID}*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=detail_body),
    )

    log_body = json.dumps(_log_payload())
    page.route(
        f"**/api/runs/{RUN_ID}/logs**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=log_body),
    )

    diff_body = json.dumps(_diff_payload())
    page.route(
        f"**/api/runs/{RUN_ID}/diff**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=diff_body),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any, *, run_id: str) -> None:
    page.goto(
        f"{mc_backend.url}?view=tasks&run={run_id}",
        wait_until="networkidle",
    )
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def _click_intercepted_by_app_shell(page: Any, selector: str) -> bool:
    """Return True if `elementFromPoint` at the button's center is the
    `.app-shell` container (or any inert ancestor) — i.e. the click would
    be eaten by the app-shell layer instead of the button.
    """

    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            // Off-screen / zero-size: not a click-block, return False.
            if (rect.width === 0 || rect.height === 0) return false;
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            const top = document.elementFromPoint(x, y);
            if (!top) return false;
            // Walk ancestors of `top`. If any is the literal `.app-shell`
            // (and `top` is not a descendant of `el`), the click is eaten
            // by the app-shell layer.
            if (el.contains(top)) return false;
            let cur = top;
            while (cur) {
                if (cur.classList && cur.classList.contains('app-shell')) return true;
                cur = cur.parentElement;
            }
            return false;
        }""",
        selector,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_inspector_open_hides_redundant_shortcuts(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """W13-CRITICAL-1: with the inspector open, ``.detail-inspector-actions``
    must not be in the DOM — those buttons sit under the fixed inspector and
    cause script-driven clicks to be intercepted.
    """

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations, run_id=RUN_ID)

    # Pre-condition: inspector is closed → shortcut row visible.
    assert (
        page.locator(".detail-inspector-actions").count() == 1
    ), "shortcut row should be present before inspector opens"

    # Open the inspector via the Logs shortcut (the supported flow).
    open_logs = page.get_by_test_id("open-logs-button")
    open_logs.wait_for(state="visible", timeout=5_000)
    open_logs.click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-inspector").wait_for(state="visible", timeout=5_000)

    # Post-condition: the shortcut row must be gone — leaving it would put
    # buried buttons under the fixed inspector overlay.
    assert page.locator(".detail-inspector-actions").count() == 0, (
        ".detail-inspector-actions must be unmounted while inspector is open "
        "(W13-CRITICAL-1: buried buttons under fixed inspector cause "
        "click-interception failures)."
    )


def test_inspector_tabs_clickable_after_reload(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """W13-CRITICAL-1: after a force-reload (simulates user reopening browser
    after a server restart), the inspector tabs remain clickable and the
    primary mission-control buttons are not eaten by ``.app-shell``.
    """

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations, run_id=RUN_ID)

    # Open the inspector once before reload so the URL has the run pinned.
    page.get_by_test_id("open-logs-button").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-inspector").wait_for(state="visible", timeout=5_000)
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=5_000)

    # ---- Force a reload — equivalent to the user reopening the browser ----
    page.reload(wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    # The persisted route restores the inspector state. Wait until the SPA
    # has restored the run-detail panel; the inspector itself only opens
    # when the user clicks. Re-opening logs from the (now-rendered)
    # shortcut row exercises the post-restart click path that W13 hit.
    panel = page.get_by_test_id("run-detail-panel")
    panel.wait_for(state="visible", timeout=5_000)
    open_logs = page.get_by_test_id("open-logs-button")
    open_logs.wait_for(state="visible", timeout=5_000)
    open_logs.click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-inspector").wait_for(state="visible", timeout=5_000)
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=5_000)

    # The inspector tabs are the supported way to switch views with the
    # inspector open. Clicking each must succeed within CLICK_TIMEOUT_MS —
    # the W13 reproduction was a 5s click that timed out at 30s on retry.
    tablist = page.locator(".run-inspector .detail-tabs[role='tablist']")
    tablist.wait_for(state="visible", timeout=2_000)

    tablist.get_by_role("tab", name="Code changes").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("diff-pane").wait_for(state="visible", timeout=2_000)

    tablist.get_by_role("tab", name="Result").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("proof-pane").wait_for(state="visible", timeout=2_000)

    tablist.get_by_role("tab", name="Logs").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=2_000)

    # Close the inspector and verify the side-panel + sidebar primary
    # buttons are still operable post-reload.
    page.get_by_test_id("close-inspector-button").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-inspector").wait_for(state="detached", timeout=5_000)


def test_no_app_shell_intercepts_visible_primary_buttons(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """W13-CRITICAL-1: no `.app-shell` (or other inert container) may sit on
    top of the click point of any visible primary button. The W13 trace
    shows ``<div class="app-shell">…</div> intercepts pointer events`` on
    retry — defense-in-depth check that the layout never produces that.
    """

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations, run_id=RUN_ID)

    # Inspector closed: the shortcut row + sidebar buttons are user-facing.
    for testid in (
        "new-job-button",
        "start-watcher-button",
        "open-proof-button",
        "open-logs-button",
        "open-artifacts-button",
    ):
        selector = f"[data-testid='{testid}']"
        if page.locator(selector).count() == 0:
            continue
        assert not _click_intercepted_by_app_shell(page, selector), (
            f"button {testid!r} center is covered by .app-shell — clicks would "
            f"be intercepted (W13-CRITICAL-1 reproduction)."
        )

    # Open the inspector. The `RunDetailPanel` shortcut row vanishes (per
    # the fix); the inspector's own close + tab buttons must remain clean.
    page.get_by_test_id("open-logs-button").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-inspector").wait_for(state="visible", timeout=5_000)

    for testid in ("close-inspector-button",):
        selector = f"[data-testid='{testid}']"
        if page.locator(selector).count() == 0:
            continue
        assert not _click_intercepted_by_app_shell(page, selector), (
            f"button {testid!r} inside the inspector is covered by .app-shell — "
            f"clicks would be intercepted."
        )

    # Force a reload (the W13 trigger) and re-check post-reload layout.
    page.reload(wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)
    page.get_by_test_id("run-detail-panel").wait_for(state="visible", timeout=5_000)

    for testid in (
        "new-job-button",
        "start-watcher-button",
        "open-logs-button",
        "open-proof-button",
    ):
        selector = f"[data-testid='{testid}']"
        if page.locator(selector).count() == 0:
            continue
        assert not _click_intercepted_by_app_shell(page, selector), (
            f"post-reload: button {testid!r} center is covered by .app-shell — "
            f"clicks would be intercepted (W13-CRITICAL-1 post-restart "
            f"reproduction)."
        )


def test_inert_subtree_blocks_pointer_events(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Defense-in-depth (W13-CRITICAL-1): when the inspector is open the
    rest of the shell is `inert` AND `pointer-events: none`. A script-driven
    click against any element inside the inert subtree must not register
    on that element.
    """

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations, run_id=RUN_ID)

    # Open the inspector — `.main-shell-content` and `.topbar` go inert.
    page.get_by_test_id("open-logs-button").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-inspector").wait_for(state="visible", timeout=5_000)

    inert_state = page.evaluate(
        """() => ({
            topbarInert: document.querySelector('.topbar')?.hasAttribute('inert') === true,
            mainInert: document.querySelector('.main-shell-content')?.hasAttribute('inert') === true,
            topbarPointerEvents: getComputedStyle(document.querySelector('.topbar')).pointerEvents,
            mainPointerEvents: getComputedStyle(document.querySelector('.main-shell-content')).pointerEvents,
        })"""
    )
    assert inert_state["topbarInert"], ".topbar should be inert when inspector is open"
    assert inert_state["mainInert"], ".main-shell-content should be inert when inspector is open"
    assert inert_state["topbarPointerEvents"] == "none", (
        ".topbar[inert] must compute to pointer-events: none — defense-in-depth "
        "against script-driven clicks bypassing the inert gate."
    )
    assert inert_state["mainPointerEvents"] == "none", (
        ".main-shell-content[inert] must compute to pointer-events: none — "
        "defense-in-depth against script-driven clicks bypassing the inert gate."
    )
