"""Browser regressions for launcher-mode boot before a project is selected."""

from __future__ import annotations

import json
from typing import Any

import pytest


pytestmark = pytest.mark.browser


def _project(path: str = "/tmp/managed/existing-app") -> dict[str, Any]:
    return {
        "path": path,
        "name": "existing-app",
        "branch": "main",
        "dirty": False,
        "head_sha": "abc1234",
        "managed": True,
    }


def _run_view(session_id: str = "run-1") -> dict[str, Any]:
    return {
        "run_id": session_id,
        "status": "passed",
        "intent": "Verify the expense export flow",
        "project_kind": "webapp",
        "verdict": "passed",
        "features": [],
        "groups": [],
        "components": [],
        "guardrails": [],
        "stages": [],
        "cost_usd": 0.12,
        "wall_s": 4.0,
        "meta": {
            "session_id": session_id,
            "spec_path": "spec.json",
            "spec_version": 1,
            "proof_packet_html": None,
            "proof_packet_json": None,
            "started_at": "2026-05-06T00:00:00Z",
            "finished_at": "2026-05-06T00:00:04Z",
            "intent_hash": "abc123",
        },
        "findings": [],
    }


def test_launcher_mode_without_project_shows_launcher_not_run_error(
    mc_backend: Any,
    page: Any,
) -> None:
    """No selected project should render the launcher and not poll /api/run-view."""

    run_view_calls = {"count": 0}

    def projects(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": True,
                    "projects_root": "/tmp/managed",
                    "current": None,
                    "projects": [],
                }
            ),
        )

    def run_view(route: Any) -> None:
        run_view_calls["count"] += 1
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"detail": "No project selected."}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/run-view", run_view)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="launcher-empty-state"]', timeout=10_000)

    assert page.get_by_text("Create your first project").is_visible()
    assert page.get_by_text("Failed to load sessions").count() == 0
    assert run_view_calls["count"] == 0


def test_selecting_project_allows_run_list_to_load(
    mc_backend: Any,
    page: Any,
) -> None:
    """Opening a launcher project flips into the run-list route."""

    page.set_viewport_size({"width": 1440, "height": 900})
    selected = {"value": False}
    run_view_calls = {"count": 0}
    project = _project()

    def projects(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": True,
                    "projects_root": "/tmp/managed",
                    "current": project if selected["value"] else None,
                    "projects": [project],
                }
            ),
        )

    def select_project(route: Any) -> None:
        selected["value"] = True
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "project": project, "projects": [project]}),
        )

    def clear_project(route: Any) -> None:
        selected["value"] = False
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "current": None, "projects": [project]}),
        )

    def run_view(route: Any) -> None:
        run_view_calls["count"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runs": [], "sessions": []}),
        )

    page.route("**/api/projects/clear", clear_project)
    page.route("**/api/projects/select", select_project)
    page.route("**/api/projects", projects)
    page.route("**/api/run-view", run_view)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector(".project-row", timeout=10_000)
    launcher_box = page.locator(".launcher-page").bounding_box()
    project_box = page.locator(".launcher-section").first.bounding_box()
    assert launcher_box is not None
    assert project_box is not None
    assert launcher_box["x"] > 80
    assert abs((launcher_box["x"] + launcher_box["width"] / 2) - 720) < 48
    assert project_box["x"] > launcher_box["x"] + 320

    page.get_by_role("button", name="existing-app").click()
    page.wait_for_selector('[data-testid="run-list-empty"]', timeout=10_000)

    assert selected["value"] is True
    assert run_view_calls["count"] >= 1
    assert page.get_by_text("Failed to load sessions").count() == 0
    assert page.get_by_test_id("switch-project-button").is_visible()
    assert page.get_by_text("existing-app").first.is_visible()

    page.get_by_test_id("switch-project-button").click()
    page.wait_for_selector('[data-testid="launcher-empty-state"], .project-row', timeout=10_000)

    assert selected["value"] is False
    assert page.get_by_text("Open a project").is_visible()


def test_selected_project_brand_returns_to_launcher(
    mc_backend: Any,
    page: Any,
) -> None:
    """The top-left brand should mean project home in launcher mode."""

    selected = {"value": True}
    project = _project()

    def projects(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": True,
                    "projects_root": "/tmp/managed",
                    "current": project if selected["value"] else None,
                    "projects": [project],
                }
            ),
        )

    def clear_project(route: Any) -> None:
        selected["value"] = False
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "current": None, "projects": [project]}),
        )

    def run_view(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runs": [], "sessions": []}),
        )

    page.route("**/api/projects/clear", clear_project)
    page.route("**/api/projects", projects)
    page.route("**/api/run-view", run_view)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="run-list-empty"]', timeout=10_000)

    page.get_by_label("Otto Mission Control").click()
    page.wait_for_selector('[data-testid="launcher-empty-state"], .project-row', timeout=10_000)

    assert selected["value"] is False
    assert page.get_by_text("Open a project").is_visible()


def test_run_card_opens_side_drawer_without_route_navigation(
    mc_backend: Any,
    page: Any,
) -> None:
    """Run cards open the in-place drawer; deep-link route remains separate."""

    project = _project()
    run_view_calls = {"list": 0, "detail": 0}

    def projects(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": True,
                    "projects_root": "/tmp/managed",
                    "current": project,
                    "projects": [project],
                }
            ),
        )

    def run_list(route: Any) -> None:
        run_view_calls["list"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "runs": ["run-1"],
                    "sessions": [
                        {
                            "id": "run-1",
                            "intent": "Verify the expense export flow",
                            "status": "passed",
                            "verdict": "passed",
                            "cost_usd": 0.12,
                            "wall_s": 4.0,
                            "feature_total": 0,
                            "feature_passed": 0,
                            "critical_findings": 0,
                            "quality_score": None,
                            "group_count": 0,
                            "finished_at": "2026-05-06T00:00:04Z",
                            "lifecycle": "approved",
                        }
                    ],
                }
            ),
        )

    def run_detail(route: Any) -> None:
        run_view_calls["detail"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_run_view("run-1")),
        )

    page.route("**/api/run-view/run-1", run_detail)
    page.route("**/api/projects", projects)
    page.route("**/api/run-view", run_list)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="landing-card"]', timeout=10_000)

    page.get_by_test_id("landing-card").click()
    page.wait_for_selector('[data-testid="run-list-detail-drawer"]', timeout=10_000)
    page.wait_for_selector('[data-testid="run-drawer"]', timeout=10_000)

    assert "?view=run-view" not in page.url
    assert run_view_calls["list"] >= 1
    assert run_view_calls["detail"] == 1
    assert page.get_by_test_id("run-drawer").is_visible()
    assert page.get_by_test_id("run-list").is_visible()

    page.get_by_test_id("run-list-detail-drawer-close").click()
    page.get_by_test_id("run-list-detail-drawer").wait_for(state="detached", timeout=10_000)
