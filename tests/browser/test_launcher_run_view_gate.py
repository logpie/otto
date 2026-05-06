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
