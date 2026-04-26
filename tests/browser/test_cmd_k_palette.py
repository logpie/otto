"""Browser test for the Cmd-K command palette (project switcher).

Heavy-user paper cut #4 (mc-audit `_hunter-findings/heavy-user.md`):

    Power user toggling between two projects (otto-dev ↔ bench-suite) hits
    the launcher every time, picks the row, waits for state to load. No
    quick-switcher.

The fix: Cmd/Ctrl-K opens a small command-palette overlay listing the
projects from `/api/projects`, allows fuzzy-substring filtering and
Enter-to-switch. Escape closes.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_cmd_k_palette.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _project(name: str, path: str) -> dict[str, Any]:
    return {
        "path": path,
        "name": name,
        "branch": "main",
        "dirty": False,
        "head_sha": "abc1234",
        "managed": True,
    }


PROJECT_CURRENT = _project("kanban-portal", "/tmp/projects/kanban-portal")
PROJECT_BENCH = _project("bench-suite", "/tmp/projects/bench-suite")
PROJECT_OTTO = _project("otto-dev", "/tmp/projects/otto-dev")


def _projects_payload() -> dict[str, Any]:
    return {
        "launcher_enabled": True,
        "projects_root": "/tmp/projects",
        "current": PROJECT_CURRENT,
        "projects": [PROJECT_CURRENT, PROJECT_BENCH, PROJECT_OTTO],
    }


def _state_payload() -> dict[str, Any]:
    return {
        "project": {
            **PROJECT_CURRENT,
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


def _install_routes(page: Any) -> dict[str, Any]:
    """Install routes; return a tracker the test can read for /api/projects/select."""
    selected: dict[str, Any] = {"path": None}

    page.route(
        "**/api/projects",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_projects_payload()),
        ),
    )
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_state_payload()),
        ),
    )

    def _select(route: Any) -> None:
        try:
            body = json.loads(route.request.post_data or "{}")
        except Exception:
            body = {}
        selected["path"] = body.get("path")
        # Echo a sensible mutation response so the SPA happily updates state.
        next_current = next(
            (p for p in (PROJECT_CURRENT, PROJECT_BENCH, PROJECT_OTTO) if p["path"] == selected["path"]),
            PROJECT_CURRENT,
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True,
                "project": next_current,
                "current": next_current,
                "projects": [PROJECT_CURRENT, PROJECT_BENCH, PROJECT_OTTO],
            }),
        )

    page.route("**/api/projects/select", _select)
    return selected


def _hydrate(page: Any, mc_backend: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)
    page.get_by_test_id("new-job-button").wait_for(state="visible", timeout=5_000)


def test_cmd_k_opens_palette(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Pressing Cmd-K (or Ctrl-K) opens the palette and lists projects."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    # Try Meta+K first; fall back to Control+K. Playwright treats Meta+K
    # as Cmd-K on darwin and a no-op on linux — we accept either path.
    page.keyboard.press("Meta+K")
    palette = page.get_by_test_id("command-palette")
    if not palette.is_visible():
        page.keyboard.press("Control+K")
    palette.wait_for(state="visible", timeout=3_000)
    # The current project row is rendered with the "current" badge.
    page.locator(f"[data-testid='command-palette-row-{PROJECT_CURRENT['path']}']").wait_for(state="visible", timeout=2_000)
    page.locator(f"[data-testid='command-palette-row-{PROJECT_BENCH['path']}']").wait_for(state="visible", timeout=2_000)
    page.locator(f"[data-testid='command-palette-row-{PROJECT_OTTO['path']}']").wait_for(state="visible", timeout=2_000)


def test_cmd_k_fuzzy_filter_narrows(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Typing in the input filters projects by substring."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.keyboard.press("Meta+K")
    if not page.get_by_test_id("command-palette").is_visible():
        page.keyboard.press("Control+K")
    page.get_by_test_id("command-palette-input").wait_for(state="visible", timeout=3_000)
    page.get_by_test_id("command-palette-input").fill("bench")
    # Only bench-suite remains in the list.
    page.locator(f"[data-testid='command-palette-row-{PROJECT_BENCH['path']}']").wait_for(state="visible", timeout=2_000)
    assert page.locator(f"[data-testid='command-palette-row-{PROJECT_OTTO['path']}']").count() == 0
    assert page.locator(f"[data-testid='command-palette-row-{PROJECT_CURRENT['path']}']").count() == 0


def test_cmd_k_enter_switches_project(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Filter + Enter posts to /api/projects/select with the matched path."""
    selected = _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.keyboard.press("Meta+K")
    if not page.get_by_test_id("command-palette").is_visible():
        page.keyboard.press("Control+K")
    box = page.get_by_test_id("command-palette-input")
    box.wait_for(state="visible", timeout=3_000)
    box.fill("otto")
    # Only otto-dev now matches; first-and-only highlighted is index 0 →
    # Enter triggers /api/projects/select.
    box.press("Enter")
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=command-palette]')",
        timeout=3_000,
    )
    # The fake select route captured the requested path.
    assert selected["path"] == PROJECT_OTTO["path"], f"selected={selected!r}"


def test_cmd_k_escape_closes(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Escape from the palette closes it without selecting."""
    selected = _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.keyboard.press("Meta+K")
    if not page.get_by_test_id("command-palette").is_visible():
        page.keyboard.press("Control+K")
    page.get_by_test_id("command-palette-input").wait_for(state="visible", timeout=3_000)
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=command-palette]')",
        timeout=3_000,
    )
    assert selected["path"] is None, f"unexpected select on Escape: {selected!r}"


def test_cmd_k_current_project_marked(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The current project shows a 'current' badge and is non-selectable."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.keyboard.press("Meta+K")
    if not page.get_by_test_id("command-palette").is_visible():
        page.keyboard.press("Control+K")
    row = page.locator(f"[data-testid='command-palette-row-{PROJECT_CURRENT['path']}']")
    row.wait_for(state="visible", timeout=3_000)
    assert "current" in (row.get_attribute("class") or ""), f"missing 'current' class: {row.get_attribute('class')!r}"
    select_btn = page.get_by_test_id(f"command-palette-select-{PROJECT_CURRENT['path']}")
    assert select_btn.is_disabled(), "current project button must be disabled"
