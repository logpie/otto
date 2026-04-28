"""Browser test for disabled-tab visual distinctness.

Cluster: mc-audit microinteractions I7 (IMPORTANT).

Problem: A disabled tab (e.g. Diff while no diff is available) used the
default ``button:disabled`` styling, which collided with ``.tab``'s
``--surface-alt`` background — making the disabled tab visually identical
to an inactive but still-clickable tab. Operators could not tell that
the Diff tab was actually unavailable.

Fix: Add explicit ``.tab:disabled`` styling — striped background, lower
opacity, strikethrough text, ``cursor: not-allowed``. Each of those
properties differs from a plain inactive tab; we assert at least the
opacity / cursor / text-decoration differs between the two.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_disabled_tab_distinct_from_inactive.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


RUN_ID = "run-disabled-tab"
TASK_ID = "task-disabled-tab"


def _live_item() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": TASK_ID,
        "status": "failed",
        "terminal_outcome": "failed",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": TASK_ID,
        "merge_id": None,
        "branch": None,
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "failed",
        "active": False,
        "display_id": RUN_ID,
        "branch_task": TASK_ID,
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": 0.0,
        "cost_display": "$0.00",
        "last_event": "failed",
        "row_label": TASK_ID,
        "overlay": None,
    }


def _state() -> dict[str, Any]:
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
            "items": [],
            "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
            "collisions": [],
            "merge_blocked": False,
            "merge_blockers": [],
            "dirty_files": [],
            "target": "main",
        },
        "live": {
            "items": [_live_item()],
            "total_count": 1,
            "active_count": 0,
            "refresh_interval_s": 5.0,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
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


def _detail_no_branch() -> dict[str, Any]:
    """Detail with NO branch + no diff_error — canShowDiff returns false
    so the Diff tab is disabled inside the inspector. Other tabs (Review,
    Logs, Artifacts) remain enabled."""

    return {
        "run_id": RUN_ID,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": TASK_ID,
        "status": "failed",
        "terminal_outcome": "failed",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": TASK_ID,
        "merge_id": None,
        "branch": None,
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "failed",
        "active": False,
        "source": "live",
        "title": TASK_ID,
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": ["/tmp/proj/otto_logs/sessions/x/build/narrative.log"],
        "selected_log_index": 0,
        "selected_log_path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "legal_actions": [],
        "review_packet": {
            "headline": TASK_ID,
            "status": "failed",
            "summary": TASK_ID,
            "readiness": {
                "state": "needs_attention",
                "label": "Needs attention",
                "tone": "danger",
                "blockers": [],
                "next_step": "",
            },
            "checks": [],
            "next_action": {"label": "review", "action_key": None, "enabled": False, "reason": None},
            "certification": {
                "stories_passed": 0,
                "stories_tested": 0,
                "passed": False,
                "summary_path": None,
                "stories": [],
                "proof_report": {"json_path": None, "html_path": None, "html_url": None, "available": False},
            },
            "changes": {
                "branch": None,  # ← disables canShowDiff
                "target": "main",
                "merged": False,
                "merge_id": None,
                "file_count": 0,
                "files": [],
                "truncated": False,
                "diff_command": "",
                "diff_error": None,
            },
            "evidence": [],
            "failure": None,
        },
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


def _install_projects_route(page: Any) -> None:
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


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    body = json.dumps(payload)
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_detail_route(page: Any) -> None:
    body = json.dumps(_detail_no_branch())
    page.route(
        f"**/api/runs/{RUN_ID}",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )
    page.route(
        f"**/api/runs/{RUN_ID}?**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_logs_route(page: Any) -> None:
    body = json.dumps({
        "path": "/tmp/proj/otto_logs/sessions/x/build/narrative.log",
        "offset": 0,
        "next_offset": 0,
        "text": "",
        "exists": True,
        "total_bytes": 0,
        "eof": True,
    })
    page.route(
        f"**/api/runs/{RUN_ID}/logs**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_artifacts_route(page: Any) -> None:
    page.route(
        f"**/api/runs/{RUN_ID}/artifacts",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": RUN_ID, "artifacts": []}),
        ),
    )


# --------------------------------------------------------------------------- #
# Test
# --------------------------------------------------------------------------- #


def test_disabled_diff_tab_visually_distinct_from_inactive_tabs(
    mc_backend: Any, page: Any
) -> None:
    """Diff tab disabled (no branch) → its computed style differs from an
    inactive but enabled tab on at least one of: opacity, cursor,
    text-decoration. Without the I7 fix, both would be near-identical.

    NOTE: we deliberately do NOT call ``disable_animations`` here — that
    fixture only kills CSS animations/transitions and does not touch
    static styling, but skipping it removes any incidental opacity
    transitions that could race the assertion.
    """

    payload = _state()
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_detail_route(page)
    _install_logs_route(page)
    _install_artifacts_route(page)

    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)

    # Open the inspector (Logs is enabled, so use that as the entry point).
    open_btn = page.get_by_test_id("open-logs-button")
    open_btn.wait_for(state="visible", timeout=5_000)
    open_btn.click()

    # Wait for the inspector tablist to mount.
    page.wait_for_selector(".run-inspector .detail-tabs[role='tablist']", timeout=5_000)

    styles = page.evaluate(
        """() => {
            const tablist = document.querySelector(
                ".run-inspector .detail-tabs[role='tablist']"
            );
            if (!tablist) return null;
            const tabs = Array.from(tablist.querySelectorAll('button[role="tab"]'));
            const diff = tabs.find((t) => t.dataset.tabId === 'diff');
            // Pick an enabled but inactive tab (i.e. not currently selected,
            // and not disabled). Logs is the active tab here, so pick any
            // tab that is not Logs and not Diff and not disabled.
            const inactiveEnabled = tabs.find(
                (t) => t.dataset.tabId !== 'diff' && !t.disabled && t.getAttribute('aria-selected') !== 'true'
            );
            if (!diff || !inactiveEnabled) {
                return {error: 'tabs not found', diffPresent: !!diff, inactivePresent: !!inactiveEnabled};
            }
            const diffStyle = window.getComputedStyle(diff);
            const inactiveStyle = window.getComputedStyle(inactiveEnabled);
            return {
                diff: {
                    disabled: diff.disabled,
                    opacity: diffStyle.opacity,
                    cursor: diffStyle.cursor,
                    textDecoration: diffStyle.textDecorationLine || diffStyle.textDecoration,
                    backgroundImage: diffStyle.backgroundImage,
                },
                inactive: {
                    opacity: inactiveStyle.opacity,
                    cursor: inactiveStyle.cursor,
                    textDecoration: inactiveStyle.textDecorationLine || inactiveStyle.textDecoration,
                    backgroundImage: inactiveStyle.backgroundImage,
                },
            };
        }"""
    )

    assert styles is not None, "could not query tab styles"
    assert "error" not in styles, f"missing tabs: {styles!r}"
    assert styles["diff"]["disabled"], f"Diff tab should be disabled: {styles!r}"

    diff = styles["diff"]
    inactive = styles["inactive"]

    # At least one visual distinction must exist. The fix sets opacity≈0.55,
    # cursor=not-allowed, text-decoration=line-through, plus a striped
    # backgroundImage. We accept any one of those as proof of distinctness.
    distinctions: list[str] = []
    if diff["opacity"] != inactive["opacity"]:
        distinctions.append(
            f"opacity diff={diff['opacity']} inactive={inactive['opacity']}"
        )
    if diff["cursor"] != inactive["cursor"]:
        distinctions.append(
            f"cursor diff={diff['cursor']} inactive={inactive['cursor']}"
        )
    if (diff["textDecoration"] or "").strip() != (inactive["textDecoration"] or "").strip():
        distinctions.append(
            f"text-decoration diff={diff['textDecoration']!r} inactive={inactive['textDecoration']!r}"
        )

    assert distinctions, (
        "disabled Diff tab is visually identical to inactive tab — "
        f"styles={styles!r}"
    )

    # Belt-and-braces: cursor must be not-allowed when disabled.
    assert diff["cursor"] == "not-allowed", (
        f"disabled tab cursor must be not-allowed, got {diff['cursor']!r}"
    )
