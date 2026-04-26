"""Browser regression tests for the modal-backdrop cleanup bug.

Source: live W11 dogfood run uncovered W11-CRITICAL-2 — after a JobDialog
was dismissed (or its Submit silently rejected by the dirty-target guard),
the ``<div class="modal-backdrop">`` lingered on the page and intercepted
every subsequent click. Documented in
``docs/mc-audit/live-findings.md`` (search "W11-CRITICAL-2").

The fix lives in ``otto/web/client/src/App.tsx`` — both ``JobDialog`` and
``ConfirmDialog`` now wire an ``onClick`` on the backdrop that calls
``onClose`` / ``onCancel`` when the click target is the backdrop itself
(not a dialog descendant), and skips dismissal while a POST is mid-flight
or a 3-second grace window is counting down.

These tests pin down three invariants:

1. With no dialog open the backdrop must NOT be present in the DOM
   (relies on conditional render, not CSS toggling).
2. After dismissing a JobDialog (Escape, Close button, or backdrop click)
   the backdrop is removed and a button below where the dialog used to be
   becomes clickable.
3. Same invariant for ConfirmDialog.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_modal_backdrop_cleanup.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


# ---------------------------------------------------------------------------
# Synthetic state — keep minimal; we only need a project + an empty queue
# so the New job button shows and a Land ready button can later open a
# confirm dialog.
# ---------------------------------------------------------------------------


def _ready_landing_item(idx: int) -> dict[str, Any]:
    task_id = f"task-{idx:02d}"
    return {
        "task_id": task_id,
        "run_id": f"run-{idx:02d}",
        "branch": f"otto/{task_id}",
        "worktree": f".worktrees/{task_id}",
        "summary": f"task {idx}",
        "queue_status": "done",
        "landing_state": "ready",
        "label": "Ready to land",
        "merge_id": None,
        "merge_status": None,
        "merge_run_status": None,
        "duration_s": 12.0,
        "cost_usd": 0.0,
        "stories_passed": 1,
        "stories_tested": 1,
        "changed_file_count": 2 + idx,
        "changed_files": [f"src/{task_id}/a.txt", f"src/{task_id}/b.txt"],
        "diff_error": None,
    }


def _state(ready_count: int = 0) -> dict[str, Any]:
    items = [_ready_landing_item(i) for i in range(1, ready_count + 1)]
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
            "counts": {"queued": 0, "running": 0, "done": ready_count},
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
            "items": items,
            "counts": {"ready": ready_count, "merged": 0, "blocked": 0, "total": ready_count},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [
                {
                    "run_id": item["run_id"],
                    "domain": "queue",
                    "run_type": "queue",
                    "command": "build",
                    "display_name": item["task_id"],
                    "status": "done",
                    "terminal_outcome": "success",
                    "project_dir": "/tmp/proj",
                    "cwd": "/tmp/proj",
                    "queue_task_id": item["task_id"],
                    "merge_id": None,
                    "branch": item["branch"],
                    "worktree": item["worktree"],
                    "provider": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "adapter_key": "queue.attempt",
                    "version": 1,
                    "display_status": "done",
                    "active": False,
                    "display_id": item["run_id"],
                    "branch_task": item["task_id"],
                    "elapsed_s": 12.0,
                    "elapsed_display": "12s",
                    "cost_usd": 0.0,
                    "cost_display": "$0.00",
                    "last_event": "done",
                    "row_label": item["task_id"],
                    "overlay": None,
                }
                for item in items
            ],
            "total_count": ready_count,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": ready_count,
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
            "files": {
                "queue": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "state": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "commands": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "processing": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
            },
            "supervisor": {
                "mode": "stopped",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": None,
                "matches_blocking_pid": False,
                "can_start": False,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_target_pid": None,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": None,
            },
            "issues": [],
        },
    }


def _install_projects_route(page: Any) -> None:
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


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/state*", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)


def _open_job_dialog(page: Any) -> None:
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector(".job-dialog", timeout=5_000)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_backdrop_with_no_dialog(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Initial render must not contain a stray .modal-backdrop element."""

    payload = _state(ready_count=0)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    # New-job button rendered → app fully hydrated. No dialog opened yet.
    page.get_by_test_id("new-job-button").wait_for(state="visible", timeout=5_000)
    assert page.locator(".modal-backdrop").count() == 0, (
        ".modal-backdrop must not be in the DOM when no dialog is open"
    )


def test_backdrop_removed_after_job_dialog_escape(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Open JobDialog → Escape → backdrop removed and underlying button clickable."""

    payload = _state(ready_count=0)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    new_job = page.get_by_test_id("new-job-button")
    new_job.wait_for(state="visible", timeout=5_000)

    _open_job_dialog(page)
    assert page.locator(".modal-backdrop").count() == 1, "backdrop should mount with dialog"

    # Press Escape. The intent textarea is initially focused inside the dialog;
    # the useDialogFocus hook listens on the dialog element so we send the key
    # to the page (Playwright dispatches to the focused element).
    page.keyboard.press("Escape")

    # Wait for the dialog to detach.
    page.wait_for_selector(".job-dialog", state="detached", timeout=5_000)

    # Backdrop must be gone too.
    assert page.locator(".modal-backdrop").count() == 0, (
        ".modal-backdrop lingered after JobDialog dismissal — would block clicks"
    )

    # Clicking the New job button (which sat below the backdrop) must work.
    # If the backdrop were still intercepting, this click would time out.
    new_job.click(timeout=2_000)
    page.wait_for_selector(".job-dialog", timeout=5_000)


def test_backdrop_click_dismisses_job_dialog(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Defense-in-depth: clicking the backdrop (outside the dialog) closes it.

    This is the user-facing fix for W11-CRITICAL-2 — even if a JobDialog was
    silently rejected at submit time and the user perceives it as closed, a
    click on the visible-but-unrecognized backdrop dismisses the dialog
    rather than getting stuck.
    """

    payload = _state(ready_count=0)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)
    _open_job_dialog(page)
    assert page.locator(".modal-backdrop").count() == 1

    # Click the backdrop at coordinates outside the dialog (e.g. top-left
    # corner inside the backdrop padding area). Use force=True so Playwright
    # doesn't reroute to a child element via the actionability check.
    page.locator(".modal-backdrop").click(position={"x": 5, "y": 5})

    page.wait_for_selector(".job-dialog", state="detached", timeout=5_000)
    assert page.locator(".modal-backdrop").count() == 0


def test_backdrop_click_inside_dialog_does_not_dismiss(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Clicking inside the dialog (e.g. on a label) must not close the dialog.

    Guards the ``event.target !== event.currentTarget`` check in the
    onBackdropClick handler — without it, every click inside the form would
    bubble and dismiss the dialog.
    """

    payload = _state(ready_count=0)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)
    _open_job_dialog(page)

    # Click on the dialog header (a non-button area); the dialog must stay open.
    page.locator(".job-dialog h2").click()
    assert page.locator(".job-dialog").count() == 1, (
        "click inside the dialog must not bubble up to the backdrop dismiss"
    )
    assert page.locator(".modal-backdrop").count() == 1


def test_backdrop_removed_after_confirm_dialog_escape(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Open ConfirmDialog (via Land ready) → Escape → backdrop removed."""

    payload = _state(ready_count=2)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    land_btn = page.get_by_test_id("mission-land-ready-button")
    land_btn.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=mission-land-ready-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    land_btn.click()

    page.wait_for_selector(".confirm-dialog", timeout=5_000)
    assert page.locator(".modal-backdrop").count() == 1, "confirm backdrop should mount"

    page.keyboard.press("Escape")
    page.wait_for_selector(".confirm-dialog", state="detached", timeout=5_000)

    assert page.locator(".modal-backdrop").count() == 0, (
        ".modal-backdrop lingered after ConfirmDialog dismissal — would block clicks"
    )

    # Sanity: subsequent click on the same Land button works.
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=mission-land-ready-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    land_btn.click(timeout=2_000)
    page.wait_for_selector(".confirm-dialog", timeout=5_000)
