"""Browser regression for mc-audit microinteractions I5 — drawer animation.

The TaskCard's More/Less drawer used to expand/collapse instantly with no
visual cue:

* No height transition — layout snapped open/closed.
* No chevron — only the label "More"/"Less" changed.

Fix:

* Drawer wraps in a grid container that animates ``grid-template-rows``
  ``0fr → 1fr`` over ``180ms`` ease-out (≤200ms requirement).
* Chevron child rotates ``0deg → 90deg`` via ``transform`` transition,
  driven by the toggle button's ``aria-expanded`` attribute.
* The global ``prefers-reduced-motion: reduce`` rule shortens every
  animation/transition to ``0.001ms`` (≤1ms requirement).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_drawer_animation.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


def _projects_payload() -> dict[str, Any]:
    return {
        "launcher_enabled": False,
        "projects_root": "/tmp/managed",
        "current": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": "abc1234",
        },
        "projects": [],
    }


def _landing_item() -> dict[str, Any]:
    return {
        "task_id": "task-drawer",
        "summary": "build the drawer scenario",
        "branch": "build/task-drawer",
        "branch_exists": True,
        "queue_status": "done",
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": None,
        "queue_finished_at": None,
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": "ready",
        "merge_blocked": False,
        "blockers": [],
        "merge_id": None,
        "merged_at": None,
        "diff_path": None,
        "diff_relpath": None,
        "diff_error": None,
        "changed_file_count": 3,
        "changed_files": ["a.py", "b.py", "c.py"],
        "diff_size_bytes": 120,
        "diff_truncated": False,
        "merge_target": SAMPLE_TARGET,
        "merge_base_sha": None,
        "head_sha": None,
        "target_sha": None,
        "exit_code": None,
        "elapsed_s": 90,
        "cost_usd": 0.05,
        "duration_s": 90,
        "stories_passed": 2,
        "stories_tested": 2,
        "label": "ready",
        "merge_status": None,
        "merge_run_status": None,
        "actions": [],
        "intent": None,
        "run_id": "run-drawer",
    }


def _state() -> dict[str, Any]:
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
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
            "items": [_landing_item()],
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {"items": [], "total_count": 0, "active_count": 0, "refresh_interval_s": 1.5},
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 80, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 1,
            "state_tasks": 1,
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


def _install_routes(page: Any) -> None:
    payload = _state()

    def projects(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_projects_payload()))

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(mc_backend: Any, page: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)


def test_drawer_chevron_rotates_on_toggle(mc_backend: Any, page: Any) -> None:
    """Click the More toggle: chevron transform rotates from 0deg to 90deg."""

    _install_routes(page)
    _hydrate(mc_backend, page)

    # Find the first task-card-toggle button.
    toggle = page.locator(".task-card-toggle").first
    toggle.wait_for(state="visible", timeout=5_000)

    chevron = page.locator(".task-card-toggle .task-card-toggle-chevron").first
    chevron.wait_for(state="visible", timeout=2_000)

    # Initial collapsed state: aria-expanded is "false", chevron rotation is identity.
    initial = page.evaluate(
        """() => {
            const btn = document.querySelector('.task-card-toggle');
            const chev = btn?.querySelector('.task-card-toggle-chevron');
            return {
                expanded: btn?.getAttribute('aria-expanded'),
                transform: chev ? window.getComputedStyle(chev).transform : null,
            };
        }"""
    )
    assert initial["expanded"] == "false", f"toggle should start collapsed; got {initial!r}"
    # transform should be 'none' or matrix(1,0,0,1,0,0) (no rotation).
    assert initial["transform"] in {"none", "matrix(1, 0, 0, 1, 0, 0)"}, (
        f"chevron should start unrotated; got {initial!r}"
    )

    toggle.click()
    page.wait_for_function(
        "() => document.querySelector('.task-card-toggle')?.getAttribute('aria-expanded') === 'true'",
        timeout=2_000,
    )
    # Wait for the chevron CSS transition (≤180ms) to finish before sampling
    # the computed transform — otherwise we may see an intermediate matrix.
    page.wait_for_function(
        """() => {
            const chev = document.querySelector('.task-card-toggle .task-card-toggle-chevron');
            if (!chev) return false;
            const t = window.getComputedStyle(chev).transform;
            if (t === 'none') return false;
            const nums = t.startsWith('matrix(') ? t.slice(7, -1).split(',').map(Number) : [];
            // matrix[0] = cos(angle); we want angle ≈ 90deg → cos ≈ 0.
            return nums.length >= 2 && Math.abs(nums[0]) < 0.05 && Math.abs(nums[1] - 1) < 0.05;
        }""",
        timeout=2_000,
    )
    # After the transition completes the chevron should be at 90deg rotation.
    expanded = page.evaluate(
        """() => {
            const btn = document.querySelector('.task-card-toggle');
            const chev = btn?.querySelector('.task-card-toggle-chevron');
            return {
                expanded: btn?.getAttribute('aria-expanded'),
                transform: chev ? window.getComputedStyle(chev).transform : null,
            };
        }"""
    )
    assert expanded["expanded"] == "true"
    # 90deg rotation = matrix(cos90, sin90, -sin90, cos90, 0, 0) ≈ matrix(0, 1, -1, 0, 0, 0)
    # Browsers may render with floating-point precision (e.g. 6.12e-17). Assert
    # that the matrix encodes a 90-ish-degree rotation by extracting its
    # components rather than string-matching.
    transform = expanded["transform"] or ""
    assert transform.startswith("matrix("), (
        f"chevron transform should be a matrix() after expand; got {transform!r}"
    )
    # Parse "matrix(a, b, c, d, e, f)" → a should be near 0 (cos 90°),
    # b should be near 1 (sin 90°). Tolerate floating-point noise.
    nums = [float(x) for x in transform[len("matrix("):-1].split(",")]
    assert abs(nums[0]) < 0.1, f"matrix[0]=cos(angle) should be near 0; got {transform!r}"
    assert abs(nums[1] - 1.0) < 0.1, f"matrix[1]=sin(angle) should be near 1; got {transform!r}"


def test_drawer_height_transition_under_200ms(mc_backend: Any, page: Any) -> None:
    """Drawer wrap has a transition on grid-template-rows of ≤200ms."""

    _install_routes(page)
    _hydrate(mc_backend, page)

    toggle = page.locator(".task-card-toggle").first
    toggle.wait_for(state="visible", timeout=5_000)

    info = page.evaluate(
        """() => {
            const wrap = document.querySelector('.task-card-drawer-wrap');
            if (!wrap) return null;
            const style = window.getComputedStyle(wrap);
            return {
                transitionProperty: style.transitionProperty,
                transitionDuration: style.transitionDuration,
                gridTemplateRows: style.gridTemplateRows,
            };
        }"""
    )
    assert info is not None, "expected .task-card-drawer-wrap on the page"
    # transitionProperty should reference grid-template-rows (or 'all').
    prop = info["transitionProperty"] or ""
    assert "grid-template-rows" in prop or prop == "all", (
        f"drawer wrap should transition grid-template-rows; got {info!r}"
    )
    # transitionDuration parses to seconds; assert ≤0.2s.
    duration = info["transitionDuration"] or "0s"
    seconds = float(duration.split(",")[0].rstrip("s"))
    assert seconds <= 0.20, (
        f"drawer transition must be ≤200ms; got {duration!r}"
    )


def test_drawer_transition_disabled_under_reduced_motion(
    mc_backend: Any, browser: Any
) -> None:
    """With prefers-reduced-motion=reduce, transition-duration must be ≤1ms."""

    context = browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    try:
        _install_routes(page)
        _hydrate(mc_backend, page)

        toggle = page.locator(".task-card-toggle").first
        toggle.wait_for(state="visible", timeout=5_000)

        info = page.evaluate(
            """() => {
                const wrap = document.querySelector('.task-card-drawer-wrap');
                const chev = document.querySelector('.task-card-toggle .task-card-toggle-chevron');
                return {
                    wrapDuration: wrap ? window.getComputedStyle(wrap).transitionDuration : null,
                    chevronDuration: chev ? window.getComputedStyle(chev).transitionDuration : null,
                };
            }"""
        )
        for key in ("wrapDuration", "chevronDuration"):
            duration = info[key] or "0s"
            # Take the first transition entry if comma-separated.
            seconds = float(duration.split(",")[0].rstrip("s"))
            # ≤1ms = 0.001s, allow a tiny epsilon.
            assert seconds <= 0.0011, (
                f"{key} under prefers-reduced-motion must be ≤1ms; got {duration!r}"
            )
    finally:
        context.close()
