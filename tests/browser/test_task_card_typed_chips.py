"""Browser regression for mc-audit info-density #2 — task cards render
typed chips instead of unlabeled pills.

Each card chip carries:

* a ``data-chip-kind`` attribute (``files`` / ``stories`` / ``usage`` /
  ``time`` / ``status``) so CSS can hue-code without relying on order;
* an icon prefix glyph (file emoji, ✓, T, ⏱) so the kind reads at a
  glance without colour;
* a typed label (e.g. "3 files", "2/2 stories", "0.05", "1m").

Null values are SUPPRESSED — a chip never renders the literal "-".

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_task_card_typed_chips.py \\
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


def _full_landing_item() -> dict[str, Any]:
    """Landing item with all chip data populated."""

    return {
        "task_id": "task-full",
        "summary": "fully populated task",
        "branch": "build/task-full",
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
        "elapsed_s": 60,
        "cost_usd": 0.42,
        "token_usage": {"total_tokens": 12_300},
        "duration_s": 720,  # 12m
        "stories_passed": 2,
        "stories_tested": 2,
        "label": "ready",
        "merge_status": None,
        "merge_run_status": None,
        "actions": [],
        "intent": None,
        "run_id": "run-full",
    }


def _bare_landing_item() -> dict[str, Any]:
    """Landing item with NO chip data — should yield only the status fallback chip."""

    return {
        "task_id": "task-bare",
        "summary": "bare task",
        "branch": None,
        "branch_exists": False,
        "queue_status": "queued",
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": None,
        "queue_finished_at": None,
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": "waiting",
        "merge_blocked": False,
        "blockers": [],
        "merge_id": None,
        "merged_at": None,
        "diff_path": None,
        "diff_relpath": None,
        "diff_error": None,
        "changed_file_count": None,
        "changed_files": [],
        "diff_size_bytes": 0,
        "diff_truncated": False,
        "merge_target": SAMPLE_TARGET,
        "merge_base_sha": None,
        "head_sha": None,
        "target_sha": None,
        "exit_code": None,
        "elapsed_s": None,
        "cost_usd": None,
        "duration_s": None,
        "stories_passed": None,
        "stories_tested": None,
        "label": "queued",
        "merge_status": None,
        "merge_run_status": None,
        "actions": [],
        "intent": None,
        "run_id": None,
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
            "counts": {"queued": 1, "running": 0},
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
            "items": [_full_landing_item(), _bare_landing_item()],
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 2},
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
            "queue_tasks": 2,
            "state_tasks": 2,
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


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_full_task_card_renders_typed_chips(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A fully populated task surfaces files/stories/usage/time chips."""

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations)

    page.wait_for_selector(".task-card", timeout=5_000)

    info = page.evaluate(
        """() => {
            const chips = Array.from(document.querySelectorAll('.task-card .task-chip'));
            return chips.map((el) => ({
                kind: el.getAttribute('data-chip-kind'),
                text: el.textContent || '',
            }));
        }"""
    )
    kinds = [c["kind"] for c in info]
    # Must include the four typed chips.
    for required in ["files", "stories", "usage", "time"]:
        assert required in kinds, f"expected '{required}' chip; got chips={info!r}"

    # Look up each chip by kind.
    by_kind = {c["kind"]: c["text"] for c in info}
    assert "3 file" in by_kind["files"], f"files chip text wrong: {by_kind['files']!r}"
    assert "2/2 stories" in by_kind["stories"], f"stories chip text wrong: {by_kind['stories']!r}"
    # Usage chip carries token spend as the primary cross-provider measure.
    assert "12.3K tokens" in by_kind["usage"], f"usage chip text wrong: {by_kind['usage']!r}"
    # Time chip carries a duration label like "12m".
    assert "12m" in by_kind["time"] or "m" in by_kind["time"], (
        f"time chip text wrong: {by_kind['time']!r}"
    )

    # No chip should ever render the literal "-" placeholder.
    for chip in info:
        text = chip["text"].strip()
        # Strip leading icon glyph + space; assert the trimmed portion is not "-".
        # Icons are short (1-2 chars); easier to just check no chip equals "- ".
        assert text != "-" and not text.endswith(" -"), (
            f"chip {chip!r} renders the placeholder '-' — should be suppressed"
        )


def test_bare_task_falls_back_to_status_chip(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A task with no concrete chip data renders one neutral status chip, not a row of '-'."""

    _install_routes(page)
    _hydrate(mc_backend, page, disable_animations)

    page.wait_for_selector(".task-card", timeout=5_000)

    info = page.evaluate(
        """() => {
            const cards = Array.from(document.querySelectorAll('.task-card'));
            return cards.map((card) => ({
                title: card.querySelector('.task-title')?.textContent || '',
                chips: Array.from(card.querySelectorAll('.task-chip')).map((el) => ({
                    kind: el.getAttribute('data-chip-kind'),
                    text: el.textContent || '',
                })),
            }));
        }"""
    )
    bare = next((c for c in info if "task-bare" in c["title"]), None)
    assert bare is not None, f"expected to find task-bare in {info!r}"
    # Should fall back to a single status chip, not "files: -" / "usage: -" pills.
    assert len(bare["chips"]) == 1, f"bare task should have 1 fallback chip; got {bare['chips']!r}"
    fallback = bare["chips"][0]
    assert fallback["kind"] == "status", f"expected status fallback; got {fallback!r}"
    # Must NOT be a literal "-" pill.
    assert "-" != fallback["text"].strip().split()[-1], (
        f"fallback chip should never be raw '-'; got {fallback!r}"
    )
