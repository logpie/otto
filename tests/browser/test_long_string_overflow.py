"""Browser regression tests for the long-string / overflow cluster.

Source: ``docs/mc-audit/_hunter-findings/codex-long-string-overflow.md``,
findings #6, #7, #8, #9, #10, #12, #14 (all IMPORTANT, all CSS-leaning).

The fixes live in ``otto/web/client/src/styles.css`` (plus a ``title``
attribute on the diff toolbar branch span). They establish a shared
wrap-anywhere policy and per-component max-heights so that 500-char
URLs / branch names / API errors can never push a sibling control
offscreen or trigger document-level horizontal scroll.

Each test stubs a synthetic state response carrying a long token in
the relevant field, renders the affected element, and asserts:

  1. The element's ``getBoundingClientRect()`` does not exceed the
     parent's available width (the long string wrapped instead of
     stretching its container).
  2. ``document.documentElement.scrollWidth`` is ``<= clientWidth``
     (no horizontal scroll on the page itself).
  3. The full string remains readable (the rendered ``textContent``
     still contains the long marker token, i.e. the fix did not
     truncate via JS — only via CSS line-clamp / overflow-y).

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_long_string_overflow.py \\
            -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Long-string fixtures
# ---------------------------------------------------------------------------

# 500-char single-token strings — no spaces, so wrapping must be character-
# level (overflow-wrap: anywhere). The "MARKER" sentinel lets a test grep
# the rendered DOM to prove the string was actually rendered, not silently
# replaced or truncated to "...".
LONG_URL = "https://example.com/" + ("aMARKER" + "x" * 7) * 40
LONG_ERROR = "ERROR-MARKER-" + ("a" * 487)
LONG_BRANCH = "feature/" + ("longBranchMARKER-" + "y" * 12) * 20
LONG_HEADLINE = "HEADLINE-MARKER-" + ("z" * 484)
LONG_PATH = "src/" + ("deep/path/MARKER/" + "p" * 4) * 30 + "/file.ts"

# Sanity: assert the constants are at least 500 chars so the test
# environment matches the spec ("500-char URL/error").
assert len(LONG_URL) >= 500, len(LONG_URL)
assert len(LONG_ERROR) >= 500, len(LONG_ERROR)
assert len(LONG_BRANCH) >= 500, len(LONG_BRANCH)
assert len(LONG_HEADLINE) >= 500, len(LONG_HEADLINE)
assert len(LONG_PATH) >= 500, len(LONG_PATH)


SAMPLE_TARGET = "main"


# ---------------------------------------------------------------------------
# State payload helpers
# ---------------------------------------------------------------------------


def _empty_landing() -> dict[str, Any]:
    return {
        "items": [],
        "counts": {"ready": 0, "merged": 0, "blocked": 0, "total": 0},
        "merge_blocked": False,
        "merge_blockers": [],
        "dirty_files": [],
        "target": SAMPLE_TARGET,
        "collisions": [],
    }


def _ready_landing(branch: str, files: list[str]) -> dict[str, Any]:
    item = {
        "task_id": "task-01",
        "run_id": "run-01",
        "branch": branch,
        "worktree": ".worktrees/task-01",
        "summary": "task 1",
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
        "changed_file_count": len(files),
        "changed_files": files,
        "diff_error": None,
    }
    return {
        "items": [item],
        "counts": {"ready": 1, "merged": 0, "blocked": 0, "total": 1},
        "merge_blocked": False,
        "merge_blockers": [],
        "dirty_files": [],
        "target": SAMPLE_TARGET,
        "collisions": [],
    }


def _state(
    *,
    landing: dict[str, Any] | None = None,
    live_items: list[dict[str, Any]] | None = None,
    history_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "landing": landing or _empty_landing(),
        "live": {
            "items": live_items or [],
            "total_count": len(live_items or []),
            "active_count": 0,
            "refresh_interval_s": 1.5,
        },
        "history": {
            "items": history_items or [],
            "page": 0,
            "page_size": 25,
            "total_rows": len(history_items or []),
            "total_pages": 1,
        },
        "events": {
            "path": "",
            "items": [],
            "total_count": 0,
            "malformed_count": 0,
            "limit": 80,
            "truncated": False,
        },
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


def _live_item(branch: str) -> dict[str, Any]:
    return {
        "run_id": "run-01",
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": "task-01",
        "status": "running",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "queue_task_id": "task-01",
        "merge_id": None,
        "branch": branch,
        "worktree": ".worktrees/task-01",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "running",
        "active": True,
        "display_id": "run-01",
        "branch_task": "task-01",
        "elapsed_s": 12.0,
        "elapsed_display": "12s",
        "cost_usd": 0.0,
        "cost_display": "$0.00",
        "last_event": "running",
        "row_label": "task-01",
        "overlay": None,
    }


def _run_detail(
    *,
    branch: str | None = None,
    headline: str = "Headline",
    summary: str = "Summary",
    files: list[str] | None = None,
    diff_command: str | None = None,
    failure_reason: str | None = None,
    readiness_state: str = "needs_attention",
    readiness_label: str = "Needs review",
    readiness_tone: str = "warning",
) -> dict[str, Any]:
    files_list = files or []
    return {
        "run_id": "run-01",
        "queue_task_id": "task-01",
        "merge_id": None,
        "domain": "queue",
        "run_type": "queue",
        "command": "build",
        "display_name": "task-01",
        "status": "needs_review",
        "terminal_outcome": None,
        "project_dir": "/tmp/proj",
        "cwd": "/tmp/proj",
        "branch": branch,
        "worktree": ".worktrees/task-01",
        "provider": "claude",
        "model": None,
        "reasoning_effort": None,
        "adapter_key": "queue.attempt",
        "version": 1,
        "display_status": "needs_review",
        "active": False,
        "source": "live",
        "title": "Run 01",
        "summary_lines": [],
        "overlay": None,
        "artifacts": [],
        "log_paths": [],
        "selected_log_index": 0,
        "selected_log_path": None,
        "legal_actions": [],
        "review_packet": {
            "headline": headline,
            "status": "needs_review",
            "summary": summary,
            "readiness": {
                "state": readiness_state,
                "label": readiness_label,
                "tone": readiness_tone,
                "blockers": [],
                "next_step": "Inspect the diff and either approve or reject.",
            },
            "checks": [
                {
                    "key": "story",
                    "label": "Story check",
                    "status": "warn",
                    "detail": (
                        "CHECK-MARKER-" + ("c" * 480)
                    ),
                }
            ],
            "next_action": {
                "label": "Land",
                "action_key": None,
                "enabled": False,
                "reason": None,
            },
            "certification": {
                "stories_passed": 1,
                "stories_tested": 1,
                "passed": True,
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
                "branch": branch,
                "target": SAMPLE_TARGET,
                "merged": False,
                "merge_id": None,
                "file_count": len(files_list),
                "files": files_list,
                "truncated": False,
                "diff_command": diff_command,
                "diff_error": None,
            },
            "evidence": [],
            "failure": (
                {
                    "reason": failure_reason,
                    "last_event": None,
                    "excerpt": None,
                    "source": None,
                }
                if failure_reason is not None
                else None
            ),
        },
        "landing_state": "ready",
        "merge_info": None,
        "record": {},
    }


def _diff_response(*, branch: str, files: list[str]) -> dict[str, Any]:
    text = "\n".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1 @@\n+content\n"
        for p in files
    )
    return {
        "run_id": "run-01",
        "branch": branch,
        "target": SAMPLE_TARGET,
        "branch_sha": "deadbeef",
        "target_sha": "cafebabe",
        "text": text,
        "file_count": len(files),
        "files": files,
        "shown_hunks": len(files),
        "total_hunks": len(files),
        "full_size_chars": len(text),
        "fetched_at": "2026-04-25T12:00:00Z",
        "command": "git diff",
        "error": None,
    }


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _install_projects_route(page: Any) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "launcher_enabled": False,
                    "projects_root": "",
                    "current": None,
                    "projects": [],
                }
            ),
        )

    page.route("**/api/projects", handler)


def _install_state_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/state*", handler)


def _install_run_detail_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/runs/*", handler)


def _install_diff_route(page: Any, payload: dict[str, Any]) -> None:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/runs/*/diff", handler)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


def _hydrate_diagnostics(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Navigate to the diagnostics view where the LiveRuns table renders the
    ``live-row-activator-<run_id>`` buttons used to open the inspector."""

    page.goto(f"{mc_backend.url}/?view=diagnostics", wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


# ---------------------------------------------------------------------------
# Generic invariant assertions
# ---------------------------------------------------------------------------


def _assert_no_doc_horizontal_scroll(page: Any) -> None:
    overflow = page.evaluate(
        "() => ({"
        "scrollWidth: document.documentElement.scrollWidth, "
        "clientWidth: document.documentElement.clientWidth"
        "})"
    )
    assert overflow["scrollWidth"] <= overflow["clientWidth"] + 1, (
        f"document overflow detected: scrollWidth={overflow['scrollWidth']} "
        f"> clientWidth={overflow['clientWidth']}"
    )


def _assert_fits_in_parent(page: Any, selector: str) -> None:
    """Assert the element at ``selector`` does not extend past its parent's
    inner width. We check the *content* width — if the element is itself
    scrollable we trust its overflow:auto contains the long token, but its
    *box* must remain inside the parent.
    """

    geometry = page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          const parent = el.parentElement;
          if (!parent) return null;
          const eRect = el.getBoundingClientRect();
          const pRect = parent.getBoundingClientRect();
          return {
            elRight: eRect.right,
            elLeft: eRect.left,
            elWidth: eRect.width,
            parentRight: pRect.right,
            parentLeft: pRect.left,
            parentInnerWidth: parent.clientWidth,
          };
        }""",
        selector,
    )
    assert geometry is not None, f"selector not found: {selector}"
    # 1px tolerance for sub-pixel rounding
    assert geometry["elRight"] <= geometry["parentRight"] + 1, (
        f"{selector} extends beyond parent: "
        f"el.right={geometry['elRight']} > parent.right={geometry['parentRight']}"
    )
    assert geometry["elLeft"] >= geometry["parentLeft"] - 1, (
        f"{selector} extends beyond parent on the left: "
        f"el.left={geometry['elLeft']} < parent.left={geometry['parentLeft']}"
    )


def _element_text_contains(page: Any, selector: str, marker: str) -> bool:
    return page.evaluate(
        """(args) => {
          const el = document.querySelector(args.sel);
          if (!el) return false;
          return (el.textContent || '').includes(args.marker);
        }""",
        {"sel": selector, "marker": marker},
    )


# ---------------------------------------------------------------------------
# #6 — Toast wraps long URL/error
# ---------------------------------------------------------------------------


def test_toast_wraps_long_url(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A 500-char single-token toast message must wrap, never push offscreen.

    The toast component normally appears via ``showToast`` in response to API
    errors. To exercise the styling deterministically we render the same
    ``#toast`` markup the app would, then assert CSS contains it.
    """

    payload = _state()
    _install_projects_route(page)
    _install_state_route(page, payload)
    _hydrate(mc_backend, page, disable_animations)

    # Render the toast directly using the same className the app applies in
    # showToast — this is a CSS-only invariant test.
    page.evaluate(
        """(message) => {
          const root = document.getElementById('root');
          const el = document.createElement('div');
          el.id = 'toast';
          el.className = 'visible toast-error';
          el.setAttribute('role', 'status');
          el.setAttribute('aria-live', 'polite');
          el.textContent = message;
          root.appendChild(el);
        }""",
        LONG_URL,
    )

    # 1) document does not gain a horizontal scrollbar
    _assert_no_doc_horizontal_scroll(page)

    # 2) toast box width is bounded by viewport (max-width rule already
    # caps at min(420px, 100vw - 36px); we re-prove it via measurement).
    rect = page.evaluate(
        """() => {
          const el = document.getElementById('toast');
          const r = el.getBoundingClientRect();
          return {width: r.width, vw: window.innerWidth};
        }"""
    )
    assert rect["width"] <= rect["vw"], (
        f"toast width {rect['width']} exceeds viewport {rect['vw']}"
    )

    # 3) full string is still rendered (no JS truncation)
    assert _element_text_contains(page, "#toast", "MARKER"), (
        "toast must still render the long URL — fix is CSS, not JS truncation"
    )

    # 4) wrap/scroll behaviour: with overflow-wrap:anywhere applied, the
    # toast height must exceed a single-line height (the long token wrapped
    # to multiple visual lines instead of overflowing horizontally).
    height = page.evaluate(
        "document.getElementById('toast').getBoundingClientRect().height"
    )
    assert height > 20, (
        f"toast height {height}px suggests the long URL did not wrap"
    )


# ---------------------------------------------------------------------------
# #7 — Last-error / result banner clamps long messages
# ---------------------------------------------------------------------------


def test_last_error_banner_clamps_long_message(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A long ``lastError`` value must wrap-anywhere and the banner span must
    cap with internal scroll so the dismiss button stays reachable.

    We synthesise the banner directly because lastError is set from React
    state that we cannot drive without React DevTools. The CSS rule is what
    we are pinning down.
    """

    payload = _state()
    _install_projects_route(page)
    _install_state_route(page, payload)
    _hydrate(mc_backend, page, disable_animations)

    page.evaluate(
        """(message) => {
          const root = document.getElementById('root');
          const wrap = document.createElement('div');
          wrap.style.maxWidth = '600px';
          wrap.id = 'banner-host';
          wrap.innerHTML = `
            <div class="status-banner error" data-testid="synthetic-banner">
              <strong>Last error</strong>
              <span data-testid="synthetic-banner-span"></span>
              <button type="button" data-testid="synthetic-banner-dismiss">Dismiss</button>
            </div>`;
          wrap.querySelector('[data-testid=synthetic-banner-span]').textContent = message;
          root.appendChild(wrap);
        }""",
        LONG_ERROR,
    )

    _assert_no_doc_horizontal_scroll(page)
    _assert_fits_in_parent(page, "[data-testid=synthetic-banner]")

    # Dismiss button must remain within the banner row
    button_geom = page.evaluate(
        """() => {
          const btn = document.querySelector('[data-testid=synthetic-banner-dismiss]');
          const banner = document.querySelector('[data-testid=synthetic-banner]');
          const br = banner.getBoundingClientRect();
          const btnR = btn.getBoundingClientRect();
          return {
            btnRight: btnR.right,
            bannerRight: br.right,
            btnVisible: btnR.width > 0 && btnR.height > 0,
          };
        }"""
    )
    assert button_geom["btnVisible"], "dismiss button must remain visible"
    assert button_geom["btnRight"] <= button_geom["bannerRight"] + 1, (
        "dismiss button must remain inside the banner — long error should not push it offscreen"
    )

    # Span max-height clamp engages: the rendered span must not exceed the
    # ~9.6em cap (with 12px font-size that's ~115px). Use 200px as a loose
    # ceiling that still proves clamping.
    span_height = page.evaluate(
        """() => document.querySelector('[data-testid=synthetic-banner-span]').getBoundingClientRect().height"""
    )
    assert span_height <= 200, (
        f"banner span height {span_height}px — max-height clamp did not engage"
    )

    # Full string still readable (just hidden behind internal scroll).
    assert _element_text_contains(
        page, "[data-testid=synthetic-banner-span]", "ERROR-MARKER"
    )


# ---------------------------------------------------------------------------
# #8 — Job dialog status does not push submit offscreen
# ---------------------------------------------------------------------------


def test_job_dialog_status_does_not_push_submit_offscreen(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """A long footer-status message in JobDialog must wrap; submit button must
    remain inside the dialog.
    """

    payload = _state()
    _install_projects_route(page)
    _install_state_route(page, payload)
    _hydrate(mc_backend, page, disable_animations)

    # Open the dialog
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector(".job-dialog", timeout=5_000)

    # Inject a long status into the live #jobDialogStatus span. The footer
    # is a flex row; the fix wraps the status span and lets it scroll-y.
    page.evaluate(
        """(status) => {
          const el = document.getElementById('jobDialogStatus');
          el.textContent = status;
        }""",
        LONG_ERROR,
    )

    _assert_no_doc_horizontal_scroll(page)

    # Submit button must remain inside the dialog content area horizontally.
    geom = page.evaluate(
        """() => {
          const dialog = document.querySelector('.job-dialog');
          const submit = document.querySelector('[data-testid=job-dialog-submit-button]');
          const dr = dialog.getBoundingClientRect();
          const sr = submit.getBoundingClientRect();
          return {
            submitRight: sr.right,
            submitLeft: sr.left,
            submitWidth: sr.width,
            dialogRight: dr.right,
            dialogLeft: dr.left,
          };
        }"""
    )
    assert geom["submitWidth"] > 0, "submit button must be rendered"
    assert geom["submitRight"] <= geom["dialogRight"] + 1, (
        f"submit button pushed past dialog right edge: "
        f"submit.right={geom['submitRight']} > dialog.right={geom['dialogRight']}"
    )
    assert geom["submitLeft"] >= geom["dialogLeft"] - 1, (
        f"submit button pushed past dialog left edge: "
        f"submit.left={geom['submitLeft']} < dialog.left={geom['dialogLeft']}"
    )

    # Status max-height engages so footer height stays manageable.
    status_height = page.evaluate(
        "document.getElementById('jobDialogStatus').getBoundingClientRect().height"
    )
    assert status_height <= 200, (
        f"status height {status_height}px — max-height clamp did not engage"
    )

    # Full string remains in the DOM (CSS overflow, not JS truncation).
    assert _element_text_contains(page, "#jobDialogStatus", "ERROR-MARKER")


# ---------------------------------------------------------------------------
# #9 — Confirm dialog wraps long branch names
# ---------------------------------------------------------------------------


def test_confirm_dialog_wraps_long_branch_names(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Bulk-land confirm dialog with a long branch name must not overflow.

    Triggers the confirm dialog via the Land Ready CTA, then verifies the
    rendered branch token (inside ``<code>``) wraps within the dialog.
    """

    landing = _ready_landing(branch=LONG_BRANCH, files=[LONG_PATH])
    payload = _state(
        landing=landing,
        live_items=[_live_item(branch=LONG_BRANCH)],
    )
    _install_projects_route(page)
    _install_state_route(page, payload)
    # Run-detail / diff are fetched on selection but we only need the
    # confirm dialog, which is driven by `landing`.
    _install_run_detail_route(
        page,
        _run_detail(branch=LONG_BRANCH, files=[LONG_PATH]),
    )
    _hydrate(mc_backend, page, disable_animations)

    land_btn = page.get_by_test_id("mission-land-ready-button")
    land_btn.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('[data-testid=mission-land-ready-button]'); return b && !b.disabled; }",
        timeout=5_000,
    )
    land_btn.click()

    page.wait_for_selector(".confirm-dialog", timeout=5_000)

    _assert_no_doc_horizontal_scroll(page)

    # The bulk row must fit within the dialog body.
    bulk_geom = page.evaluate(
        """() => {
          const row = document.querySelector('[data-testid^=confirm-bulk-row-]');
          if (!row) return null;
          const dialog = document.querySelector('.confirm-dialog');
          const rr = row.getBoundingClientRect();
          const dr = dialog.getBoundingClientRect();
          return {
            rowRight: rr.right,
            rowLeft: rr.left,
            dialogRight: dr.right,
            dialogLeft: dr.left,
          };
        }"""
    )
    assert bulk_geom is not None, "bulk-row must be rendered"
    assert bulk_geom["rowRight"] <= bulk_geom["dialogRight"] + 1, (
        f"bulk row overflows dialog right edge: {bulk_geom}"
    )
    assert bulk_geom["rowLeft"] >= bulk_geom["dialogLeft"] - 1, (
        f"bulk row overflows dialog left edge: {bulk_geom}"
    )

    # The branch name was actually rendered (no truncation).
    has_marker = page.evaluate(
        """() => {
          const code = document.querySelector('.confirm-bulk-row-head code');
          return !!code && (code.textContent || '').includes('MARKER');
        }"""
    )
    assert has_marker, "long branch name must render in confirm bulk row"


# ---------------------------------------------------------------------------
# #10 — Review packet long text wraps consistently
# ---------------------------------------------------------------------------


def test_review_packet_long_text_wraps_consistently(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Long review-packet headline / summary / check detail must wrap and
    stay inside the inspector column.
    """

    payload = _state(live_items=[_live_item(branch="feature/x")])
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_run_detail_route(
        page,
        _run_detail(
            branch="feature/x",
            headline=LONG_HEADLINE,
            summary="SUMMARY-MARKER-" + ("s" * 488),
            files=[LONG_PATH],
        ),
    )
    _hydrate_diagnostics(mc_backend, page, disable_animations)

    # Open inspector via the live row activator (LiveRuns table is in
    # the diagnostics view).
    page.locator("[data-testid=live-row-activator-run-01]").click()
    page.wait_for_selector(".review-packet", timeout=5_000)

    _assert_no_doc_horizontal_scroll(page)

    # Headline + summary fit within review-head's column.
    head_geom = page.evaluate(
        """() => {
          const head = document.querySelector('.review-head > div');
          if (!head) return null;
          const strong = head.querySelector('strong');
          const span = head.querySelector('span[title]') || head.querySelectorAll('span')[1];
          const hr = head.getBoundingClientRect();
          return {
            headRight: hr.right,
            headLeft: hr.left,
            strongRight: strong.getBoundingClientRect().right,
            spanRight: span ? span.getBoundingClientRect().right : hr.right,
          };
        }"""
    )
    assert head_geom is not None
    assert head_geom["strongRight"] <= head_geom["headRight"] + 1, (
        f"headline overflows review-head column: {head_geom}"
    )
    assert head_geom["spanRight"] <= head_geom["headRight"] + 1, (
        f"summary overflows review-head column: {head_geom}"
    )

    # Markers rendered (no truncation)
    headline_text = page.evaluate(
        "document.querySelector('.review-head strong').textContent"
    )
    assert "HEADLINE-MARKER" in headline_text

    # Check detail wraps too
    check_geom = page.evaluate(
        """() => {
          const checks = document.querySelectorAll('.review-check');
          if (!checks.length) return null;
          const c = checks[0];
          const p = c.querySelector('p');
          const cr = c.getBoundingClientRect();
          const pr = p.getBoundingClientRect();
          return {checkRight: cr.right, pRight: pr.right};
        }"""
    )
    if check_geom is not None:
        assert check_geom["pRight"] <= check_geom["checkRight"] + 1, (
            f"check detail overflows review-check column: {check_geom}"
        )


# ---------------------------------------------------------------------------
# #12 — Changed-file list wraps long paths
# ---------------------------------------------------------------------------


def test_changed_file_list_wraps_long_paths(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Long file paths in the review packet's Changed Files drawer and the
    diff_command code block must wrap, never overflow.
    """

    payload = _state(live_items=[_live_item(branch="feature/x")])
    _install_projects_route(page)
    _install_state_route(page, payload)
    _install_run_detail_route(
        page,
        _run_detail(
            branch="feature/x",
            files=[LONG_PATH, "src/short/file.ts"],
            diff_command="git -c core.pager=cat diff --no-color " + ("longArg-MARKER-" + "z" * 12) * 12,
            readiness_state="ready",
            readiness_label="Ready to land",
            readiness_tone="success",
        ),
    )
    _hydrate_diagnostics(mc_backend, page, disable_animations)

    page.locator("[data-testid=live-row-activator-run-01]").click()
    page.wait_for_selector(".review-packet", timeout=5_000)

    # Open the "Changed files" drawer (a <details>). Click its summary.
    page.evaluate(
        """() => {
          for (const det of document.querySelectorAll('.review-drawer')) {
            const sum = det.querySelector('summary');
            if (sum && sum.textContent && sum.textContent.includes('Changed files')) {
              det.open = true;
            }
          }
        }"""
    )

    _assert_no_doc_horizontal_scroll(page)

    # File <li> must fit within its <ul>
    li_geom = page.evaluate(
        """() => {
          const li = document.querySelector('.review-files li');
          if (!li) return null;
          const ul = li.parentElement;
          return {
            liRight: li.getBoundingClientRect().right,
            ulRight: ul.getBoundingClientRect().right,
            ulInnerRight: ul.getBoundingClientRect().left + ul.clientWidth,
          };
        }"""
    )
    assert li_geom is not None, "review-files li must render"
    assert li_geom["liRight"] <= li_geom["ulRight"] + 1, (
        f"changed-file <li> overflows its <ul>: {li_geom}"
    )

    # diff_command <code> is now display:block — verify it wraps within the
    # drawer and doesn't trigger a horizontal scrollbar inside its parent.
    code_geom = page.evaluate(
        """() => {
          const code = document.querySelector('.review-packet code');
          if (!code) return null;
          const parent = code.parentElement;
          const cs = window.getComputedStyle(code);
          return {
            display: cs.display,
            whiteSpace: cs.whiteSpace,
            codeRight: code.getBoundingClientRect().right,
            parentRight: parent.getBoundingClientRect().right,
            parentScrollWidth: parent.scrollWidth,
            parentClientWidth: parent.clientWidth,
          };
        }"""
    )
    assert code_geom is not None, "diff_command <code> must render"
    assert code_geom["display"] == "block", (
        f"diff_command code must be display:block, got {code_geom['display']}"
    )
    assert code_geom["codeRight"] <= code_geom["parentRight"] + 1, (
        f"diff_command code overflows parent: {code_geom}"
    )
    assert code_geom["parentScrollWidth"] <= code_geom["parentClientWidth"] + 1, (
        f"diff_command parent has horizontal overflow: {code_geom}"
    )

    # Both markers rendered
    li_text = page.evaluate(
        "Array.from(document.querySelectorAll('.review-files li')).map(l => l.textContent).join('|')"
    )
    assert "MARKER" in li_text, "long path must render with MARKER token"
    code_text = page.evaluate(
        "document.querySelector('.review-packet code').textContent"
    )
    assert "MARKER" in code_text, "diff_command must render with MARKER token"


# ---------------------------------------------------------------------------
# #14 — Diff toolbar truncates long branch names
# ---------------------------------------------------------------------------


def test_diff_toolbar_branch_truncates_long_names(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """The diff toolbar's branch → target span must be flex-constrained so
    it cannot push the toolbar wider than the inspector.
    """

    payload = _state(live_items=[_live_item(branch=LONG_BRANCH)])
    _install_projects_route(page)
    _install_state_route(page, payload)
    # Diff is gated on canShowDiff(detail) which requires changed files +
    # readiness != "in_progress". Use a packet that's ready-to-land so the
    # Code changes button is enabled.
    _install_run_detail_route(
        page,
        _run_detail(
            branch=LONG_BRANCH,
            files=[LONG_PATH],
            readiness_state="ready",
            readiness_label="Ready to land",
            readiness_tone="success",
        ),
    )
    _install_diff_route(
        page,
        _diff_response(branch=LONG_BRANCH, files=[LONG_PATH]),
    )
    _hydrate_diagnostics(mc_backend, page, disable_animations)

    page.locator("[data-testid=live-row-activator-run-01]").click()
    page.wait_for_selector(".review-packet", timeout=5_000)

    # Open the inspector in diff mode by clicking the Code changes button.
    page.locator("[data-testid=open-diff-button]").click()
    page.wait_for_selector(".run-inspector", timeout=5_000)
    page.wait_for_selector(".diff-toolbar", timeout=5_000)

    _assert_no_doc_horizontal_scroll(page)

    # Toolbar must fit inside the inspector body
    toolbar_geom = page.evaluate(
        """() => {
          const toolbar = document.querySelector('.diff-toolbar');
          if (!toolbar) return null;
          // Walk up to find the inspector body (its scroll container).
          let parent = toolbar.parentElement;
          while (parent && !parent.classList.contains('run-inspector') &&
                 !parent.classList.contains('run-inspector-body') &&
                 !parent.classList.contains('diff-viewer')) {
            parent = parent.parentElement;
          }
          parent = parent || toolbar.parentElement;
          const tr = toolbar.getBoundingClientRect();
          const pr = parent.getBoundingClientRect();
          const span = toolbar.querySelector('span');
          const sr = span.getBoundingClientRect();
          const cs = window.getComputedStyle(span);
          return {
            toolbarRight: tr.right,
            toolbarLeft: tr.left,
            parentRight: pr.right,
            parentLeft: pr.left,
            spanRight: sr.right,
            spanWidth: sr.width,
            spanWhiteSpace: cs.whiteSpace,
            spanOverflow: cs.overflow,
            spanTextOverflow: cs.textOverflow,
            spanTitle: span.getAttribute('title') || '',
          };
        }"""
    )
    assert toolbar_geom is not None
    assert toolbar_geom["toolbarRight"] <= toolbar_geom["parentRight"] + 1, (
        f"diff-toolbar overflows parent: {toolbar_geom}"
    )
    assert toolbar_geom["spanRight"] <= toolbar_geom["toolbarRight"] + 1, (
        f"diff-toolbar span overflows toolbar: {toolbar_geom}"
    )
    # Span must use ellipsis truncation OR wrap — either way it cannot
    # dominate the toolbar width. Our fix uses nowrap+ellipsis with a title
    # tooltip for full visibility.
    assert toolbar_geom["spanWhiteSpace"] == "nowrap", (
        f"diff-toolbar span should be nowrap-with-ellipsis, got {toolbar_geom['spanWhiteSpace']}"
    )
    assert toolbar_geom["spanTextOverflow"] == "ellipsis", (
        f"diff-toolbar span must use text-overflow:ellipsis, got {toolbar_geom['spanTextOverflow']}"
    )
    # Title attribute exposes the full string for accessibility.
    assert "MARKER" in toolbar_geom["spanTitle"], (
        f"diff-toolbar span must carry a title with the full branch/target, got {toolbar_geom['spanTitle']!r}"
    )
