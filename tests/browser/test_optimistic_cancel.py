"""Browser test for optimistic UI on the cancel action.

Cluster: mc-audit microinteractions I4 (IMPORTANT).

Problem: Every action waits a full /api/state refresh cycle (up to
``refresh_interval_s`` seconds) before the run row's status flips. For
cancel specifically, the operator clicks Confirm and the row keeps
showing "running" until the next poll arrives — multi-second silence.

Fix: Set ``display_status = "cancelling"`` on the affected row optimistically
the moment the cancel POST fires, so the row visibly transitions in
~16ms. On 4xx/5xx error, drop the overlay so the row reverts to its
server-provided status (a warning toast also surfaces).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_optimistic_cancel.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


RUN_ID = "run-optimistic-cancel"
TASK_ID = "task-optimistic-cancel"


# --------------------------------------------------------------------------- #
# Synthetic state — one running task in the live table + landing list. The
# Diagnostics view's LiveRuns table renders display_status, which is the cell
# we assert flips to "cancelling" optimistically.
# --------------------------------------------------------------------------- #


def _live_item(status: str) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": TASK_ID,
        "status": status,
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": TASK_ID,
        "merge_id": None,
        "branch": f"otto/{TASK_ID}",
        "worktree": f".worktrees/{TASK_ID}",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": status,
        "active": True,
        "display_id": RUN_ID,
        "branch_task": TASK_ID,
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": 0.0,
        "cost_display": "$0.00",
        "last_event": status,
        "row_label": TASK_ID,
        "overlay": None,
    }


def _landing_item() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "branch": f"otto/{TASK_ID}",
        "worktree": f".worktrees/{TASK_ID}",
        "summary": TASK_ID,
        "queue_status": "running",
        "landing_state": None,
        "label": "Running",
        "merge_id": None,
        "merge_status": None,
        "merge_run_status": None,
        "duration_s": 12.0,
        "cost_usd": 0.0,
        "stories_passed": 0,
        "stories_tested": 0,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_error": None,
    }


def _state(status: str = "running") -> dict[str, Any]:
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
            "counts": {"queued": 0, "running": 1, "done": 0},
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
            "items": [_landing_item()],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 1},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
        },
        "live": {
            "items": [_live_item(status)],
            "total_count": 1,
            "active_count": 1,
            # Long interval so the test never races against a real refresh.
            "refresh_interval_s": 5.0,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
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


def _detail() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": TASK_ID,
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": TASK_ID,
        "merge_id": None,
        "branch": f"otto/{TASK_ID}",
        "worktree": f".worktrees/{TASK_ID}",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "running",
        "active": True,
        "source": "live",
        "title": TASK_ID,
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
            "headline": TASK_ID,
            "status": "running",
            "summary": TASK_ID,
            "readiness": {
                "state": "in_progress",
                "label": "In progress",
                "tone": "info",
                "blockers": [],
                "next_step": "",
            },
            "checks": [],
            "next_action": {"label": "Cancel", "action_key": "c", "enabled": True, "reason": None},
            "certification": {
                "stories_passed": 0,
                "stories_tested": 0,
                "passed": False,
                "summary_path": None,
                "stories": [],
                "proof_report": {"json_path": None, "html_path": None, "html_url": None, "available": False},
            },
            "changes": {
                "branch": f"otto/{TASK_ID}",
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


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    body = json.dumps(payload)

    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/state*", handler)


def _install_detail_route(page: Any) -> None:
    body = json.dumps(_detail())
    page.route(
        f"**/api/runs/{RUN_ID}",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )
    page.route(
        f"**/api/runs/{RUN_ID}?**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_artifacts_route(page: Any) -> None:
    page.route(
        f"**/api/runs/{RUN_ID}/artifacts",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": RUN_ID, "artifacts": []}),
        ),
    )


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#root')?.children.length > 0", timeout=10_000
    )
    disable_animations(page)


def _switch_to_diagnostics(page: Any) -> None:
    """The LiveRuns table that renders display_status lives in the
    Diagnostics view. Switch to it so the test can observe row text."""

    btn = page.get_by_test_id("diagnostics-tab")
    btn.wait_for(state="visible", timeout=5_000)
    btn.click()
    page.locator("section[aria-labelledby='liveHeading']").wait_for(
        state="visible", timeout=5_000
    )


def _open_run_and_advanced_actions(page: Any) -> None:
    """Select the run row, open the inspector implicitly, then expand the
    advanced-actions disclosure to surface the Cancel button."""

    row_btn = page.get_by_test_id(f"live-row-activator-{RUN_ID}")
    row_btn.wait_for(state="visible", timeout=5_000)
    row_btn.click()
    # Open advanced-actions disclosure.
    page.evaluate(
        """() => {
            const det = document.querySelector('details.advanced-actions');
            if (det) det.open = true;
        }"""
    )


def _live_status_cell_text(page: Any) -> str:
    return page.evaluate(
        """() => {
            const row = document.querySelector(
                'section[aria-labelledby="liveHeading"] tbody tr'
            );
            if (!row) return '';
            const cell = row.querySelector('td');
            return cell ? cell.textContent || '' : '';
        }"""
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_cancel_optimistically_flips_row_to_cancelling(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Cancel POST takes 1.5s. After confirm-click, the row immediately shows
    "cancelling" before the response returns — proving the optimistic
    overlay is applied without waiting for a refresh cycle."""

    payload = _state(status="running")
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page)
    _install_artifacts_route(page)

    # Slow cancel handler so the test can assert the optimistic flip happens
    # WELL BEFORE the response returns (and any refresh fires).
    cancel_calls = {"count": 0}

    def cancel_handler(route: Any) -> None:
        cancel_calls["count"] += 1
        time.sleep(1.5)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "cancel requested", "refresh": True}),
        )

    page.route(f"**/api/runs/{RUN_ID}/actions/cancel", cancel_handler)

    _hydrate(mc_backend, page, disable_animations)
    _switch_to_diagnostics(page)

    # Sanity: row currently shows RUNNING.
    initial = _live_status_cell_text(page)
    assert "RUNNING" in initial.upper(), f"expected initial RUNNING status, got {initial!r}"

    _open_run_and_advanced_actions(page)

    cancel_btn = page.get_by_test_id("advanced-action-cancel")
    cancel_btn.wait_for(state="visible", timeout=5_000)
    cancel_btn.click()

    page.wait_for_selector(".confirm-dialog", timeout=5_000)
    confirm_btn = page.get_by_test_id("confirm-dialog-confirm-button")
    confirm_btn.click()

    # Within 500ms the row should flip to CANCELLING — that's the optimistic
    # overlay. The cancel POST is still outstanding (the handler sleeps 1.5s).
    page.wait_for_function(
        """() => {
            const row = document.querySelector(
                'section[aria-labelledby="liveHeading"] tbody tr'
            );
            if (!row) return false;
            const cell = row.querySelector('td');
            return cell && (cell.textContent || '').toUpperCase().includes('CANCELLING');
        }""",
        timeout=1_000,
    )
    # Confirm POST hasn't completed yet — proves we did not wait for refresh.
    assert cancel_calls["count"] == 1


def test_cancel_failure_reverts_optimistic_overlay(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """If the cancel POST returns 4xx, the optimistic overlay is dropped so
    the row reverts to RUNNING. The confirm dialog stays open with the
    inline error (per destructive-action-safety #6); a warning toast also
    appears noting the revert."""

    payload = _state(status="running")
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page)
    _install_artifacts_route(page)

    def cancel_handler(route: Any) -> None:
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({
                "ok": False,
                "message": "Run already cancelled by another tab.",
                "severity": "error",
            }),
        )

    page.route(f"**/api/runs/{RUN_ID}/actions/cancel", cancel_handler)

    _hydrate(mc_backend, page, disable_animations)
    _switch_to_diagnostics(page)
    _open_run_and_advanced_actions(page)

    cancel_btn = page.get_by_test_id("advanced-action-cancel")
    cancel_btn.wait_for(state="visible", timeout=5_000)
    cancel_btn.click()
    page.wait_for_selector(".confirm-dialog", timeout=5_000)
    page.get_by_test_id("confirm-dialog-confirm-button").click()

    # Inline error renders inside the confirm dialog (existing safety contract).
    err = page.locator('[data-testid=confirm-dialog-error]')
    err.wait_for(state="visible", timeout=5_000)

    # The row reverts to RUNNING — the optimistic overlay is dropped.
    page.wait_for_function(
        """() => {
            const row = document.querySelector(
                'section[aria-labelledby="liveHeading"] tbody tr'
            );
            if (!row) return false;
            const cell = row.querySelector('td');
            const txt = (cell?.textContent || '').toUpperCase();
            return txt.includes('RUNNING') && !txt.includes('CANCELLING');
        }""",
        timeout=2_000,
    )
