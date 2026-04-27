"""Browser regression for mc-audit codex-first-time-user #15 — first-run
projects avoid internal counters and show an actionable task-list empty state.

Before fix: a brand-new project showed ``Watcher: stopped``,
``Heartbeat: -``, ``In flight: 0``, ``queued 0 / ready 0 / landed 0`` —
seven internal-vocabulary counters dumped on a first-run user with no
context.

After fix: ``ProjectMeta`` switches to a single-line summary,
"Project ready · No jobs yet". The full counter dashboard returns the
moment ANY counter fills (history row, live run, queued task, landing
item).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_sidebar_first_run_collapsed.py \\
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


def _state_empty() -> dict[str, Any]:
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
            "items": [],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
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
            "queue_tasks": 0,
            "state_tasks": 0,
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


def _state_with_history() -> dict[str, Any]:
    payload = _state_empty()
    payload["history"]["items"] = [
        {
            "run_id": "run-old-1",
            "domain": "build",
            "run_type": "build",
            "command": "build",
            "status": "completed",
            "terminal_outcome": "success",
            "queue_task_id": "old-task",
            "merge_id": None,
            "branch": "feature/x",
            "worktree": None,
            "summary": "first build",
            "intent": "first build",
            "completed_at_display": "2026-04-25 11:30",
            "outcome_display": "success",
            "duration_s": 120,
            "duration_display": "2m",
            "cost_usd": 0.05,
            "cost_display": "$0.05",
            "resumable": False,
            "adapter_key": "build",
        }
    ]
    payload["history"]["total_rows"] = 1
    return payload


def _install_routes(page: Any, payload: dict[str, Any]) -> None:
    def projects(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_projects_payload()))

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_first_run_collapses_to_single_status_line(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Empty project shows a concise first-run task-board empty state."""

    _install_routes(page, _state_empty())
    _hydrate(mc_backend, page, disable_animations)

    collapsed = page.get_by_test_id("task-board-empty")
    collapsed.wait_for(state="visible", timeout=5_000)

    text = collapsed.text_content() or ""
    assert "No work queued" in text and "Queue your first job" in text, (
        f"expected first-run empty state; got {text!r}"
    )

    assert page.locator("[data-testid=project-meta-first-run]").count() == 0
    assert page.locator("[data-testid=project-meta-full]").count() == 0


def test_populated_project_shows_full_counters(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Once a project has history, project stats are available in the supplementary section."""

    _install_routes(page, _state_with_history())
    _hydrate(mc_backend, page, disable_animations)

    page.get_by_test_id("tasks-supplementary").locator("summary").click()
    overview = page.locator(".project-overview")
    overview.wait_for(state="visible", timeout=5_000)

    text = overview.text_content() or ""
    assert "Run history" in text and "1 runs" in text, f"expected project overview stats; got {text!r}"

    # Legacy sidebar metadata is no longer rendered in the topbar redesign.
    collapsed_count = page.locator("[data-testid=project-meta-first-run]").count()
    assert collapsed_count == 0, "legacy first-run project meta should not render"
