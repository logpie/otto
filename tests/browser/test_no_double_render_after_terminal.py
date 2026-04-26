"""W9-IMPORTANT-2: a run must not appear twice when it has a terminal
``terminal_outcome`` in ``history[]`` AND is still echoed in ``live[]``.

Source: live W9 dogfood run (mc-audit/live-findings.md, search
"W9-IMPORTANT-2").

Reproduction summary
--------------------

When the watcher transitioned a run from "running" to "success", the
backend briefly carried the same ``run_id`` in BOTH ``live.items`` and
``history.items`` for one poll cycle. The UI showed the run twice — once
labeled "running" in the live pane, once labeled "success" in the
history pane — confusing the operator.

Fix verified
------------

Client-side dedupe lives in ``dedupeLiveAgainstHistory`` (App.tsx).
When a ``run_id`` appears in ``history.items`` with a non-null
``terminal_outcome``, the matching ``live.items`` entry is dropped
before the data flows into any consumer (TaskBoard, LiveRuns, etc.).

Test asserts: a synthetic ``/api/state`` payload that violates the
invariant — same ``run_id`` in live + history — renders ONLY one row
in the diagnostics view (history table), not two.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_no_double_render_after_terminal.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


RUN_ID = "2026-04-26-065128-c86cde"
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


def _live_item() -> dict[str, Any]:
    """The stale live entry — backend hasn't yet pruned it."""

    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "display_name": "build:hello-world",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": "task-hello",
        "merge_id": None,
        "branch": "build/hello",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "claude",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": RUN_ID,
        "branch_task": "task-hello",
        "elapsed_s": 130.0,
        "elapsed_display": "2m 10s",
        "cost_usd": 0.12,
        "cost_display": "$0.12",
        "last_event": "running",
        "row_label": "build:hello-world",
        "overlay": None,
    }


def _history_item() -> dict[str, Any]:
    """Same ``run_id`` but already terminal in history."""

    return {
        "run_id": RUN_ID,
        "domain": "build",
        "run_type": "build",
        "command": "otto build",
        "status": "passed",
        "terminal_outcome": "success",
        "queue_task_id": "task-hello",
        "merge_id": None,
        "branch": "build/hello",
        "worktree": None,
        "summary": "build hello-world",
        "intent": "Add hello-world endpoint",
        "completed_at_display": "12:00",
        "outcome_display": "passed",
        "duration_s": 159.0,
        "duration_display": "2m 39s",
        "cost_usd": 0.12,
        "cost_display": "$0.12",
        "resumable": False,
        "adapter_key": "claude",
    }


def _state_payload() -> dict[str, Any]:
    return {
        "project": {
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
            "target": SAMPLE_TARGET,
        },
        # The W9 violation: live AND history both carry the same run_id.
        "live": {
            "items": [_live_item()],
            "total_count": 1,
            "active_count": 1,
            "refresh_interval_s": 1.5,
        },
        "history": {
            "items": [_history_item()],
            "page": 0,
            "page_size": 25,
            "total_rows": 1,
            "total_pages": 1,
        },
        "events": {"items": [], "total_count": 0, "malformed_count": 0},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": 1,
            "command_backlog": {"pending": 0, "processing": 0, "malformed": 0, "items": []},
            "files": {
                "queue": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "state": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "commands": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
                "processing": {"path": "", "exists": True, "size_bytes": 0, "mtime": None, "error": None},
            },
            "supervisor": {
                "can_start": True,
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


def _hydrate(page: Any, mc_backend: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#root')?.children.length > 0", timeout=10_000
    )
    page.wait_for_selector("[data-mc-shell=ready]", timeout=10_000)


def test_run_id_appears_only_once_when_terminal_in_history(
    mc_backend: Any, page: Any
) -> None:
    """The W9 violation must not surface two visible rows for the same
    ``run_id``. The dedupe drops the stale live entry so only the
    history row renders.
    """

    _install_routes(page)
    _hydrate(page, mc_backend)

    # Switch to diagnostics view where both LiveRuns + History tables
    # render — the original W9 reproduction was visible in both panes.
    diagnostics_tab = page.locator("[data-testid=diagnostics-tab]")
    if diagnostics_tab.count() > 0:
        diagnostics_tab.first.click()
        # Wait for the diagnostics layout to mount.
        page.wait_for_selector(".diagnostics-layout", timeout=5_000)

    # Count rendered rows that reference the run id across every
    # surface that builds from `data.live.items`:
    #   * LiveRuns table — rows expose `live-row-activator-<run_id>`
    #   * Task board cards — `<article data-run-id="…">`
    # The dedupe must drop the stale live entry entirely, so neither
    # surface shows the run as "live".
    matches = page.evaluate(
        """(rid) => {
            const out = {
                liveRowActivators: 0,
                taskBoardLiveCards: 0,
            };
            const liveBtn = document.querySelector(
                '[data-testid=live-row-activator-' + rid + ']'
            );
            if (liveBtn) out.liveRowActivators = 1;
            const cards = document.querySelectorAll(
                '[data-testid=task-board] [data-run-id]'
            );
            for (const el of cards) {
                if (el.getAttribute('data-run-id') === rid) out.taskBoardLiveCards++;
            }
            return out;
        }""",
        RUN_ID,
    )

    assert matches["liveRowActivators"] == 0, (
        f"W9-IMPORTANT-2 violation: live runs pane still shows the row "
        f"for run_id {RUN_ID!r} after history flagged it terminal — "
        f"matches={matches!r}"
    )
    # The board builds from data.live.items via boardTaskFromLive.
    # Landing/queue items are empty in this fixture, so any rendered
    # card must have come from the (now-deduped) live array.
    assert matches["taskBoardLiveCards"] == 0, (
        f"W9-IMPORTANT-2 violation: task board surfaced "
        f"{matches['taskBoardLiveCards']} card(s) for run_id {RUN_ID!r} "
        f"after history flagged it terminal — matches={matches!r}"
    )


def test_history_row_still_renders_after_dedupe(
    mc_backend: Any, page: Any
) -> None:
    """The history row for the deduped run must remain visible. Dedupe
    drops the stale LIVE entry only; history is the source of truth for
    terminal state and must not be hidden as a side effect.
    """

    _install_routes(page)
    _hydrate(page, mc_backend)

    diagnostics_tab = page.locator("[data-testid=diagnostics-tab]")
    if diagnostics_tab.count() > 0:
        diagnostics_tab.first.click()
        page.wait_for_selector(".diagnostics-layout", timeout=5_000)

    history_btn = page.locator(
        f"[data-testid=history-row-activator-{RUN_ID}]"
    )
    assert history_btn.count() >= 1, (
        f"history row for run_id {RUN_ID!r} disappeared after dedupe — "
        f"the helper must drop only the live duplicate, not the history row"
    )
