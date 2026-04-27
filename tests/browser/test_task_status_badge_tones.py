"""Browser regression for codex info-density #3 — task status badges
must render with a tone class so ready/blocked/running/failed/cancelled
scan distinctly.

Source: ``docs/mc-audit/findings.md`` info-density theme #3. The prior
``.task-status`` class was a single gray pill regardless of meaning.

The fix in ``otto/web/client/src/styles.css`` adds tone variants
(``status-tone-info`` / ``status-tone-success`` / ``status-tone-running`` /
``status-tone-warning`` / ``status-tone-danger`` / ``status-tone-neutral``) and ``App.tsx``'s
``TaskCard`` adds them via the ``statusTone()`` helper.

Invariant: every visible task-status badge has a ``status-tone-*`` class
suffix; the data-attribute ``data-status-tone`` matches the visible
status semantics (info/success/running/warning/danger).
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
    landing_state: str = "waiting",
    queue_status: str = "queued",
    branch: str | None = None,
    merge_blocked: bool = False,
    blockers: list[str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "summary": f"build the {task_id}",
        "branch": branch or f"build/{task_id}",
        "branch_exists": True,
        "queue_status": queue_status,
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": None,
        "queue_finished_at": None,
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": landing_state,
        "merge_blocked": merge_blocked,
        "blockers": blockers or [],
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
        "run_id": run_id,
    }


def _state_with_mixed_tasks() -> dict[str, Any]:
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
            "counts": {"queued": 1, "running": 0, "done": 0},
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
            "items": [
                _landing_item(task_id="ready-task", landing_state="ready", queue_status="done", run_id="r-ready"),
                _landing_item(task_id="failed-task", landing_state="waiting", queue_status="failed", run_id="r-failed"),
                _landing_item(task_id="queued-task", landing_state="waiting", queue_status="queued"),
                _landing_item(task_id="landed-task", landing_state="merged", queue_status="done", run_id="r-landed"),
            ],
            "counts": {"ready": 1, "merged": 1, "blocked": 0, "total": 4},
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
            "queue_tasks": 4,
            "state_tasks": 4,
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
                "can_start": True,
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
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_status_badges_carry_distinct_tones(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """ready/landed/failed/queued must render with distinct tone classes."""

    _install_routes(page, _state_with_mixed_tasks())
    _hydrate(mc_backend, page, disable_animations)

    board = page.locator("[data-testid='task-board']")
    board.wait_for(state="visible", timeout=5_000)

    # Pull every visible task-status badge.
    badges = page.locator(".task-status")
    badges.first.wait_for(state="visible", timeout=5_000)
    count = badges.count()
    assert count >= 4, f"expected >=4 task-status badges, got {count}"

    tones: set[str] = set()
    for index in range(count):
        badge = badges.nth(index)
        cls = (badge.get_attribute("class") or "")
        tone_attr = badge.get_attribute("data-status-tone")
        # Every badge must have a tone class AND a matching data attribute.
        assert "status-tone-" in cls, (
            f"badge #{index} missing status-tone-* class: {cls!r}"
        )
        assert tone_attr in {"info", "success", "running", "warning", "danger", "neutral"}, (
            f"badge #{index} has unexpected data-status-tone={tone_attr!r}"
        )
        # The tone class suffix must match the data attribute.
        assert f"status-tone-{tone_attr}" in cls, (
            f"badge #{index} class {cls!r} does not match data-status-tone={tone_attr!r}"
        )
        tones.add(tone_attr or "")

    # With ready + failed + queued + landed in the mix, ready must be its
    # own action-needed info tone instead of looking identical to landed.
    assert "info" in tones, f"missing ready/action-needed info tone, observed {tones}"
    assert "success" in tones, f"missing success tone, observed {tones}"
    assert "danger" in tones, f"missing danger tone, observed {tones}"


def test_status_badge_visual_color_differs_per_tone(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Computed background-color must differ between success and danger
    badges. Without distinct CSS, the tone classes would be cosmetic."""

    _install_routes(page, _state_with_mixed_tasks())
    _hydrate(mc_backend, page, disable_animations)

    board = page.locator("[data-testid='task-board']")
    board.wait_for(state="visible", timeout=5_000)

    success_badge = page.locator(".task-status.status-tone-success").first
    danger_badge = page.locator(".task-status.status-tone-danger").first
    success_badge.wait_for(state="visible", timeout=5_000)
    danger_badge.wait_for(state="visible", timeout=5_000)

    success_bg = success_badge.evaluate(
        "(el) => window.getComputedStyle(el).backgroundColor"
    )
    danger_bg = danger_badge.evaluate(
        "(el) => window.getComputedStyle(el).backgroundColor"
    )

    assert success_bg != danger_bg, (
        f"success ({success_bg}) and danger ({danger_bg}) badges share "
        "the same background — tone classes have no visual effect."
    )


def test_ready_and_landed_badges_are_visually_distinct(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Ready means review/land; landed is terminal. They must not look identical."""

    _install_routes(page, _state_with_mixed_tasks())
    _hydrate(mc_backend, page, disable_animations)

    ready_badge = page.locator("[data-task-id='ready-task'] .task-status")
    landed_badge = page.locator("[data-task-id='landed-task'] .task-status")
    ready_badge.wait_for(state="visible", timeout=5_000)
    landed_badge.wait_for(state="visible", timeout=5_000)

    assert ready_badge.get_attribute("data-status-tone") == "info"
    assert landed_badge.get_attribute("data-status-tone") == "success"
    assert "Ready" in (ready_badge.text_content() or "")
    assert "Ready to land" in (ready_badge.get_attribute("title") or "")
    assert "Landed" in (landed_badge.text_content() or "")
    assert "→" in (ready_badge.locator(".status-icon").text_content() or "")
    assert "✓" in (landed_badge.locator(".status-icon").text_content() or "")
