"""Browser tests for the destructive-action confirm copy cluster
(mc-audit Phase 4 cluster H).

Covers all CRITICAL + IMPORTANT findings from
``docs/mc-audit/_hunter-findings/codex-destructive-action-safety.md``:

  #1 (CRITICAL) Bulk-merge enumeration + checkbox gate
  #2 Single merge confirm shows task id, branch->target, file count, files
  #3 Cleanup copy mentions worktree + irreversible (and uses "Remove
     queued task" wording for queued items)
  #4 Cancel copy includes task id + 30s SIGTERM + work-loss warning
  #5 Watcher stop confirm shows pid, running, queued, backlog counts
  #6 Action POST error keeps dialog OPEN with inline error + refresh
  #7 Queue job 3-second cancellable grace window before POST

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_destructive_actions.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Synthetic state + detail builders. Mirrors the patterns in
# test_diff_freshness.py and test_async_actions.py.
# ---------------------------------------------------------------------------


SAMPLE_TARGET = "main"


def _ready_landing_item(idx: int) -> dict[str, Any]:
    task_id = f"task-{idx:02d}"
    return {
        "task_id": task_id,
        "run_id": f"run-{idx:02d}",
        "branch": f"otto/{task_id}",
        "worktree": f".worktrees/{task_id}",
        "summary": f"task {idx}",
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
        "changed_file_count": 2 + idx,
        "changed_files": [f"src/{task_id}/a.txt", f"src/{task_id}/b.txt"],
        "diff_error": None,
    }


def _state_with_ready_tasks(count: int) -> dict[str, Any]:
    items = [_ready_landing_item(i) for i in range(1, count + 1)]
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
            "counts": {"queued": 0, "running": 0, "done": count},
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
            "items": items,
            "counts": {"ready": count, "merged": 0, "blocked": 0, "total": count},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": SAMPLE_TARGET,
        },
        "live": {
            "items": [
                {
                    "run_id": item["run_id"],
                    "domain": "queue",
                    "run_type": "queue",
                    "command": "build",
                    "display_name": item["task_id"],
                    "status": "done",
                    "terminal_outcome": "success",
                    "project_dir": "/tmp/proj",
                    "cwd": "/tmp/proj",
                    "queue_task_id": item["task_id"],
                    "merge_id": None,
                    "branch": item["branch"],
                    "worktree": item["worktree"],
                    "provider": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "adapter_key": "queue.attempt",
                    "version": 1,
                    "display_status": "done",
                    "active": False,
                    "display_id": item["run_id"],
                    "branch_task": item["task_id"],
                    "elapsed_s": 12.0,
                    "elapsed_display": "12s",
                    "cost_usd": 0.0,
                    "cost_display": "$0.00",
                    "last_event": "done",
                    "row_label": item["task_id"],
                    "overlay": None,
                }
                for item in items
            ],
            "total_count": count,
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 0,
            "state_tasks": count,
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


def _state_with_running_watcher(running: int, queued: int, backlog: int, pid: int = 4321) -> dict[str, Any]:
    """A state where the watcher is running with a known operational footprint."""
    payload = _state_with_ready_tasks(0)
    payload["watcher"] = {
        "alive": True,
        "watcher": {"pid": pid},
        "counts": {"queued": queued, "running": running, "done": 0},
        "health": {
            "state": "running",
            "blocking_pid": pid,
            "watcher_pid": pid,
            "watcher_process_alive": True,
            "lock_pid": pid,
            "lock_process_alive": True,
            "heartbeat": "2026-04-25T12:00:00Z",
            "heartbeat_age_s": 1.0,
            "started_at": "2026-04-25T11:55:00Z",
            "log_path": "/tmp/watcher.log",
            "next_action": "",
        },
    }
    payload["runtime"]["command_backlog"]["pending"] = backlog
    payload["runtime"]["supervisor"]["can_start"] = False
    payload["runtime"]["supervisor"]["can_stop"] = True
    payload["runtime"]["supervisor"]["supervised_pid"] = pid
    payload["runtime"]["supervisor"]["matches_blocking_pid"] = True
    payload["runtime"]["supervisor"]["stop_target_pid"] = pid
    return payload


def _detail_for_run(
    *,
    run_id: str,
    task_id: str,
    branch: str,
    target: str,
    status: str,
    file_count: int,
    files: list[str],
    actions: list[dict[str, Any]],
    worktree: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": task_id,
        "status": status,
        "terminal_outcome": status if status in {"success", "failed", "cancelled"} else None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": task_id,
        "merge_id": None,
        "branch": branch,
        "worktree": worktree if worktree is not None else f".worktrees/{task_id}",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": status,
        "active": status in {"running", "starting"},
        "source": "live",
        "title": task_id,
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": actions,
        "review_packet": {
            "headline": task_id,
            "status": status,
            "summary": task_id,
            "readiness": {
                "state": "ready" if status in {"done", "success"} else "needs_attention",
                "label": "Ready" if status in {"done", "success"} else "Needs attention",
                "tone": "success" if status in {"done", "success"} else "danger",
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
                "branch": branch,
                "target": target,
                "merged": False,
                "merge_id": None,
                "file_count": file_count,
                "files": files,
                "truncated": False,
                "diff_command": f"git diff {target}...{branch}",
                "diff_error": None,
            },
            "evidence": [],
            "failure": None,
        },
        "landing_state": "ready" if status in {"done", "success"} else None,
        "merge_info": None,
        "record": {},
    }


# ---------------------------------------------------------------------------
# Route stubs
# ---------------------------------------------------------------------------


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


def _install_detail_route(page: Any, run_id: str, detail: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))

    page.route(f"**/api/runs/{run_id}", handler)
    page.route(f"**/api/runs/{run_id}?**", handler)


def _install_artifacts_route(page: Any, run_id: str) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": run_id, "artifacts": []}),
        )

    page.route(f"**/api/runs/{run_id}/artifacts", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)


def _open_run(page: Any, task_id: str) -> None:
    card = page.get_by_test_id(f"task-card-{task_id}")
    card.wait_for(state="visible", timeout=5_000)
    card.click()


# ---------------------------------------------------------------------------
# Tests — Bulk merge enumeration + checkbox gate (#1 CRITICAL)
# ---------------------------------------------------------------------------


def test_bulk_merge_enumerates_every_task(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """7 ready tasks → bulk merge confirm shows all 7 task ids; checkbox required."""

    payload = _state_with_ready_tasks(7)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    land_btn = page.get_by_test_id("mission-land-ready-button")
    land_btn.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=mission-land-ready-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    land_btn.click()

    # Every task id must be present in the rendered list (scrollable region).
    list_region = page.locator('[data-testid=confirm-bulk-list]')
    list_region.wait_for(state="visible", timeout=5_000)
    for i in range(1, 8):
        row = page.locator(f'[data-testid=confirm-bulk-row-task-{i:02d}]')
        assert row.count() == 1, f"task-{i:02d} row missing from bulk-merge confirm"

    # Checkbox is required for N>1; submit must be disabled until ticked.
    submit_btn = page.get_by_test_id("confirm-dialog-confirm-button")
    assert submit_btn.is_disabled(), "submit must be disabled until ack checkbox is ticked"

    ack_label = "Yes, land all 7 tasks above into main."
    body_text = page.locator(".confirm-dialog").text_content() or ""
    assert ack_label in body_text, body_text


def test_bulk_merge_disabled_until_checkbox_checked(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Bulk merge submit button enables only after the ack checkbox is ticked."""

    payload = _state_with_ready_tasks(3)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)
    page.get_by_test_id("mission-land-ready-button").click()

    submit_btn = page.get_by_test_id("confirm-dialog-confirm-button")
    submit_btn.wait_for(state="visible", timeout=5_000)
    assert submit_btn.is_disabled(), "submit should start disabled"

    page.get_by_test_id("confirm-dialog-ack-checkbox").check()
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=confirm-dialog-confirm-button]'); return b && !b.disabled; }",
        timeout=2_000,
    )
    assert not submit_btn.is_disabled(), "submit should enable after checkbox tick"


def test_bulk_merge_single_ready_task_no_checkbox(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """N=1 bulk merge does NOT require checkbox — friction reserved for N>1."""

    payload = _state_with_ready_tasks(1)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)
    page.get_by_test_id("mission-land-ready-button").click()

    submit_btn = page.get_by_test_id("confirm-dialog-confirm-button")
    submit_btn.wait_for(state="visible", timeout=5_000)
    # No checkbox is rendered for N=1.
    assert page.locator('[data-testid=confirm-dialog-ack-checkbox]').count() == 0
    assert not submit_btn.is_disabled()


# ---------------------------------------------------------------------------
# Tests — Single merge confirm details (#2 IMPORTANT)
# ---------------------------------------------------------------------------


def test_single_merge_confirm_shows_files(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Single merge confirm renders task id, branch->target, file count, files."""

    state = _state_with_ready_tasks(1)
    item = state["landing"]["items"][0]
    detail = _detail_for_run(
        run_id=item["run_id"],
        task_id=item["task_id"],
        branch=item["branch"],
        target=SAMPLE_TARGET,
        status="done",
        file_count=4,
        files=["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"],
        actions=[
            {"key": "m", "label": "Merge", "enabled": True, "reason": None, "preview": "Land into main"}
        ],
    )

    _install_projects_route(page)
    _install_state_route(page, state)
    _install_detail_route(page, item["run_id"], detail)
    _install_artifacts_route(page, item["run_id"])

    _hydrate(mc_backend, page, disable_animations)
    _open_run(page, item["task_id"])

    # The "Land selected" recovery button isn't surfaced for done; instead
    # we trigger the merge via the review-next-action-button which exists
    # for ready runs.
    land_btn = page.get_by_test_id("review-next-action-button")
    land_btn.wait_for(state="visible", timeout=5_000)
    land_btn.click()

    # Wait for the confirm dialog with structured merge details.
    page.wait_for_selector('[data-testid=confirm-merge-details]', timeout=5_000)
    task_id_text = page.get_by_test_id("confirm-merge-task-id").text_content() or ""
    file_count_text = page.get_by_test_id("confirm-merge-file-count").text_content() or ""
    files_text = page.get_by_test_id("confirm-merge-files").text_content() or ""
    body_text = page.locator(".confirm-dialog").text_content() or ""

    assert item["task_id"] in task_id_text, task_id_text
    assert "4" in file_count_text, file_count_text
    for path in ["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"]:
        assert path in files_text, (path, files_text)
    assert item["branch"] in body_text, body_text
    assert SAMPLE_TARGET in body_text, body_text


# ---------------------------------------------------------------------------
# Tests — Cleanup copy (#3 IMPORTANT)
# ---------------------------------------------------------------------------


def test_cleanup_copy_mentions_worktree_irreversible(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Terminal-status cleanup confirm contains 'worktree' AND 'cannot be undone'."""

    state = _state_with_ready_tasks(1)
    item = state["landing"]["items"][0]
    # Make the run "failed" so cleanup is the recovery action.
    state["live"]["items"][0]["status"] = "failed"
    state["live"]["items"][0]["display_status"] = "failed"
    state["live"]["items"][0]["terminal_outcome"] = "failed"
    detail = _detail_for_run(
        run_id=item["run_id"],
        task_id=item["task_id"],
        branch=item["branch"],
        target=SAMPLE_TARGET,
        status="failed",
        file_count=2,
        files=["a.ts", "b.ts"],
        actions=[
            {"key": "x", "label": "Cleanup", "enabled": True, "reason": None, "preview": "Remove run"}
        ],
        worktree=f".worktrees/{item['task_id']}",
    )

    _install_projects_route(page)
    _install_state_route(page, state)
    _install_detail_route(page, item["run_id"], detail)
    _install_artifacts_route(page, item["run_id"])

    _hydrate(mc_backend, page, disable_animations)
    _open_run(page, item["task_id"])

    cleanup_btn = page.get_by_test_id("recovery-action-cleanup")
    cleanup_btn.wait_for(state="visible", timeout=5_000)
    cleanup_btn.click()

    page.wait_for_selector('.confirm-dialog', timeout=5_000)
    body_text = page.locator(".confirm-dialog").text_content() or ""
    assert "worktree" in body_text.lower(), body_text
    assert "cannot be undone" in body_text.lower(), body_text
    assert item["task_id"] in body_text, body_text


def test_cleanup_queued_task_uses_different_copy(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Queued-task cleanup confirm uses 'Remove queued task' wording."""

    state = _state_with_ready_tasks(1)
    item = state["landing"]["items"][0]
    state["live"]["items"][0]["status"] = "queued"
    state["live"]["items"][0]["display_status"] = "queued"
    state["live"]["items"][0]["terminal_outcome"] = None
    detail = _detail_for_run(
        run_id=item["run_id"],
        task_id=item["task_id"],
        branch=item["branch"],
        target=SAMPLE_TARGET,
        status="queued",
        file_count=0,
        files=[],
        actions=[
            {"key": "x", "label": "Cleanup", "enabled": True, "reason": None, "preview": "Remove from queue"}
        ],
    )
    # "queued" is not in RECOVERABLE_STATUSES; fall back to advanced-action button.
    _install_projects_route(page)
    _install_state_route(page, state)
    _install_detail_route(page, item["run_id"], detail)
    _install_artifacts_route(page, item["run_id"])

    _hydrate(mc_backend, page, disable_animations)
    _open_run(page, item["task_id"])

    # Open advanced actions disclosure and click cleanup. The button has
    # data-testid="advanced-action-cleanup".
    page.evaluate(
        """() => {
            const det = document.querySelector('details.advanced-actions');
            if (det) det.open = true;
        }"""
    )
    cleanup_btn = page.get_by_test_id("advanced-action-cleanup")
    cleanup_btn.wait_for(state="visible", timeout=5_000)
    cleanup_btn.click()

    page.wait_for_selector('.confirm-dialog', timeout=5_000)
    body_text = page.locator(".confirm-dialog").text_content() or ""
    assert "Remove queued task" in body_text, body_text
    assert item["task_id"] in body_text, body_text


# ---------------------------------------------------------------------------
# Tests — Cancel copy (#4 IMPORTANT)
# ---------------------------------------------------------------------------


def test_cancel_copy_mentions_sigterm_30s_loss(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Cancel confirm contains '30s' AND 'Work in progress may be lost'."""

    state = _state_with_ready_tasks(1)
    item = state["landing"]["items"][0]
    state["live"]["items"][0]["status"] = "running"
    state["live"]["items"][0]["display_status"] = "running"
    state["live"]["items"][0]["terminal_outcome"] = None
    state["live"]["items"][0]["active"] = True
    detail = _detail_for_run(
        run_id=item["run_id"],
        task_id=item["task_id"],
        branch=item["branch"],
        target=SAMPLE_TARGET,
        status="running",
        file_count=0,
        files=[],
        actions=[
            {"key": "c", "label": "Cancel", "enabled": True, "reason": None, "preview": "Stop the run"}
        ],
    )
    _install_projects_route(page)
    _install_state_route(page, state)
    _install_detail_route(page, item["run_id"], detail)
    _install_artifacts_route(page, item["run_id"])

    _hydrate(mc_backend, page, disable_animations)
    _open_run(page, item["task_id"])

    page.evaluate(
        """() => {
            const det = document.querySelector('details.advanced-actions');
            if (det) det.open = true;
        }"""
    )
    cancel_btn = page.get_by_test_id("advanced-action-cancel")
    cancel_btn.wait_for(state="visible", timeout=5_000)
    cancel_btn.click()

    page.wait_for_selector('.confirm-dialog', timeout=5_000)
    body_text = page.locator(".confirm-dialog").text_content() or ""
    assert "30s" in body_text, body_text
    assert "Work in progress may be lost" in body_text, body_text
    assert item["task_id"] in body_text, body_text


# ---------------------------------------------------------------------------
# Tests — Watcher stop counts (#5 IMPORTANT)
# ---------------------------------------------------------------------------


def test_watcher_stop_shows_counts(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """2 running + 3 queued + 1 backlog → stop confirm shows pid + all 3 counts."""

    payload = _state_with_running_watcher(running=2, queued=3, backlog=1, pid=4321)
    _install_projects_route(page)
    _install_state_route(page, payload)

    _hydrate(mc_backend, page, disable_animations)

    stop_btn = page.get_by_test_id("stop-watcher-button")
    stop_btn.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=stop-watcher-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    stop_btn.click()

    page.wait_for_selector('[data-testid=confirm-watcher-stop-detail]', timeout=5_000)
    pid_text = page.get_by_test_id("confirm-watcher-stop-pid").text_content() or ""
    running_text = page.get_by_test_id("confirm-watcher-stop-running").text_content() or ""
    queued_text = page.get_by_test_id("confirm-watcher-stop-queued").text_content() or ""
    backlog_text = page.get_by_test_id("confirm-watcher-stop-backlog").text_content() or ""

    assert "4321" in pid_text, pid_text
    assert "2 running" in running_text, running_text
    assert "3 queued" in queued_text, queued_text
    assert "1 pending" in backlog_text, backlog_text


# ---------------------------------------------------------------------------
# Tests — Action error keeps dialog open with inline error (#6 IMPORTANT)
# ---------------------------------------------------------------------------


def test_action_error_keeps_dialog_open_with_inline_error(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Cancel POST returns 409 → dialog STAYS OPEN, inline error visible."""

    state = _state_with_ready_tasks(1)
    item = state["landing"]["items"][0]
    state["live"]["items"][0]["status"] = "running"
    state["live"]["items"][0]["display_status"] = "running"
    state["live"]["items"][0]["active"] = True
    detail = _detail_for_run(
        run_id=item["run_id"],
        task_id=item["task_id"],
        branch=item["branch"],
        target=SAMPLE_TARGET,
        status="running",
        file_count=0,
        files=[],
        actions=[
            {"key": "c", "label": "Cancel", "enabled": True, "reason": None, "preview": "Stop"}
        ],
    )
    _install_projects_route(page)
    _install_state_route(page, state)
    _install_detail_route(page, item["run_id"], detail)
    _install_artifacts_route(page, item["run_id"])

    # Track POST calls and reply with 409 conflict.
    post_count = {"value": 0}

    def cancel_handler(route: Any) -> None:
        post_count["value"] += 1
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({
                "ok": False,
                "message": "Task already cancelled by another tab.",
                "severity": "error",
            }),
        )

    page.route(f"**/api/runs/{item['run_id']}/actions/cancel", cancel_handler)

    _hydrate(mc_backend, page, disable_animations)
    _open_run(page, item["task_id"])

    page.evaluate(
        """() => {
            const det = document.querySelector('details.advanced-actions');
            if (det) det.open = true;
        }"""
    )
    page.get_by_test_id("advanced-action-cancel").click()
    page.wait_for_selector('.confirm-dialog', timeout=5_000)
    page.get_by_test_id("confirm-dialog-confirm-button").click()

    # Inline error is visible; dialog stays open.
    err = page.locator('[data-testid=confirm-dialog-error]')
    err.wait_for(state="visible", timeout=5_000)
    err_text = err.text_content() or ""
    assert "already cancelled" in err_text.lower(), err_text
    # Dialog still open.
    assert page.locator('.confirm-dialog').count() == 1


# ---------------------------------------------------------------------------
# Tests — Queue job 3-second grace window (#7 IMPORTANT)
# ---------------------------------------------------------------------------


def _open_job_dialog(page: Any) -> None:
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector('.job-dialog', timeout=5_000)


def _fill_minimum_intent(page: Any) -> None:
    page.get_by_test_id("job-dialog-intent").fill("Write a short script that does X.")


def test_queue_job_3s_grace_window_cancellable(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Submit shows 'Queueing in 3s' banner; clicking Cancel within 1s skips POST."""

    payload = _state_with_ready_tasks(0)
    _install_projects_route(page)
    _install_state_route(page, payload)

    queue_calls = {"count": 0}

    def queue_handler(route: Any) -> None:
        queue_calls["count"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "queued", "task": {}, "warnings": [], "refresh": True}),
        )

    page.route("**/api/queue/build", queue_handler)

    _hydrate(mc_backend, page, disable_animations)
    _open_job_dialog(page)
    _fill_minimum_intent(page)
    page.get_by_test_id("job-dialog-submit-button").click()

    banner = page.locator('[data-testid=job-grace-banner]')
    banner.wait_for(state="visible", timeout=2_000)
    cancel_btn = page.get_by_test_id("job-grace-cancel-button")
    cancel_btn.wait_for(state="visible", timeout=2_000)

    # Cancel within the grace window.
    page.wait_for_timeout(500)
    cancel_btn.click()

    # Wait past the 3s grace window — no POST should fire.
    page.wait_for_timeout(3500)
    assert queue_calls["count"] == 0, "POST must not fire after Cancel during grace"
    # Dialog still open with form retained.
    assert page.locator('.job-dialog').count() == 1


def test_queue_job_grace_window_completes(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Without Cancel, the POST fires after the 3-second grace window."""

    payload = _state_with_ready_tasks(0)
    _install_projects_route(page)
    _install_state_route(page, payload)

    queue_calls = {"count": 0}
    queue_lock = threading.Lock()

    def queue_handler(route: Any) -> None:
        with queue_lock:
            queue_calls["count"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "queued", "task": {}, "warnings": [], "refresh": True}),
        )

    page.route("**/api/queue/build", queue_handler)

    _hydrate(mc_backend, page, disable_animations)
    _open_job_dialog(page)
    _fill_minimum_intent(page)
    page.get_by_test_id("job-dialog-submit-button").click()

    page.locator('[data-testid=job-grace-banner]').wait_for(state="visible", timeout=2_000)
    # Wait for grace + a small slack.
    page.wait_for_timeout(3500)
    assert queue_calls["count"] == 1, f"expected 1 POST after grace, got {queue_calls['count']}"
