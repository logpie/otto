"""Browser test for ConfirmDialog header-close affordance, by tone.

Cluster: mc-audit microinteractions I6 (IMPORTANT).

Problem: ConfirmDialog renders BOTH a header "Close" button and a footer
"Cancel" button. In danger-tone flows (cleanup, cancel) the two close
affordances dilute focus — neither is emphasised as the safe choice
next to the red destructive CTA.

Fix: Drop the header Close affordance for danger-tone confirms (footer
Cancel remains as the single safe-choice button, with extra emphasis).
Non-danger confirms (and JobDialog) keep their header Close so they
match the rest of the dialog family.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_confirm_dialog_danger_no_x.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


RUN_ID = "run-confirm-tone"
TASK_ID = "task-confirm-tone"


def _live_item(status: str = "failed") -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": TASK_ID,
        "status": status,
        "terminal_outcome": status,
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
        "active": False,
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


def _state(status: str = "failed") -> dict[str, Any]:
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
            "counts": {"queued": 0, "running": 0, "done": 1},
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
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
        },
        "live": {
            "items": [_live_item(status)],
            "total_count": 1,
            "active_count": 0,
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


def _detail(status: str = "failed") -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": TASK_ID,
        "status": status,
        "terminal_outcome": status,
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
        "active": False,
        "source": "live",
        "title": TASK_ID,
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [
            {"key": "x", "label": "Cleanup", "enabled": True, "reason": None, "preview": "Remove run"}
        ],
        "review_packet": {
            "headline": TASK_ID,
            "status": status,
            "summary": TASK_ID,
            "readiness": {
                "state": "needs_attention",
                "label": "Needs attention",
                "tone": "danger",
                "blockers": [],
                "next_step": "",
            },
            "checks": [],
            "next_action": {"label": "Cleanup", "action_key": "x", "enabled": True, "reason": None},
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
                "file_count": 1,
                "files": ["a.ts"],
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
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


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


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_danger_confirm_dialog_has_no_header_close_button(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Cleanup is a danger-tone confirm — header Close button must be absent.

    Footer Cancel is the single safe-choice path; the header × is dropped
    so the operator's eye is not split between two abort affordances.
    """

    payload = _state(status="failed")
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page)
    _install_artifacts_route(page)

    _hydrate(mc_backend, page, disable_animations)

    # Open the run from the task board.
    card = page.get_by_test_id(f"task-card-{TASK_ID}")
    card.wait_for(state="visible", timeout=5_000)
    card.click()

    # Trigger cleanup via the recovery action surfaced for failed runs.
    cleanup_btn = page.get_by_test_id("recovery-action-cleanup")
    cleanup_btn.wait_for(state="visible", timeout=5_000)
    cleanup_btn.click()

    page.wait_for_selector(".confirm-dialog", timeout=5_000)
    dialog = page.locator(".confirm-dialog")

    # The dialog is marked danger.
    tone = dialog.get_attribute("data-tone") or ""
    assert tone == "danger", f"expected danger tone, got {tone!r}"

    # The header Close button is GONE in danger flows.
    header_close = dialog.locator('[data-testid=confirm-dialog-header-close]')
    assert header_close.count() == 0, (
        "danger-tone confirm dialogs must NOT render a header Close button"
    )

    # Footer Cancel still exists, with the safe-choice emphasis class.
    footer_cancel = dialog.locator('[data-testid=confirm-dialog-cancel-button]')
    assert footer_cancel.count() == 1, "footer Cancel must still exist"
    classes = footer_cancel.first.get_attribute("class") or ""
    assert "cancel-emphasis" in classes, (
        f"footer Cancel must carry cancel-emphasis class in danger flows, got {classes!r}"
    )


def test_safe_dialog_keeps_header_close_button(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """JobDialog (a non-danger / informational dialog) keeps its header
    Close affordance — the I6 fix only targets danger-tone confirms."""

    payload = _state(status="failed")
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    new_job_btn = page.get_by_test_id("new-job-button")
    new_job_btn.wait_for(state="visible", timeout=5_000)
    new_job_btn.click()

    page.wait_for_selector(".job-dialog", timeout=5_000)
    # JobDialog still has a header Close button — it is not a danger flow.
    header_close = page.locator(".job-dialog header button", has_text="Close")
    assert header_close.count() == 1, (
        "JobDialog (safe / non-danger) must retain its header Close button"
    )
