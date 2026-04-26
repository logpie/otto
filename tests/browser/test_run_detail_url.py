"""Browser regression for the per-run detail URL builder.

Bugs W2-IMPORTANT-1 / W13-IMPORTANT-1 (mc-audit live findings, 2026-04-26):

    The SPA called the run-detail endpoint via
    ``/api/runs/queue-compat:<task>?type=all&outcome=all&query=&active_only=false&history_page_size=25``
    which returned 404. Root cause: ``refreshDetail`` reused
    ``stateQueryParams`` (built for the ``/api/state`` filter pane) and so
    appended state-pane filters (``type`` / ``outcome`` / ``query`` /
    ``active_only`` / ``history_page``) to a per-run URL. With synthetic
    queue-compat run-ids those parameters mis-routed the request on the
    server.

The fix introduces ``runDetailUrl`` in ``otto/web/client/src/api.ts`` —
a detail-specific URL builder that intentionally drops state-pane
filters and only forwards ``history_page_size``. ``App.tsx`` calls it
from ``refreshDetail``.

These tests trigger a run-detail fetch, intercept the request, and
assert the URL is well-formed.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 uv run pytest \\
        tests/browser/test_run_detail_url.py -m browser -p playwright -v
"""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------------- #
# Synthetic payload helpers (same shape used by other browser tests).
# --------------------------------------------------------------------------- #


def _live_item(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "domain": "queue",
        "run_type": "queue",
        "command": "otto build",
        "display_name": f"queue:{run_id}",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": "create-a-tiny-calculator-html-page-with-c53170",
        "merge_id": None,
        "branch": None,
        "worktree": None,
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": run_id,
        "branch_task": None,
        "elapsed_s": 5.0,
        "elapsed_display": "5s",
        "cost_usd": None,
        "cost_display": "-",
        "last_event": "running",
        "row_label": f"queue:{run_id}",
        "overlay": None,
    }


def _state_payload(run_id: str) -> dict[str, Any]:
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
            "counts": {"queued": 0, "running": 1},
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
            "counts": {"ready": 0, "merged": 0, "total": 0},
            "merge_blocked": False,
            "dirty_files": [],
            "target": "main",
        },
        "live": {
            "items": [_live_item(run_id)],
            "total_count": 1,
            "active_count": 1,
            "refresh_interval_s": 1.5,
        },
        "history": {"items": [], "page": 1, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"items": [], "total_count": 0, "malformed_count": 0},
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
                "can_start": False,
                "can_stop": True,
                "start_blocked_reason": None,
                "stop_blocked_reason": None,
            },
            "issues": [],
        },
    }


def _detail_payload(run_id: str) -> dict[str, Any]:
    item = _live_item(run_id)
    return {
        **item,
        "source": "live",
        "title": f"queue: {run_id}",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [],
        "review_packet": {
            "headline": "Queue run",
            "status": "running",
            "summary": "",
            "readiness": {
                "state": "in_progress",
                "label": "running",
                "tone": "info",
                "blockers": [],
                "next_step": "",
            },
            "checks": [],
            "next_action": {"label": "", "action_key": None, "enabled": False, "reason": None},
            "certification": {
                "stories_passed": 0,
                "stories_tested": 0,
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


def _install_state_route(page: Any, run_id: str) -> None:
    body = json.dumps(_state_payload(run_id))
    page.route(
        "**/api/state*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


# --------------------------------------------------------------------------- #
# Detail-route capturer — records every URL that hits /api/runs/<id>.
# --------------------------------------------------------------------------- #


class _DetailCapture:
    """Captures every per-run detail request and replies with a fixed body."""

    # State-pane filter param names that must NEVER appear on a detail URL.
    FORBIDDEN_PARAMS = ("type", "outcome", "query", "active_only", "history_page")
    # Whitelist of params that are allowed on a detail URL. Anything else
    # is a regression.
    ALLOWED_PARAMS = ("history_page_size",)

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.urls: list[str] = []
        self.queries: list[dict[str, list[str]]] = []
        self.statuses: list[int] = []
        self._lock = threading.Lock()
        self._body = json.dumps(_detail_payload(run_id))

    def install(self, page: Any) -> None:
        page.route("**/api/runs/**", self._handle)

    def _handle(self, route: Any) -> None:
        url = route.request.url
        parsed = urlparse(url)
        # Skip child endpoints (/logs, /diff, /artifacts, /actions/...).
        # We only want the bare detail route here.
        path = parsed.path
        if not path.endswith(self._encoded_run_id_suffix()) and not path.endswith(self.run_id):
            # Not the bare detail call — child endpoints may pass through.
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps({})
            )
            return
        with self._lock:
            self.urls.append(url)
            self.queries.append(parse_qs(parsed.query, keep_blank_values=True))
            self.statuses.append(200)
        route.fulfill(status=200, content_type="application/json", body=self._body)

    def _encoded_run_id_suffix(self) -> str:
        # The browser percent-encodes the run-id (queue-compat:foo -> queue-compat%3Afoo).
        # We compare against the encoded form by re-encoding the colon manually.
        return self.run_id.replace(":", "%3A")

    def assert_no_state_filters(self) -> None:
        for query in self.queries:
            for forbidden in self.FORBIDDEN_PARAMS:
                assert forbidden not in query, (
                    f"detail URL must not include state-pane param {forbidden!r}; "
                    f"saw query={query!r}"
                )

    def assert_only_whitelisted(self) -> None:
        for query in self.queries:
            for key in query:
                assert key in self.ALLOWED_PARAMS, (
                    f"detail URL contained unexpected param {key!r}; "
                    f"only {self.ALLOWED_PARAMS!r} are allowed; saw query={query!r}"
                )

    def assert_run_id_in_path(self, expected: str) -> None:
        assert self.urls, "no detail URL was captured"
        for url in self.urls:
            decoded_path = unquote(urlparse(url).path)
            assert decoded_path.endswith(expected), (
                f"expected URL path to end with run-id {expected!r}; got path={decoded_path!r}"
            )


# --------------------------------------------------------------------------- #
# Common page setup.
# --------------------------------------------------------------------------- #


def _hydrate_with_run(page: Any, mc_backend: Any, disable_animations: Any, run_id: str) -> None:
    # `?run=<id>` causes the SPA to start with that run selected, which
    # triggers `refreshDetail` on first hydration.
    page.goto(f"{mc_backend.url}?view=tasks&run={run_id}", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def _wait_for_detail_capture(capture: _DetailCapture, page: Any, *, min_count: int = 1) -> None:
    page.wait_for_function(
        # We don't have a JS-side hook to introspect the route capture, so
        # poll briefly. Each hydration triggers exactly one detail fetch
        # for the selected run.
        "() => true",
        timeout=200,
    )
    # Up to ~3s for the detail fetch to actually happen.
    deadline_ms = 3_000
    elapsed = 0
    step = 100
    while elapsed < deadline_ms:
        if len(capture.urls) >= min_count:
            return
        page.wait_for_timeout(step)
        elapsed += step
    raise AssertionError(
        f"timed out waiting for detail capture; got {len(capture.urls)} urls, "
        f"expected at least {min_count}"
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_run_detail_url_does_not_include_state_filters(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Bug W2-IMPORTANT-1: detail URL must not carry /api/state filter params.

    The SPA used to append `type=...&outcome=...&query=...&active_only=...
    &history_page=...` to the per-run detail URL because it reused the
    state-pane query builder. Verify those params are stripped.
    """

    run_id = "queue-compat:create-a-tiny-calculator-html-page-with-c53170"
    capture = _DetailCapture(run_id)
    _install_projects_route(page)
    _install_state_route(page, run_id)
    capture.install(page)

    _hydrate_with_run(page, mc_backend, disable_animations, run_id)
    _wait_for_detail_capture(capture, page)

    capture.assert_no_state_filters()


def test_run_detail_url_includes_only_relevant_params(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Whitelist guard: only `history_page_size` may appear on a detail URL.

    If a future change adds a new query param to the detail URL, this test
    will flag it. That's intentional — adding state to per-run URLs has
    repeatedly caused 404s on synthetic run-ids; new params should
    require an explicit decision.
    """

    run_id = "queue-compat:my-task-abc123"
    capture = _DetailCapture(run_id)
    _install_projects_route(page)
    _install_state_route(page, run_id)
    capture.install(page)

    _hydrate_with_run(page, mc_backend, disable_animations, run_id)
    _wait_for_detail_capture(capture, page)

    capture.assert_only_whitelisted()


def test_run_detail_url_resolves_with_queue_compat_id(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Per-run URL must be well-formed for queue-compat ids (containing `:`).

    The run-id `queue-compat:<task>` contains a colon. The SPA must
    URL-encode it to `queue-compat%3A<task>` so the path component is
    well-formed. The captured URL also must not return 404 — we control
    the response in this test (200), but the URL shape itself is the
    contract under test.
    """

    run_id = "queue-compat:build-a-small-todo-list-app-with-html-bb07f8"
    capture = _DetailCapture(run_id)
    _install_projects_route(page)
    _install_state_route(page, run_id)
    capture.install(page)

    _hydrate_with_run(page, mc_backend, disable_animations, run_id)
    _wait_for_detail_capture(capture, page)

    # URL-decoded path must end with the literal run id (colon and all).
    capture.assert_run_id_in_path(f"/api/runs/{run_id}")
    # And the encoded form must contain `%3A` (the encoded colon).
    assert any("%3A" in url for url in capture.urls), (
        f"expected at least one captured URL to contain encoded colon; got urls={capture.urls!r}"
    )
    # And every captured response was 200 (we own the route — sanity check
    # we never accidentally 404'd).
    assert all(status == 200 for status in capture.statuses), (
        f"expected all detail responses to be 200; got statuses={capture.statuses!r}"
    )
