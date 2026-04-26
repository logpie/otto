"""Browser tests for the diff-freshness UI cluster (mc-audit Phase 4 cluster E).

Covers the CRITICAL diff-freshness contract finding plus the IMPORTANT
truncation-clarity finding from
``docs/mc-audit/_hunter-findings/codex-evidence-trustworthiness.md``.

Each test stubs ``/api/runs/{id}/diff`` (and the surrounding state /
detail / projects endpoints) so we can drive the SPA into the exact
freshness + truncation states without seeding a real merge run. Stubs
are the right tool here: we are testing the *UI* contract — does the
operator see the SHAs, the truncation footprint, the warning when a
SHA is null — independently of the live git plumbing (which is covered
by ``tests/test_diff_freshness.py``).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest tests/browser/test_diff_freshness.py \
        -m browser -p playwright -v

The bundle build is required for the new DiffPane markup to be visible
in the served SPA. After implementation, run once with the full bundle
build (omit the env var) to confirm the live bundle has them.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Synthetic state + detail + diff payloads
# --------------------------------------------------------------------------- #


SAMPLE_RUN_ID = "run-ready"
SAMPLE_BRANCH = "audit/2026-04-25"
SAMPLE_TARGET = "main"
SAMPLE_TARGET_SHA = "abc1234abc1234abc1234abc1234abc1234abc12"
SAMPLE_BRANCH_SHA = "def5678def5678def5678def5678def5678def56"
SAMPLE_MERGE_BASE = "9876543987654398765439876543987654398765"


def _state_with_one_ready_run() -> dict[str, Any]:
    """Minimal StateResponse with one ready run so the inspector can open."""

    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": False,
            "head_sha": SAMPLE_TARGET_SHA,
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
            "counts": {"queued": 0, "running": 0, "done": 1},
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
            "items": [
                {
                    "task_id": "ready-task",
                    "run_id": SAMPLE_RUN_ID,
                    "branch": SAMPLE_BRANCH,
                    "worktree": ".worktrees/ready-task",
                    "summary": "ready task",
                    "queue_status": "done",
                    "landing_state": "ready",
                    "label": "Ready to land",
                    "merge_id": None,
                    "merge_status": None,
                    "merge_run_status": None,
                    "duration_s": 12.0,
                    "cost_usd": 0.0,
                    "stories_passed": 1,
                    "stories_tested": 1,
                    "changed_file_count": 1,
                    "changed_files": ["feature.txt"],
                    "diff_error": None,
                }
            ],
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [
                {
                    "run_id": SAMPLE_RUN_ID,
                    "domain": "queue",
                    "run_type": "queue",
                    "command": "build ready",
                    "display_name": "ready-task",
                    "status": "done",
                    "terminal_outcome": "success",
                    "project_dir": "/tmp/proj",
                    "cwd": "/tmp/proj",
                    "queue_task_id": "ready-task",
                    "merge_id": None,
                    "branch": SAMPLE_BRANCH,
                    "worktree": ".worktrees/ready-task",
                    "provider": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "adapter_key": "queue.attempt",
                    "version": 1,
                    "display_status": "done",
                    "active": False,
                    "display_id": SAMPLE_RUN_ID,
                    "branch_task": "ready-task",
                    "elapsed_s": 12.0,
                    "elapsed_display": "12s",
                    "cost_usd": 0.0,
                    "cost_display": "$0.00",
                    "last_event": "done",
                    "row_label": "ready-task",
                    "overlay": None,
                }
            ],
            "total_count": 1,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
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
        },
    }


def _detail_for_ready_run() -> dict[str, Any]:
    """Minimum RunDetail shape for SAMPLE_RUN_ID; covers the inspector deps."""

    return {
        "run_id": SAMPLE_RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build ready",
        "display_name": "ready-task",
        "status": "done",
        "terminal_outcome": "success",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": "ready-task",
        "merge_id": None,
        "branch": SAMPLE_BRANCH,
        "worktree": ".worktrees/ready-task",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "done",
        "active": False,
        "source": "live",
        "title": "ready task",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [
            {"key": "m", "label": "Merge", "enabled": True, "reason": None, "preview": "Land into main"}
        ],
        "review_packet": {
            "headline": "Ready for review",
            "status": "done",
            "summary": "ready task",
            "readiness": {
                "state": "ready",
                "label": "Ready to land in main",
                "tone": "success",
                "blockers": [],
                "next_step": "Review evidence and land the task.",
            },
            "checks": [
                {"key": "run", "label": "Run", "status": "pass", "detail": "ok"},
                {"key": "certification", "label": "Certification", "status": "pass", "detail": "1/1 stories passed."},
                {"key": "changes", "label": "Changes", "status": "pass", "detail": "1 file changed."},
                {"key": "landing", "label": "Landing", "status": "pass", "detail": "Safe to land into main."},
            ],
            "next_action": {"label": "Land selected", "action_key": "m", "enabled": True, "reason": None},
            "certification": {
                "stories_passed": 1,
                "stories_tested": 1,
                "passed": True,
                "summary_path": None,
                "stories": [],
                "proof_report": {"json_path": None, "html_path": None, "html_url": None, "available": False},
            },
            "changes": {
                "branch": SAMPLE_BRANCH,
                "target": SAMPLE_TARGET,
                "merged": False,
                "merge_id": None,
                "file_count": 1,
                "files": ["feature.txt"],
                "truncated": False,
                "diff_command": f"git diff {SAMPLE_TARGET}...{SAMPLE_BRANCH}",
                "diff_error": None,
            },
            "evidence": [],
            "failure": None,
        },
        "landing_state": "ready",
        "merge_info": None,
        "record": {},
    }


def _diff_payload(
    *,
    target_sha: str | None = SAMPLE_TARGET_SHA,
    branch_sha: str | None = SAMPLE_BRANCH_SHA,
    merge_base: str | None = SAMPLE_MERGE_BASE,
    truncated: bool = False,
    full_size_chars: int = 1200,
    text: str | None = None,
    fetched_at: str = "2026-04-25T12:00:00Z",
    errors: list[str] | None = None,
) -> dict[str, Any]:
    if text is None:
        text = (
            "diff --git a/feature.txt b/feature.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/feature.txt\n"
            "@@ -0,0 +1,1 @@\n"
            "+ready\n"
        )
    limit_chars = 240_000
    shown_text = text[:limit_chars] if truncated else text
    return {
        "run_id": SAMPLE_RUN_ID,
        "branch": SAMPLE_BRANCH,
        "target": SAMPLE_TARGET,
        "command": f"git diff {SAMPLE_TARGET}...{SAMPLE_BRANCH}",
        "files": ["feature.txt"],
        "file_count": 1,
        "text": shown_text,
        "error": None,
        "truncated": truncated,
        "fetched_at": fetched_at,
        "target_sha": target_sha,
        "branch_sha": branch_sha,
        "merge_base": merge_base,
        "limit_chars": limit_chars,
        "full_size_chars": full_size_chars,
        "shown_hunks": 1,
        "total_hunks": 5 if truncated else 1,
        "errors": errors or [],
    }


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/state*", handler)


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


def _install_detail_route(page: Any, detail: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))

    # Match /api/runs/<id> (and /api/runs/<id>?...) but not /api/runs/<id>/...
    page.route(f"**/api/runs/{SAMPLE_RUN_ID}", handler)
    page.route(f"**/api/runs/{SAMPLE_RUN_ID}?**", handler)


def _install_diff_route(page: Any, payload_factory: Any) -> dict[str, int]:
    """Stub the diff endpoint; track call count for refresh tests."""

    counters = {"calls": 0}
    lock = threading.Lock()

    def handler(route: Any) -> None:
        with lock:
            counters["calls"] += 1
        body = payload_factory(counters["calls"])
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(f"**/api/runs/{SAMPLE_RUN_ID}/diff", handler)
    return counters


def _install_artifacts_route(page: Any) -> None:
    """Stub artifact list (the inspector calls it on certain modes)."""

    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": SAMPLE_RUN_ID, "artifacts": []}),
        )

    page.route(f"**/api/runs/{SAMPLE_RUN_ID}/artifacts", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)


def _open_diff_inspector(page: Any) -> None:
    """Click into the run row on the task board, then click the Diff button."""
    # The TaskBoard renders a card per landing/queue task; the testid
    # matches `task-card-<task-id>` (see ``testIdForTask`` in App.tsx).
    card = page.get_by_test_id("task-card-ready-task")
    card.wait_for(state="visible", timeout=5_000)
    card.click()
    diff_btn = page.get_by_test_id("open-diff-button")
    diff_btn.wait_for(state="visible", timeout=5_000)
    # Force-click via JS in case actionability / disabled flicker bites.
    page.evaluate(
        """() => {
            const btn = document.querySelector('[data-testid=open-diff-button]');
            if (btn) btn.click();
        }"""
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_diff_pane_shows_metadata_header(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Diff pane renders SHAs, fetch time, and a refresh button."""

    _install_projects_route(page)
    _install_state_route(page, _state_with_one_ready_run())
    _install_detail_route(page, _detail_for_ready_run())
    _install_artifacts_route(page)
    _install_diff_route(page, lambda _n: _diff_payload())

    _hydrate(mc_backend, page, disable_animations)
    _open_diff_inspector(page)

    page.wait_for_selector('[data-testid="diff-freshness"]', timeout=5_000)

    target_sha_text = page.get_by_test_id("diff-target-sha").text_content()
    branch_sha_text = page.get_by_test_id("diff-branch-sha").text_content()
    fetched_at_text = page.get_by_test_id("diff-fetched-at").text_content()
    refresh_btn = page.get_by_test_id("diff-refresh-button")

    assert SAMPLE_TARGET in (target_sha_text or ""), target_sha_text
    assert SAMPLE_TARGET_SHA[:7] in (target_sha_text or ""), target_sha_text
    assert SAMPLE_BRANCH in (branch_sha_text or ""), branch_sha_text
    assert SAMPLE_BRANCH_SHA[:7] in (branch_sha_text or ""), branch_sha_text
    assert "Captured" in (fetched_at_text or ""), fetched_at_text
    refresh_btn.wait_for(state="visible")


def test_diff_pane_shows_truncation_clarity(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Truncated diffs render hunk + size footprint, not a bare ``truncated``."""

    big_text = "x" * 250_000  # over the 240k slice limit
    _install_projects_route(page)
    _install_state_route(page, _state_with_one_ready_run())
    _install_detail_route(page, _detail_for_ready_run())
    _install_artifacts_route(page)
    _install_diff_route(
        page,
        lambda _n: _diff_payload(truncated=True, text=big_text, full_size_chars=400_000),
    )

    _hydrate(mc_backend, page, disable_animations)
    _open_diff_inspector(page)

    page.wait_for_selector('[data-testid="diff-truncation"]', timeout=5_000)
    truncation_text = page.get_by_test_id("diff-truncation").text_content() or ""
    assert "Showing" in truncation_text or "hunk" in truncation_text.lower(), truncation_text
    assert "of" in truncation_text, truncation_text
    # The bare "truncated" string is replaced by a meaningful banner. The
    # toolbar should no longer carry a "· truncated" suffix.
    toolbar = page.locator(".diff-toolbar").first.text_content() or ""
    assert "truncated" not in toolbar.lower(), toolbar
    # Copy-command button is offered for the bigger diff.
    page.get_by_test_id("diff-copy-command-button").wait_for(state="visible")


def test_diff_pane_refresh_button_re_fetches(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Clicking refresh fires a second GET /api/runs/<id>/diff."""

    _install_projects_route(page)
    _install_state_route(page, _state_with_one_ready_run())
    _install_detail_route(page, _detail_for_ready_run())
    _install_artifacts_route(page)
    counters = _install_diff_route(
        page,
        lambda n: _diff_payload(fetched_at=f"2026-04-25T12:00:{n:02d}Z"),
    )

    _hydrate(mc_backend, page, disable_animations)
    _open_diff_inspector(page)

    page.wait_for_selector('[data-testid="diff-refresh-button"]', timeout=5_000)
    initial_calls = counters["calls"]
    assert initial_calls >= 1, counters

    page.get_by_test_id("diff-refresh-button").click()
    page.wait_for_function(
        "window.fetch && true && document.querySelector('[data-testid=diff-fetched-at]')?.textContent?.length > 0",
        timeout=5_000,
    )
    # Wait briefly for the refresh round trip + re-render.
    page.wait_for_timeout(500)
    assert counters["calls"] > initial_calls, counters


def test_merge_confirm_dialog_shows_branch_target_sha(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """After viewing the diff, clicking Merge surfaces the SHAs in the confirm copy."""

    _install_projects_route(page)
    _install_state_route(page, _state_with_one_ready_run())
    _install_detail_route(page, _detail_for_ready_run())
    _install_artifacts_route(page)
    _install_diff_route(page, lambda _n: _diff_payload())

    _hydrate(mc_backend, page, disable_animations)
    _open_diff_inspector(page)
    # Wait for the diff to load so the SPA has the SHAs cached. The
    # confirm copy is built from the cached diff.
    page.wait_for_selector('[data-testid="diff-target-sha"]', timeout=5_000)

    # Close the inspector so the review-packet button (which lives in
    # the run-detail panel underneath) is clickable.
    page.get_by_test_id("close-inspector-button").click()

    land_btn = page.get_by_test_id("review-next-action-button")
    land_btn.wait_for(state="visible", timeout=5_000)
    land_btn.click()

    dialog = page.locator('.confirm-dialog')
    dialog.wait_for(state="visible", timeout=5_000)
    body_text = dialog.text_content() or ""
    assert SAMPLE_TARGET in body_text, body_text
    assert SAMPLE_BRANCH in body_text, body_text
    assert SAMPLE_TARGET_SHA[:7] in body_text, body_text
    assert SAMPLE_BRANCH_SHA[:7] in body_text, body_text


def test_diff_pane_warns_when_sha_unknown(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Server returning ``branch_sha=null`` surfaces a warning, not a silent miss."""

    _install_projects_route(page)
    _install_state_route(page, _state_with_one_ready_run())
    _install_detail_route(page, _detail_for_ready_run())
    _install_artifacts_route(page)
    _install_diff_route(
        page,
        lambda _n: _diff_payload(
            branch_sha=None,
            merge_base=None,
            errors=["branch audit/2026-04-25: bad ref"],
        ),
    )

    _hydrate(mc_backend, page, disable_animations)
    _open_diff_inspector(page)
    warning = page.get_by_test_id("diff-branch-sha-missing")
    warning.wait_for(state="visible", timeout=5_000)
    text = warning.text_content() or ""
    assert "Could not resolve" in text, text
    assert "stale" in text.lower(), text
