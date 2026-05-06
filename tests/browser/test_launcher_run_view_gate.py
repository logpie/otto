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


def _build_config() -> dict[str, Any]:
    return {
        "command_family": "build",
        "provider": "codex",
        "model": "gpt-5.4",
        "reasoning_effort": "medium",
        "certifier_mode": "fast",
        "skip_product_qa": False,
        "certification": "fast certification",
        "planning": "direct",
        "spec_file_path": None,
        "run_budget_seconds": 3600,
        "spec_timeout": 600,
        "max_certify_rounds": 8,
        "max_turns_per_call": 200,
        "strict_mode": False,
        "split_mode": True,
        "allow_dirty_repo": False,
        "default_branch": "main",
        "test_command": None,
        "queue": {
            "concurrent": 3,
            "task_timeout_s": 4200.0,
            "worktree_dir": ".worktrees",
            "on_watcher_restart": "resume",
            "merge_certifier_mode": "standard",
        },
        "agents": {
            "build": {"provider": "codex", "model": "gpt-5.4", "reasoning_effort": "medium"},
            "certifier": {"provider": "codex", "model": "gpt-5.4-mini", "reasoning_effort": "low"},
            "spec": {"provider": "codex", "model": "gpt-5.4-mini", "reasoning_effort": "low"},
            "fix": {"provider": "codex", "model": "gpt-5.4", "reasoning_effort": "medium"},
        },
        "config_file_exists": True,
        "config_error": None,
    }


def _state(
    project: dict[str, Any] | None = None,
    *,
    live_items: list[dict[str, Any]] | None = None,
    landing_items: list[dict[str, Any]] | None = None,
    history_items: list[dict[str, Any]] | None = None,
    watcher_running: bool = False,
) -> dict[str, Any]:
    live_items = live_items or []
    landing_items = landing_items or []
    history_items = history_items or []
    return {
        "project": project or _project(),
        "project_stats": {
            "active_count": len(live_items),
            "history_count": len(history_items),
            "success_count": 0,
            "failed_count": 0,
            "total_duration_s": 0,
            "duration_display": "-",
            "reported_cost_usd": None,
            "cost_display": "-",
            "token_usage": {},
            "total_tokens": 0,
            "token_display": "-",
            "stories_passed": 0,
            "stories_tested": 0,
        },
        "watcher": {
            "alive": watcher_running,
            "watcher": {"pid": 1234} if watcher_running else None,
            "counts": {
                "queued": 0,
                "starting": 0,
                "initializing": len(live_items),
                "running": 0,
            },
            "health": {
                "state": "running" if watcher_running else "stopped",
                "blocking_pid": 1234 if watcher_running else None,
                "watcher_pid": 1234 if watcher_running else None,
                "watcher_process_alive": watcher_running,
                "lock_pid": 1234 if watcher_running else None,
                "lock_process_alive": watcher_running,
                "heartbeat": "2026-05-06T06:02:53Z" if watcher_running else None,
                "heartbeat_age_s": 1.0 if watcher_running else None,
                "started_at": "2026-05-06T06:01:47Z" if watcher_running else None,
                "log_path": "/tmp/watcher.log",
                "next_action": "Stop queue runner." if watcher_running else "Start queue runner.",
            },
        },
        "autopilot": {
            "mode": "assisted",
            "enabled": True,
            "policy": {
                "mode": "assisted",
                "max_actions_per_hour": 8,
                "max_pilot_calls_per_hour": 2,
                "allow_auto_land": False,
                "verification_policy": "smart",
                "pilot_enabled": True,
                "pilot_timeout_s": 300,
            },
            "health": "idle",
            "last_tick_at": None,
            "next_tick_hint": "Idle.",
            "incidents": [],
            "decisions": [],
            "pending_decisions": [],
            "recent_events": [],
            "budgets": {
                "actions_used_last_hour": 0,
                "actions_limit_per_hour": 8,
                "pilot_calls_used_last_hour": 0,
                "pilot_calls_limit_per_hour": 2,
            },
            "counters": {
                "incidents_open": 0,
                "decisions_pending": 0,
                "actions_executed": 0,
                "actions_failed": 0,
            },
        },
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-05-06T06:02:54Z",
            "queue_tasks": len(landing_items),
            "state_tasks": len(landing_items),
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
            "files": {},
            "supervisor": {
                "mode": "local-single-user",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": 1234 if watcher_running else None,
                "matches_blocking_pid": watcher_running,
                "can_start": not watcher_running,
                "can_stop": watcher_running,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
                "stop_target_pid": 1234 if watcher_running else None,
                "watcher_log_path": "/tmp/watcher.log",
                "web_log_exists": True,
                "queue_lock_holder_pid": 1234 if watcher_running else None,
            },
            "issues": [],
        },
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "landing": {
            "target": "main",
            "items": landing_items,
            "counts": {
                "ready": 0,
                "merged": len(history_items),
                "blocked": len(landing_items),
                "reviewed": 0,
                "total": len(landing_items) + len(history_items),
            },
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
        },
        "live": {
            "items": live_items,
            "total_count": len(live_items),
            "active_count": len([item for item in live_items if item.get("active")]),
            "refresh_interval_s": 1.0,
        },
        "history": {
            "items": history_items,
            "page": 0,
            "page_size": 50,
            "total_rows": len(history_items),
            "total_pages": 1,
        },
    }


def _landing_item(task_id: str = "build-a-micro-twitter", run_id: str = "run-1") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "branch": "build/micro-twitter",
        "worktree": ".worktrees/build-a-micro-twitter",
        "summary": "build a micro twitter",
        "build_config": _build_config(),
        "queue_status": "initializing",
        "landing_state": "blocked",
        "label": "In progress",
        "merge_id": None,
        "merge_status": None,
        "merge_run_status": None,
        "started_at": "2026-05-06T06:01:47Z",
        "finished_at": None,
        "updated_at": "2026-05-06T06:02:53Z",
        "queued_at": None,
        "duration_s": None,
        "cost_usd": None,
        "token_usage": {},
        "stories_passed": None,
        "stories_tested": None,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_error": None,
    }


def _live_item(task_id: str = "build-a-micro-twitter", run_id: str = "run-1") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "domain": "queue",
        "run_type": "queue",
        "command": "build build a micro twitter",
        "display_name": "build-a-micro-twitter: build a micro twitter",
        "status": "initializing",
        "terminal_outcome": None,
        "started_at": "2026-05-06T06:01:47Z",
        "updated_at": "2026-05-06T06:02:53Z",
        "heartbeat_at": "2026-05-06T06:02:53Z",
        "finished_at": None,
        "queued_at": None,
        "project_dir": "/tmp/managed/existing-app",
        "cwd": "/tmp/managed/existing-app/.worktrees/build-a-micro-twitter",
        "queue_task_id": task_id,
        "merge_id": None,
        "branch": "build/micro-twitter",
        "worktree": ".worktrees/build-a-micro-twitter",
        "provider": "codex",
        "model": "gpt-5.4",
        "reasoning_effort": "medium",
        "certifier_mode": "fast",
        "skip_product_qa": False,
        "build_config": _build_config(),
        "run_config": _build_config(),
        "adapter_key": "queue.attempt",
        "version": 3,
        "display_status": "initializing",
        "active": True,
        "display_id": task_id,
        "branch_task": "build/micro-twitter",
        "elapsed_s": 67.0,
        "elapsed_display": "1:07",
        "cost_usd": None,
        "cost_display": "...",
        "token_usage": {},
        "last_event": "Build phase — dispatching group agents",
        "progress": "",
        "row_label": "build-a-micro-twitter: build a micro twitter",
        "overlay": None,
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
    project_get_calls = {"count": 0}
    project = _project()

    def projects(route: Any) -> None:
        project_get_calls["count"] += 1
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
        assert "include_projects=false" in route.request.url
        selected["value"] = True
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "project": project, "current": project}),
        )

    def clear_project(route: Any) -> None:
        assert "include_projects=false" in route.request.url
        selected["value"] = False
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "current": None}),
        )

    def state(route: Any) -> None:
        run_view_calls["count"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state(project)),
        )

    page.route("**/api/projects/clear**", clear_project)
    page.route("**/api/projects/select**", select_project)
    page.route("**/api/projects", projects)
    page.route("**/api/state", state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector(".project-row", timeout=10_000)
    launcher_box = page.locator(".launcher-page").bounding_box()
    hero_box = page.locator(".launcher-hero").bounding_box()
    project_box = page.locator(".launcher-projects").bounding_box()
    create_box = page.locator(".launcher-create").bounding_box()
    first_row_box = page.locator(".project-row").first.bounding_box()
    assert launcher_box is not None
    assert hero_box is not None
    assert project_box is not None
    assert create_box is not None
    assert first_row_box is not None
    assert launcher_box["x"] > 80
    assert abs((launcher_box["x"] + launcher_box["width"] / 2) - 720) < 48
    assert hero_box["height"] <= 72
    assert project_box["y"] <= 110
    assert first_row_box["y"] <= 190
    assert project_box["width"] >= 700
    assert project_box["x"] < create_box["x"]
    assert create_box["x"] > project_box["x"] + project_box["width"]

    page.get_by_role("button", name="existing-app").click()
    page.wait_for_selector('[data-testid="project-workspace"]', timeout=10_000)

    assert selected["value"] is True
    assert run_view_calls["count"] >= 1
    assert page.get_by_text("Failed to load sessions").count() == 0
    assert page.get_by_text("Project workspace").is_visible()
    assert page.get_by_test_id("switch-project-button").is_visible()
    assert page.get_by_text("existing-app").first.is_visible()

    page.get_by_test_id("switch-project-button").click()
    page.wait_for_selector('[data-testid="launcher-empty-state"], .project-row', timeout=10_000)

    assert selected["value"] is False
    assert page.get_by_text("Open a project").is_visible()
    assert run_view_calls["count"] >= 1
    assert project_get_calls["count"] == 1


def test_selected_project_brand_returns_to_project_launcher(
    mc_backend: Any,
    page: Any,
) -> None:
    """In launcher mode, the top-left brand should return to project selection."""

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
            body=json.dumps({"ok": True, "current": None}),
        )

    def state(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state(project)),
        )

    page.route("**/api/projects/clear**", clear_project)
    page.route("**/api/projects", projects)
    page.route("**/api/state", state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="project-workspace"]', timeout=10_000)

    page.get_by_label("Otto Mission Control — project launcher").click()
    page.wait_for_selector('[data-testid="project-launcher"]', timeout=10_000)

    assert selected["value"] is False
    assert page.get_by_text("Open a project").is_visible()


def test_launcher_dirty_project_uses_text_badge(
    mc_backend: Any,
    page: Any,
) -> None:
    """Dirty projects need a readable badge, not an unlabeled color dot."""

    project = _project()
    project["dirty"] = True

    def projects(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": True,
                    "projects_root": "/tmp/managed",
                    "current": None,
                    "projects": [project],
                }
            ),
        )

    page.route("**/api/projects", projects)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="project-launcher"]', timeout=10_000)

    assert page.get_by_text("Local changes").is_visible()


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

    def state(route: Any) -> None:
        run_view_calls["list"] += 1
        history_item = {
            "run_id": "run-1",
            "domain": "build",
            "run_type": "build",
            "command": "build",
            "status": "completed",
            "terminal_outcome": "success",
            "timestamp": "2026-05-06T00:00:04Z",
            "started_at": "2026-05-06T00:00:00Z",
            "finished_at": "2026-05-06T00:00:04Z",
            "queue_task_id": "verify-expense-export",
            "merge_id": None,
            "branch": "build/expense-export",
            "worktree": None,
            "summary": "Verify the expense export flow",
            "intent": "Verify the expense export flow",
            "completed_at_display": "just now",
            "outcome_display": "success",
            "duration_s": 4.0,
            "duration_display": "4s",
            "cost_usd": 0.12,
            "cost_display": "$0.12",
            "token_usage": {},
            "resumable": False,
            "adapter_key": "build",
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state(project, history_items=[history_item])),
        )

    def run_detail(route: Any) -> None:
        run_view_calls["detail"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_run_view("run-1")),
        )

    def run_logs(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "session_id": "run-1",
                    "logs": [
                        {
                            "label": "spec-state.jsonl",
                            "path": "spec-state.jsonl",
                            "size_bytes": 24,
                            "text": '{"kind":"group.started"}',
                            "truncated": False,
                        }
                    ],
                    "empty": False,
                }
            ),
        )

    page.route("**/api/run-view/run-1/logs", run_logs)
    page.route("**/api/run-view/run-1", run_detail)
    page.route("**/api/projects", projects)
    page.route("**/api/state", state)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="task-card-verify-expense-export"]', timeout=10_000)

    page.get_by_test_id("task-card-verify-expense-export").click()
    page.wait_for_selector('[data-testid="run-list-detail-drawer"]', timeout=10_000)
    page.wait_for_selector('[data-testid="run-drawer"]', timeout=10_000)

    assert "?view=run-view" not in page.url
    assert run_view_calls["list"] >= 1
    assert run_view_calls["detail"] == 1
    assert page.get_by_test_id("run-drawer").is_visible()
    assert page.get_by_test_id("project-workspace").is_visible()
    assert page.get_by_test_id("run-quick-action-proof").is_disabled()
    page.get_by_test_id("run-quick-action-logs").click()
    page.wait_for_selector('[data-testid="run-resource-panel-logs"]', timeout=10_000)
    page.get_by_text("spec-state.jsonl").wait_for(timeout=10_000)
    page.get_by_text("group.started").wait_for(timeout=10_000)

    page.go_back()
    page.get_by_test_id("run-list-detail-drawer").wait_for(state="detached", timeout=10_000)
    assert page.get_by_test_id("project-workspace").is_visible()

    page.get_by_test_id("task-card-verify-expense-export").click()
    page.wait_for_selector('[data-testid="run-list-detail-drawer"]', timeout=10_000)
    page.wait_for_selector('[data-testid="run-drawer"]', timeout=10_000)
    page.get_by_test_id("run-list-detail-drawer-close").click()
    page.get_by_test_id("run-list-detail-drawer").wait_for(state="detached", timeout=10_000)


def test_run_view_wraps_long_feature_names_without_page_overflow(
    mc_backend: Any,
    page: Any,
) -> None:
    """Issue-sized feature names must wrap instead of widening the whole app."""

    project = _project()
    run_view = _run_view("long-run")
    run_view["status"] = "blocked"
    run_view["verdict"] = "blocked"
    long_name = (
        "Feature with an extremely long title that should wrap without covering "
        "actions or breaking the drawer layout "
        * 2
    )
    run_view["features"] = [
        {
            "id": f"f-{index}",
            "name": f"{long_name}{index}",
            "description": "Long feature description.",
            "acceptance_detail": "The UI wraps all text and leaves controls clickable.",
            "evidence_kinds": ["RepoTestCheck", "BrowserCheck"],
            "group_id": "foundation",
            "group_name": "Foundation",
            "build_status": "blocked" if index < 2 else "passed",
            "verdict": None,
            "evidence_completeness": "full",
            "coverage_confidence": "high",
            "multi_actor_required": False,
            "audit_pre_merge": False,
            "evidence_refs": [],
        }
        for index in range(8)
    ]
    run_view["groups"] = [
        {
            "id": "foundation",
            "name": "Foundation",
            "description": "",
            "feature_ids": [feature["id"] for feature in run_view["features"]],
            "status": "blocked",
            "branch": "i2p/long-run/foundation",
            "owned_paths": ["src/App.tsx"],
            "dependencies": [],
            "cost_usd": 0.1,
            "wall_s": 12.0,
            "repair_attempts": 0,
        }
    ]

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

    def detail(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(run_view))

    page.set_viewport_size({"width": 1440, "height": 900})
    page.route("**/api/projects", projects)
    page.route("**/api/run-view/long-run", detail)

    page.goto(f"{mc_backend.url}?view=run-view&session=long-run", wait_until="networkidle")
    page.wait_for_selector('[data-testid="run-drawer"]', timeout=10_000)

    widths = page.evaluate(
        """() => ({
            viewport: innerWidth,
            body: document.body.scrollWidth,
            document: document.documentElement.scrollWidth,
        })"""
    )
    assert widths["body"] <= widths["viewport"] + 2
    assert widths["document"] <= widths["viewport"] + 2

    first_name = page.locator(".feature-row .feature-name").first.bounding_box()
    first_row = page.locator(".feature-row").first.bounding_box()
    assert first_name is not None
    assert first_row is not None
    assert first_name["width"] <= first_row["width"]


def test_feature_drilldown_uses_build_state_and_real_group_resources(
    mc_backend: Any,
    page: Any,
) -> None:
    """Feature detail must not show stale pending status or fake action stubs."""

    project = _project()
    run_view = _run_view("run-1")
    run_view["status"] = "blocked"
    run_view["verdict"] = "blocked"
    run_view["features"] = [
        {
            "id": "f1",
            "name": "Scaffold checkout flow",
            "description": "Part of Foundation.",
            "acceptance_detail": "",
            "evidence_kinds": ["RepoTestCheck"],
            "group_id": "foundation",
            "group_name": "Foundation",
            "build_status": "blocked",
            "verdict": None,
            "evidence_completeness": "full",
            "coverage_confidence": "high",
            "multi_actor_required": False,
            "audit_pre_merge": False,
            "evidence_refs": [],
        }
    ]
    run_view["groups"] = [
        {
            "id": "foundation",
            "name": "Foundation",
            "description": "",
            "feature_ids": ["f1"],
            "status": "blocked",
            "branch": "i2p/run-1/foundation",
            "owned_paths": ["src/App.tsx"],
            "dependencies": [],
            "cost_usd": 0.1,
            "wall_s": 12.0,
            "repair_attempts": 0,
        }
    ]

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

    def detail(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(run_view))

    def group_logs(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "session_id": "run-1",
                    "group_id": "foundation",
                    "logs": [
                        {
                            "label": "build/foundation/live.log",
                            "path": "build/foundation/live.log",
                            "size_bytes": 12,
                            "text": "real group log",
                            "truncated": False,
                        }
                    ],
                    "empty": False,
                }
            ),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/run-view/run-1/groups/foundation/logs", group_logs)
    page.route("**/api/run-view/run-1", detail)

    page.goto(f"{mc_backend.url}?view=run-view&session=run-1", wait_until="networkidle")
    page.get_by_test_id("feature-f1").click()
    page.wait_for_selector('[data-testid="feature-drilldown"]', timeout=10_000)

    assert page.get_by_test_id("feature-drilldown-verdict").inner_text() == "blocked"
    back_box = page.get_by_test_id("feature-drilldown-back").bounding_box()
    crumb_box = page.get_by_test_id("feature-drilldown-breadcrumb").bounding_box()
    assert back_box is not None
    assert crumb_box is not None
    assert crumb_box["x"] <= back_box["x"] + back_box["width"] + 80
    assert page.locator(".feature-drilldown-honesty").evaluate(
        "node => getComputedStyle(node).borderStyle"
    ) != "none"
    assert page.locator(".feature-drilldown-honesty dl").evaluate(
        "node => getComputedStyle(node).display"
    ) == "grid"
    assert page.get_by_test_id("feature-action-evidence").count() == 0
    assert page.get_by_test_id("feature-action-reaudit").count() == 0

    page.get_by_test_id("feature-action-group-logs").click()
    page.wait_for_selector('[data-testid="feature-resource-logs"]', timeout=10_000)
    page.get_by_text("build/foundation/live.log").wait_for(timeout=10_000)


def test_diagnostics_query_route_opens_health_surface(
    mc_backend: Any,
    page: Any,
) -> None:
    project = _project()

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

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_state(project)))

    page.route("**/api/projects", projects)
    page.route("**/api/state", state)

    page.goto(f"{mc_backend.url}?view=diagnostics", wait_until="networkidle")
    page.wait_for_selector('[data-testid="diagnostics-view"]', timeout=10_000)

    assert page.get_by_test_id("diagnostics-tab").get_attribute("aria-pressed") == "true"
    assert page.get_by_text("Health").first.is_visible()


def test_spec_diff_without_versions_hides_noop_controls(
    mc_backend: Any,
    page: Any,
) -> None:
    project = _project()

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

    def versions(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session_id": "run-1", "versions": []}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/specs/run-1/versions", versions)

    page.goto(f"{mc_backend.url}?view=spec-diff&session=run-1", wait_until="networkidle")
    page.wait_for_selector('[data-testid="spec-diff-empty"]', timeout=10_000)

    assert page.get_by_test_id("spec-diff-from").count() == 0
    assert page.get_by_test_id("spec-diff-fold-toggle").count() == 0
    assert page.get_by_text("No archived spec versions").is_visible()


def test_spec_review_approved_banner_stays_web_native(
    mc_backend: Any,
    page: Any,
) -> None:
    """The primary Web UI should not tell users to paste a CLI command."""

    project = _project()

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

    def markdown(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "spec_id": "run-1",
                    "session_id": "run-1",
                    "markdown": "# Build app\n\n## Features\n\n### Auth\n",
                    "intent_hash": "abc123",
                    "lifecycle": "approved",
                    "updated_at": "2026-05-06T00:00:00Z",
                }
            ),
        )

    def versions(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session_id": "run-1", "versions": []}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/specs/run-1/versions", versions)
    page.route("**/api/specs/run-1/markdown", markdown)

    page.goto(f"{mc_backend.url}?view=spec-review&spec=run-1", wait_until="networkidle")
    page.wait_for_selector('[data-testid="spec-review-readonly-banner"]', timeout=10_000)

    banner = page.get_by_test_id("spec-review-readonly-banner")
    assert "Queue an improvement or new build" in banner.inner_text()
    assert "otto build" not in banner.inner_text()


def test_new_run_queues_from_web_and_starts_runner(
    mc_backend: Any,
    page: Any,
) -> None:
    """The primary web surface queues real work; it must not emit CLI-copy UX."""

    project = _project()
    queue_posts: list[dict[str, Any]] = []
    watcher_starts = {"count": 0}

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

    def state(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state(project)),
        )

    def queue_build(route: Any) -> None:
        queue_posts.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "message": "queued build",
                    "task": {"id": "build-social-feed"},
                    "warnings": [],
                    "refresh": True,
                }
            ),
        )

    def watcher_start(route: Any) -> None:
        watcher_starts["count"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "watcher started", "refresh": True}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/state", state)
    page.route("**/api/queue/build", queue_build)
    page.route("**/api/watcher/start", watcher_start)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="project-workspace"]', timeout=10_000)

    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector('[data-testid="job-dialog-submit-button"]', timeout=10_000)

    assert page.get_by_text("Copy command").count() == 0
    assert page.get_by_text("Otto runs are launched from the CLI").count() == 0
    assert page.get_by_role("heading", name="Build with Otto").is_visible()
    assert page.get_by_text("spec, groups, feature work").is_visible()

    page.get_by_test_id("job-dialog-intent").fill(
        "build a webapp like a micro twitter with social and post features"
    )
    page.wait_for_function(
        "() => document.querySelector('[data-testid=job-dialog-submit-button]')?.disabled === false",
        timeout=5_000,
    )
    page.get_by_test_id("job-dialog-submit-button").click()
    page.wait_for_selector('[data-testid="job-grace-banner"]', timeout=10_000)
    page.wait_for_timeout(3_600)

    assert len(queue_posts) == 1
    assert queue_posts[0]["intent"].startswith("build a webapp")
    assert "--split" not in queue_posts[0]["extra_args"]
    assert "--agentic" not in queue_posts[0]["extra_args"]
    assert "--rounds" not in queue_posts[0]["extra_args"]
    assert watcher_starts["count"] == 1
    assert page.get_by_test_id("run-list-queue-banner").is_visible()


def test_mission_control_product_smoke_launch_and_group_run_view(
    mc_backend: Any,
    page: Any,
) -> None:
    """Product smoke: launch controls, queue POST, grouped RunView, logs, diff."""

    project = _project()
    project["defaults"] = {
        "provider": "claude",
        "model": "sonnet",
        "reasoning_effort": None,
        "certifier_mode": "fast",
        "skip_product_qa": False,
        "run_budget_seconds": 3600,
        "spec_timeout": 600,
        "max_certify_rounds": 8,
        "max_turns_per_call": 200,
        "strict_mode": False,
        "split_mode": True,
        "allow_dirty_repo": False,
        "default_branch": "main",
        "test_command": None,
        "queue_concurrent": 3,
        "queue_task_timeout_s": 4200.0,
        "queue_worktree_dir": ".worktrees",
        "queue_on_watcher_restart": "resume",
        "queue_merge_certifier_mode": "standard",
        "config_file_exists": True,
        "config_error": None,
    }
    queue_posts: list[dict[str, Any]] = []
    run_view = _run_view("run-1")
    run_view.update(
        {
            "status": "building",
            "verdict": None,
            "token_usage": {"input_tokens": 1200, "output_tokens": 240, "total_tokens": 1440},
            "dispatch": {
                "max_concurrent": 3,
                "running_group_ids": ["feed"],
                "ready_group_ids": ["profile"],
                "waiting_group_ids": ["notifications"],
                "blocked_group_ids": [],
                "completed_group_ids": ["foundation"],
                "parallelizable_group_ids": ["feed", "profile"],
                "summary": "running 1/3; ready 1; waiting on dependencies 1; blocked 0",
            },
            "features": [
                {
                    "id": "feed-list",
                    "name": "Feed list",
                    "description": "Shows posts in reverse chronological order.",
                    "acceptance_detail": "Browser can see the newest post first.",
                    "evidence_kinds": ["BrowserJourney"],
                    "group_id": "feed",
                    "group_name": "Feed",
                    "build_status": "in_progress",
                    "verdict": None,
                    "evidence_completeness": "full",
                    "coverage_confidence": "high",
                    "multi_actor_required": False,
                    "audit_pre_merge": False,
                    "evidence_refs": [],
                }
            ],
            "groups": [
                {
                    "id": "foundation",
                    "name": "Foundation",
                    "description": "",
                    "feature_ids": [],
                    "status": "passing",
                    "branch": "i2p/run-1/foundation",
                    "owned_paths": ["src/db.ts"],
                    "dependencies": [],
                    "cost_usd": 0.03,
                    "wall_s": 12.0,
                    "repair_attempts": 0,
                },
                {
                    "id": "feed",
                    "name": "Feed",
                    "description": "",
                    "feature_ids": ["feed-list"],
                    "status": "in_progress",
                    "branch": "i2p/run-1/feed",
                    "owned_paths": ["src/feed.tsx"],
                    "dependencies": ["foundation"],
                    "cost_usd": 0.05,
                    "wall_s": 18.0,
                    "repair_attempts": 0,
                },
                {
                    "id": "profile",
                    "name": "Profile",
                    "description": "",
                    "feature_ids": [],
                    "status": "pending",
                    "branch": "",
                    "owned_paths": ["src/profile.tsx"],
                    "dependencies": ["foundation"],
                    "cost_usd": 0.0,
                    "wall_s": 0.0,
                    "repair_attempts": 0,
                },
                {
                    "id": "notifications",
                    "name": "Notifications",
                    "description": "",
                    "feature_ids": [],
                    "status": "pending",
                    "branch": "",
                    "owned_paths": ["src/notifications.tsx"],
                    "dependencies": ["feed"],
                    "cost_usd": 0.0,
                    "wall_s": 0.0,
                    "repair_attempts": 0,
                },
            ],
        }
    )

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

    def state(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state(project, watcher_running=True)),
        )

    def queue_build(route: Any) -> None:
        queue_posts.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "message": "queued build-social-feed",
                    "task": {"id": "build-social-feed"},
                    "warnings": [],
                    "refresh": True,
                }
            ),
        )

    def run_detail(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(run_view))

    def group_logs(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "logs": [
                        {
                            "label": "build/feed/narrative.log",
                            "path": "build/feed/narrative.log",
                            "size_bytes": 21,
                            "text": "feed group is building",
                            "truncated": False,
                        }
                    ],
                    "empty": False,
                }
            ),
        )

    def group_diff(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "group_id": "feed",
                    "branch": "i2p/run-1/feed",
                    "diff": "diff --git a/src/feed.tsx b/src/feed.tsx\n+render feed\n",
                    "truncated": False,
                    "error": None,
                }
            ),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/state", state)
    page.route("**/api/queue/build", queue_build)
    page.route("**/api/run-view/run-1/groups/feed/logs", group_logs)
    page.route("**/api/run-view/run-1/groups/feed/diff", group_diff)
    page.route("**/api/run-view/run-1", run_detail)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="project-workspace"]', timeout=10_000)
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector('[data-testid="job-dialog"]', timeout=10_000)

    assert page.get_by_test_id("job-provider-select").is_visible()
    assert page.get_by_test_id("job-budget-input").is_visible()
    assert page.get_by_test_id("job-max-turns-input").is_visible()

    page.get_by_test_id("job-provider-select").select_option("codex")
    page.get_by_test_id("job-budget-input").fill("900")
    page.get_by_test_id("job-max-turns-input").fill("80")
    page.get_by_test_id("job-dialog-intent").fill("build a browser-verified social feed")
    page.get_by_test_id("job-dialog-submit-button").click()
    page.wait_for_selector('[data-testid="job-grace-banner"]', timeout=10_000)
    page.wait_for_timeout(3_600)

    assert len(queue_posts) == 1
    assert queue_posts[0]["intent"] == "build a browser-verified social feed"
    assert queue_posts[0]["extra_args"][:6] == [
        "--provider",
        "codex",
        "--budget",
        "900",
        "--max-turns",
        "80",
    ]

    page.goto(f"{mc_backend.url}?view=run-view&session=run-1", wait_until="networkidle")
    page.wait_for_selector('[data-testid="run-drawer"]', timeout=10_000)

    expect_line = page.get_by_test_id("run-drawer-active-line")
    assert "Running 1/3 groups" in (expect_line.text_content() or "")
    dispatch = page.get_by_test_id("group-dispatch-status")
    dispatch_text = dispatch.text_content() or ""
    assert "Ready now 1" in dispatch_text
    assert "Waiting 1" in dispatch_text
    assert "2 can run now" in dispatch_text
    assert page.get_by_test_id("metrics").get_by_text("Tokens", exact=True).is_visible()

    page.get_by_test_id("group-logs-feed").click()
    page.wait_for_selector('[data-testid="run-resource-panel-group-logs-feed"]', timeout=10_000)
    page.get_by_text("feed group is building").wait_for(timeout=10_000)
    page.get_by_test_id("group-diff-feed").click()
    page.wait_for_selector('[data-testid="run-resource-panel-group-diff-feed"]', timeout=10_000)
    page.get_by_text("render feed").wait_for(timeout=10_000)


def test_project_workspace_shows_active_queue_from_state_when_run_view_is_empty(
    mc_backend: Any,
    page: Any,
) -> None:
    """Queued i2p work must not disappear just because it has no completed run-view session."""

    project = _project()
    run_view_calls = {"count": 0}

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

    def state(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                _state(
                    project,
                    live_items=[_live_item()],
                    landing_items=[_landing_item()],
                    watcher_running=True,
                )
            ),
        )

    def run_view(route: Any) -> None:
        run_view_calls["count"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runs": [], "sessions": []}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/state", state)
    page.route("**/api/run-view", run_view)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="project-workspace"]', timeout=10_000)

    body = page.locator("body").text_content() or ""
    assert "build-a-micro-twitter" in body
    assert "Build phase" in body
    assert "No sessions yet" not in body
    assert page.get_by_test_id("task-card-build-a-micro-twitter").is_visible()
    assert run_view_calls["count"] == 0


def test_project_workspace_surfaces_recovery_plan(
    mc_backend: Any,
    page: Any,
) -> None:
    """Interrupted queue work must be recoverable from the primary workspace."""

    project = _project()
    approvals: list[str] = []

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

    def state(route: Any) -> None:
        body = _state(
            project,
            landing_items=[
                {
                    **_landing_item(),
                    "queue_status": "interrupted",
                    "run_id": "run-interrupted",
                    "label": "Needs action",
                }
            ],
            watcher_running=False,
        )
        body["autopilot"]["health"] = "attention"
        body["autopilot"]["pending_decisions"] = [
            {
                "id": "decision-1",
                "incident_id": "incident-1",
                "created_at": "2026-05-06T06:10:00Z",
                "title": "Recover interrupted task",
                "action": "requeue",
                "action_label": "Recover task",
                "reason": "The queue runner stopped while this task was in progress.",
                "severity": "warning",
                "target": "run-interrupted",
                "run_id": "run-interrupted",
                "task_id": "build-a-micro-twitter",
                "requires_pilot": False,
                "status": "pending",
                "requires_approval": True,
                "includes_actions": ["requeue", "start_watcher"],
                "plan_steps": [
                    {"action": "requeue", "label": "Requeue task", "detail": "Create a fresh attempt."},
                    {"action": "start_watcher", "label": "Start queue runner", "detail": "Process the retry."},
                ],
                "result": None,
                "error": None,
            }
        ]
        body["autopilot"]["incidents"] = [
            {
                "id": "incident-1",
                "kind": "landing_interrupted",
                "severity": "warning",
                "title": "Task interrupted",
                "detail": "Task needs recovery.",
                "action": "requeue",
                "run_id": "run-interrupted",
                "task_id": "build-a-micro-twitter",
            }
        ]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def approve(route: Any) -> None:
        approvals.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "approved", "message": "Recovery approved."}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/state", state)
    page.route("**/api/autopilot/decisions/**/approve", approve)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-testid="workspace-recovery"]', timeout=10_000)

    assert page.get_by_text("Recover interrupted task").is_visible()
    assert page.get_by_text("Requeue task").is_visible()
    page.get_by_test_id("workspace-recovery-approve").click()
    page.wait_for_timeout(250)
    assert len(approvals) == 1


def test_missing_run_deep_link_error_is_near_top(
    mc_backend: Any,
    page: Any,
) -> None:
    """A stale run deep link should not strand the 404 card mid-page."""

    page.set_viewport_size({"width": 1440, "height": 900})
    project = _project()

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

    def missing_run(route: Any) -> None:
        route.fulfill(
            status=404,
            content_type="application/json",
            body=json.dumps({"detail": "session not found: missing-run"}),
        )

    page.route("**/api/projects", projects)
    page.route("**/api/run-view/missing-run", missing_run)

    page.goto(f"{mc_backend.url}/?view=run-view&session=missing-run", wait_until="networkidle")
    page.wait_for_selector('[data-testid="run-view-not-found"]', timeout=10_000)

    topbar_box = page.get_by_test_id("otto-app-shell-topbar").bounding_box()
    not_found_box = page.get_by_test_id("run-view-not-found").bounding_box()
    assert topbar_box is not None
    assert not_found_box is not None
    assert not_found_box["y"] <= topbar_box["height"] + 96
    assert page.get_by_text("Run not found").is_visible()
    assert page.get_by_test_id("otto-app-shell-back-to-runs").is_visible()
