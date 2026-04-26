"""Browser test for filter URL persistence.

Heavy-user paper cut #1 (mc-audit `_hunter-findings/heavy-user.md`):

    Filters (type, outcome, query, activeOnly) live in React state only. A
    refresh / project-switch / popstate loses the filter. Cannot share a
    filtered URL with a teammate.

The fix: URL params `?ft=…&fo=…&fq=…&fa=true` mirror the filter set;
hydrated on first paint, replaced on every change.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_filters_url_persistence.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _state_payload() -> dict[str, Any]:
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
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
            "counts": {"queued": 0, "running": 0},
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
            "items": [],
            "counts": {"ready": 0, "merged": 0, "total": 0},
            "merge_blocked": False,
            "dirty_files": [],
            "target": "main",
        },
        "live": {"items": [], "refresh_interval_s": 1.5},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"items": [], "total_count": 0, "malformed_count": 0},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": 0,
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
            "files": {
                "queue": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "state": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "commands": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "processing": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
            },
            "supervisor": {
                "can_start": False,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


def _install_routes(page: Any) -> None:
    page.route(
        "**/api/projects",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "launcher_enabled": False,
                "projects_root": "",
                "current": None,
                "projects": [],
            }),
        ),
    )
    body = json.dumps(_state_payload())
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _hydrate(page: Any, mc_backend: Any, disable_animations: Any, query: str = "") -> None:
    page.goto(f"{mc_backend.url}{query}", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    # Toolbar filter selects need to be present before tests poke them.
    page.get_by_test_id("filter-type-select").wait_for(state="visible", timeout=5_000)


def test_type_filter_pushes_to_url(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Selecting type=build adds ?ft=build to the URL."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.get_by_test_id("filter-type-select").select_option("build")
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('ft') === 'build'",
        timeout=3_000,
    )


def test_outcome_filter_pushes_to_url(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Selecting outcome=failed adds ?fo=failed to the URL."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.get_by_test_id("filter-outcome-select").select_option("failed")
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('fo') === 'failed'",
        timeout=3_000,
    )


def test_search_query_persists_to_url(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Typing in the search box (debounced) writes ?fq=<query>."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    box = page.get_by_test_id("filter-search-input")
    box.fill("auth")
    # The toolbar debounces query commit by ~200ms.
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('fq') === 'auth'",
        timeout=3_000,
    )


def test_active_only_persists_to_url(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Toggling Active checkbox writes ?fa=true."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.locator(".toolbar input[type=checkbox]").first.check()
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('fa') === 'true'",
        timeout=3_000,
    )


def test_deep_link_hydrates_filters(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """`?ft=build&fo=failed&fq=kanban&fa=true` lands with all filters set."""
    _install_routes(page)
    _hydrate(
        page,
        mc_backend,
        disable_animations,
        query="?ft=build&fo=failed&fq=kanban&fa=true",
    )

    assert page.get_by_test_id("filter-type-select").input_value() == "build"
    assert page.get_by_test_id("filter-outcome-select").input_value() == "failed"
    assert page.get_by_test_id("filter-search-input").input_value() == "kanban"
    active_box = page.locator(".toolbar input[type=checkbox]").first
    assert active_box.is_checked(), "expected active-only checkbox to be hydrated"
