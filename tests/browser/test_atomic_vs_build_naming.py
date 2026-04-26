"""Browser regression for W11-IMPORTANT-2 — UI must alias domain "atomic"
to user-facing "build" so the inspector reads consistently with the rest
of the app (sidebar, dialogs, focus banner all say "build").

Source: live W11 dogfood — the standalone CLI build appeared with
``domain="atomic"`` everywhere a domain was rendered, but every label
the user reads says "build". External integrations searching for
``domain == "build"`` got zero hits; the harness itself fell into this
trap. The fix in ``otto/web/client/src/App.tsx`` adds a ``domainLabel``
helper and uses it in the run inspector. See
``docs/mc-audit/live-findings.md`` (search "W11-IMPORTANT-2").

Invariant: when the run inspector opens for a record with
``domain="atomic"``, the rendered ``Type`` field must read
``build / <run_type>`` (not ``atomic / <run_type>``).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_atomic_vs_build_naming.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"
ATOMIC_RUN_ID = "2026-04-26-011318-aaaaaa"


def _atomic_live_item() -> dict[str, Any]:
    return {
        "run_id": ATOMIC_RUN_ID,
        "domain": "atomic",
        "run_type": "build",
        "command": "build",
        "display_name": "build kanban",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "main",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "atomic.build",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": ATOMIC_RUN_ID,
        "branch_task": "build-kanban",
        "elapsed_s": 30.0,
        "elapsed_display": "30s",
        "cost_usd": None,
        "cost_display": "…",
        "last_event": "running",
        "row_label": "build kanban",
        "overlay": None,
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
        },
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
            "items": [_atomic_live_item()],
            "total_count": 1,
            "active_count": 1,
            "refresh_interval_s": 0.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
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


def _atomic_detail() -> dict[str, Any]:
    item = _atomic_live_item()
    return {
        # RunSummary
        "run_id": item["run_id"],
        "domain": item["domain"],
        "run_type": item["run_type"],
        "command": item["command"],
        "display_name": item["display_name"],
        "status": item["status"],
        "terminal_outcome": item["terminal_outcome"],
        "project_dir": item["project_dir"],
        "cwd": item["cwd"],
        "queue_task_id": item["queue_task_id"],
        "merge_id": item["merge_id"],
        "branch": item["branch"],
        "worktree": item["worktree"],
        "provider": item["provider"],
        "model": item["model"],
        "reasoning_effort": item["reasoning_effort"],
        "adapter_key": item["adapter_key"],
        "version": item["version"],
        # RunDetail extras
        "display_status": item["display_status"],
        "active": True,
        "source": "live",
        "title": item["display_name"],
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [],
        "review_packet": {
            "headline": item["display_name"],
            "status": "running",
            "summary": "",
            "readiness": {
                "state": "in_progress",
                "label": "Running",
                "tone": "info",
                "blockers": [],
                "next_step": "Wait for completion.",
            },
            "checks": [],
            "next_action": {
                "label": "",
                "action_key": None,
                "enabled": False,
                "reason": None,
            },
            "certification": {
                "stories_passed": None,
                "stories_tested": None,
                "passed": False,
                "summary_path": None,
                "stories": [],
                "proof_report": {
                    "json_path": None,
                    "html_path": None,
                    "html_url": None,
                    "available": False,
                },
            },
            "changes": {
                "branch": item["branch"],
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


def _install_detail_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"**/api/runs/{ATOMIC_RUN_ID}*", handler)


def test_inspector_displays_atomic_as_build(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Open inspector for an atomic run → Type field reads "build / build"."""

    _install_projects_route(page)
    _install_state_route(page, _state())
    _install_detail_route(page, _atomic_detail())

    # Pre-select the run via URL so the inspector opens immediately on hydrate.
    page.goto(f"{mc_backend.url}?view=tasks&run={ATOMIC_RUN_ID}", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    # The Type field lives inside a collapsed <details> "Run metadata" panel.
    # The dd element is in the DOM either way (its text content is what we
    # care about for the rename test) but the parent <details> is closed by
    # default. Wait for the testid to attach (proves inspector hydrated +
    # detail loaded), then read text_content directly — visibility is not
    # required for this assertion since we only validate string content.
    type_dd = page.locator("[data-testid=run-detail-type]")
    type_dd.wait_for(state="attached", timeout=5_000)
    text = (type_dd.text_content() or "").strip()

    # Must say "build / build", NOT "atomic / build".
    assert "atomic" not in text, (
        f"inspector Type field still shows 'atomic' — should be aliased to 'build'. Got: {text!r}"
    )
    assert text.startswith("build"), (
        f"inspector Type field should start with 'build'. Got: {text!r}"
    )
