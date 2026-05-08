"""Browser regressions for RunDrawer quick actions."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser

RUN_ID = "specless-run"


def _install_projects_route(page: Any) -> None:
    page.route(
        "**/api/projects",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": False,
                    "projects_root": "",
                    "current": None,
                    "projects": [],
                }
            ),
        ),
    )


def _run_view_payload() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "status": "compiling",
        "control_plane": {
            "status": "compiling",
            "raw_status": "running",
            "failure_reason": None,
            "conflict": False,
            "conflict_reason": None,
        },
        "intent": "Build a personal finance dashboard web app",
        "project_kind": "webapp",
        "verdict": None,
        "features": [],
        "groups": [],
        "components": [],
        "guardrails": [],
        "stages": [
            {
                "name": "compile",
                "status": "active",
                "duration_s": None,
                "cost_usd": None,
                "started_at": "2026-05-08T04:26:01Z",
                "finished_at": None,
            },
            {
                "name": "spec_review",
                "status": "pending",
                "duration_s": None,
                "cost_usd": None,
                "started_at": None,
                "finished_at": None,
            },
        ],
        "cost_usd": 0.0,
        "wall_s": 25.0,
        "meta": {
            "session_id": RUN_ID,
            "spec_path": "",
            "spec_version": 0,
            "proof_packet_html": None,
            "proof_packet_json": None,
            "started_at": "2026-05-08T04:26:01Z",
            "finished_at": None,
            "intent_hash": "abc123",
        },
        "findings": [],
    }


def _install_run_view_route(page: Any) -> None:
    body = json.dumps(_run_view_payload())
    page.route(
        f"**/api/run-view/{RUN_ID}",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def test_view_spec_action_is_disabled_until_spec_exists(
    mc_backend: Any,
    page: Any,
    disable_animations: Any,
) -> None:
    """A compiling run without spec_path must not send users to a spec 404."""

    _install_projects_route(page)
    _install_run_view_route(page)

    page.goto(f"{mc_backend.url}?view=run-view&session={RUN_ID}", wait_until="networkidle")
    page.get_by_test_id("run-drawer").wait_for(state="visible", timeout=10_000)
    disable_animations(page)

    spec_button = page.get_by_test_id("run-quick-action-spec")
    spec_button.wait_for(state="visible", timeout=5_000)

    assert spec_button.is_disabled()
    assert spec_button.get_attribute("title") == "Spec is not available yet"
    assert "view=run-view" in page.url
