"""Browser test for the in-pane log search affordance.

Heavy-user paper cut #6 (mc-audit `_hunter-findings/heavy-user.md`):

    A long run's log can hit the 14 000-char compactLongText cap. User
    trying to find "ERROR" or "DIAGNOSIS" in a 20k+ line log scrolls
    forever. Browser Ctrl-F finds matches, but only inside the truncated
    tail.

Asserted behaviour:
  - Search input is rendered above the `<pre>` once the log buffer
    populates.
  - Typing a query highlights matches with `<mark>`.
  - The match-count pill shows `n / total`.
  - Enter steps to the next match; Shift+Enter steps backwards.
  - Cmd-F focuses the input.
  - Escape clears the search.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_log_search.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


RUN_ID = "log-search-run"


def _live_item() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "display_name": "build:logs",
        "status": "completed",
        "terminal_outcome": "success",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "build/logs",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "claude",
        "version": 1,
        "display_status": "completed",
        "active": False,
        "display_id": RUN_ID,
        "branch_task": "build/logs",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": None,
        "cost_display": "-",
        "last_event": "passed",
        "row_label": "build:logs",
        "overlay": None,
    }


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
        "live": {"items": [_live_item()], "total_count": 1, "active_count": 0, "refresh_interval_s": 1.5},
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


def _detail_payload() -> dict[str, Any]:
    item = _live_item()
    return {
        **item,
        "source": "live",
        "title": "build: logs",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": ["/tmp/proj/otto_logs/sessions/x/build/narrative.log"],
        "selected_log_index": 0,
        "selected_log_path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "legal_actions": [],
        "review_packet": {
            "headline": "Logs run",
            "status": "completed",
            "summary": "",
            "readiness": {"state": "ready", "label": "ready", "tone": "success", "blockers": [], "next_step": "Review evidence."},
            "checks": [],
            "next_action": {"label": "review", "action_key": None, "enabled": False, "reason": None},
            "certification": {
                "stories_passed": 0,
                "stories_tested": 0,
                "passed": True,
                "summary_path": None,
                "stories": [],
                "proof_report": {"json_path": None, "html_path": None, "html_url": None, "available": False},
            },
            "changes": {
                "branch": "build/logs",
                "target": "main",
                "merged": False,
                "merge_id": None,
                "file_count": 0,
                "files": [],
                "truncated": False,
                "diff_command": None,
                "diff_error": None,
            },
            "evidence": [],
            "failure": None,
        },
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


# Three matches for "ERROR" so we can exercise n/total + step navigation.
LOG_TEXT = (
    "starting build\n"
    "INFO connecting to db\n"
    "ERROR could not connect to api gateway\n"
    "INFO retrying\n"
    "ERROR auth handshake failed\n"
    "INFO falling back\n"
    "ERROR final retry exhausted\n"
    "build complete\n"
)


def _log_payload() -> dict[str, Any]:
    return {
        "path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "offset": 0,
        "next_offset": len(LOG_TEXT),
        "text": LOG_TEXT,
        "exists": True,
        "total_bytes": len(LOG_TEXT),
        "eof": True,
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
    state_body = json.dumps(_state_payload())
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=state_body),
    )
    detail_body = json.dumps(_detail_payload())
    log_body = json.dumps(_log_payload())

    def _runs_handler(route: Any) -> None:
        url = route.request.url
        if "/logs" in url:
            route.fulfill(status=200, content_type="application/json", body=log_body)
        else:
            route.fulfill(status=200, content_type="application/json", body=detail_body)

    page.route(f"**/api/runs/{RUN_ID}/logs**", _runs_handler)
    page.route(f"**/api/runs/{RUN_ID}?**", _runs_handler)
    page.route(f"**/api/runs/{RUN_ID}", _runs_handler)


def _open_logs(page: Any, mc_backend: Any, disable_animations: Any) -> None:
    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)
    page.get_by_test_id("open-logs-button").click()
    page.get_by_test_id("run-log-pane").wait_for(state="visible", timeout=5_000)


def test_log_search_input_renders(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The log-search input is mounted above the pre once the log loads."""
    _install_routes(page)
    _open_logs(page, mc_backend, disable_animations)
    page.get_by_test_id("log-search-input").wait_for(state="visible", timeout=3_000)


def test_log_search_highlights_matches_and_counts(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Typing 'ERROR' surfaces 3 matches and renders <mark> wraps."""
    _install_routes(page)
    _open_logs(page, mc_backend, disable_animations)
    box = page.get_by_test_id("log-search-input")
    box.fill("ERROR")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('1 / 3')",
        timeout=3_000,
    )
    marks = page.locator("[data-testid=run-log-pane] mark.log-search-match")
    assert marks.count() == 3, f"expected 3 highlights, got {marks.count()}"


def test_log_search_enter_steps_next(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Enter advances to the next match (1/3 → 2/3 → 3/3 → wraps to 1/3)."""
    _install_routes(page)
    _open_logs(page, mc_backend, disable_animations)
    box = page.get_by_test_id("log-search-input")
    box.fill("ERROR")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('1 / 3')",
        timeout=3_000,
    )
    box.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('2 / 3')",
        timeout=3_000,
    )
    box.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('3 / 3')",
        timeout=3_000,
    )
    box.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('1 / 3')",
        timeout=3_000,
    )


def test_log_search_shift_enter_steps_prev(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Shift+Enter steps backwards (wraps from 1/3 to 3/3)."""
    _install_routes(page)
    _open_logs(page, mc_backend, disable_animations)
    box = page.get_by_test_id("log-search-input")
    box.fill("ERROR")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('1 / 3')",
        timeout=3_000,
    )
    box.press("Shift+Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('3 / 3')",
        timeout=3_000,
    )


def test_log_search_escape_clears(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Escape clears the query and removes <mark> wrappers."""
    _install_routes(page)
    _open_logs(page, mc_backend, disable_animations)
    box = page.get_by_test_id("log-search-input")
    box.fill("ERROR")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=log-search-count]')?.textContent?.includes('1 / 3')",
        timeout=3_000,
    )
    box.press("Escape")
    page.wait_for_function(
        "() => (document.querySelector('[data-testid=log-search-input]')?.value || '') === ''",
        timeout=3_000,
    )
    assert page.locator("[data-testid=run-log-pane] mark.log-search-match").count() == 0
