"""Browser regression for mc-audit responsive layout — no horizontal scroll
or clipped content at mini / MBA / iPhone viewports.

The shell adds an explicit ``< 1024px`` breakpoint that stacks the sidebar
above the workspace, plus a global ``html, body { overflow-x: hidden }``
catch-all so transient overflow during data hydration cannot trigger a
horizontal scrollbar.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_responsive_layout_breaks.py \\
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
            "items": [],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
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


def _install_routes(page: Any) -> None:
    payload = _state()

    def projects(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_projects_payload()))

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(page: Any, mc_backend: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


@pytest.mark.parametrize(
    "label,viewport_kwargs",
    [
        ("mac-mini", {"viewport": {"width": 1920, "height": 1080}}),
        ("mba", {"viewport": {"width": 1440, "height": 900}}),
        ("iphone", None),  # filled at runtime from playwright.devices
    ],
)
def test_no_horizontal_scroll_or_clipping(
    mc_backend: Any,
    browser: Any,
    playwright: Any,
    disable_animations: Any,
    label: str,
    viewport_kwargs: dict[str, Any] | None,
) -> None:
    """At each device size, ``document.scrollWidth`` must equal ``clientWidth``."""

    if viewport_kwargs is None:
        viewport_kwargs = dict(playwright.devices["iPhone 14"])

    context = browser.new_context(**viewport_kwargs)
    page = context.new_page()
    try:
        _install_routes(page)
        _hydrate(page, mc_backend, disable_animations)

        info = page.evaluate(
            """() => ({
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                bodyScrollWidth: document.body.scrollWidth,
                bodyClientWidth: document.body.clientWidth,
            })"""
        )
        # Allow 1px slack for sub-pixel rounding.
        assert info["scrollWidth"] - info["clientWidth"] <= 1, (
            f"[{label}] horizontal overflow on documentElement; got {info!r}"
        )
        assert info["bodyScrollWidth"] - info["bodyClientWidth"] <= 1, (
            f"[{label}] horizontal overflow on body; got {info!r}"
        )

        # Verify primary controls are within the viewport (right edge).
        viewport_width = info["clientWidth"]
        clipped = page.evaluate(
            f"""() => {{
                const viewport = {viewport_width};
                const targets = ['[data-testid=new-job-button]', '.brand', '.project-meta'];
                const results = {{}};
                for (const sel of targets) {{
                    const el = document.querySelector(sel);
                    if (!el) {{ results[sel] = null; continue; }}
                    const r = el.getBoundingClientRect();
                    results[sel] = {{
                        right: r.right,
                        left: r.left,
                        clipped: r.right > viewport + 1 || r.left < -1,
                    }};
                }}
                return results;
            }}"""
        )
        for sel, data in clipped.items():
            if data is None:
                continue
            assert not data["clipped"], (
                f"[{label}] selector {sel!r} clipped at viewport ({viewport_width}px); got {data!r}"
            )
    finally:
        context.close()
