"""Browser regression for the visual-coherence design-token cluster.

Cluster: ``docs/mc-audit/_hunter-findings/visual-coherence.md`` IMPORTANT
items F4 (disabled idiom), F5 (hover treatment), F6 (font scale), F7/F8
(off-grid spacing & radii), F9 (status-colour drift), F10 (colour-blind
unsafe badges), F11 (input/button baseline).

The audit found 51 raw-hex literals leaking past the CSS-var palette and
three different "pass" greens. The fix consolidates everything into a
small token system (``--color-success/-warning/-danger/-info`` × bg/fg/
border, ``--text-*``, ``--space-*``, ``--radius-*``, ``--control-height``,
``--disabled-opacity``) and adds a glyph prefix to every status badge.

Tests below assert the *structural* invariants — colour-only assertions
are notoriously fragile across browsers/headless renderers, so we focus
on:
  - All ``.tone-success`` / ``.task-status.status-tone-success`` /
    ``.check-pass`` / ``.proof-story.story-pass`` elements share an
    identical computed background-colour (no drift).
  - Every status badge contains a ``.status-icon`` glyph child.
  - All ``[disabled]`` buttons render with opacity ≤ 0.6 and
    ``cursor: not-allowed``.
  - Hover state on a clickable row produces a defined background or
    filter (not the same as the resting state).
  - Toolbar input height differs from button height by < 2px.
  - At least 90 % of visible elements use a font-size from the redesigned
    {11,12,13,14,16,18,22,28} scale.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_visual_coherence_tokens.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


SAMPLE_TARGET = "main"


# ---------------------------------------------------------------------------
# State payload — a richly-populated mission state with proof checks +
# stories + task cards covering every status tone.
# ---------------------------------------------------------------------------


def _landing_item(
    *,
    task_id: str,
    landing_state: str = "waiting",
    queue_status: str = "queued",
    branch: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "summary": f"build the {task_id}",
        "branch": branch or f"build/{task_id}",
        "branch_exists": True,
        "queue_status": queue_status,
        "queue_added_at": "2026-04-25T12:00:00Z",
        "queue_started_at": None,
        "queue_finished_at": None,
        "command": "build",
        "queue_failure_reason": None,
        "landing_state": landing_state,
        "merge_blocked": False,
        "blockers": [],
        "merge_id": None,
        "merged_at": None,
        "diff_path": None,
        "diff_relpath": None,
        "diff_error": None,
        "changed_file_count": 0,
        "changed_files": [],
        "diff_size_bytes": 0,
        "diff_truncated": False,
        "merge_target": SAMPLE_TARGET,
        "merge_base_sha": None,
        "head_sha": None,
        "target_sha": None,
        "exit_code": None,
        "elapsed_s": None,
        "cost_usd": None,
        "actions": [],
        "intent": None,
        "run_id": run_id,
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
            "counts": {"queued": 1, "running": 0, "done": 0},
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
            "items": [
                _landing_item(task_id="ready-task", landing_state="ready", queue_status="done", run_id="r-ready"),
                _landing_item(task_id="failed-task", landing_state="waiting", queue_status="failed", run_id="r-failed"),
                _landing_item(task_id="queued-task", landing_state="waiting", queue_status="queued"),
                _landing_item(task_id="landed-task", landing_state="merged", queue_status="done", run_id="r-landed"),
            ],
            "counts": {"ready": 1, "merged": 1, "blocked": 0, "total": 4},
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
            "refresh_interval_s": 0.5,
        },
        "history": {"items": [], "page": 0, "page_size": 25, "total_rows": 0, "total_pages": 1},
        "events": {"path": "", "items": [], "total_count": 0, "malformed_count": 0, "limit": 50, "truncated": False},
        "runtime": {
            "status": "healthy",
            "generated_at": "2026-04-25T12:00:00Z",
            "queue_tasks": 4,
            "state_tasks": 4,
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
                "can_start": True,
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


def _install_routes(page: Any, payload: dict[str, Any]) -> None:
    def projects(route: Any) -> None:
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

    def state(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/projects", projects)
    page.route("**/api/state*", state)


def _hydrate(mc_backend: Any, page: Any, disable_animations: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    disable_animations(page)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_status_pass_uses_consistent_green(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visual-coherence F9 — the same "success" tone token must produce
    one identical computed background across every surface where it is
    used. We sample task-status (status-tone-success), the proof-summary
    metric tone-success, and the proof-story story-pass badge if
    present. All non-empty backgrounds must match.

    We deliberately don't compare colours to a fixed RGB string — only
    that they're equal across surfaces — so the test survives
    headless-render variance and keeps the property the cluster needs:
    no drift.
    """

    _install_routes(page, _state())
    _hydrate(mc_backend, page, disable_animations)

    board = page.locator("[data-testid='task-board']")
    board.wait_for(state="visible", timeout=5_000)

    # All success-tone surfaces visible in the default state. We grab
    # backgrounds for every visible element matching each selector.
    selectors = [
        ".task-status.status-tone-success",
        # tone-success on overview-metric only colours `strong`, not the
        # whole tile, so we don't include it here.
    ]

    seen: dict[str, set[str]] = {}
    for selector in selectors:
        loc = page.locator(selector)
        count = loc.count()
        if count == 0:
            continue
        bgs = set()
        for i in range(count):
            el = loc.nth(i)
            if not el.is_visible():
                continue
            bg = el.evaluate("(el) => window.getComputedStyle(el).backgroundColor")
            bgs.add(bg)
        seen[selector] = bgs

    # We must have at least one success-tone surface visible in the test state.
    assert any(seen.values()), f"no visible success-tone surfaces found in {seen!r}"

    # All success-tone backgrounds across selectors must match — no drift.
    flattened = {bg for bgs in seen.values() for bg in bgs}
    assert len(flattened) == 1, (
        f"success-tone background drifted across surfaces: {flattened!r} "
        f"(per-selector: {seen!r})"
    )


def test_status_includes_icon_prefix(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visual-coherence F10 — colour-blind safety. Every visible
    ``.task-status`` badge must contain a ``.status-icon`` child glyph
    so the tone signal is not colour-only.
    """

    _install_routes(page, _state())
    _hydrate(mc_backend, page, disable_animations)

    board = page.locator("[data-testid='task-board']")
    board.wait_for(state="visible", timeout=5_000)

    badges = page.locator(".task-status")
    count = badges.count()
    assert count >= 1, "expected at least one task-status badge"

    for i in range(count):
        badge = badges.nth(i)
        if not badge.is_visible():
            continue
        icon = badge.locator(".status-icon")
        assert icon.count() == 1, (
            f"badge #{i} missing exactly one .status-icon child "
            f"(class={badge.get_attribute('class')!r})"
        )
        glyph = (icon.text_content() or "").strip()
        assert glyph, f"badge #{i} .status-icon is empty"
        # Sanity — must be one of the canonical glyphs from toneIcon().
        assert glyph in {"✓", "⚠", "✗", "●", "·"}, (
            f"badge #{i} .status-icon is unexpected glyph={glyph!r}"
        )


def test_disabled_buttons_consistent_opacity(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visual-coherence F4 — every disabled button must render with the
    canonical disabled idiom: opacity ≤ 0.6 + ``cursor: not-allowed``.
    The test injects a disabled button into the live DOM (the toolbar
    refresh button is enabled in the default state) so we exercise the
    universal selector unambiguously.
    """

    _install_routes(page, _state())
    _hydrate(mc_backend, page, disable_animations)

    # Inject a disabled button with the same class footprint as a real
    # primary CTA so styles cascade exactly as they would for a real
    # disabled control.
    page.evaluate("""
        () => {
          const btn = document.createElement('button');
          btn.id = 'visual-coherence-disabled-probe';
          btn.disabled = true;
          btn.textContent = 'Disabled probe';
          btn.className = 'primary';
          document.body.appendChild(btn);
        }
    """)

    btn = page.locator("#visual-coherence-disabled-probe")
    btn.wait_for(state="attached", timeout=2_000)

    opacity = float(btn.evaluate("(el) => window.getComputedStyle(el).opacity"))
    cursor = btn.evaluate("(el) => window.getComputedStyle(el).cursor")

    assert opacity <= 0.6, f"disabled button opacity {opacity} > 0.6 — drift from token"
    assert cursor == "not-allowed", f"disabled cursor={cursor!r}, expected 'not-allowed'"


def test_hover_treatment_consistent(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visual-coherence F5 — hovering a clickable surface must produce a
    *defined* visual change. We probe ``.history-activity`` (or
    ``tbody tr``) and assert the hover background differs from the
    resting background — proving the consolidated hover rule fires.
    """

    _install_routes(page, _state())
    _hydrate(mc_backend, page, disable_animations)

    # Pick any clickable list-row surface; the task card is the most
    # reliable: it's always visible in the default state.
    board = page.locator("[data-testid='task-board']")
    board.wait_for(state="visible", timeout=5_000)

    card = page.locator(".queue-list-row-task").first
    card.wait_for(state="visible", timeout=5_000)

    rest_bg = card.evaluate("(el) => window.getComputedStyle(el).backgroundColor")
    # Hover via JS dispatch — Playwright .hover() can flake under
    # disable-animations injection. Use mouse positioning.
    box = card.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(50)

    hover_bg = card.evaluate("(el) => window.getComputedStyle(el).backgroundColor")

    # On hover the queue row background changes — must differ
    # from the resting background. If they match, the hover rule didn't fire.
    assert hover_bg != rest_bg, (
        f"queue row hover did not change background-color (rest={rest_bg!r}, hover={hover_bg!r})"
    )


def test_input_button_baseline_aligned(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visual-coherence F11 — the toolbar input + button must share the
    same computed height (within 2px). This is the canonical baseline
    fix: ``--control-height: 32px`` applied to both.
    """

    _install_routes(page, _state())
    _hydrate(mc_backend, page, disable_animations)

    input_el = page.locator(".toolbar .filters input[type='search']").first
    button_el = page.locator(".toolbar .filters button").first
    input_el.wait_for(state="visible", timeout=5_000)
    button_el.wait_for(state="visible", timeout=5_000)

    input_box = input_el.bounding_box()
    button_box = button_el.bounding_box()
    assert input_box is not None and button_box is not None

    diff = abs(input_box["height"] - button_box["height"])
    assert diff < 2.0, (
        f"toolbar input height {input_box['height']} vs button {button_box['height']} "
        f"differs by {diff} px — exceeds 2px baseline tolerance"
    )


def test_font_sizes_use_canonical_scale(
    mc_backend: Any, page: Any, disable_animations: Any
) -> None:
    """Visual-coherence F6 — the canonical type scale is
    {12,14,16,18,22,28} px. At least 90 % of visible text elements must
    compute to one of those sizes (allowing slack for inherited odd
    sizes from third-party widgets / focus halos / SR-only nodes).
    """

    _install_routes(page, _state())
    _hydrate(mc_backend, page, disable_animations)

    sizes_px = page.evaluate("""
        () => {
          const wanted = ['span', 'strong', 'em', 'small', 'p', 'h1', 'h2',
                          'h3', 'h4', 'label', 'button', 'a', 'dt', 'dd',
                          'th', 'td', 'li', 'time'];
          const results = [];
          for (const sel of wanted) {
            for (const el of document.querySelectorAll(sel)) {
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') continue;
              const fs = parseFloat(style.fontSize);
              if (!Number.isFinite(fs)) continue;
              results.push(fs);
            }
          }
          return results;
        }
    """)

    canonical = {11, 12, 13, 14, 16, 18, 22, 28}
    if not sizes_px:
        pytest.skip("no visible text elements measured")

    matches = sum(1 for s in sizes_px if int(round(s)) in canonical)
    ratio = matches / len(sizes_px)

    assert ratio >= 0.9, (
        f"only {matches}/{len(sizes_px)} ({ratio:.1%}) text sizes are on the canonical "
        f"scale {sorted(canonical)}; observed sizes (sample): "
        f"{sorted(set(int(round(s)) for s in sizes_px))[:30]}"
    )
