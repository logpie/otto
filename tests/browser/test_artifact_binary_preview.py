"""Browser tests for binary artifact preview handling.

Cluster ``codex-evidence-trustworthiness`` #6: artifact content was
always decoded as UTF-8 and rendered in a ``<pre>`` — binary evidence
came out as garbage. The server now MIME-detects and the client
renders image/video previews via ``<img>`` / ``<video>`` against the
``/raw`` endpoint, or shows a "no text preview; download artifact"
message when the type is otherwise non-previewable.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \
        uv run pytest tests/browser/test_artifact_binary_preview.py \
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser

RUN_ID = "binary-preview-run"


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
        "display_name": "build:bin",
        "status": "done",
        "terminal_outcome": "success",
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": None,
        "merge_id": None,
        "branch": "build/bin",
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "atomic.build",
        "version": 1,
        "display_status": "done",
        "active": False,
        "display_id": RUN_ID,
        "branch_task": "build/bin",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": 0.0,
        "cost_display": "$0.00",
        "last_event": "done",
        "row_label": "build:bin",
        "overlay": None,
    }


def _artifact(kind: str, label: str, path: str, index: int = 0) -> dict[str, Any]:
    return {
        "index": index,
        "label": label,
        "path": path,
        "kind": kind,
        "exists": True,
        "size_bytes": 4096,
        "mtime": "2026-04-25T12:00:00Z",
        "sha256": "ab" * 32,
    }


def _detail_payload(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    item = _live_item()
    return {
        **item,
        "source": "live",
        "title": "build: bin",
        "summary_lines": [],
        "overlay": None,
        "artifacts": artifacts,
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [],
        "review_packet": {
            "headline": "Ready",
            "status": "done",
            "summary": "binary preview",
            "readiness": {"state": "ready", "label": "Ready", "tone": "success", "blockers": [], "next_step": "Review."},
            "checks": [],
            "next_action": {"label": "review", "action_key": None, "enabled": False, "reason": None},
            "certification": {
                "stories_passed": 1,
                "stories_tested": 1,
                "passed": True,
                "summary_path": None,
                "stories": [],
                "rounds": [],
                "proof_report": {
                    "json_path": None,
                    "html_path": None,
                    "html_url": None,
                    "available": False,
                    "generated_at": None,
                    "run_id": None,
                    "session_id": None,
                    "branch": None,
                    "head_sha": None,
                    "file_mtime": None,
                    "sha256": None,
                    "run_id_matches": None,
                },
            },
            "changes": {
                "branch": "build/bin",
                "target": "main",
                "merged": False,
                "merge_id": None,
                "file_count": 0,
                "files": [],
                "truncated": False,
                "diff_command": None,
                "diff_error": None,
            },
            "evidence": artifacts,
            "failure": None,
        },
        "landing_state": None,
        "merge_info": None,
        "record": {},
    }


def _install_state_routes(page: Any, *, detail: dict[str, Any]) -> None:
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


def _install_artifact_content_route(
    page: Any,
    *,
    artifact_index: int,
    label: str,
    mime: str,
    previewable: bool,
    content: str = "",
) -> None:
    body = json.dumps({
        "artifact": {
            "index": artifact_index,
            "label": label,
            "path": "/tmp/proj/x/" + label,
            "kind": "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "binary",
            "exists": True,
            "size_bytes": 4096,
            "mtime": "2026-04-25T12:00:00Z",
            "sha256": "ab" * 32,
        },
        "content": content,
        "truncated": False,
        "previewable": previewable,
        "mime_type": mime,
        "size_bytes": 4096,
    })
    page.route(
        f"**/api/runs/{RUN_ID}/artifacts/{artifact_index}/content",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _install_raw_route(page: Any, *, artifact_index: int, mime: str) -> None:
    # The SPA loads the raw asset via <img src=...>; intercept and serve a
    # tiny stub body. Image element doesn't care about real bytes for the
    # purpose of asserting that the src points at this URL.
    page.route(
        f"**/api/runs/{RUN_ID}/artifacts/{artifact_index}/raw",
        lambda route: route.fulfill(status=200, content_type=mime, body=b"\x00\x00\x00\x00"),
    )


def _open_proof_drawer(page: Any) -> None:
    btn = page.get_by_test_id("open-proof-button")
    btn.wait_for(state="visible", timeout=5_000)
    btn.click()
    page.get_by_test_id("proof-pane").wait_for(state="visible", timeout=5_000)


def test_image_artifact_renders_as_img_tag(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A PNG evidence artifact loads via <img src=raw> instead of garbage <pre>."""

    artifact = _artifact("image", "screenshot.png", "/tmp/proj/x/shot.png")
    detail = _detail_payload([artifact])
    _install_state_routes(page, detail=detail)
    _install_artifact_content_route(
        page,
        artifact_index=0,
        label="screenshot.png",
        mime="image/png",
        previewable=False,
        content="",
    )
    _install_raw_route(page, artifact_index=0, mime="image/png")

    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    _open_proof_drawer(page)
    img = page.get_by_test_id("proof-evidence-image")
    img.wait_for(state="visible", timeout=5_000)
    src = img.get_attribute("src") or ""
    assert src.endswith(f"/api/runs/{RUN_ID}/artifacts/0/raw"), src
    # MIME label is rendered alongside the image preview.
    mime_label = page.get_by_test_id("proof-evidence-mime").text_content() or ""
    assert "image/png" in mime_label


def test_binary_artifact_shows_no_text_preview_message(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A non-image binary (PDF) shows the 'No text preview' card + download link."""

    artifact = _artifact("binary", "report.pdf", "/tmp/proj/x/report.pdf")
    detail = _detail_payload([artifact])
    _install_state_routes(page, detail=detail)
    _install_artifact_content_route(
        page,
        artifact_index=0,
        label="report.pdf",
        mime="application/pdf",
        previewable=False,
        content="",
    )
    _install_raw_route(page, artifact_index=0, mime="application/pdf")

    page.goto(f"{mc_backend.url}?view=tasks&run={RUN_ID}", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)

    _open_proof_drawer(page)
    no_preview = page.get_by_test_id("proof-evidence-no-preview")
    no_preview.wait_for(state="visible", timeout=5_000)
    text = no_preview.text_content() or ""
    assert "No text preview" in text
    download = page.get_by_test_id("proof-evidence-download")
    download.wait_for(state="visible", timeout=2_000)
    href = download.get_attribute("href") or ""
    assert href.endswith(f"/api/runs/{RUN_ID}/artifacts/0/raw"), href
