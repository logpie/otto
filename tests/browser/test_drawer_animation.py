"""Browser regression for task-list microinteractions.

Mission Control now uses a single task list instead of the older card drawer.
These tests preserve the original intent of the drawer-animation regression:
state changes should be visually legible, fast, and disabled under reduced
motion.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_drawer_animation.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _projects_payload() -> dict[str, Any]:
    return {
        "launcher_enabled": False,
        "projects_root": "/tmp/managed",
        "current": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": "abc1234",
        },
        "projects": [],
    }


def _landing_item() -> dict[str, Any]:
    return {
        "task_id": "task-drawer",
        "summary": "build the drawer scenario",
        "branch": "build/task-drawer",
        "branch_exists": True,
        "queue_status": "done",
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": None,
        "queue_finished_at": None,
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": "ready",
        "merge_blocked": False,
        "blockers": [],
        "merge_id": None,
        "merged_at": None,
        "diff_path": None,
        "diff_relpath": None,
        "diff_error": None,
        "changed_file_count": 3,
        "changed_files": ["a.py", "b.py", "c.py"],
        "diff_size_bytes": 120,
        "diff_truncated": False,
        "merge_target": SAMPLE_TARGET,
        "merge_base_sha": None,
        "head_sha": None,
        "target_sha": None,
        "exit_code": None,
        "elapsed_s": 90,
        "cost_usd": 0.05,
        "duration_s": 90,
        "stories_passed": 2,
        "stories_tested": 2,
        "label": "ready",
        "merge_status": None,
        "merge_run_status": None,
        "actions": [],
        "intent": None,
        "run_id": "run-drawer",
    }


def _state() -> dict[str, Any]:
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": "abc1234",
            "defaults": {
                "provider": "claude",
                "model": "sonnet-4-7",
                "reasoning_effort": "high",
                "certifier_mode": "fast",
                "skip_product_qa": False,
                "config_file_exists": True,
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
            "items": [_landing_item()],
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {"items": [], "total_count": 0, "active_count": 0, "refresh_interval_s": 1.5},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 80, "truncated": False},
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
                "mode": "manual",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": None,
                "matches_blocking_pid": None,
                "can_start": False,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
                "stop_target_pid": None,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": None,
            },
            "issues": [],
        },
    }


def _install_routes(page: Any) -> None:
    payload = _state()

    def projects(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_projects_payload()))

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(mc_backend: Any, page: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)


def test_task_row_marks_selection_on_click(mc_backend: Any, page: Any) -> None:
    """Clicking a task row gives it a selected treatment."""

    _install_routes(page)
    _hydrate(mc_backend, page)

    row_button = page.get_by_test_id("task-card-task-drawer")
    row_button.wait_for(state="visible", timeout=5_000)
    row_button.click()
    page.wait_for_function(
        "() => document.querySelector('.queue-list-row-task')?.classList.contains('selected') === true",
        timeout=2_000,
    )


def test_task_row_background_transition_under_200ms(mc_backend: Any, page: Any) -> None:
    """Task row hover/selection transition is fast enough for list scanning."""

    _install_routes(page)
    _hydrate(mc_backend, page)

    row = page.locator(".queue-list-row-task").first
    row.wait_for(state="visible", timeout=5_000)

    info = page.evaluate(
        """() => {
            const row = document.querySelector('.queue-list-row-task');
            if (!row) return null;
            const style = window.getComputedStyle(row);
            return {
                transitionProperty: style.transitionProperty,
                transitionDuration: style.transitionDuration,
            };
        }"""
    )
    assert info is not None, "expected .queue-list-row-task on the page"
    prop = info["transitionProperty"] or ""
    assert "background" in prop or prop == "all", (
        f"task row should transition background; got {info!r}"
    )
    duration = info["transitionDuration"] or "0s"
    seconds = float(duration.split(",")[0].rstrip("s"))
    assert seconds <= 0.20, (
        f"task row transition must be ≤200ms; got {duration!r}"
    )


def test_drawer_transition_disabled_under_reduced_motion(
    mc_backend: Any, browser: Any
) -> None:
    """With prefers-reduced-motion=reduce, transition-duration must be ≤1ms."""

    context = browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    try:
        _install_routes(page)
        _hydrate(mc_backend, page)

        row = page.locator(".queue-list-row-task").first
        row.wait_for(state="visible", timeout=5_000)

        info = page.evaluate(
            """() => {
                const row = document.querySelector('.queue-list-row-task');
                return {
                    rowDuration: row ? window.getComputedStyle(row).transitionDuration : null,
                };
            }"""
        )
        duration = info["rowDuration"] or "0s"
        seconds = float(duration.split(",")[0].rstrip("s"))
        assert seconds <= 0.0011, (
            f"row transition under prefers-reduced-motion must be ≤1ms; got {duration!r}"
        )
    finally:
        context.close()
