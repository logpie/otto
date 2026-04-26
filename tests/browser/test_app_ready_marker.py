"""W3-CRITICAL-2 regression — deterministic SPA-ready marker.

Live W3 dogfood lost a $0 build because the harness's standard probe
(`#root` has children) returned true on the LoadingMissionControl
skeleton — the actionable shell hadn't rendered yet, so a
`getByTestId("mission-new-job-button")` against the post-probe DOM
silently missed and the harness ran 200s of no-ops.

Fix: the SPA exposes two equivalent ready signals once /api/projects +
/api/state have both resolved AND the boot-loading gate has cleared:

  1. `[data-mc-shell="ready"]` attribute on the top-level shell wrapper.
     Selectable from any DOM-aware automation, including playwright's
     `wait_for_selector`. The attribute is ABSENT during boot.
  2. `window.__OTTO_MC_READY === true`. Useful for headless contexts
     that don't have data-attribute access (jsdom snapshots, MCP
     `evaluate_script`).

These tests stub /api/projects with a programmable delay so we can
inspect the DOM mid-boot, then again after the responses fulfill.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_app_ready_marker.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Fixture payloads — kept in-file so the tests survive future shape churn in
# the shared `_fixtures/` package.
# --------------------------------------------------------------------------- #


def _state_payload() -> dict[str, Any]:
    """Minimal /api/state payload — enough fields for the main shell to
    decide it has a project and render the actionable Mission Control."""

    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
            "dirty": False,
            "head_sha": "abc1234",
            "defaults": {
                "provider": "claude",
                "model": "sonnet-4-7",
                "reasoning_effort": "high",
                "certifier_mode": "fast",
                "skip_product_qa": False,
                "config_file_exists": True,
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
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
            "collisions": [],
        },
        "live": {"items": [], "total_count": 0, "active_count": 0, "refresh_interval_s": 1.5},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 80, "truncated": False},
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
                "mode": "manual",
                "path": "",
                "metadata": None,
                "metadata_error": None,
                "supervised_pid": None,
                "matches_blocking_pid": None,
                "can_start": False,
                "can_stop": False,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
                "stop_target_pid": None,
                "watcher_log_path": "",
                "web_log_exists": False,
                "queue_lock_holder_pid": None,
            },
            "issues": [],
        },
    }


def _projects_payload() -> dict[str, Any]:
    return {
        "launcher_enabled": False,
        "projects_root": "/tmp/managed",
        "current": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
            "dirty": False,
            "head_sha": "abc1234",
        },
        "projects": [],
    }


def _install_state_route(page: Any, payload: dict[str, Any], *, delay_s: float = 0.0) -> None:
    body = json.dumps(payload)

    def handler(route: Any) -> None:
        if delay_s > 0:
            time.sleep(delay_s)
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/state*", handler)


def _install_projects_route(page: Any, payload: dict[str, Any], *, delay_s: float = 0.0) -> None:
    body = json.dumps(payload)

    def handler(route: Any) -> None:
        if delay_s > 0:
            time.sleep(delay_s)
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/projects", handler)


# --------------------------------------------------------------------------- #
# Required: marker absent during boot
# --------------------------------------------------------------------------- #


def test_app_ready_attribute_absent_during_boot_loading(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """While /api/projects is delayed, `data-mc-shell="ready"` must NOT appear.

    Mirrors the live W3 race: the boot-loading skeleton renders into #root
    immediately, but external automation must NOT treat that as a ready
    signal.
    """
    # 1s delay — comfortably wider than the test's snapshot window.
    _install_projects_route(page, _projects_payload(), delay_s=1.0)
    _install_state_route(page, _state_payload())

    page.goto(mc_backend.url, wait_until="domcontentloaded")

    # Sample within the delay — the skeleton has populated #root but neither
    # ready signal should be set.
    snapshot = page.evaluate(
        """() => ({
            hasReadyAttr: !!document.querySelector('[data-mc-shell="ready"]'),
            windowReady: window.__OTTO_MC_READY === true,
            hasBootLoading: !!document.querySelector('[data-testid=boot-loading]'),
            rootChildren: document.querySelector('#root')?.children.length || 0,
        })"""
    )
    assert snapshot["hasBootLoading"], (
        f"boot-loading skeleton missing — fixture broken? snapshot={snapshot!r}"
    )
    assert snapshot["rootChildren"] > 0, (
        f"#root should already have the skeleton — snapshot={snapshot!r}"
    )
    assert not snapshot["hasReadyAttr"], (
        f"data-mc-shell=ready leaked during boot. snapshot={snapshot!r}"
    )
    assert not snapshot["windowReady"], (
        f"window.__OTTO_MC_READY leaked during boot. snapshot={snapshot!r}"
    )


# --------------------------------------------------------------------------- #
# Required: marker set after boot
# --------------------------------------------------------------------------- #


def test_app_ready_attribute_set_after_boot(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Once /api/projects + /api/state both resolve, the marker appears."""
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, _state_payload())

    page.goto(mc_backend.url, wait_until="networkidle")

    # Single deterministic probe — `wait_for_selector` is the recommended
    # public boot signal documented for external automation.
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)

    snapshot = page.evaluate(
        """() => ({
            hasReadyAttr: !!document.querySelector('[data-mc-shell="ready"]'),
            attrValue: document.querySelector('[data-mc-shell]')?.getAttribute('data-mc-shell') || null,
            hasNewJob: !!document.querySelector('[data-testid=new-job-button]'),
            hasBootLoading: !!document.querySelector('[data-testid=boot-loading]'),
        })"""
    )
    assert snapshot["hasReadyAttr"], snapshot
    assert snapshot["attrValue"] == "ready", snapshot
    assert snapshot["hasNewJob"], (
        f"actionable controls must exist when marker is set — snapshot={snapshot!r}"
    )
    assert not snapshot["hasBootLoading"], (
        f"boot-loading skeleton must be gone when marker is set — snapshot={snapshot!r}"
    )


# --------------------------------------------------------------------------- #
# Required: equivalent window-global signal
# --------------------------------------------------------------------------- #


def test_window_otto_mc_ready_global(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """`window.__OTTO_MC_READY` is a parallel boot signal for headless contexts."""
    _install_projects_route(page, _projects_payload())
    _install_state_route(page, _state_payload())

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)

    # Poll the window flag — it flips inside a useEffect so it's set after
    # render but should be true before any test interaction.
    page.wait_for_function("window.__OTTO_MC_READY === true", timeout=5_000)

    snapshot = page.evaluate(
        """() => ({
            windowReady: window.__OTTO_MC_READY,
            attrReady: !!document.querySelector('[data-mc-shell="ready"]'),
        })"""
    )
    assert snapshot["windowReady"] is True, snapshot
    assert snapshot["attrReady"] is True, (
        f"both signals must agree — snapshot={snapshot!r}"
    )


# --------------------------------------------------------------------------- #
# Defensive: no false positive on launcher-only render
# --------------------------------------------------------------------------- #


def test_launcher_only_does_not_set_ready_marker(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Launcher placeholder must NOT advertise the actionable shell.

    Launcher mode is its own destination; tooling that targets the
    Mission Control shell would otherwise click into a different UI.
    """
    launcher_projects = {
        "launcher_enabled": True,
        "projects_root": "/tmp/managed",
        "current": None,
        "projects": [],
    }
    _install_projects_route(page, launcher_projects)
    # State is intentionally NOT installed — launcher renders before /api/state.

    page.goto(mc_backend.url, wait_until="domcontentloaded")
    # Wait for ANY visible launcher chrome so we know the SPA hydrated.
    page.wait_for_selector('[data-testid=launcher-subhead]', timeout=10_000)

    snapshot = page.evaluate(
        """() => ({
            hasReadyAttr: !!document.querySelector('[data-mc-shell="ready"]'),
            windowReady: window.__OTTO_MC_READY === true,
            hasLauncher: !!document.querySelector('[data-testid=launcher-subhead]'),
        })"""
    )
    assert snapshot["hasLauncher"], snapshot
    assert not snapshot["hasReadyAttr"], (
        f"launcher must not flip the actionable-shell marker — snapshot={snapshot!r}"
    )
    assert not snapshot["windowReady"], (
        f"launcher must not flip the window flag — snapshot={snapshot!r}"
    )
