"""Browser regression for W1-IMPORTANT-2 — JobDialog validation hint must
disappear once the user has hit Submit, even before the POST resolves.

Source: live W1 dogfood run captured a screenshot showing the JobDialog
with `aria-busy=true` on the Queue button BUT the validation hint
"Submit is disabled." still rendered. The user reads "Submit is disabled"
right after they clicked Submit and assumes the click was lost. The hint's
visibility predicate did not exclude the 3-second pre-POST grace window
introduced by mc-audit codex-destructive-action-safety #7. See
``docs/mc-audit/live-findings.md`` (search "W1-IMPORTANT-2") and the fix
in ``otto/web/client/src/App.tsx`` JobDialog form (``submitDisabled &&
!submitting && pendingSeconds === null``).

Invariant pinned by this test: from the moment the user clicks "Queue
job" until either the grace timer expires OR the user hits Cancel, the
``job-dialog-validation-hint`` element must NOT be in the DOM.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_job_dialog_validation_clears_on_submit.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


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
            "items": [],
            "total_count": 0,
            "active_count": 0,
            "refresh_interval_s": 1.5,
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


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def test_validation_hint_hidden_during_grace_window(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Click Submit → validation hint must NOT show during the 3s grace window.

    Reproduces the W1-IMPORTANT-2 screenshot: the hint "Submit is disabled."
    appeared right after the user clicked Queue job. Pin the inverse — the
    hint element must not be in the DOM once submitting/grace has begun.
    """

    _install_projects_route(page)
    _install_state_route(page, _state())

    # Block the queue POST so we observe the in-flight render without the
    # POST resolving and tearing the dialog down.
    queue_calls: list[dict[str, Any]] = []

    def queue_handler(route: Any) -> None:
        queue_calls.append({"url": route.request.url})
        # Never call route.fulfill — leave the request hanging. After the
        # test completes Playwright will drop it.
        # We still satisfy the contract by aborting after a short pause so
        # the test can progress. Use abort so the SPA's catch-clause does
        # not surface a confusing 500-style error toast either way.
        route.abort()

    page.route("**/api/queue/build", queue_handler)

    _hydrate(mc_backend, page, disable_animations)

    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector(".job-dialog", timeout=5_000)

    intent = page.get_by_test_id("job-dialog-intent")
    intent.fill("ship a feature please")

    # Sanity: with intent filled and target clean, the hint should be gone.
    assert page.locator("[data-testid=job-dialog-validation-hint]").count() == 0, (
        "with valid intent + clean target the hint must already be hidden"
    )

    # Click Queue job — this enters the 3s grace window. The validation hint
    # must NOT appear under any circumstance during the grace window.
    page.locator(".job-dialog button[type=submit]").click()

    # Grace countdown should be visible.
    page.wait_for_selector("[data-testid=job-grace-countdown]", timeout=2_000)

    assert page.locator("[data-testid=job-dialog-validation-hint]").count() == 0, (
        "validation hint must be hidden during the 3s grace/in-flight window"
    )

    # Even after the queue POST aborts (dialog stays open since submit failed),
    # the hint should re-appear only if validation actually fails — which it
    # doesn't here. So still hidden.
    # Intent is still valid + project clean → no validation hint.
    assert page.locator("[data-testid=job-dialog-validation-hint]").count() == 0


def test_validation_hint_returns_after_grace_cancel(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Defensive: cancel during grace window + clear intent → hint reappears.

    Pins the negative case: the fix only suppresses the hint while a submit
    is in flight (grace or POST). After the user cancels and the form is
    invalid again, the hint must still surface.
    """

    _install_projects_route(page)
    _install_state_route(page, _state())

    _hydrate(mc_backend, page, disable_animations)

    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector(".job-dialog", timeout=5_000)

    # Initially intent is empty → hint visible.
    page.wait_for_selector("[data-testid=job-dialog-validation-hint]", timeout=2_000)

    # Fill intent so submit is enabled.
    page.get_by_test_id("job-dialog-intent").fill("ship a feature please")
    assert page.locator("[data-testid=job-dialog-validation-hint]").count() == 0

    # Submit → grace begins → hint hidden.
    page.locator(".job-dialog button[type=submit]").click()
    page.wait_for_selector("[data-testid=job-grace-countdown]", timeout=2_000)
    assert page.locator("[data-testid=job-dialog-validation-hint]").count() == 0

    # Cancel the grace window.
    page.locator("[data-testid=job-grace-cancel-button]").click(timeout=2_000)

    # Clear intent. Now the form is invalid again.
    page.get_by_test_id("job-dialog-intent").fill("")

    # Hint must reappear (not stuck-hidden by our fix).
    page.wait_for_selector("[data-testid=job-dialog-validation-hint]", timeout=2_000)
