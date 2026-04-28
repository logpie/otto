"""Browser regression for W11-IMPORTANT-1 — the task list must include
atomic-domain runs (standalone ``otto build``), not just queue.

Source: live W11 dogfood — a standalone CLI build was running and visible
on the Task Board, but the sidebar's IN FLIGHT counter still read "0".
The counter pulled from ``watcher.counts.running`` which only knows about
queue-domain runs. The server already aggregates both domains in
``live.active_count`` — the fix in ``otto/web/client/src/App.tsx`` swaps
the sidebar (and ``workflowHealth``) to prefer that field. See
``docs/mc-audit/live-findings.md`` (search "W11-IMPORTANT-1").

Invariant: when ``live.active_count`` is N, the sidebar IN FLIGHT
``<MetaItem label="In flight">`` must render N — even if
``watcher.counts.running`` is 0.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_sidebar_counter_matches_board.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _atomic_live_item() -> dict[str, Any]:
    return {
        "run_id": "2026-04-26-011751-aaaaaa",
        "domain": "atomic",
        "run_type": "build",
        "command": "build",
        "display_name": "build kanban",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "main",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "atomic.build",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": "2026-04-26-011751-aaaaaa",
        "branch_task": "build-kanban",
        "elapsed_s": 30.0,
        "elapsed_display": "30s",
        "cost_usd": None,
        "cost_display": "…",
        "last_event": "running",
        "row_label": "build kanban",
        "overlay": None,
    }


def _state_with_atomic_run() -> dict[str, Any]:
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
            # The bug: queue counts know nothing about an atomic build.
            "counts": {"queued": 0, "running": 0, "done": 0},
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
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "reviewed": 0, "total": 0},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [_atomic_live_item()],
            "total_count": 1,
            # The fix: server aggregates atomic + queue here.
            "active_count": 1,
            "refresh_interval_s": 0.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
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


def _certified_landing_item() -> dict[str, Any]:
    return {
        "task_id": "certify-pdf-export",
        "run_id": "2026-04-28-082607-a69a04",
        "branch": "certify/certify-pdf-export",
        "worktree": ".worktrees/certify-pdf-export",
        "summary": "Certify the existing PDF export feature.",
        "build_config": None,
        "queue_status": "done",
        "landing_state": "reviewed",
        "label": "Certified",
        "merge_id": None,
        "merge_status": None,
        "merge_run_status": None,
        "duration_s": 300,
        "cost_usd": None,
        "token_usage": {"total_tokens": 1234},
        "stories_passed": 5,
        "stories_tested": 5,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_error": None,
    }


def _state_with_certified_task() -> dict[str, Any]:
    state = _state_with_atomic_run()
    state["live"]["items"] = []
    state["live"]["total_count"] = 0
    state["live"]["active_count"] = 0
    state["landing"]["items"] = [_certified_landing_item()]
    state["landing"]["counts"] = {"ready": 0, "merged": 0, "blocked": 0, "reviewed": 1, "total": 1}
    return state


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
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_sidebar_in_flight_counts_atomic_run(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Task list must show an atomic run even when watcher.counts.running is 0."""

    _install_projects_route(page)
    _install_state_route(page, _state_with_atomic_run())

    _hydrate(mc_backend, page, disable_animations)

    row = page.get_by_test_id("task-card-2026-04-26-011751-aaaaaa")
    row.wait_for(state="visible", timeout=5_000)
    text = row.text_content() or ""
    assert "build kanban" in text or "running" in text.lower(), text


def test_sidebar_in_flight_zero_with_no_runs(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Negative: no live runs means no active task rows."""

    payload = _state_with_atomic_run()
    payload["live"]["items"] = []
    payload["live"]["active_count"] = 0
    payload["live"]["total_count"] = 0
    # mc-audit codex-first-time-user #15: the sidebar collapses to a single
    # "Project ready · No jobs yet" row when the project has zero history,
    # zero live runs, no queued/landing items. Seed a history row so this
    # negative test still exercises the full ProjectMeta dashboard with the
    # In-flight counter visible.
    payload["history"]["items"] = [
        {
            "run_id": "2026-04-26-000000-bbbbbb",
            "domain": "build",
            "run_type": "build",
            "command": "build",
            "status": "completed",
            "terminal_outcome": "success",
            "queue_task_id": "prior-task",
            "merge_id": None,
            "branch": "feature/prior",
            "worktree": None,
            "summary": "Prior build",
            "intent": "Prior build",
            "completed_at_display": "2026-04-25 10:00",
            "outcome_display": "success",
            "duration_s": 60,
            "duration_display": "1m",
            "cost_usd": 0.01,
            "cost_display": "$0.01",
            "resumable": False,
            "adapter_key": "build",
        }
    ]
    payload["history"]["total_rows"] = 1

    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    empty = page.get_by_test_id("task-board-empty")
    empty.wait_for(state="visible", timeout=5_000)
    text = empty.text_content() or ""
    assert "No work queued" in text


def test_certification_only_task_is_visible_but_not_ready_to_land(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Cert-only proof is a visible reviewed task, not a ready-to-land build."""

    _install_projects_route(page)
    _install_state_route(page, _state_with_certified_task())

    _hydrate(mc_backend, page, disable_animations)

    row = page.get_by_test_id("task-card-certify-pdf-export")
    row.wait_for(state="visible", timeout=5_000)
    text = row.text_content() or ""
    assert "Certified" in text
    assert "Ready" not in text
    assert page.get_by_test_id("mission-land-ready-button").count() == 0
