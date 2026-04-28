"""Browser regression for the inspector tab toolbar.

Bug W1-CRITICAL-1 (mc-audit live findings, 2026-04-26):

    Opening the inspector and switching to the Logs tab leaves a `<pre
    data-testid="run-log-pane">` covering the heading-row tab buttons
    (Review / Code changes / Logs / Artifacts). Playwright reports the
    `<pre>` "intercepts pointer events" — the user can never leave the
    Logs tab once entered.

The bug surfaces only when the LogPane has rendered the `<pre>` (i.e.
log text was returned). This regression renders that exact state and
clicks each heading tab in sequence, asserting the corresponding panel
becomes visible. We use a 2s click timeout so a regression of the same
shape fails fast (the default 30s click timeout would mask it).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest tests/browser/test_inspector_tabs.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser

RUN_ID = "tabs-test-run"

# 2s per click — enough for the SPA to react, short enough to fail fast
# when the click is intercepted (default would wait 30s before failing).
CLICK_TIMEOUT_MS = 2_000


# --------------------------------------------------------------------------- #
# Fixtures — minimum payloads to make the inspector mount with all four tabs
# selectable (Review/Code changes/Logs/Artifacts).
# --------------------------------------------------------------------------- #


def _state_payload() -> dict[str, Any]:
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
            "items": [_live_item()],
            "total_count": 1,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "page": 1, "page_size": 25, "total_rows": 0, "total_pages": 1},
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


def _live_item() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "display_name": "build:tabs",
        "status": "passed",
        "terminal_outcome": "passed",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": "merge-1",
        "branch": "build/tabs",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "claude",
        "version": 1,
        "display_status": "merged",
        "active": False,
        "display_id": "tabs-test-run",
        "branch_task": "build/tabs",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": None,
        "cost_display": "-",
        "last_event": "passed",
        "row_label": "build:tabs",
        "overlay": None,
    }


def _detail_payload() -> dict[str, Any]:
    item = _live_item()
    return {
        **item,
        "source": "live",
        "title": "build: tabs",
        "summary_lines": [],
        "overlay": None,
        # One artifact so the Artifacts tab has content to render.
        "artifacts": _artifact_refs(),
        "log_paths": ["/tmp/proj/otto_logs/sessions/x/build/narrative.log"],
        "selected_log_index": 0,
        "selected_log_path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "legal_actions": [],
        "review_packet": _review_packet_skeleton(),
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


def _review_packet_skeleton() -> dict[str, Any]:
    return {
        "headline": "Tabs run",
        "status": "passed",
        "summary": "",
        "readiness": {
            "state": "merged",
            "label": "landed in main",
            "tone": "success",
            "blockers": [],
            "next_step": "No merge action is needed.",
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
            "demo_evidence": {
                "demo_status": "strong",
                "app_kind": "web",
                "required": True,
                "primary_demo": {"href": "evidence/recording.webm", "label": "recording.webm"},
                "items": [],
                "stories": [],
            },
        },
        "changes": {
            "branch": "build/tabs",
            "target": "main",
            "merged": False,
            "merge_id": "merge-1",
            "file_count": 1,
            "files": ["src/foo.py"],
            "truncated": False,
            "diff_command": "git diff main...build/tabs",
            "diff_error": None,
        },
        "evidence": _artifact_refs(),
        "failure": None,
    }


def _artifact_refs() -> list[dict[str, Any]]:
    return [
        {
            "index": 0,
            "label": "summary.json",
            "kind": "json",
            "path": "/tmp/proj/otto_logs/sessions/x/summary.json",
            "exists": True,
            "size_bytes": 12,
        },
        {
            "index": 1,
            "label": "proof markdown",
            "kind": "text",
            "path": "/tmp/proj/otto_logs/sessions/x/certify/proof-of-work.md",
            "exists": True,
            "size_bytes": 128,
        },
        {
            "index": 2,
            "label": "recording.webm",
            "kind": "video",
            "path": "/tmp/proj/otto_logs/sessions/x/certify/evidence/recording.webm",
            "exists": True,
            "size_bytes": 512,
        },
    ]


def _diff_payload() -> dict[str, Any]:
    return {
        "diff": "diff --git a/src/foo.py b/src/foo.py\n+hello world\n",
        "branch": "build/tabs",
        "target": "main",
        "stale": False,
        "command": "git diff main...build/tabs",
        "error": None,
        "head_sha": "deadbeef",
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


# --------------------------------------------------------------------------- #
# Route installers
# --------------------------------------------------------------------------- #


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
    payload = _state_payload()
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )


def _install_detail_route(page: Any) -> None:
    payload = _detail_payload()
    body = json.dumps(payload)
    page.route(
        f"**/api/runs/{RUN_ID}?**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )
    page.route(
        f"**/api/runs/{RUN_ID}",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_log_route(page: Any, text: str = "line one\nline two\nline three\n") -> None:
    body = json.dumps(_log_payload(text))
    page.route(
        f"**/api/runs/{RUN_ID}/logs**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_diff_route(page: Any) -> None:
    body = json.dumps(_diff_payload())
    page.route(
        f"**/api/runs/{RUN_ID}/diff**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_artifact_route(page: Any) -> None:
    body = json.dumps({
        "artifact": {
            "index": 0,
            "label": "summary.json",
            "kind": "json",
            "path": "/tmp/proj/otto_logs/sessions/x/summary.json",
            "exists": True,
            "size_bytes": 12,
        },
        "content": '{"ok": true}',
        "truncated": False,
    })
    page.route(
        f"**/api/runs/{RUN_ID}/artifact**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )
    page.route(
        f"**/api/runs/{RUN_ID}/artifacts",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": RUN_ID, "artifacts": _artifact_refs()}),
        ),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _open_inspector_logs(page: Any) -> None:
    """Open the inspector by clicking its Logs button so the LogPane renders.

    Reproduces the W1 user flow: a completed run is selected, user opens
    the Logs view first, then tries to switch tabs from inside the
    inspector.
    """

    btn = page.get_by_test_id("open-logs-button")
    btn.wait_for(state="visible", timeout=5_000)
    btn.click()
    # Wait for the LogPane to mount AND for the <pre> to render — that's
    # the element accused of intercepting pointer events.
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=5_000)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_inspector_tab_buttons_are_clickable_from_logs(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Bug W1-CRITICAL-1: from the Logs tab, every other tab must be clickable.

    The log `<pre>` must not steal pointer events from the heading-row
    tab toolbar. We click every tab in sequence with a short timeout so
    a stacking-context regression fails fast.
    """

    _install_projects_route(page)
    _install_state_route(page)
    _install_detail_route(page)
    _install_log_route(page)
    _install_diff_route(page)
    _install_artifact_route(page)

    page.goto(
        f"{mc_backend.url}?view=tasks&run={RUN_ID}",
        wait_until="networkidle",
    )
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    _open_inspector_logs(page)

    # The role=tablist inside the inspector heading. Use it as the scope
    # for tab-button lookups so we don't accidentally hit row-level tabs.
    tablist = page.locator(".run-inspector .detail-tabs[role='tablist']")
    tablist.wait_for(state="visible", timeout=2_000)

    # Clicking Code changes from Logs is the exact failure mode in W1.
    # If the <pre> intercepts the click we time out at 2s instead of 30s.
    tablist.get_by_role("tab", name="Code changes").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("diff-pane").wait_for(state="visible", timeout=2_000)

    # Back to Logs.
    tablist.get_by_role("tab", name="Logs").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=2_000)

    # Review tab.
    tablist.get_by_role("tab", name="Review").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("proof-pane").wait_for(state="visible", timeout=2_000)

    # Back to Logs again so the <pre> is the *current* element on screen
    # (this is the original W1 starting state — the <pre> rendered, then
    # the user tried to switch).
    tablist.get_by_role("tab", name="Logs").click(timeout=CLICK_TIMEOUT_MS)
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=2_000)

    # Artifacts from Logs — same shape as the W1 click that failed.
    tablist.get_by_role("tab", name="Artifacts").click(timeout=CLICK_TIMEOUT_MS)
    # The artifact list shows one button labelled "summary.json".
    page.locator(".run-inspector .artifact-pane").wait_for(state="visible", timeout=2_000)


def test_run_detail_overview_is_actionable_and_resizable(
    mc_backend: Any, browser: Any, disable_animations: Any, viewport_mba: dict[str, Any]
) -> None:
    """Run detail overview avoids duplicate proof/artifact controls.

    Regression coverage for the overview redesign:
    - Proof means key review evidence, Artifacts means the full bundle.
    - Generic all-pass check drawers stay out of the first screen.
    - Stat tiles are the shortcuts; there is no extra "View all evidence" link.
    - The first detail drawer is resizable, matching the deeper inspector.
    """

    context = browser.new_context(**viewport_mba)
    page = context.new_page()
    try:
        _install_projects_route(page)
        _install_state_route(page)
        _install_detail_route(page)
        _install_log_route(page)
        _install_diff_route(page)
        _install_artifact_route(page)

        page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
        page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
        disable_animations(page)

        panel = page.get_by_test_id("run-detail-panel")
        panel.wait_for(state="visible", timeout=5_000)

        expect_visible = [
            "review-metric-stories",
            "review-metric-files",
            "review-metric-proof",
            "review-metric-artifacts",
        ]
        for test_id in expect_visible:
            page.get_by_test_id(test_id).wait_for(state="visible", timeout=2_000)

        assert page.get_by_test_id("review-metric-proof").text_content() == "ProofStrong"
        assert page.get_by_test_id("review-metric-artifacts").text_content() == "Artifacts3 files"
        assert page.get_by_text("View all evidence").count() == 0
        assert page.locator(".review-drawer").count() == 0

        page.get_by_test_id("review-metric-artifacts").click()
        page.locator(".run-inspector .artifact-pane").wait_for(state="visible", timeout=2_000)
        assert page.get_by_test_id("run-detail-panel").count() == 0

        page.get_by_role("button", name="Close inspector").click()
        panel.wait_for(state="visible", timeout=2_000)
        before = panel.bounding_box()
        assert before is not None
        handle = page.locator(".run-panel-resize-handle")
        handle.wait_for(state="visible", timeout=2_000)
        box = handle.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] - 120, box["y"] + box["height"] / 2)
        page.mouse.up()
        after = panel.bounding_box()
        assert after is not None
        assert after["width"] > before["width"] + 40
        layout_padding = page.evaluate(
            """() => {
                const layout = document.querySelector('.mission-layout');
                return layout ? Number.parseFloat(getComputedStyle(layout).paddingRight) : 0;
            }"""
        )
        assert layout_padding > before["width"] + 40

        page.get_by_test_id("open-diff-button").click()
        page.locator(".run-inspector .diff-viewer").wait_for(state="visible", timeout=2_000)
        assert page.get_by_test_id("run-detail-panel").count() == 0
        assert page.locator(".run-drawer-backdrop").count() == 0
    finally:
        context.close()


def test_run_detail_respects_laptop_workspace_after_saved_wide_resize(
    mc_backend: Any, browser: Any, disable_animations: Any, viewport_mba: dict[str, Any]
) -> None:
    """A saved wide drawer must not cover the task list on MBA-sized screens."""

    context = browser.new_context(**viewport_mba)
    context.add_init_script(
        """
        localStorage.setItem('otto.runDetailWidth', '900');
        localStorage.setItem('otto.inspectorWidth', '960');
        """
    )
    page = context.new_page()
    try:
        _install_projects_route(page)
        _install_state_route(page)
        _install_detail_route(page)
        _install_log_route(page)
        _install_diff_route(page)
        _install_artifact_route(page)

        page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
        page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
        disable_animations(page)

        panel = page.get_by_test_id("run-detail-panel")
        panel.wait_for(state="visible", timeout=5_000)
        panel_box = panel.bounding_box()
        task_box = page.get_by_test_id("task-board").bounding_box()
        assert panel_box is not None
        assert task_box is not None
        assert task_box["x"] + task_box["width"] <= panel_box["x"]

        page.get_by_test_id("open-diff-button").click()
        page.locator(".run-inspector .diff-viewer").wait_for(state="visible", timeout=2_000)
        inspector_box = page.get_by_test_id("run-inspector").bounding_box()
        task_box = page.get_by_test_id("task-board").bounding_box()
        assert inspector_box is not None
        assert task_box is not None
        assert task_box["x"] + task_box["width"] <= inspector_box["x"]
        assert page.get_by_test_id("run-detail-panel").count() == 0
    finally:
        context.close()


def test_run_detail_and_inspector_are_full_screen_on_iphone(
    mc_backend: Any, browser: Any, disable_animations: Any, viewport_iphone: dict[str, Any]
) -> None:
    """Phone detail surfaces must not leak the underlying page or dark pane theme."""

    context = browser.new_context(**viewport_iphone)
    page = context.new_page()
    try:
        _install_projects_route(page)
        _install_state_route(page)
        _install_detail_route(page)
        _install_log_route(page)
        _install_diff_route(page)
        _install_artifact_route(page)

        page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
        page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
        disable_animations(page)

        viewport_width = page.evaluate("window.innerWidth")
        viewport_height = page.evaluate("window.innerHeight")
        panel = page.get_by_test_id("run-detail-panel")
        panel.wait_for(state="visible", timeout=5_000)
        panel_box = panel.bounding_box()
        assert panel_box is not None
        assert panel_box["x"] == 0
        assert panel_box["y"] == 0
        assert panel_box["width"] <= viewport_width
        assert panel_box["height"] >= viewport_height - 1

        page.get_by_test_id("open-diff-button").click()
        page.locator(".run-inspector .diff-viewer").wait_for(state="visible", timeout=2_000)
        inspector_box = page.get_by_test_id("run-inspector").bounding_box()
        assert inspector_box is not None
        assert inspector_box["x"] == 0
        assert inspector_box["y"] == 0
        assert inspector_box["width"] <= viewport_width
        assert inspector_box["height"] >= viewport_height - 1
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.locator(".run-inspector .diff-viewer").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        ) != "rgb(11, 16, 32)"
    finally:
        context.close()
