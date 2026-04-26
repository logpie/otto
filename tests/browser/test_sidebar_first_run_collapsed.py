"""Browser regression for mc-audit codex-first-time-user #15 — sidebar
collapses Watcher/Heartbeat/In-flight/queued/ready/landed counters when
the project has zero history AND no live runs.

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
    """Empty project shows the collapsed first-run sidebar variant."""

    _install_routes(page, _state_empty())
    _hydrate(mc_backend, page, disable_animations)

    collapsed = page.get_by_test_id("project-meta-first-run")
    collapsed.wait_for(state="visible", timeout=5_000)

    text = collapsed.text_content() or ""
    assert "Project ready" in text and "No jobs yet" in text, (
        f"expected first-run collapsed status line; got {text!r}"
    )

    # Detailed counters MUST NOT be rendered on first-run.
    info = page.evaluate(
        """() => ({
            watcher: !!document.body.textContent && /Watcher/.test(document.querySelector('.project-meta')?.textContent || ''),
            heartbeat: /Heartbeat/.test(document.querySelector('.project-meta')?.textContent || ''),
            inFlight: /In flight/.test(document.querySelector('.project-meta')?.textContent || ''),
            tasks: /queued .* ready .* landed/.test(document.querySelector('.project-meta')?.textContent || ''),
        })"""
    )
    assert not info["watcher"], f"first-run sidebar must not show Watcher counter; got {info!r}"
    assert not info["heartbeat"], f"first-run sidebar must not show Heartbeat counter; got {info!r}"
    assert not info["inFlight"], f"first-run sidebar must not show In flight counter; got {info!r}"
    assert not info["tasks"], f"first-run sidebar must not show queued/ready/landed line; got {info!r}"


def test_populated_project_shows_full_counters(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Once a project has a single history row, the full sidebar counters render."""

    _install_routes(page, _state_with_history())
    _hydrate(mc_backend, page, disable_animations)

    full = page.get_by_test_id("project-meta-full")
    full.wait_for(state="visible", timeout=5_000)

    text = full.text_content() or ""
    assert "Watcher" in text, f"expected Watcher counter; got {text!r}"
    assert "Heartbeat" in text, f"expected Heartbeat counter; got {text!r}"
    assert "In flight" in text, f"expected In flight counter; got {text!r}"
    assert "queued" in text and "ready" in text and "landed" in text, (
        f"expected queued/ready/landed counter line; got {text!r}"
    )

    # The collapsed first-run variant must NOT be present when full is rendered.
    collapsed_count = page.locator("[data-testid=project-meta-first-run]").count()
    assert collapsed_count == 0, "first-run variant must not coexist with full ProjectMeta"
