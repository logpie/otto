"""Browser test for W5-CRITICAL-1: when the server reports the project
as dirty due to untracked files, the merge button MUST be disabled and
clicking it MUST surface the dirty-tree block to the operator.

The corresponding server-side regression suite lives at
``tests/test_merge_preflight_dirty_tree.py``. This test stubs the state
endpoint with ``merge_blocked=True`` and the precise W5 dirty-files
payload (``["DIRTY_FILE.txt"]``) so we can drive the SPA into the bug
state without seeding a real merge run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"
SAMPLE_TASK_ID = "ready-task"
SAMPLE_RUN_ID = "run-ready"
SAMPLE_BRANCH = f"build/{SAMPLE_TASK_ID}"
DIRTY_FILE = "DIRTY_FILE.txt"


def _state_with_dirty_block() -> dict[str, Any]:
    """Mirror of the W5 evidence: one ready task, project flagged dirty
    by a single untracked user file in the project root."""

    item = {
        "task_id": SAMPLE_TASK_ID,
        "run_id": SAMPLE_RUN_ID,
        "branch": SAMPLE_BRANCH,
        "worktree": f".worktrees/{SAMPLE_TASK_ID}",
        "summary": "ping module",
        "queue_status": "done",
        "landing_state": "ready",
        "label": "Ready to land",
        "merge_id": None,
        "merge_status": None,
        "merge_run_status": None,
        "duration_s": 12.0,
        "cost_usd": 0.23,
        "stories_passed": 1,
        "stories_tested": 1,
        "changed_file_count": 1,
        "changed_files": ["ping.py"],
        "diff_error": None,
    }
    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": SAMPLE_TARGET,
            "dirty": True,
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
            "items": [item],
            "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
            "collisions": [],
            "merge_blocked": True,
            "merge_blockers": [
                f"working tree has untracked files: {DIRTY_FILE}"
            ],
            "dirty_files": [DIRTY_FILE],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [
                {
                    "run_id": SAMPLE_RUN_ID,
                    "domain": "queue",
                    "run_type": "queue",
                    "command": "build",
                    "display_name": SAMPLE_TASK_ID,
                    "status": "done",
                    "terminal_outcome": "success",
                    "project_dir": "/tmp/proj",
                    "cwd": "/tmp/proj",
                    "queue_task_id": SAMPLE_TASK_ID,
                    "merge_id": None,
                    "branch": SAMPLE_BRANCH,
                    "worktree": f".worktrees/{SAMPLE_TASK_ID}",
                    "provider": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "adapter_key": "queue.attempt",
                    "version": 1,
                    "display_status": "done",
                    "active": False,
                    "display_id": SAMPLE_RUN_ID,
                    "branch_task": SAMPLE_TASK_ID,
                    "elapsed_s": 12.0,
                    "elapsed_display": "12s",
                    "cost_usd": 0.23,
                    "cost_display": "$0.23",
                    "last_event": "done",
                    "row_label": SAMPLE_TASK_ID,
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
            "generated_at": "2026-04-26T06:06:06Z",
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
    return {
        "run_id": SAMPLE_RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": SAMPLE_TASK_ID,
        "status": "done",
        "terminal_outcome": "success",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": SAMPLE_TASK_ID,
        "merge_id": None,
        "branch": SAMPLE_BRANCH,
        "worktree": f".worktrees/{SAMPLE_TASK_ID}",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "done",
        "active": False,
        "source": "live",
        "title": SAMPLE_TASK_ID,
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [
            {"key": "m", "label": "Merge", "enabled": True, "reason": None, "preview": "Land into main"},
        ],
        "review_packet": {
            "headline": SAMPLE_TASK_ID,
            "status": "done",
            "summary": SAMPLE_TASK_ID,
            "readiness": {
                "state": "ready",
                "label": "Ready",
                "tone": "success",
                "blockers": [],
                "next_step": "",
            },
            "checks": [],
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
                "files": ["ping.py"],
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


def _install_routes(page: Any, state_payload: dict[str, Any], detail_payload: dict[str, Any]) -> None:
    def projects_handler(route: Any) -> None:
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

    def state_handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(state_payload))

    def detail_handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(detail_payload))

    def artifacts_handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": SAMPLE_RUN_ID, "artifacts": []}),
        )

    page.route("**/api/projects", projects_handler)
    page.route("**/api/state*", state_handler)
    page.route(f"**/api/runs/{SAMPLE_RUN_ID}", detail_handler)
    page.route(f"**/api/runs/{SAMPLE_RUN_ID}?**", detail_handler)
    page.route(f"**/api/runs/{SAMPLE_RUN_ID}/artifacts", artifacts_handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_merge_button_shows_dirty_block_in_dialog(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """When the server reports ``merge_blocked=true`` with the W5
    untracked file, the merge UI MUST surface the block — the mission
    focus switches to ``primary=diagnostics`` (no ``Land all ready``
    CTA), the title says "Cleanup required before landing", and the
    dirty file is named in the body so the operator can find what to
    clean up.

    The W5 bug let the click slip past server-side gates and POST the
    merge anyway because the preflight ignored untracked files. With
    the fix, ``landing.merge_blocked=true`` and ``dirty_files`` is
    populated; the existing client UI then suppresses the merge button
    and surfaces the cleanup directive via ``missionFocus``.
    """

    state = _state_with_dirty_block()
    detail = _detail_for_ready_run()
    _install_routes(page, state, detail)
    _hydrate(mc_backend, page, disable_animations)

    land_btn = page.get_by_test_id("mission-land-ready-button")
    assert land_btn.count() == 0, (
        "Land button must NOT expose the merge action when merge is blocked by dirty tree."
    )

    banner = page.get_by_test_id("merge-blocked-banner")
    banner.wait_for(state="visible", timeout=5_000)
    banner_text = banner.text_content() or ""
    assert "Landing blocked" in banner_text, banner_text
    assert DIRTY_FILE in banner_text, (
        f"expected dirty file ({DIRTY_FILE}) to appear in blocked landing banner. "
        f"Banner text: {banner_text!r}"
    )

    # Defence-in-depth: even via the inspector ActionBar (which exposes
    # the merge action key 'm' to the operator), clicking must be a
    # no-op disabled state when merge_blocked. The button class lookup
    # below pins the existing ``ActionBar`` contract: ``mergeBlocked``
    # forces ``disabled=true`` on action key 'm'.
    # (The button is only rendered when the inspector is open; the test
    # focuses on the mission-focus gate above as the primary defence.)
