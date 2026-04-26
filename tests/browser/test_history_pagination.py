"""Browser tests for the history-pagination cluster (mc-audit Phase 4 cluster D).

Each test fails before the corresponding fix in `App.tsx`/`api.ts`/`model.py`
and passes after. They are paired with the fixes in this cluster:

  - `/api/state` accepts `history_page` + `history_page_size` and returns
    `total_pages`; the client now sends them and renders pagination
    controls (mc-audit codex-state-management #6, heavy-user CRITICAL,
    long-string-overflow #3).
  - URL persistence so power users can deep-link to page N and use the
    browser Back button to step backwards through pages.
  - Filter changes reset to page 1 (avoids the confusing "page 5 with 0
    matches" empty state).
  - Stale deep-links (?hp=99) recover with an actionable hint instead of
    silently snapping to the last page with no explanation.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_history_pagination.py -m browser -p playwright -v

The bundle build is required for the fixes to be visible in the served SPA.
After implementation, run once with the full bundle build (omit the env var)
to confirm the live bundle has them.

Tests stub `/api/state` and `/api/projects` so the SPA renders deterministic
history payloads without seeding a real queue. The handler reads the
`history_page` and `history_page_size` query params and slices a fixed
synthetic history, mirroring what the real `MissionControlModel` does
server-side. This keeps each test self-contained and avoids any timing
dependency on the real watcher subprocess.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Synthetic history dataset and StateResponse builder
# --------------------------------------------------------------------------- #


def _history_item(index: int) -> dict[str, Any]:
    """One HistoryItem row. Keep all fields the SPA reads non-null."""
    run_id = f"run-{index:04d}"
    return {
        "run_id": run_id,
        "domain": "build",
        "run_type": "build",
        "command": "build",
        "status": "completed",
        "terminal_outcome": "success",
        "queue_task_id": f"task-{index:04d}",
        "merge_id": None,
        "branch": None,
        "worktree": None,
        "summary": f"Synthetic run {index}",
        "intent": None,
        "completed_at_display": "2026-04-25 12:00",
        "outcome_display": "success",
        "duration_s": 30.0,
        "duration_display": "30s",
        "cost_usd": 0.01,
        "cost_display": "$0.01",
        "resumable": False,
        "adapter_key": "build",
    }


def _build_state_payload(
    history_total: int,
    *,
    history_page_zero_based: int,
    history_page_size: int,
) -> dict[str, Any]:
    """A StateResponse with `history_total` synthetic rows, sliced to one page.

    `history_page_zero_based` mirrors the server's 0-based page index. The UI
    sends 1-based and our `stateQueryParams` translates at the boundary, so
    the handler in each test will see whatever the SPA actually sent.
    """

    items = [_history_item(i) for i in range(1, history_total + 1)]
    page_size = max(1, history_page_size)
    total_pages = max(1, (history_total + page_size - 1) // page_size) if history_total else 1
    page = max(0, min(history_page_zero_based, total_pages - 1)) if history_total else 0
    start = page * page_size
    page_items = items[start : start + page_size]
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
        "history": {
            "items": page_items,
            "page": page,
            "page_size": page_size,
            "total_rows": history_total,
            "total_pages": total_pages,
        },
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


def _install_projects_route(page: Any) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "launcher_enabled": False,
                "projects_root": "",
                "current": None,
                "projects": [],
            }),
        )

    page.route("**/api/projects", handler)


class _StateRouter:
    """Captures the latest `/api/state` query and replies with a synthetic payload."""

    def __init__(self, history_total: int) -> None:
        self.history_total = history_total
        self.last_query: dict[str, list[str]] = {}
        self.queries: list[dict[str, list[str]]] = []
        self._lock = threading.Lock()

    def install(self, page: Any) -> None:
        page.route("**/api/state*", self._handle)

    def update_total(self, history_total: int) -> None:
        self.history_total = history_total

    def _handle(self, route: Any) -> None:
        url = route.request.url
        query = parse_qs(urlparse(url).query)
        with self._lock:
            self.last_query = query
            self.queries.append(query)
        page_param = query.get("history_page", ["0"])[0]
        size_param = query.get("history_page_size", ["25"])[0]
        try:
            page_idx = max(0, int(page_param))
        except (TypeError, ValueError):
            page_idx = 0
        try:
            size = int(size_param)
        except (TypeError, ValueError):
            size = 25
        if size not in {10, 25, 50, 100}:
            size = 25
        payload = _build_state_payload(
            self.history_total,
            history_page_zero_based=page_idx,
            history_page_size=size,
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )


def _hydrate(page: Any, mc_backend: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)


def _open_diagnostics(page: Any) -> None:
    """Switch to the Diagnostics view where the History panel renders."""
    page.get_by_test_id("diagnostics-tab").click()
    page.locator("[data-testid=history-pagination]").wait_for(state="visible", timeout=5_000)


# --------------------------------------------------------------------------- #
# CRITICAL — pagination renders at all
# --------------------------------------------------------------------------- #


def test_history_pagination_renders_when_total_pages_gt_one(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """With 60 rows at page-size 25, controls render with 'Page 1 of 3'.

    Before fix: only the table renders. Power users see `total_rows=60` in
    the pill but no way to advance past row 25.
    """

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    status = page.get_by_test_id("history-pagination-status").text_content() or ""
    assert "Page 1 of 3" in status, f"expected 'Page 1 of 3' in status, got: {status!r}"
    assert "60" in status, f"expected total-rows in status, got: {status!r}"


def test_history_pagination_next_advances_page(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Clicking Next pushes ?hp=2 to the URL and shows page-2 rows."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    # Sanity: page 1 shows run-0001.
    assert page.locator("text=task-0001").first.is_visible()

    page.get_by_test_id("history-next-button").click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 2 of 3')",
        timeout=5_000,
    )
    # URL reflects the page change (1-based in URL).
    assert page.url.endswith("hp=2") or "hp=2" in page.url, f"expected ?hp=2 in URL, got {page.url}"
    # Row 26 (first row of page 2) appears.
    assert page.locator("text=task-0026").first.is_visible()


def test_history_pagination_previous_disabled_on_page_one(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The Previous button must be disabled when on page 1."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    prev = page.get_by_test_id("history-prev-button")
    assert prev.is_disabled()
    assert prev.get_attribute("aria-disabled") == "true"


def test_history_pagination_next_disabled_on_last_page(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Jumping to the last page disables Next."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    # Use the jump-to-page input to land on page 3 (last page for 60/25).
    jump = page.get_by_test_id("history-jump-input")
    jump.fill("3")
    jump.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 3 of 3')",
        timeout=5_000,
    )
    nxt = page.get_by_test_id("history-next-button")
    assert nxt.is_disabled()
    assert nxt.get_attribute("aria-disabled") == "true"


def test_history_pagination_jump_to_page_input(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Typing 3 + Enter in the jump input navigates to page 3."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    jump = page.get_by_test_id("history-jump-input")
    jump.fill("3")
    jump.press("Enter")

    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 3 of 3')",
        timeout=5_000,
    )
    assert "hp=3" in page.url
    # Page 3 shows run 51 onward.
    assert page.locator("text=task-0051").first.is_visible()


def test_history_pagination_page_size_selector_changes_page_size(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Switching the page-size selector to 50 sends history_page_size=50."""

    router = _StateRouter(history_total=120)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    # Move to page 2 first so we can verify page resets to 1 after the change.
    page.get_by_test_id("history-next-button").click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 2')",
        timeout=5_000,
    )

    page.get_by_test_id("history-page-size-select").select_option("50")

    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 1')",
        timeout=5_000,
    )
    # The next /api/state request must include history_page_size=50.
    page.wait_for_function(
        "() => true",  # let the network settle one tick
        timeout=500,
    )
    # Verify the most recent state request carried the new size.
    saw_size = any(
        q.get("history_page_size", [None])[0] == "50" for q in router.queries[-5:]
    )
    assert saw_size, f"expected history_page_size=50 in recent queries; got {router.queries[-5:]!r}"
    # Total pages should now be 3 (120/50).
    status = page.get_by_test_id("history-pagination-status").text_content() or ""
    assert "Page 1 of 3" in status, f"expected 'Page 1 of 3', got {status!r}"


def test_history_deep_link_loads_correct_page(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visiting `?hp=3` directly loads page 3 on first paint."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    page.goto(f"{mc_backend.url}?view=diagnostics&hp=3", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    page.locator("[data-testid=history-pagination]").wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 3 of 3')",
        timeout=5_000,
    )
    # Page 3 contains run 51 onward.
    assert page.locator("text=task-0051").first.is_visible()


def test_history_invalid_deep_link_recovers(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visiting `?hp=99` on a 3-page dataset shows a recovery hint, not silent clamp."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    page.goto(f"{mc_backend.url}?view=diagnostics&hp=99", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    page.locator("[data-testid=history-out-of-range]").wait_for(state="visible", timeout=5_000)
    text = page.get_by_test_id("history-out-of-range").text_content() or ""
    assert "Page 99" in text, f"expected 'Page 99' in recovery text, got {text!r}"
    # Recovery button resets to page 1.
    page.get_by_test_id("history-recover-button").click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 1 of 3')",
        timeout=5_000,
    )


def test_history_filter_resets_page(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Changing the type filter while on page 3 resets to page 1."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    page.goto(f"{mc_backend.url}?view=diagnostics&hp=3", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    page.locator("[data-testid=history-pagination]").wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 3')",
        timeout=5_000,
    )

    page.get_by_test_id("filter-type-select").select_option("build")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 1')",
        timeout=5_000,
    )
    # URL no longer carries `?hp=3`; it should be cleared (page 1 is implicit).
    assert "hp=3" not in page.url, f"expected hp=3 cleared from URL after filter change, got {page.url}"


def test_history_back_button_returns_to_previous_page(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Browser Back after Next, Next walks back through the page history."""

    router = _StateRouter(history_total=60)
    _install_projects_route(page)
    router.install(page)

    _hydrate(page, mc_backend, disable_animations)
    _open_diagnostics(page)

    # page 1 -> Next -> page 2
    page.get_by_test_id("history-next-button").click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 2 of 3')",
        timeout=5_000,
    )
    # page 2 -> Next -> page 3
    page.get_by_test_id("history-next-button").click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 3 of 3')",
        timeout=5_000,
    )

    # Back -> page 2
    page.go_back()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 2 of 3')",
        timeout=5_000,
    )
    # Back -> page 1
    page.go_back()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=history-pagination-status]')?.textContent?.includes('Page 1 of 3')",
        timeout=5_000,
    )
