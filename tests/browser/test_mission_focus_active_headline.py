"""Browser regression for codex info-density #6 — Mission Focus working
state must surface a headline for the hottest active run, not just a count.

Source: ``docs/mc-audit/findings.md`` info-density theme. The prior copy
read ``"<N> task(s) in flight"`` even when only one run was active. With
multi-second LLM builds + several queue tasks racing, the user couldn't
tell which run was hottest, what branch it was on, how long it had run,
how much it had cost, or what the latest event was — all data available
in ``data.live.items``.

The fix in ``otto/web/client/src/App.tsx`` (``missionFocus`` working
branch) joins ``task-id · branch · elapsed · cost · last event`` for the
freshest live item. Each segment is omitted when missing so the line
never reads as ``" ·  · "``.

Invariant: when one queue task is running with a known branch + elapsed,
the H2 inside ``[data-testid='mission-focus']`` includes the task id and
branch, and is NOT just ``"1 task in flight"``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _live_running_item(
    *,
    queue_task_id: str = "build-feature-x",
    branch: str = "build/build-feature-x",
    elapsed_s: float = 42.0,
    elapsed_display: str = "42s",
    cost_display: str = "$0.12",
    last_event: str = "tool_use:Bash",
) -> dict[str, Any]:
    return {
        "run_id": "2026-04-26-011751-aaaaaa",
        "domain": "queue",
        "run_type": "build",
        "command": "build",
        "display_name": "build-feature-x",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj/.worktrees/build-feature-x",
        "queue_task_id": queue_task_id,
        "merge_id": None,
        "branch": branch,
        "worktree": ".worktrees/build-feature-x",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.build",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": "2026-04-26-011751-aaaaaa",
        "branch_task": "build-feature-x",
        "elapsed_s": elapsed_s,
        "elapsed_display": elapsed_display,
        "cost_usd": 0.12,
        "cost_display": cost_display,
        "last_event": last_event,
        "row_label": "build-feature-x",
        "overlay": None,
    }


def _state_with_running_task(item: dict[str, Any]) -> dict[str, Any]:
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
            "alive": True,
            "watcher": None,
            "counts": {"queued": 0, "running": 1, "done": 0},
            "health": {
                "state": "running",
                "blocking_pid": None,
                "watcher_pid": 1234,
                "watcher_process_alive": True,
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
        "live": {
            "items": [item],
            "total_count": 1,
            "active_count": 1,
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
                "mode": "running",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": 1234,
                "matches_blocking_pid": False,
                "can_start": False,
                "can_stop": True,
                "start_blocked_reason": None,
                "stop_target_pid": 1234,
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
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_mission_focus_headline_includes_task_branch_elapsed_cost_and_event(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """When one task is running, the headline must include identifying info."""

    item = _live_running_item()
    _install_projects_route(page)
    _install_state_route(page, _state_with_running_task(item))

    _hydrate(mc_backend, page, disable_animations)

    focus = page.locator("[data-testid='mission-focus']")
    focus.wait_for(state="visible", timeout=5_000)
    heading = focus.locator("h2").first
    text = heading.text_content() or ""

    # Must NOT be the bare-count fallback.
    assert "task in flight" not in text and "tasks in flight" not in text, (
        f"mission focus headline regressed to plain count: {text!r}"
    )

    # Must include the task id, branch, elapsed display, cost display, and
    # the last event — all separated by a middle dot.
    for needle in [
        item["queue_task_id"],
        item["branch"],
        item["elapsed_display"],
        item["cost_display"],
        item["last_event"],
    ]:
        assert needle in text, f"expected {needle!r} in headline {text!r}"


def test_mission_focus_headline_omits_missing_segments(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """When cost is the loading placeholder ('…'), it is omitted from the headline."""

    item = _live_running_item(cost_display="…")
    _install_projects_route(page)
    _install_state_route(page, _state_with_running_task(item))

    _hydrate(mc_backend, page, disable_animations)

    focus = page.locator("[data-testid='mission-focus']")
    focus.wait_for(state="visible", timeout=5_000)
    heading = focus.locator("h2").first
    text = heading.text_content() or ""

    assert "…" not in text, (
        f"placeholder cost ('…') leaked into mission focus headline: {text!r}"
    )
    # task id + branch + elapsed + last event still surface
    assert item["queue_task_id"] in text
    assert item["branch"] in text


def test_mission_focus_headline_falls_back_to_count_when_no_live_items(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Defensive: if the working count is non-zero but live.items is empty
    (transient inconsistency between landing + live), fall back to the
    plain count instead of crashing."""

    payload = _state_with_running_task(_live_running_item())
    payload["live"]["items"] = []
    payload["live"]["total_count"] = 0
    payload["live"]["active_count"] = 0
    # But fake the landing side so taskBoardColumns still computes working > 0.
    payload["landing"]["items"] = [{
        "task_id": "build-feature-x",
        "summary": "build the feature",
        "branch": "build/build-feature-x",
        "branch_exists": True,
        "queue_status": "running",
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": "2026-04-25T12:00:00Z",
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
        "run_id": None,
    }]
    payload["landing"]["counts"] = {"ready": 0, "merged": 0, "blocked": 0, "total": 1}
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    focus = page.locator("[data-testid='mission-focus']")
    focus.wait_for(state="visible", timeout=5_000)
    heading = focus.locator("h2").first
    text = heading.text_content() or ""

    # Either the landing-derived headline OR the bare count fallback is
    # acceptable — the test is that the page renders without crashing.
    assert text.strip(), f"mission focus headline must not be empty, got {text!r}"
