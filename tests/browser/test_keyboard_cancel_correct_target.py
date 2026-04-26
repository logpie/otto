"""Browser regression for W8-CRITICAL-1.

Source: ``docs/mc-audit/live-findings.md`` — W8-CRITICAL-1 (data-loss-adjacent).

Bug:
    Pressing Enter to "cancel a queued task" via keyboard navigation
    actually fired the inspector's ``review-next-action-button``. The
    per-row cancel affordance had no proper keyboard target — Tab
    walked past the queued/running card straight into the inspector's
    next-action button, which can fire merge / next-step actions.
    A keyboard user thinking "I just cancelled the job" instead
    activated the inspector's next-action POST.

Fix:
    Render an explicit ``[data-testid="task-card-cancel-<id>"]`` button
    as a SIBLING of ``task-card-main`` for any task whose status is
    cancel-eligible (queued / starting / initializing / running) AND
    has a runId. The button gets its own tab stop in the row's focus
    chain and routes through ``runActionForRun(runId, "cancel", …)``
    — the same code path as the inspector's Cancel — but bypasses the
    inspector entirely. The click handler stops propagation defensively
    and triggers ONLY the cancel POST, never any inspector action.

Tests:
    1. ``test_keyboard_cancel_on_queued_card_fires_cancel`` — focus a
       queued task card, press Enter on its Cancel button, confirm.
       Assert ``POST /api/runs/{id}/actions/cancel`` fires.
    2. ``test_keyboard_cancel_does_not_fire_inspector_next_action`` —
       with the inspector open showing a different run, focus the
       queued card's cancel via keyboard, activate. Assert the
       inspector's next-action POST does NOT fire — only the cancel
       POST for the cancel target's runId fires.
    3. ``test_keyboard_cancel_target_matches_visual_focus`` — Tab
       focuses the row's main button, then a second Tab lands on the
       Cancel button. Asserts the focused element's testid matches the
       per-row cancel testid (and is NOT ``review-next-action-button``).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_keyboard_cancel_correct_target.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# Two distinct runs in the live table:
#   - QUEUED_RUN_ID  → the task we want to cancel (queued, has runId)
#   - RUNNING_RUN_ID → the currently-running task whose details the
#                      inspector is showing (so the inspector's next-
#                      action button is for a DIFFERENT run than the
#                      one being cancelled — proves they don't collide).
QUEUED_RUN_ID = "run-queued-001"
QUEUED_TASK_ID = "build-queued-feature"
RUNNING_RUN_ID = "run-running-002"
RUNNING_TASK_ID = "build-running-feature"


# --------------------------------------------------------------------------- #
# Synthetic state builders.
# --------------------------------------------------------------------------- #


def _live_item(run_id: str, task_id: str, *, status: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": task_id,
        "status": status,
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": task_id,
        "merge_id": None,
        "branch": f"otto/{task_id}",
        "worktree": f".worktrees/{task_id}",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": status,
        "active": True,
        "display_id": run_id,
        "branch_task": task_id,
        "elapsed_s": 8.0,
        "elapsed_display": "8s",
        "cost_usd": 0.0,
        "cost_display": "$0.00",
        "last_event": status,
        "row_label": task_id,
        "overlay": None,
    }


def _landing_item(run_id: str, task_id: str, *, queue_status: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "branch": f"otto/{task_id}",
        "worktree": f".worktrees/{task_id}",
        "summary": task_id,
        "queue_status": queue_status,
        "landing_state": "blocked",
        "label": queue_status,
        "merge_id": None,
        "merge_status": None,
        "merge_run_status": None,
        "duration_s": 8.0,
        "cost_usd": 0.0,
        "stories_passed": 0,
        "stories_tested": 0,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_error": None,
    }


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
            "alive": True,
            "watcher": {"pid": 4321},
            "counts": {"queued": 1, "running": 1, "done": 0},
            "health": {
                "state": "running",
                "blocking_pid": 4321,
                "watcher_pid": 4321,
                "watcher_process_alive": True,
                "lock_pid": 4321,
                "lock_process_alive": True,
                "heartbeat": "2026-04-25T12:00:00Z",
                "heartbeat_age_s": 1.0,
                "started_at": "2026-04-25T11:55:00Z",
                "log_path": "/tmp/watcher.log",
                "next_action": "",
            },
        },
        "landing": {
            "items": [
                _landing_item(QUEUED_RUN_ID, QUEUED_TASK_ID, queue_status="queued"),
                _landing_item(RUNNING_RUN_ID, RUNNING_TASK_ID, queue_status="running"),
            ],
            "counts": {"ready": 0, "merged": 0, "blocked": 2, "total": 2},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
        },
        "live": {
            "items": [
                _live_item(QUEUED_RUN_ID, QUEUED_TASK_ID, status="queued"),
                _live_item(RUNNING_RUN_ID, RUNNING_TASK_ID, status="running"),
            ],
            "total_count": 2,
            "active_count": 2,
            # Long interval so the test never races against a real refresh.
            "refresh_interval_s": 60.0,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 2,
            "state_tasks": 2,
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
            "files": {
                "queue": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "state": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "commands": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "processing": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
            },
            "supervisor": {
                "mode": "running",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": 4321,
                "matches_blocking_pid": True,
                "can_start": False,
                "can_stop": True,
                "start_blocked_reason": None,
                "stop_target_pid": 4321,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": 4321,
            },
            "issues": [],
        },
    }


def _detail_for(run_id: str, task_id: str, *, status: str) -> dict[str, Any]:
    """RunDetail payload — the inspector's next-action button fires
    `review-next-action-button` against this run when active."""

    return {
        "run_id": run_id,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": task_id,
        "status": status,
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": task_id,
        "merge_id": None,
        "branch": f"otto/{task_id}",
        "worktree": f".worktrees/{task_id}",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": status,
        "active": True,
        "source": "live",
        "title": task_id,
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [
            {"key": "c", "label": "Cancel", "enabled": True, "reason": None, "preview": "Stop the run"}
        ],
        "review_packet": {
            "headline": task_id,
            "status": status,
            "summary": task_id,
            "readiness": {
                "state": "in_progress",
                "label": "In progress",
                "tone": "info",
                "blockers": [],
                "next_step": "Cancel if you want to stop the run.",
            },
            "checks": [],
            # Inspector's next-action button — the BUG was that keyboard
            # nav landed here instead of on a per-row cancel target.
            "next_action": {
                "label": "Cancel",
                "action_key": "c",
                "enabled": True,
                "reason": None,
            },
            "certification": {
                "stories_passed": 0,
                "stories_tested": 0,
                "passed": False,
                "summary_path": None,
                "stories": [],
                "proof_report": {"json_path": None, "html_path": None, "html_url": None, "available": False},
            },
            "changes": {
                "branch": f"otto/{task_id}",
                "target": "main",
                "merged": False,
                "merge_id": None,
                "file_count": 0,
                "files": [],
                "truncated": False,
                "diff_command": "",
                "diff_error": None,
            },
            "evidence": [],
            "failure": None,
        },
        "landing_state": None,
        "merge_info": None,
        "record": {},
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
    body = json.dumps(_state())

    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/state*", handler)


def _install_detail_routes(page: Any) -> None:
    """Install detail handlers for both runs.

    The inspector calls ``GET /api/runs/{run_id}`` when a row is selected.
    Each run gets its own next-action button — proving the inspector
    button is per-run, distinct from the per-row cancel affordance.
    """

    queued_body = json.dumps(_detail_for(QUEUED_RUN_ID, QUEUED_TASK_ID, status="queued"))
    running_body = json.dumps(_detail_for(RUNNING_RUN_ID, RUNNING_TASK_ID, status="running"))

    def handler(route: Any) -> None:
        url = route.request.url
        if QUEUED_RUN_ID in url:
            route.fulfill(status=200, content_type="application/json", body=queued_body)
        elif RUNNING_RUN_ID in url:
            route.fulfill(status=200, content_type="application/json", body=running_body)
        else:
            route.fulfill(status=404, content_type="application/json", body="{}")

    page.route("**/api/runs/run-*", handler)


def _install_artifacts_route(page: Any) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": "", "artifacts": []}),
        )

    page.route("**/api/runs/*/artifacts", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def _per_row_cancel_testid(task_id: str) -> str:
    """Mirror App.tsx's testid sanitisation: `[^a-zA-Z0-9_-]+ → -`."""

    sanitised = "".join(
        ch if ch.isalnum() or ch in ("_", "-") else "-" for ch in task_id
    )
    return f"task-card-cancel-{sanitised}"


def test_keyboard_cancel_on_queued_card_fires_cancel(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Focus the per-row Cancel button on a queued task card via keyboard,
    activate it (Enter), confirm the dialog. Assert the cancel POST hits
    /api/runs/{QUEUED_RUN_ID}/actions/cancel — and ONLY that endpoint."""

    posts: dict[str, list[str]] = {"cancel_for_queued": [], "other": []}
    posts_lock = threading.Lock()

    _install_projects_route(page)
    _install_state_route(page)
    _install_detail_routes(page)
    _install_artifacts_route(page)

    def cancel_for_queued(route: Any) -> None:
        with posts_lock:
            posts["cancel_for_queued"].append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "cancel requested", "refresh": False}),
        )

    def other_action(route: Any) -> None:
        # Catch any POST we didn't whitelist explicitly. If the bug returns
        # this fires for the inspector's review-next-action-button.
        with posts_lock:
            posts["other"].append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "other", "refresh": False}),
        )

    # Playwright resolves routes in REVERSE registration order (last-registered
    # is tried first). Register the wildcard catch-all FIRST so the specific
    # cancel route below overrides it for /actions/cancel against QUEUED_RUN_ID.
    page.route("**/api/runs/*/actions/*", other_action)
    page.route(f"**/api/runs/{QUEUED_RUN_ID}/actions/cancel", cancel_for_queued)

    _hydrate(mc_backend, page, disable_animations)

    # The per-row cancel button is rendered for any cancel-eligible card.
    cancel_btn_id = _per_row_cancel_testid(QUEUED_TASK_ID)
    cancel_btn = page.get_by_test_id(cancel_btn_id)
    cancel_btn.wait_for(state="visible", timeout=5_000)
    assert cancel_btn.is_enabled(), (
        f"per-row cancel button must be enabled (testid={cancel_btn_id})"
    )

    # Keyboard-only path: focus the cancel button, press Enter.
    cancel_btn.focus()
    page.keyboard.press("Enter")

    # Confirm dialog opens; press Enter again on the confirm button.
    page.wait_for_selector(".confirm-dialog", timeout=5_000)
    confirm_btn = page.get_by_test_id("confirm-dialog-confirm-button")
    confirm_btn.focus()
    page.keyboard.press("Enter")

    # Poll our captured posts. A short sleep is OK because the routes are
    # synchronous fulfill handlers in the playwright proxy.
    import time as _time
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        with posts_lock:
            if posts["cancel_for_queued"]:
                break
        _time.sleep(0.05)

    with posts_lock:
        assert posts["cancel_for_queued"], (
            "expected POST /api/runs/{queued}/actions/cancel to fire from "
            f"keyboard activation; got cancel={posts['cancel_for_queued']!r}, "
            f"other={posts['other']!r}"
        )
        # CRITICAL: no other action POST fires. If the bug regressed
        # (Tab-walk past the row → inspector's review-next-action), the
        # cancel for the inspector's *running* run would fire here.
        assert not posts["other"], (
            "no other action endpoints should fire from per-row cancel "
            f"keyboard path; got: {posts['other']!r}"
        )


def test_keyboard_cancel_does_not_fire_inspector_next_action(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Open the inspector on the RUNNING task (so its
    `review-next-action-button` targets RUNNING_RUN_ID), then keyboard-
    cancel the QUEUED card. Assert the cancel POST hits the QUEUED run
    and the inspector's next-action POST never fires for the running
    run."""

    posts: dict[str, list[str]] = {
        "cancel_queued": [],
        "cancel_running": [],
        "other": [],
    }
    posts_lock = threading.Lock()

    _install_projects_route(page)
    _install_state_route(page)
    _install_detail_routes(page)
    _install_artifacts_route(page)

    def cancel_queued(route: Any) -> None:
        with posts_lock:
            posts["cancel_queued"].append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "cancel queued", "refresh": False}),
        )

    def cancel_running(route: Any) -> None:
        with posts_lock:
            posts["cancel_running"].append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "cancel running", "refresh": False}),
        )

    def other(route: Any) -> None:
        with posts_lock:
            posts["other"].append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "other", "refresh": False}),
        )

    # Wildcard catch-all FIRST (Playwright matches routes in reverse
    # registration order; last-registered wins).
    page.route("**/api/runs/*/actions/*", other)
    page.route(f"**/api/runs/{QUEUED_RUN_ID}/actions/cancel", cancel_queued)
    page.route(f"**/api/runs/{RUNNING_RUN_ID}/actions/cancel", cancel_running)

    _hydrate(mc_backend, page, disable_animations)

    # Open inspector on the RUNNING run by clicking its main card body.
    running_main = page.get_by_test_id(f"task-card-{RUNNING_TASK_ID}")
    running_main.wait_for(state="visible", timeout=5_000)
    running_main.click()

    # Wait for the inspector's review-next-action-button to be in the DOM
    # — this is the button the bug used to land on.
    page.wait_for_selector(
        "[data-testid='review-next-action-button']", timeout=5_000
    )

    # Now keyboard-cancel the QUEUED card via its per-row Cancel.
    cancel_btn_id = _per_row_cancel_testid(QUEUED_TASK_ID)
    cancel_btn = page.get_by_test_id(cancel_btn_id)
    cancel_btn.wait_for(state="visible", timeout=5_000)
    cancel_btn.focus()
    page.keyboard.press("Enter")

    page.wait_for_selector(".confirm-dialog", timeout=5_000)
    page.get_by_test_id("confirm-dialog-confirm-button").focus()
    page.keyboard.press("Enter")

    import time as _time
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        with posts_lock:
            if posts["cancel_queued"]:
                break
        _time.sleep(0.05)

    with posts_lock:
        assert posts["cancel_queued"], (
            "expected cancel POST against the QUEUED run; got "
            f"queued={posts['cancel_queued']!r} "
            f"running={posts['cancel_running']!r} other={posts['other']!r}"
        )
        # The data-loss bug: the inspector's next-action button targets
        # the RUNNING run; if Tab/Enter fires THAT button instead of the
        # per-row cancel, this assertion fails.
        assert not posts["cancel_running"], (
            "inspector's next-action button must NOT fire for the running "
            f"run when the user keyboard-cancels the queued card; got: "
            f"{posts['cancel_running']!r}"
        )
        assert not posts["other"], (
            f"no other action endpoints should fire; got: {posts['other']!r}"
        )


def test_keyboard_cancel_target_matches_visual_focus(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The focused element when a user Tab-walks from the queued card's
    main button must be the per-row cancel — NOT the inspector's
    review-next-action-button. Asserts on the focused element's
    data-testid attribute."""

    _install_projects_route(page)
    _install_state_route(page)
    _install_detail_routes(page)
    _install_artifacts_route(page)

    _hydrate(mc_backend, page, disable_animations)

    # Open the inspector on the RUNNING run so its
    # `review-next-action-button` is in the DOM (it's where the bug used
    # to land). The QUEUED card's row-level focus chain must still walk
    # to the per-row cancel before any inspector tabstop.
    running_main = page.get_by_test_id(f"task-card-{RUNNING_TASK_ID}")
    running_main.wait_for(state="visible", timeout=5_000)
    running_main.click()

    page.wait_for_selector(
        "[data-testid='review-next-action-button']", timeout=5_000
    )

    # Focus the queued task's main card button explicitly.
    queued_main_id = f"task-card-{QUEUED_TASK_ID}"
    queued_main = page.get_by_test_id(queued_main_id)
    queued_main.wait_for(state="visible", timeout=5_000)
    queued_main.focus()

    # Tab once: the next focusable element should be the row's per-row
    # Cancel button (our fix). Before the fix, Tab walked past the row
    # entirely and (depending on layout) eventually landed on the
    # inspector's review-next-action-button.
    page.keyboard.press("Tab")

    focused_testid = page.evaluate(
        """() => document.activeElement && document.activeElement.getAttribute('data-testid')"""
    )

    cancel_btn_id = _per_row_cancel_testid(QUEUED_TASK_ID)
    assert focused_testid == cancel_btn_id, (
        f"Tab from queued card's main button must land on per-row cancel "
        f"({cancel_btn_id!r}); got {focused_testid!r}. If this is "
        f"'review-next-action-button', the W8-CRITICAL-1 bug has regressed."
    )
    assert focused_testid != "review-next-action-button", (
        "focus must NOT walk into the inspector's next-action button when "
        "the user expected to cancel the row"
    )
