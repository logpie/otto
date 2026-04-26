"""Browser regression for codex first-time-user #19 — queued task cards
without a runId must be clickable so the user can open Details.

Source: ``docs/mc-audit/findings.md`` codex first-time-user theme #19.
The prior ``TaskCard`` rendered ``disabled={!task.runId}``, so a queued
card whose watcher hadn't picked it up yet had a dead button — the
user could see it but couldn't open Details, leaving them with no way
to inspect the queued intent or know what to do next.

The fix in ``otto/web/client/src/App.tsx`` adds an ``onSelectQueued``
prop to ``TaskCard`` and a ``selectedQueuedTask`` state in App. The
``RunDetailPanel`` renders a "Waiting for watcher" placeholder with
status, branch, intent, and a Start watcher CTA when the queued-task
selection is set.

Invariants:
  - A queued landing item without ``run_id`` renders a clickable card.
  - Clicking opens ``[data-testid='run-detail-queued']`` in the detail panel.
  - The placeholder shows the task title + a "Start watcher" CTA when
    the watcher is stopped.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _queued_landing_item(*, task_id: str = "build-pending") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "summary": f"build the {task_id} feature",
        "branch": f"build/{task_id}",
        "branch_exists": True,
        "queue_status": "queued",
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": None,
        "queue_finished_at": None,
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": "waiting",
        "merge_blocked": False,
        "blockers": [],
        "merge_id": None,
        "merged_at": None,
        "diff_path": None,
        "diff_relpath": None,
        "diff_error": None,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_size_bytes": 0,
        "diff_truncated": False,
        "merge_target": SAMPLE_TARGET,
        "merge_base_sha": None,
        "head_sha": None,
        "target_sha": None,
        "exit_code": None,
        "elapsed_s": None,
        "cost_usd": None,
        "actions": [],
        "intent": None,
        "run_id": None,  # the bug: no runId yet → card was disabled
    }


def _state_with_queued_task(item: dict[str, Any], *, watcher_running: bool = False) -> dict[str, Any]:
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
            "alive": watcher_running,
            "watcher": None,
            "counts": {"queued": 1, "running": 0, "done": 0},
            "health": {
                "state": "running" if watcher_running else "stopped",
                "blocking_pid": None,
                "watcher_pid": 1234 if watcher_running else None,
                "watcher_process_alive": watcher_running,
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
            "items": [item],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 1},
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
                "mode": "running" if watcher_running else "stopped",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": 1234 if watcher_running else None,
                "matches_blocking_pid": False,
                "can_start": not watcher_running,
                "can_stop": watcher_running,
                "start_blocked_reason": None,
                "stop_target_pid": 1234 if watcher_running else None,
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


def test_queued_card_without_run_id_is_clickable(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A queued landing item with run_id=None must render an enabled
    button. Before the fix, the card's main button was ``disabled``."""

    item = _queued_landing_item(task_id="needs-watcher")
    _install_routes(page, _state_with_queued_task(item))
    _hydrate(mc_backend, page, disable_animations)

    card_button = page.locator(
        "[data-testid='task-card-needs-watcher']"
    )
    card_button.wait_for(state="visible", timeout=5_000)
    assert card_button.is_enabled(), (
        "queued card without runId must be clickable; was disabled in the bug"
    )
    # The data attribute documents intent for downstream automation.
    queued_attr = card_button.get_attribute("data-queued-no-run")
    assert queued_attr == "true", (
        f"data-queued-no-run should be 'true' for queued cards, got {queued_attr!r}"
    )


def test_clicking_queued_card_opens_waiting_placeholder(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Clicking a queued card opens a 'Waiting for watcher' placeholder
    in the detail panel — including the task title."""

    item = _queued_landing_item(task_id="needs-watcher")
    _install_routes(page, _state_with_queued_task(item))
    _hydrate(mc_backend, page, disable_animations)

    card_button = page.locator("[data-testid='task-card-needs-watcher']")
    card_button.wait_for(state="visible", timeout=5_000)
    card_button.click()

    placeholder = page.locator("[data-testid='run-detail-queued']")
    placeholder.wait_for(state="visible", timeout=5_000)
    text = placeholder.text_content() or ""
    assert "needs-watcher" in text, (
        f"placeholder must include the task title, got {text!r}"
    )
    assert "waiting for watcher" in text.lower(), (
        f"placeholder must explain the wait, got {text!r}"
    )


def test_queued_placeholder_offers_start_watcher_when_stopped(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """When the watcher is stopped, the placeholder must surface a
    Start watcher CTA. When it's running, the CTA is hidden."""

    # Stopped watcher
    item = _queued_landing_item(task_id="needs-watcher")
    _install_routes(page, _state_with_queued_task(item, watcher_running=False))
    _hydrate(mc_backend, page, disable_animations)

    page.locator("[data-testid='task-card-needs-watcher']").click()
    cta = page.locator("[data-testid='run-detail-queued-start-watcher']")
    cta.wait_for(state="visible", timeout=5_000)
    assert cta.is_enabled()
