"""Browser tests for the multi-round certification tab strip.

Cluster ``codex-evidence-trustworthiness`` #4: Mission Control was
flattening certifications down to final stories/counts and dropping
earlier rounds. The proof drawer now renders a tab per round with
verdict + counts + duration + diagnosis.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \
        uv run pytest tests/browser/test_certification_round_tabs.py \
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser

RUN_ID = "rounds-test-run"


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
            "refresh_interval_s": 1.5,
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


def _live_item() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "domain": "atomic",
        "run_type": "build",
        "command": "build",
        "display_name": "build:rounds",
        "status": "done",
        "terminal_outcome": "success",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "build/rounds",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "atomic.build",
        "version": 1,
        "display_status": "done",
        "active": False,
        "display_id": RUN_ID,
        "branch_task": "build/rounds",
        "elapsed_s": 22.0,
        "elapsed_display": "22s",
        "cost_usd": 0.09,
        "cost_display": "$0.09",
        "last_event": "done",
        "row_label": "build:rounds",
        "overlay": None,
    }


def _detail_payload(*, round_count: int = 2) -> dict[str, Any]:
    rounds = []
    if round_count >= 1:
        rounds.append(
            {
                "round": 1,
                "verdict": "failed",
                "stories_tested": 2,
                "passed_count": 1,
                "failed_count": 1,
                "warn_count": 0,
                "failing_story_ids": ["restore-filter"],
                "warn_story_ids": [],
                "diagnosis": "Restore did not apply filter.",
                "duration_s": 12.0,
                "duration_human": "12s",
                "cost_usd": 0.05,
                "cost_estimated": False,
                "fix_commits": ["abc1234"],
                "fix_diff_stat": "1 file changed",
                "still_failing_after_fix": [],
                "subagent_errors": [],
            }
        )
    if round_count >= 2:
        rounds.append(
            {
                "round": 2,
                "verdict": "passed",
                "stories_tested": 2,
                "passed_count": 2,
                "failed_count": 0,
                "warn_count": 0,
                "failing_story_ids": [],
                "warn_story_ids": [],
                "diagnosis": "All stories passed after fix.",
                "duration_s": 10.0,
                "duration_human": "10s",
                "cost_usd": 0.04,
                "cost_estimated": False,
                "fix_commits": [],
                "fix_diff_stat": "",
                "still_failing_after_fix": [],
                "subagent_errors": [],
            }
        )
    item = _live_item()
    return {
        **item,
        "source": "live",
        "title": "build: rounds",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [],
        "review_packet": {
            "headline": "Ready for review",
            "status": "done",
            "summary": "rounds run",
            "readiness": {
                "state": "ready",
                "label": "Ready",
                "tone": "success",
                "blockers": [],
                "next_step": "Review evidence.",
            },
            "checks": [],
            "next_action": {"label": "review", "action_key": None, "enabled": False, "reason": None},
            "certification": {
                "stories_passed": 2,
                "stories_tested": 2,
                "passed": True,
                "summary_path": None,
                "stories": [],
                "rounds": rounds,
                "proof_report": {
                    "json_path": None,
                    "html_path": "/tmp/proj/proof.html",
                    "html_url": f"/api/runs/{RUN_ID}/proof-report",
                    "available": True,
                    "generated_at": "2026-04-25T12:34:56Z",
                    "run_id": RUN_ID,
                    "session_id": "sess-rounds",
                    "branch": "build/rounds",
                    "head_sha": "deadbee",
                    "file_mtime": "2026-04-25T12:34:57Z",
                    "sha256": "ab" * 32,
                    "run_id_matches": True,
                },
            },
            "changes": {
                "branch": "build/rounds",
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


def _install_routes(page: Any, *, detail: dict[str, Any]) -> None:
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
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_state_payload())
        ),
    )
    body = json.dumps(detail)
    page.route(
        f"**/api/runs/{RUN_ID}",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )
    page.route(
        f"**/api/runs/{RUN_ID}?**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _open_proof_drawer(page: Any) -> None:
    btn = page.get_by_test_id("open-proof-button")
    btn.wait_for(state="visible", timeout=5_000)
    btn.click()
    page.get_by_test_id("proof-pane").wait_for(state="visible", timeout=5_000)


def test_proof_drawer_renders_round_tabs_for_multi_round_run(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Multi-round certs render a tab per round with verdict labels."""

    _install_routes(page, detail=_detail_payload(round_count=2))
    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    _open_proof_drawer(page)
    page.get_by_test_id("proof-round-tabs").wait_for(state="visible", timeout=5_000)

    tab1 = page.get_by_test_id("proof-round-tab-1")
    tab2 = page.get_by_test_id("proof-round-tab-2")
    tab1.wait_for(state="visible", timeout=2_000)
    tab2.wait_for(state="visible", timeout=2_000)
    assert "Round 1" in (tab1.text_content() or "")
    assert "Round 2" in (tab2.text_content() or "")
    # Verdict label is rendered into the tab.
    assert "FAILED" in (tab1.text_content() or "")
    assert "PASSED" in (tab2.text_content() or "")


def test_round_tab_shows_per_round_verdict_and_timestamp(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Clicking round 1 reveals failing stories + diagnosis + fix commit."""

    _install_routes(page, detail=_detail_payload(round_count=2))
    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    _open_proof_drawer(page)
    page.get_by_test_id("proof-round-tabs").wait_for(state="visible", timeout=5_000)

    # Default: latest round is active. Switch to round 1 explicitly.
    page.get_by_test_id("proof-round-tab-1").click()

    detail = page.get_by_test_id("proof-round-detail-1")
    detail.wait_for(state="visible", timeout=2_000)
    assert "failed" in (page.get_by_test_id("proof-round-verdict").text_content() or "").lower()
    duration_text = page.get_by_test_id("proof-round-duration").text_content() or ""
    assert "12" in duration_text, duration_text
    diagnosis_text = page.get_by_test_id("proof-round-diagnosis").text_content() or ""
    assert "Restore did not apply" in diagnosis_text, diagnosis_text
    failing = page.get_by_test_id("proof-round-failing-restore-filter")
    failing.wait_for(state="visible", timeout=2_000)


def test_proof_provenance_renders_run_id_and_sha(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Provenance card shows generated_at, run_id, head_sha, sha256."""

    _install_routes(page, detail=_detail_payload(round_count=2))
    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    _open_proof_drawer(page)
    prov = page.get_by_test_id("proof-provenance")
    prov.wait_for(state="visible", timeout=5_000)
    assert RUN_ID in (page.get_by_test_id("proof-run-id").text_content() or "")
    assert "deadbee" in (page.get_by_test_id("proof-head-sha").text_content() or "")
    sha_text = page.get_by_test_id("proof-sha256").text_content() or ""
    assert "ab" * 6 in sha_text  # first 12 hex chars rendered


def test_proof_provenance_warns_on_run_id_mismatch(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """When proof.run_id != run record id, a mismatch banner is shown."""

    detail = _detail_payload(round_count=1)
    detail["review_packet"]["certification"]["proof_report"]["run_id"] = "stale-other-run"
    detail["review_packet"]["certification"]["proof_report"]["run_id_matches"] = False
    _install_routes(page, detail=detail)
    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)

    _open_proof_drawer(page)
    warning = page.get_by_test_id("proof-provenance-mismatch")
    warning.wait_for(state="visible", timeout=5_000)
    text = warning.text_content() or ""
    assert "stale-other-run" in text
    assert RUN_ID in text
