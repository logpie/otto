"""Browser regression for codex error-empty-states #11 — Task Board
empty-state copy must reflect *why* it's empty (filtered vs. truly empty).

Source: ``docs/mc-audit/findings.md`` error-empty-states #11. With
filters active, the prior empty banner read "No work queued" — telling
the user to queue more work even though they had work that was just
hidden by the filter. The fix in ``otto/web/client/src/App.tsx``
``TaskBoard`` adds a ``computeBoardEmptyReason()`` helper that classifies
the empty as ``loading | true-empty | filtered-empty | no-project`` and
the banner offers ``Clear filters`` (filtered) or ``Queue job`` (true-empty).

Invariants:
  - With tasks present + filters that match nothing, the banner reads
    "No matching tasks." and exposes a ``data-testid='task-board-empty-clear-filters'``
    button.
  - With no tasks and filters at default, the banner reads "No work
    queued..." and exposes a ``Queue job`` button.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _landing_item(
    *,
    task_id: str,
    landing_state: str = "ready",
    queue_status: str = "done",
    run_id: str | None = "r-x",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "summary": f"build the {task_id}",
        "branch": f"build/{task_id}",
        "branch_exists": True,
        "queue_status": queue_status,
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": "2026-04-25T12:00:00Z",
        "queue_finished_at": "2026-04-25T12:01:00Z",
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": landing_state,
        "merge_blocked": False,
        "blockers": [],
        "merge_id": None,
        "merged_at": None,
        "diff_path": None,
        "diff_relpath": None,
        "diff_error": None,
        "changed_file_count": 1,
        "changed_files": ["x.py"],
        "diff_size_bytes": 0,
        "diff_truncated": False,
        "merge_target": SAMPLE_TARGET,
        "merge_base_sha": None,
        "head_sha": None,
        "target_sha": None,
        "exit_code": 0,
        "elapsed_s": 60.0,
        "cost_usd": 0.05,
        "actions": [],
        "intent": None,
        "run_id": run_id,
    }


def _base_state(items: list[dict[str, Any]]) -> dict[str, Any]:
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
            "items": items,
            "counts": {"ready": len(items), "merged": 0, "blocked": 0, "total": len(items)},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [],
            "total_count": 0,
            "active_count": 0,
            "refresh_interval_s": 0.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": len(items),
            "state_tasks": len(items),
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


def _install_routes(page: Any, payload: dict[str, Any]) -> None:
    def projects(route: Any) -> None:
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

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)


def test_filtered_empty_says_no_matching_tasks(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """With tasks present and a filter that hides them, the banner must
    say "No matching tasks" — NOT "No work queued."."""

    payload = _base_state([_landing_item(task_id="ready-x")])
    _install_routes(page, payload)
    _hydrate(mc_backend, page, disable_animations)

    # Type a search query that does not match any task to trigger the
    # filtered-empty state.
    search = page.locator(
        "[data-testid='filter-search-input'], input[type='search'], input[placeholder*='Search' i]"
    ).first
    search.wait_for(state="visible", timeout=5_000)
    search.fill("zzz-no-match-string-zzz")

    banner = page.locator("[data-testid='task-board-empty']")
    banner.wait_for(state="visible", timeout=5_000)
    text = (banner.text_content() or "").lower()
    assert "no matching tasks" in text, (
        f"banner should say 'No matching tasks' under filtered-empty, got {text!r}"
    )
    assert "no work queued" not in text, (
        f"banner regressed to filter-blind 'No work queued.': {text!r}"
    )

    # Clear-filters CTA must be present and clickable.
    clear_btn = page.locator("[data-testid='task-board-empty-clear-filters']")
    clear_btn.wait_for(state="visible", timeout=2_000)
    assert clear_btn.is_enabled()

    # Subtitle of the panel must also reflect filter awareness.
    subtitle = page.locator("[data-testid='task-board'] .panel-subtitle").first
    subtitle.wait_for(state="visible", timeout=2_000)
    sub_text = (subtitle.text_content() or "").lower()
    assert "no tasks match" in sub_text or "filters" in sub_text or "hidden" in sub_text, (
        f"panel subtitle should reflect filter awareness, got {sub_text!r}"
    )


def test_true_empty_offers_queue_job_action(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """With zero tasks total and default filters, the banner must offer
    a Queue job action — not pretend the filters are hiding anything."""

    payload = _base_state([])
    _install_routes(page, payload)
    _hydrate(mc_backend, page, disable_animations)

    banner = page.locator("[data-testid='task-board-empty']")
    banner.wait_for(state="visible", timeout=5_000)
    text = (banner.text_content() or "").lower()
    assert "no work queued" in text, (
        f"true-empty banner should say 'No work queued', got {text!r}"
    )
    queue_btn = page.locator("[data-testid='task-board-empty-queue-job']")
    queue_btn.wait_for(state="visible", timeout=2_000)
    assert queue_btn.is_enabled()


def test_filter_blind_does_not_appear_when_tasks_visible(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """With tasks visible (no active filters), the empty banner must NOT
    render — only the columns. Negative control."""

    payload = _base_state([_landing_item(task_id="visible-x")])
    _install_routes(page, payload)
    _hydrate(mc_backend, page, disable_animations)

    page.locator("[data-testid='task-board']").wait_for(state="visible", timeout=5_000)
    banner = page.locator("[data-testid='task-board-empty']")
    # Either the locator is missing or it's hidden — either way, count == 0.
    assert banner.count() == 0, (
        "task-board-empty banner should not render when tasks are visible"
    )
