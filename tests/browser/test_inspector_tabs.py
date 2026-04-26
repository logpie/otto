"""Browser regression for the inspector tab toolbar.

Bug W1-CRITICAL-1 (mc-audit live findings, 2026-04-26):

    Opening the inspector and switching to the Logs tab leaves a `<pre
    data-testid="run-log-pane">` covering the heading-row tab buttons
    (Result / Code changes / Logs / Artifacts). Playwright reports the
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
# selectable (Result/Code changes/Logs/Artifacts).
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
        "display_status": "passed",
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
        "evidence": [],
        "failure": None,
    }


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

    # Result tab.
    tablist.get_by_role("tab", name="Result").click(timeout=CLICK_TIMEOUT_MS)
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
