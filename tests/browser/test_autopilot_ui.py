"""Browser coverage for the Mission Control Autopilot surface.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \
      .venv/bin/pytest tests/browser/test_autopilot_ui.py -m browser -p playwright -q
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.browser.test_app_ready_marker import (
    _install_projects_route,
    _install_state_route,
    _projects_payload,
    _state_payload,
)

pytestmark = pytest.mark.browser


def _autopilot_payload() -> dict[str, Any]:
    return {
        "mode": "assisted",
        "configured_mode": "assisted",
        "enabled": True,
        "health": "attention",
        "last_tick_at": "2026-04-25T12:00:00Z",
        "next_tick_hint": "1 recovery decision awaiting approval.",
        "policy": {
            "mode": "assisted",
            "max_actions_per_hour": 8,
            "max_pilot_calls_per_hour": 2,
            "allow_auto_land": False,
            "verification_policy": "smart",
            "pilot_enabled": True,
            "pilot_timeout_s": 300,
        },
        "budgets": {
            "actions_used_last_hour": 0,
            "actions_limit_per_hour": 8,
            "pilot_calls_used_last_hour": 0,
            "pilot_calls_limit_per_hour": 2,
        },
        "counters": {
            "incidents_open": 1,
            "decisions_pending": 1,
            "actions_executed": 0,
            "actions_failed": 0,
        },
        "incidents": [{
            "id": "incident-queued",
            "kind": "queued_without_runner",
            "severity": "info",
            "title": "Queued work is waiting",
            "detail": "1 queued task will not start until the queue runner is running.",
            "action": "start_watcher",
            "run_id": None,
            "task_id": None,
        }],
        "pending_decisions": [{
            "id": "decision-start",
            "incident_id": "incident-queued",
            "created_at": "2026-04-25T12:00:00Z",
            "title": "Queued work is waiting",
            "action": "start_watcher",
            "action_label": "Start queue runner",
            "reason": "1 queued task will not start until the queue runner is running.",
            "severity": "info",
            "target": "queue",
            "run_id": None,
            "task_id": None,
            "requires_pilot": False,
            "status": "pending",
            "result": None,
            "error": None,
        }],
        "recent_events": [{
            "id": "event-scan",
            "created_at": "2026-04-25T12:00:00Z",
            "kind": "scan",
            "severity": "info",
            "message": "Autopilot found 1 suggested action.",
            "incident_id": "incident-queued",
            "decision_id": "decision-start",
            "action": "start_watcher",
            "details": {},
        }],
    }


def test_autopilot_panel_shows_living_loop_and_approval(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    state = _state_payload()
    state["autopilot"] = _autopilot_payload()
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, state)

    tick_posts = 0
    approve_posts = 0

    def tick_handler(route: Any) -> None:
        nonlocal tick_posts
        tick_posts += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "Autopilot found 1 suggested action.", "refresh": True}),
        )

    def approve_handler(route: Any) -> None:
        nonlocal approve_posts
        approve_posts += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "watcher launch requested", "refresh": True}),
        )

    page.route("**/api/autopilot/tick", tick_handler)
    page.route("**/api/autopilot/decisions/**/approve", approve_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("diagnostics-tab").click()
    panel = page.get_by_test_id("autopilot-panel")
    panel.wait_for(state="visible", timeout=5_000)

    assert "Autopilot" in panel.text_content()
    assert "Observe" in panel.text_content()
    assert "Queued work is waiting" in panel.text_content()
    assert page.get_by_test_id("autopilot-mode-select").input_value() == "assisted"

    page.get_by_test_id("autopilot-scan-button").click()
    page.get_by_test_id("autopilot-approve-button").click()

    page.wait_for_timeout(200)
    assert tick_posts == 1
    assert approve_posts == 1
