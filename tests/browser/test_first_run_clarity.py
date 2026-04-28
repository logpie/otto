"""Browser tests for cluster F — boot-loading gate + first-run clarity.

Covers mc-audit codex-first-time-user.md findings #1, #2, #3, #5, #6, #7,
#8, #10, #11, #12, #13, #14, #15. Each test is paired with the fix it
guards in `App.tsx` / `styles.css` / `api.ts`.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest tests/browser/test_first_run_clarity.py \\
        -m browser -p playwright -v

The bundle build is required for the fixes to be visible in the served SPA.
After implementation, run once with the full bundle build (omit the env var)
to confirm the live bundle has them.

Tests rely on Playwright's `page.route` to inject synthetic responses for
both the projects + state endpoints — same pattern as test_async_actions.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Fixture state payloads
# --------------------------------------------------------------------------- #


def _state_idle_first_run() -> dict[str, Any]:
    """Empty-history project — used to test first-run CTA copy."""

    return {
        "project": {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
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
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
            "collisions": [],
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


def _state_with_one_history_run() -> dict[str, Any]:
    """Same as idle but with one completed run so the CTA flips to 'New job'."""

    payload = _state_idle_first_run()
    payload["history"]["items"] = [{
        "run_id": "run-2026-04-25-1",
        "domain": "build",
        "run_type": "build",
        "command": "build",
        "status": "completed",
        "terminal_outcome": "success",
        "queue_task_id": "task-1",
        "merge_id": None,
        "branch": "feature/x",
        "worktree": None,
        "summary": "First build done",
        "intent": "Build a thing",
        "completed_at_display": "2026-04-25 11:30",
        "outcome_display": "success",
        "duration_s": 120,
        "duration_display": "2m",
        "cost_usd": 0.05,
        "cost_display": "$0.05",
        "resumable": False,
        "adapter_key": "build",
    }]
    payload["history"]["total_rows"] = 1
    payload["history"]["total_pages"] = 1
    payload["landing"]["counts"] = {"ready": 0, "merged": 1, "blocked": 0, "total": 1}
    return payload


def _state_with_dirty_project() -> dict[str, Any]:
    payload = _state_idle_first_run()
    payload["project"]["dirty"] = True
    payload["landing"]["dirty_files"] = [
        "src/server.py",
        "src/utils.py",
        "tests/test_x.py",
        "tests/test_y.py",
        "README.md",
        "extra-1.md",
        "extra-2.md",
    ]
    return payload


def _state_with_failed_run() -> dict[str, Any]:
    """State plus a stubbed `/api/runs/<id>` payload with recovery actions."""

    payload = _state_idle_first_run()
    payload["live"]["items"] = [{
        "run_id": "run-failed-1",
        "domain": "build",
        "run_type": "build",
        "command": "build",
        "display_name": "build run",
        "status": "failed",
        "terminal_outcome": "failed",
        "project_dir": "/tmp/proj",
        "cwd": None,
        "queue_task_id": "task-failed",
        "merge_id": None,
        "branch": "feature/broken",
        "worktree": None,
        "provider": "claude",
        "model": "sonnet-4-7",
        "reasoning_effort": "high",
        "adapter_key": "build",
        "version": 1,
        "display_status": "failed",
        "active": False,
        "display_id": "run-failed-1",
        "branch_task": "feature/broken / task-failed",
        "elapsed_s": 60,
        "elapsed_display": "1m",
        "cost_usd": 0.01,
        "cost_display": "$0.01",
        "last_event": "Build failed",
        "row_label": "task-failed",
        "overlay": None,
    }]
    payload["live"]["total_count"] = 1
    payload["live"]["active_count"] = 0
    return payload


def _detail_for_failed_run() -> dict[str, Any]:
    return {
        "run_id": "run-failed-1",
        "domain": "build",
        "run_type": "build",
        "command": "build",
        "display_name": "build run",
        "status": "failed",
        "terminal_outcome": "failed",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": "task-failed",
        "merge_id": None,
        "branch": "feature/broken",
        "worktree": None,
        "provider": "claude",
        "model": "sonnet-4-7",
        "reasoning_effort": "high",
        "adapter_key": "build",
        "version": 1,
        "display_status": "failed",
        "active": False,
        "source": "live",
        "title": "build run",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [
            {"key": "R", "label": "retry", "enabled": True, "reason": None, "preview": "Retry from scratch"},
            {"key": "x", "label": "cleanup", "enabled": True, "reason": None, "preview": "Remove run record"},
            {"key": "r", "label": "resume", "enabled": False, "reason": "no checkpoint", "preview": ""},
        ],
        "review_packet": {
            "headline": "Build failed",
            "status": "failed",
            "summary": "Build failed during step 3.",
            "readiness": {
                "state": "blocked",
                "label": "Blocked",
                "tone": "danger",
                "blockers": ["Build error"],
                "next_step": "Retry the build or remove the task.",
            },
            "checks": [],
            "next_action": {"label": "retry", "action_key": "R", "enabled": True, "reason": None},
            "certification": {
                "stories_passed": 0,
                "stories_tested": 0,
                "passed": False,
                "summary_path": None,
                "stories": [],
                "proof_report": {"json_path": None, "html_path": None, "html_url": None, "available": False},
            },
            "changes": {
                "branch": None,
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
            "failure": {"reason": "Build failed", "last_event": None, "excerpt": None, "source": None},
        },
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


def _projects_payload(launcher_enabled: bool = False, projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "launcher_enabled": launcher_enabled,
        "projects_root": "/tmp/managed",
        "current": None if launcher_enabled and not projects else {
            "path": "/tmp/proj",
            "name": "proj",
            "branch": "main",
            "dirty": False,
            "head_sha": "abc1234",
        },
        "projects": projects or [],
    }


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/state*", handler)


def _install_projects_route(page: Any, payload: dict[str, Any] | None = None, *, delay_s: float = 0.0) -> None:
    body = json.dumps(payload if payload is not None else _projects_payload(launcher_enabled=False))

    def handler(route: Any) -> None:
        if delay_s > 0:
            time.sleep(delay_s)
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/projects", handler)


def _install_run_detail_route(page: Any, run_id: str, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"**/api/runs/{run_id}*", handler)


def _wait_launcher(page: Any) -> None:
    page.wait_for_selector('[data-testid="launcher-subhead"]', timeout=10_000)


def _switch_command(page: Any, command: str) -> None:
    if command == "build":
        page.get_by_test_id("job-command-select").click()
    else:
        page.get_by_test_id(f"job-command-{command}").click()


# --------------------------------------------------------------------------- #
# CRITICAL #1 — boot-loading gate
# --------------------------------------------------------------------------- #


def test_no_main_shell_during_boot_loading(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """While `/api/projects` is in flight, the main shell + New job button must NOT render."""

    payload = _state_idle_first_run()
    _install_projects_route(page, _projects_payload(launcher_enabled=False), delay_s=0.6)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="domcontentloaded")

    # Snapshot quickly — within the 600ms hold there must be NO new-job button.
    snapshot = page.evaluate(
        """() => ({
            hasNewJob: !!document.querySelector('[data-testid=new-job-button]'),
            hasBootLoading: !!document.querySelector('[data-testid=boot-loading]'),
        })"""
    )
    assert snapshot["hasBootLoading"], f"expected boot-loading placeholder; snapshot={snapshot!r}"
    assert not snapshot["hasNewJob"], f"main shell rendered before /api/projects returned; snapshot={snapshot!r}"


def test_main_shell_renders_after_boot_loading_completes(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Once /api/projects + /api/state resolve, the main shell renders and New job is enabled."""

    payload = _state_idle_first_run()
    _install_projects_route(page, _projects_payload(launcher_enabled=False), delay_s=0.3)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    btn = page.get_by_test_id("new-job-button")
    btn.wait_for(state="visible", timeout=5_000)
    assert btn.is_enabled()


# --------------------------------------------------------------------------- #
# CRITICAL #2 — pre-submit summary
# --------------------------------------------------------------------------- #


def test_job_dialog_shows_pre_submit_summary(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Opening JobDialog renders a 'Will run with: …' summary outside Advanced."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    summary = page.get_by_test_id("job-dialog-summary-text")
    summary.wait_for(state="visible", timeout=2_000)
    text = summary.text_content() or ""
    assert "claude" in text and "sonnet-4-7" in text, f"summary should mention provider+model defaults; got {text!r}"
    assert "effort=high" in text, f"summary should expose reasoning effort; got {text!r}"
    assert "verification=" in text, f"summary should expose verification policy; got {text!r}"

    edit = page.get_by_test_id("job-dialog-summary-edit")
    assert edit.is_visible()


def test_job_dialog_focuses_intent_and_inerts_background(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Opening JobDialog should focus the intent field and isolate the page behind it."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    page.get_by_test_id("job-dialog-summary").wait_for(state="visible", timeout=2_000)
    page.wait_for_function(
        "() => document.activeElement?.getAttribute('data-testid') === 'job-dialog-intent'",
        timeout=2_000,
    )

    state = page.evaluate(
        """() => ({
            topbarInert: document.querySelector('.topbar')?.hasAttribute('inert') === true,
            mainInert: document.querySelector('.main-shell-content')?.hasAttribute('inert') === true,
            closeFocused: document.activeElement?.getAttribute('data-testid') === 'job-dialog-close-button',
        })"""
    )
    assert state["topbarInert"], f"topbar must be inert while JobDialog is open; got {state!r}"
    assert state["mainInert"], f"main shell must be inert while JobDialog is open; got {state!r}"
    assert not state["closeFocused"], f"initial focus should not land on Close; got {state!r}"


def test_job_dialog_summary_updates_on_advanced_change(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Changing the Advanced effort dropdown updates the summary line live."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    page.get_by_test_id("job-dialog-summary").wait_for(state="visible")

    summary = page.get_by_test_id("job-dialog-summary-text")
    edit = page.get_by_test_id("job-dialog-summary-edit")
    summary_box = summary.bounding_box()
    edit_box = edit.bounding_box()
    assert summary_box and edit_box
    assert (
        summary_box["x"] + summary_box["width"] <= edit_box["x"]
        or edit_box["x"] + edit_box["width"] <= summary_box["x"]
        or summary_box["y"] + summary_box["height"] <= edit_box["y"]
        or edit_box["y"] + edit_box["height"] <= summary_box["y"]
    ), "summary text should not overlap the options toggle"

    edit.click()
    assert edit.get_attribute("aria-expanded") == "true"
    assert "Hide options" in (edit.text_content() or "")
    page.get_by_test_id("job-effort-select").select_option("low")

    page.wait_for_function(
        "() => (document.querySelector('[data-testid=job-dialog-summary-text]')?.textContent || '').includes('effort=low')",
        timeout=2_000,
    )

    edit.click()
    assert edit.get_attribute("aria-expanded") == "false"
    assert "Edit options" in (edit.text_content() or "")
    page.get_by_test_id("job-effort-select").wait_for(state="hidden", timeout=2_000)


# --------------------------------------------------------------------------- #
# IMPORTANT #3 — launcher subhead
# --------------------------------------------------------------------------- #


def test_launcher_shows_what_mc_does(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """The launcher panel includes the explanatory subhead about Otto."""

    _install_projects_route(page, _projects_payload(launcher_enabled=True, projects=[]))
    # No state stubbing — launcher is rendered before /api/state ever fires.

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_launcher(page)
    disable_animations(page)

    sub = page.get_by_test_id("launcher-subhead")
    sub.wait_for(state="visible", timeout=5_000)
    text = sub.text_content() or ""
    assert "isolated git worktree" in text, f"expected explanatory subhead; got {text!r}"
    assert "review logs" in text or "logs" in text, f"subhead should mention reviewing logs; got {text!r}"


# --------------------------------------------------------------------------- #
# IMPORTANT #5 — empty project list CTA
# --------------------------------------------------------------------------- #


def test_launcher_empty_state_has_actionable_cta(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """Empty project list shows a 'Create your first Otto project' CTA."""

    _install_projects_route(page, _projects_payload(launcher_enabled=True, projects=[]))

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_launcher(page)
    disable_animations(page)

    empty = page.get_by_test_id("launcher-empty-state")
    empty.wait_for(state="visible", timeout=5_000)
    text = empty.text_content() or ""
    assert "Create your first" in text, f"expected actionable CTA copy; got {text!r}"


def test_launcher_many_projects_are_searchable_and_compact(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A 50-project launcher should expose search and not bury names under temp paths."""

    projects = [
        {
            "path": f"/tmp/managed/proj-{idx:03d}",
            "name": f"proj-{idx:03d}",
            "branch": "main",
            "dirty": False,
            "head_sha": "abc1234",
        }
        for idx in range(50)
    ]
    payload = {
        "launcher_enabled": True,
        "projects_root": "/tmp/managed",
        "current": None,
        "projects": projects,
    }
    _install_projects_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_launcher(page)
    disable_animations(page)

    search = page.get_by_test_id("launcher-project-search")
    search.wait_for(state="visible", timeout=5_000)
    search.fill("proj-001")

    row = page.get_by_role("button", name="proj-001")
    row.wait_for(state="visible", timeout=2_000)
    assert page.get_by_role("button", name="proj-049").count() == 0
    assert "/tmp/managed" not in (row.text_content() or "")


def test_project_switch_button_returns_to_launcher(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The current-project control should be an obvious route back to the launcher."""

    projects = [
        {
            "path": f"/tmp/managed/proj-{idx:03d}",
            "name": f"proj-{idx:03d}",
            "branch": "main",
            "dirty": False,
            "head_sha": "abc1234",
        }
        for idx in range(3)
    ]
    current = projects[2]
    project_payload = {
        "launcher_enabled": True,
        "projects_root": "/tmp/managed",
        "current": current,
        "projects": projects,
    }
    launcher_payload = {**project_payload, "current": None}
    state_payload = _state_idle_first_run()
    state_payload["project"] = {
        **state_payload["project"],
        "path": current["path"],
        "name": current["name"],
        "branch": current["branch"],
    }

    _install_projects_route(page, project_payload)
    _install_state_route(page, state_payload)
    page.route(
        "**/api/projects/clear",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "current": None, "projects": projects}),
        ),
    )

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    switcher = page.get_by_test_id("switch-project-button")
    assert "Projects" in (switcher.text_content() or "")
    switcher.click()
    _wait_launcher(page)
    page.get_by_role("button", name="proj-001").wait_for(state="visible", timeout=2_000)


# --------------------------------------------------------------------------- #
# IMPORTANT #6 — first-run CTA "Start first build"
# --------------------------------------------------------------------------- #


def test_first_run_cta_says_start_first_build(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """With zero history + no live runs, the mission-focus primary CTA reads 'Start first build'."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    cta = page.get_by_test_id("task-board-empty-queue-job")
    cta.wait_for(state="visible", timeout=5_000)
    text = cta.text_content() or ""
    assert "Queue your first job" in text, f"expected first-run queue CTA; got {text!r}"


def test_first_run_cta_reverts_after_first_run(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """After at least one run exists, the primary CTA reverts to 'New job'."""

    payload = _state_with_one_history_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    cta = page.get_by_test_id("new-job-button")
    cta.wait_for(state="visible", timeout=5_000)
    text = (cta.text_content() or "").strip()
    assert text == "New job", f"expected 'New job' once history has runs; got {text!r}"


def test_empty_task_board_with_history_mentions_history_not_first_run(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A project with past runs but no active tasks should show its run history."""

    payload = _state_with_one_history_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    row = page.get_by_test_id("task-card-task-1")
    row.wait_for(state="visible", timeout=5_000)
    text = row.text_content() or ""
    assert "task-1" in text, f"expected historical task row; got {text!r}"
    assert "First build done" in text, f"expected historical summary; got {text!r}"
    assert "Landed" in text, f"successful historical run should be grouped as landed; got {text!r}"
    assert page.get_by_test_id("task-board-empty").count() == 0
    assert "Queue your first job" not in (page.locator("body").text_content() or "")


# --------------------------------------------------------------------------- #
# IMPORTANT #7 — Build/Improve/Certify descriptions
# --------------------------------------------------------------------------- #


def test_command_select_shows_description(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """A help line appears under the command select that updates per choice."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    help_text = page.get_by_test_id("job-command-help")
    help_text.wait_for(state="visible", timeout=2_000)
    assert "Build new work" in (help_text.text_content() or "")

    _switch_command(page, "improve")
    page.wait_for_function(
        "() => (document.querySelector('[data-testid=job-command-help]')?.textContent || '').includes('Iterate on an existing run')",
        timeout=2_000,
    )

    _switch_command(page, "certify")
    page.wait_for_function(
        "() => (document.querySelector('[data-testid=job-command-help]')?.textContent || '').includes('Verify an existing run')",
        timeout=2_000,
    )


# --------------------------------------------------------------------------- #
# IMPORTANT #8 — improve/certify require focus
# --------------------------------------------------------------------------- #


def test_improve_requires_focus(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """`improve` with empty intent/focus disables submit + shows the validation hint."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    _switch_command(page, "improve")

    submit = page.get_by_test_id("job-dialog-submit-button")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=job-dialog-submit-button]')?.disabled === true",
        timeout=2_000,
    )
    assert not submit.is_enabled()

    hint = page.get_by_test_id("job-dialog-validation-hint")
    hint.wait_for(state="visible", timeout=2_000)
    assert "focus" in (hint.text_content() or "").lower()


def test_certify_requires_focus(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """`certify` with empty focus disables submit + shows the validation hint."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    _switch_command(page, "certify")

    page.wait_for_function(
        "() => document.querySelector('[data-testid=job-dialog-submit-button]')?.disabled === true",
        timeout=2_000,
    )
    hint = page.get_by_test_id("job-dialog-validation-hint")
    hint.wait_for(state="visible", timeout=2_000)


# --------------------------------------------------------------------------- #
# IMPORTANT #10 — dirty file list in confirm dialog
# --------------------------------------------------------------------------- #


def test_dirty_confirm_lists_files(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """When the project is dirty, the JobDialog target-guard lists up to 5 files."""

    payload = _state_with_dirty_project()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("new-job-button").click()
    files = page.get_by_test_id("job-dialog-dirty-files")
    files.wait_for(state="visible", timeout=2_000)
    text = files.text_content() or ""
    # First file should appear; the 7-file fixture should overflow with "+2 more"
    assert "src/server.py" in text
    assert "+2 more" in text


# --------------------------------------------------------------------------- #
# IMPORTANT #11 — after-queue CTA wording
# --------------------------------------------------------------------------- #


def test_after_queue_cta_says_start_queued_job(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """When at least one queued task exists + watcher stopped, primary mission CTA reads 'Start queued job'."""

    payload = _state_idle_first_run()
    payload["watcher"]["counts"] = {"queued": 1, "running": 0}
    payload["runtime"]["supervisor"]["can_start"] = True
    payload["runtime"]["queue_tasks"] = 1
    payload["runtime"]["state_tasks"] = 1

    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    cta = page.get_by_test_id("start-watcher-button")
    cta.wait_for(state="visible", timeout=5_000)
    title = cta.get_attribute("title") or ""
    assert "Start" in title or "queued" in title.lower(), f"expected actionable watcher start title; got {title!r}"


# --------------------------------------------------------------------------- #
# IMPORTANT #12 — empty detail copy
# --------------------------------------------------------------------------- #


def test_empty_detail_shows_descriptive_copy(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """When no run is selected the detail panel shows the descriptive copy."""

    payload = _state_idle_first_run()
    _install_projects_route(page)
    _install_state_route(page, payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    empty = page.get_by_test_id("task-board-empty")
    empty.wait_for(state="visible", timeout=5_000)
    text = empty.text_content() or ""
    assert "No work queued" in text and "build" in text and "certify" in text and "improve" in text, (
        f"empty task-board copy missing expected keywords; got {text!r}"
    )


# --------------------------------------------------------------------------- #
# IMPORTANT #14 — recovery action surfaced
# --------------------------------------------------------------------------- #


def test_recovery_action_surfaced_for_failed_run(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """A failed run surfaces a contextual Retry button at the run-header level."""

    state_payload = _state_with_failed_run()
    detail_payload = _detail_for_failed_run()

    _install_projects_route(page)
    _install_state_route(page, state_payload)
    _install_run_detail_route(page, "run-failed-1", detail_payload)

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    page.get_by_test_id("task-card-task-failed").click()
    bar = page.get_by_test_id("recovery-action-bar")
    bar.wait_for(state="visible", timeout=5_000)
    retry = page.get_by_test_id("recovery-action-retry")
    retry.wait_for(state="visible", timeout=2_000)
    assert retry.is_enabled()


# --------------------------------------------------------------------------- #
# IMPORTANT #15 — 409 recovery copy on project create
# --------------------------------------------------------------------------- #


def test_project_create_409_shows_recovery_copy(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """A 409 from /api/projects/create surfaces actionable recovery copy in the form status."""

    _install_projects_route(page, _projects_payload(launcher_enabled=True, projects=[]))

    def create_handler(route: Any) -> None:
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"ok": False, "message": "Project already exists: /tmp/managed/duplicate", "severity": "error"}),
        )

    page.route("**/api/projects/create", create_handler)

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_launcher(page)
    disable_animations(page)

    page.get_by_test_id("launcher-create-name-input").fill("duplicate")
    page.get_by_test_id("launcher-create-submit").click()

    status = page.get_by_test_id("launcher-form-status")
    page.wait_for_function(
        "() => (document.querySelector('[data-testid=launcher-form-status]')?.textContent || '').toLowerCase().includes('already exists')",
        timeout=5_000,
    )
    text = status.text_content() or ""
    assert "duplicate" in text, f"expected name to appear in recovery copy; got {text!r}"
    assert "already exists" in text.lower(), f"expected friendly recovery copy; got {text!r}"


# --------------------------------------------------------------------------- #
# IMPORTANT #4 — "managed root" clarification
# --------------------------------------------------------------------------- #


def test_launcher_managed_root_help_explains_isolation(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    """The managed-root panel explains that the launching repo is intentionally excluded."""

    _install_projects_route(page, _projects_payload(launcher_enabled=True, projects=[]))

    page.goto(mc_backend.url, wait_until="networkidle")
    _wait_launcher(page)
    disable_animations(page)

    help_el = page.get_by_test_id("launcher-managed-root-help")
    help_el.wait_for(state="visible", timeout=5_000)
    text = help_el.text_content() or ""
    assert "intentionally excluded" in text, f"expected managed-root help to mention exclusion; got {text!r}"
    assert "git worktree" in text, f"expected explanation of worktree isolation; got {text!r}"
