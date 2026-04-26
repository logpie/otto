"""Browser regression for W3-CRITICAL-1 — JobDialog improve mode must
surface a prior-run selector and submit it as ``prior_run_id``.

Source: live W3 dogfood run uncovered W3-CRITICAL-1 — the web JobDialog's
"improve" submission silently forked from ``main`` instead of iterating on
the prior build's branch. The fix introduces a prior-run dropdown
populated from history+landing items; selecting one and submitting POSTs
``prior_run_id`` to ``/api/queue/improve``. The server uses that to set
``base_ref`` on the queue task so the new improve worktree contains the
prior build's files. See ``docs/mc-audit/live-findings.md`` (search
"W3-CRITICAL-1").

Invariants pinned by this file:

1. With command=improve and at least one prior run in history, the
   ``job-prior-run-select`` dropdown renders with one option per run.

2. Selecting a prior run and clicking Submit POSTs ``prior_run_id`` in
   the JSON body.

3. With command=improve and zero prior runs, the dropdown is replaced
   by a ``job-prior-run-empty`` message and Submit is disabled.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 uv run pytest \\
        tests/browser/test_improve_dialog_prior_run_selector.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


# ---------------------------------------------------------------------------
# Synthetic state
# ---------------------------------------------------------------------------


def _project_block() -> dict[str, Any]:
    return {
        "path": "/tmp/proj",
        "name": "proj",
        "branch": SAMPLE_TARGET,
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
    }


def _watcher_block() -> dict[str, Any]:
    return {
        "alive": False,
        "watcher": None,
        "counts": {"queued": 0, "running": 0, "done": 0},
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
    }


def _runtime_block() -> dict[str, Any]:
    return {
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
            "mode": "stopped",
            "path": "",
            "metadata": None,
            "metadata_error": None,
            "supervised_pid": None,
            "matches_blocking_pid": False,
            "can_start": False,
            "can_stop": False,
            "start_blocked_reason": None,
            "stop_target_pid": None,
            "watcher_log_path": "",
            "web_log_exists": False,
            "queue_lock_holder_pid": None,
        },
        "issues": [],
    }


def _history_item(
    *,
    run_id: str,
    branch: str,
    summary: str,
    command: str = "build",
    terminal_outcome: str = "success",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "domain": "queue",
        "run_type": "queue",
        "command": command,
        "status": "done",
        "terminal_outcome": terminal_outcome,
        "queue_task_id": run_id,
        "merge_id": None,
        "branch": branch,
        "worktree": f".worktrees/{run_id}",
        "summary": summary,
        "intent": summary,
        "completed_at_display": "12:00",
        "outcome_display": "success",
        "duration_s": 30.0,
        "duration_display": "30s",
        "cost_usd": 0.05,
        "cost_display": "$0.05",
        "resumable": True,
        "adapter_key": "queue.attempt",
    }


def _state(history_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "project": _project_block(),
        "watcher": _watcher_block(),
        "landing": {
            "items": [],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [],
            "total_count": 0,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {
            "items": history_items,
            "page": 0,
            "page_size": 25,
            "total_rows": len(history_items),
            "total_pages": 1,
        },
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "runtime": _runtime_block(),
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


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/state*", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)


def _open_dialog(page: Any) -> None:
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector(".job-dialog", timeout=5_000)


def _switch_to_improve(page: Any) -> None:
    page.get_by_test_id("job-command-select").select_option("improve")
    # The improve mode select is the marker that improve is fully rendered.
    page.wait_for_selector("[data-testid=job-improve-mode-select]", timeout=5_000)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_improve_dialog_renders_prior_run_dropdown_from_history(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """With history populated, the dropdown lists each successful build/improve."""

    history = [
        _history_item(
            run_id="2026-04-25-120000-aaa",
            branch="build/greet-2026-04-25",
            summary="add greet module",
        ),
        _history_item(
            run_id="2026-04-24-110000-bbb",
            branch="build/calculator-2026-04-24",
            summary="add calculator",
        ),
    ]

    _install_projects_route(page)
    _install_state_route(page, _state(history))

    _hydrate(mc_backend, page, disable_animations)
    _open_dialog(page)
    _switch_to_improve(page)

    # Dropdown rendered with one option per history item.
    select = page.locator("[data-testid=job-prior-run-select]")
    select.wait_for(state="visible", timeout=5_000)
    options = page.locator("[data-testid=job-prior-run-select] option").all_text_contents()
    assert len(options) == 2, f"expected 2 prior-run options, got {options!r}"
    # Labels include the recognisable summary so operators can pick.
    assert any("add greet module" in label for label in options), options
    assert any("add calculator" in label for label in options), options


def test_improve_dialog_post_includes_prior_run_id(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Selecting a prior run and submitting POSTs ``prior_run_id`` in the body."""

    history = [
        _history_item(
            run_id="2026-04-25-120000-aaa",
            branch="build/greet-2026-04-25",
            summary="add greet module",
        ),
    ]

    _install_projects_route(page)
    _install_state_route(page, _state(history))

    captured: dict[str, Any] = {}

    def queue_handler(route: Any) -> None:
        try:
            request = route.request
            body_raw = request.post_data or ""
            captured["body"] = json.loads(body_raw) if body_raw else {}
        finally:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "message": "queued",
                    "task": {},
                    "warnings": [],
                    "refresh": True,
                }),
            )

    page.route("**/api/queue/improve", queue_handler)

    _hydrate(mc_backend, page, disable_animations)
    _open_dialog(page)
    _switch_to_improve(page)

    page.get_by_test_id("job-dialog-intent").fill("handle empty name correctly")

    # Auto-selected to options[0]. Click submit.
    page.get_by_test_id("job-dialog-submit-button").click()

    # Wait for the grace window to elapse and the POST to fire.
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=job-grace-countdown]')",
        timeout=10_000,
    )
    # Backstop: wait until captured body actually arrives.
    page.wait_for_function(
        "() => window.__lastImproveBody !== undefined || true",
        timeout=2_000,
    )
    # Spin until the request handler observed the body.
    deadline_attempts = 30
    for _ in range(deadline_attempts):
        if "body" in captured:
            break
        page.wait_for_timeout(100)

    assert "body" in captured, "queue/improve POST never observed"
    body = captured["body"]
    assert body.get("subcommand") == "bugs", body
    assert body.get("focus") == "handle empty name correctly", body
    assert body.get("prior_run_id") == "2026-04-25-120000-aaa", body


def test_improve_dialog_disables_submit_when_no_prior_runs(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Empty history → empty-state message + disabled Submit."""

    _install_projects_route(page)
    _install_state_route(page, _state([]))

    _hydrate(mc_backend, page, disable_animations)
    _open_dialog(page)
    _switch_to_improve(page)

    page.get_by_test_id("job-dialog-intent").fill("anything")

    # Empty-state message visible; no select.
    page.wait_for_selector("[data-testid=job-prior-run-empty]", timeout=5_000)
    assert page.locator("[data-testid=job-prior-run-select]").count() == 0

    # Submit must be disabled — server would otherwise silently fork from main.
    submit = page.get_by_test_id("job-dialog-submit-button")
    assert submit.get_attribute("disabled") is not None, (
        "Submit must be disabled when there is no prior run for an improve job"
    )

    # Validation hint surfaces the empty-state copy.
    hint = page.locator("[data-testid=job-dialog-validation-hint]")
    hint.wait_for(state="visible", timeout=5_000)
    text = hint.text_content() or ""
    assert "No prior runs" in text or "Run a build first" in text, text
