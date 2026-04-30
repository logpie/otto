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
        "pilot_agent": {
            "agent_type": "diagnostic",
            "provider": "claude",
            "model": "provider default",
            "reasoning_effort": "low",
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
    assert "Recommended action" in panel.text_content()
    assert "Queued work is waiting" in panel.text_content()
    assert "Start queue runner" in panel.text_content()
    assert "Pilot diagnostic · claude · provider default · low" in panel.text_content()
    assert page.get_by_test_id("autopilot-mode-select").input_value() == "assisted"

    page.get_by_test_id("autopilot-scan-button").click()
    page.get_by_test_id("autopilot-approve-button").click()

    page.wait_for_timeout(200)
    assert tick_posts == 1
    assert approve_posts == 1


def test_full_autopilot_checks_recoverable_work_without_manual_scan(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    state = _state_payload()
    autopilot = _autopilot_payload()
    autopilot["mode"] = "full"
    autopilot["policy"] = {**autopilot["policy"], "mode": "full"}
    state["autopilot"] = autopilot
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, state)

    tick_posts = 0

    def tick_handler(route: Any) -> None:
        nonlocal tick_posts
        tick_posts += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "Autopilot executed 1 recovery action.", "refresh": True}),
        )

    page.route("**/api/autopilot/tick", tick_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("diagnostics-tab").click()
    page.get_by_test_id("autopilot-panel").wait_for(state="visible", timeout=5_000)
    page.wait_for_timeout(500)

    assert tick_posts == 1


def test_autopilot_panel_collapses_pilot_triage_duplicates(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    state = _state_payload()
    autopilot = _autopilot_payload()
    interrupted_decision = {
        "id": "decision-pilot",
        "incident_id": "incident-interrupted",
        "created_at": "2026-04-25T12:00:00Z",
        "title": "Run interrupted",
        "action": "pilot_triage",
        "action_label": "Diagnose issue",
        "reason": "certify-existing: read-only certification was interrupted.",
        "severity": "warning",
        "target": "run-1",
        "run_id": "run-1",
        "task_id": "certify-existing",
        "requires_pilot": True,
        "status": "pending",
        "result": None,
        "error": None,
    }
    autopilot["incidents"] = [{
        "id": "incident-interrupted",
        "kind": "run_interrupted",
        "severity": "warning",
        "title": "Run interrupted",
        "detail": "certify-existing was interrupted.",
        "action": "pilot_triage",
        "run_id": "run-1",
        "task_id": "certify-existing",
    }]
    autopilot["pending_decisions"] = [interrupted_decision]
    autopilot["recent_events"] = [
        {
            "id": "event-noop",
            "created_at": "2026-04-25T12:00:03Z",
            "kind": "pilot.noop",
            "severity": "info",
            "message": "Pilot recommended no action",
            "incident_id": "incident-interrupted",
            "decision_id": "decision-pilot",
            "action": "pilot_triage",
            "details": {"pilot_plan": {"reason": "No code changes were produced.", "required_verification": "Requeue the cert if it gates release."}},
        },
        {
            "id": "event-completed",
            "created_at": "2026-04-25T12:00:02Z",
            "kind": "pilot.completed",
            "severity": "info",
            "message": "Pilot triage completed",
            "incident_id": "incident-interrupted",
            "decision_id": "decision-pilot",
            "action": "pilot_triage",
            "details": {},
        },
        {
            "id": "event-requested",
            "created_at": "2026-04-25T12:00:01Z",
            "kind": "pilot.requested",
            "severity": "info",
            "message": "Pilot triage requested",
            "incident_id": "incident-interrupted",
            "decision_id": "decision-pilot",
            "action": "pilot_triage",
            "details": {},
        },
    ]
    state["autopilot"] = autopilot
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("diagnostics-tab").click()
    panel = page.get_by_test_id("autopilot-panel")
    panel.wait_for(state="visible", timeout=5_000)

    content = panel.text_content()
    assert content is not None
    assert content.count("Run interrupted") == 1
    assert "Diagnose issue" in content
    assert "Approve recovery" not in content
    assert "Pilot triage completed" not in content
    assert "Pilot triage requested" not in content


def test_autopilot_pilot_running_state_disables_repeat_click(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    state = _state_payload()
    autopilot = _autopilot_payload()
    autopilot["pending_decisions"][0] = {
        **autopilot["pending_decisions"][0],
        "id": "decision-pilot-running",
        "incident_id": "incident-interrupted",
        "title": "Diagnose task",
        "action": "pilot_triage",
        "action_label": "Diagnose issue",
        "reason": "Task needs diagnosis before Otto chooses a recovery action.",
        "status": "running",
        "run_id": "run-1",
        "task_id": "certify-existing",
        "requires_pilot": True,
    }
    autopilot["incidents"] = [{
        "id": "incident-interrupted",
        "kind": "landing_failed",
        "severity": "warning",
        "title": "Diagnose task",
        "detail": "Task needs diagnosis before Otto chooses a recovery action.",
        "action": "pilot_triage",
        "run_id": "run-1",
        "task_id": "certify-existing",
    }]
    state["autopilot"] = autopilot
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("diagnostics-tab").click()
    panel = page.get_by_test_id("autopilot-panel")
    panel.wait_for(state="visible", timeout=5_000)

    assert "Pilot is diagnosing the selected recovery." in panel.text_content()
    button = page.get_by_test_id("autopilot-approve-button")
    assert button.text_content() == "Diagnosing..."
    assert button.is_disabled()


def test_idle_autopilot_does_not_compete_with_manual_attention(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    state = _state_payload()
    autopilot = _autopilot_payload()
    autopilot["health"] = "idle"
    autopilot["incidents"] = []
    autopilot["pending_decisions"] = []
    autopilot["recent_events"] = [{
        "id": "event-noop",
        "created_at": "2026-04-25T12:00:03Z",
        "kind": "pilot.noop",
        "severity": "info",
        "message": "Pilot recommended no action",
        "incident_id": None,
        "decision_id": "decision-pilot",
        "action": "pilot_triage",
        "details": {},
    }]
    state["autopilot"] = autopilot
    state["runtime"]["status"] = "warning"
    state["runtime"]["issues"] = [{
        "key": "task-needs-attention",
        "severity": "warning",
        "label": "Tasks need attention",
        "detail": "One task needs review.",
        "next_action": "Open the affected run and use the review packet next action.",
    }]
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("diagnostics-tab").click()
    page.get_by_role("heading", name="Manual attention").wait_for(state="visible", timeout=5_000)

    assert page.get_by_test_id("autopilot-panel").count() == 0
    body_text = page.evaluate("() => document.body.textContent")
    assert "No action pending" not in body_text
    assert "No automatic recovery recommended" not in body_text


def test_autopilot_requeue_proposal_replaces_manual_task_warning(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    state = _state_payload()
    autopilot = _autopilot_payload()
    autopilot["incidents"] = [{
        "id": "incident-requeue",
        "kind": "landing_interrupted",
        "severity": "warning",
        "title": "Requeue interrupted task",
        "detail": "Certify the existing app loads is interrupted and produced no code changes. Requeue it to get a fresh run.",
        "action": "requeue",
        "run_id": "run-interrupted",
        "task_id": "certify-existing",
    }]
    autopilot["pending_decisions"] = [{
        "id": "decision-requeue",
        "incident_id": "incident-requeue",
        "created_at": "2026-04-25T12:00:00Z",
        "title": "Recover interrupted task",
        "action": "requeue",
        "action_label": "Recover task",
        "reason": "Certify the existing app loads is interrupted and produced no code changes. Requeue it to get a fresh run. Otto will also start the queue runner so the retry actually begins.",
        "severity": "warning",
        "target": "run-interrupted",
        "run_id": "run-interrupted",
        "task_id": "certify-existing",
        "requires_pilot": False,
        "status": "pending",
        "includes_actions": ["requeue", "start_watcher"],
        "chain_actions": ["start_watcher"],
        "plan_steps": [
            {"action": "requeue", "label": "Requeue interrupted task", "status": "pending", "detail": "Create a fresh queued run from the original task definition."},
            {"action": "start_watcher", "label": "Start queue runner", "status": "pending", "detail": "Start queue processing so the retry does not sit paused."},
            {"action": "watch_retry", "label": "Watch retry", "status": "pending", "detail": "Refresh state and replace the old attempt once the retry completes."},
        ],
        "result": None,
        "error": None,
    }]
    state["autopilot"] = autopilot
    state["runtime"]["status"] = "warning"
    state["runtime"]["issues"] = [
        {
            "key": "queued-work-paused",
            "severity": "warning",
            "label": "Queued work is paused",
            "detail": "1 queued task will not start while the queue runner is stopped.",
            "next_action": "Start the queue runner when queued work should run.",
        },
        {
            "key": "task-needs-attention",
            "severity": "warning",
            "label": "Tasks need attention",
            "detail": "One task needs review.",
            "next_action": "Open the affected run and use the review packet next action.",
        },
    ]
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("diagnostics-tab").click()
    panel = page.get_by_test_id("autopilot-panel")
    panel.wait_for(state="visible", timeout=5_000)

    assert "Ask first" in panel.text_content()
    assert "Recover interrupted task" in panel.text_content()
    assert "Requeue interrupted task" in panel.text_content()
    assert "Start queue runner" in panel.text_content()
    assert "Approve plan" in panel.text_content()
    assert page.get_by_role("heading", name="Manual attention").count() == 0
