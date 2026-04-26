"""Browser test for the heavy-user history-table sort affordance.

Heavy-user paper cut #2 (mc-audit `_hunter-findings/heavy-user.md`):

    Power user sweeping history wants to sort by cost desc, duration desc,
    age. Today the columns Outcome / Run / Summary / Duration / Usage are
    non-clickable headers — no sort affordance at all.

This test asserts:
  - Each `<th>` exposes a `data-testid="history-sort-<col>"` button.
  - First click sorts ascending, indicator arrow appears, URL gains
    `hs=duration&hd=asc`.
  - Second click flips to descending, URL gains `hd=desc`.
  - Third click clears the sort, URL drops both keys.
  - Sorting `usage` desc by `cost_usd` puts the priciest row first
    regardless of the order the server returned (sort is page-local).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_history_table_sort.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Synthetic state response with three rows that have distinct sort keys
# --------------------------------------------------------------------------- #


def _history_item(idx: int, cost: float, duration: float, summary: str, outcome: str) -> dict[str, Any]:
    return {
        "run_id": f"run-{idx:03d}",
        "domain": "build",
        "run_type": "build",
        "command": "build",
        "status": "completed",
        "terminal_outcome": outcome,
        "queue_task_id": f"task-{idx:03d}",
        "merge_id": None,
        "branch": None,
        "worktree": None,
        "summary": summary,
        "intent": None,
        "completed_at_display": "2026-04-25 12:00",
        "outcome_display": outcome,
        "duration_s": duration,
        "duration_display": f"{int(duration)}s",
        "cost_usd": cost,
        "cost_display": f"${cost:.2f}",
        "resumable": False,
        "adapter_key": "build",
    }


def _state_payload() -> dict[str, Any]:
    items = [
        _history_item(1, 0.05, 30.0, "Alpha summary", "success"),
        _history_item(2, 1.20, 600.0, "Bravo summary", "failed"),
        _history_item(3, 0.40, 120.0, "Charlie summary", "interrupted"),
    ]
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
            "items": items,
            "page": 0,
            "page_size": 25,
            "total_rows": len(items),
            "total_pages": 1,
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
    target = f"{mc_backend.url}?view=diagnostics{query}"
    page.goto(target, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    page.locator("[data-testid=history-pagination]").wait_for(state="visible", timeout=5_000)


def _row_order(page: Any) -> list[str]:
    """Return the visible run ids in row order (queue_task_id values)."""
    return page.locator(".history-panel tbody tr [data-testid^='history-row-activator-']").all_text_contents()


def test_sort_button_first_click_asc_url_and_indicator(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Click `Usage` header → URL gains hs=usage&hd=asc, arrow indicator shows."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    # Sanity: rows arrive in server order (task-001, 002, 003).
    assert _row_order(page) == ["task-001", "task-002", "task-003"]

    page.get_by_test_id("history-sort-usage").click()
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('hs') === 'usage'",
        timeout=3_000,
    )
    assert "hd=asc" in page.url, f"expected hd=asc in URL, got {page.url}"

    # Asc by cost: $0.05 (task-001), $0.40 (task-003), $1.20 (task-002).
    assert _row_order(page) == ["task-001", "task-003", "task-002"]

    # Header text gains an arrow indicator.
    btn = page.get_by_test_id("history-sort-usage")
    label = btn.text_content() or ""
    assert "↑" in label or "↓" in label, f"expected sort arrow in '{label!r}'"


def test_sort_second_click_flips_desc(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Second click on the same header switches asc → desc."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    btn = page.get_by_test_id("history-sort-usage")
    btn.click()
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('hd') === 'asc'",
        timeout=3_000,
    )
    btn.click()
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('hd') === 'desc'",
        timeout=3_000,
    )
    # Desc by cost: $1.20 (task-002), $0.40 (task-003), $0.05 (task-001).
    assert _row_order(page) == ["task-002", "task-003", "task-001"]


def test_sort_third_click_resets(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Third click clears the sort (URL drops hs/hd, server order restored)."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    btn = page.get_by_test_id("history-sort-usage")
    btn.click()  # asc
    btn.click()  # desc
    btn.click()  # cleared
    page.wait_for_function(
        "() => !new URL(location.href).searchParams.get('hs') && !new URL(location.href).searchParams.get('hd')",
        timeout=3_000,
    )
    assert _row_order(page) == ["task-001", "task-002", "task-003"]


def test_sort_duration_desc_via_deeplink(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Deep-link `?hs=duration&hd=desc` lands sorted on first paint."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations, query="&hs=duration&hd=desc")

    # Desc by duration: 600s (task-002), 120s (task-003), 30s (task-001).
    assert _row_order(page) == ["task-002", "task-003", "task-001"]


def test_sort_outcome_alphabetical(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Sorting by outcome uses outcome_display alphabetical order."""
    _install_routes(page)
    _hydrate(page, mc_backend, disable_animations)

    page.get_by_test_id("history-sort-outcome").click()
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('hs') === 'outcome'",
        timeout=3_000,
    )
    # outcome_display = success, failed, interrupted → asc:
    # failed (task-002), interrupted (task-003), success (task-001).
    assert _row_order(page) == ["task-002", "task-003", "task-001"]
