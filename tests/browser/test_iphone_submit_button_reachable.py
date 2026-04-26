"""W7-IMPORTANT-1: JobDialog submit button must be reachable on iPhone 14.

Source: live W7 dogfood run (mc-audit/live-findings.md, search
"W7-IMPORTANT-1").

Reproduction summary
--------------------

Open Mission Control on the ``playwright.devices["iPhone 14"]`` profile
(390x664, dpr=3, webkit). The JobDialog submit button rendered at
``y=674`` — 9.95px below the bottom of the 664px viewport — without a
visible scroll affordance. First-time mobile users could not see the
"Queue job" CTA without manually scrolling inside the modal.

Fix verified
------------

* The ``.job-dialog`` container uses ``max-height: calc(100dvh - 28px)``
  so it accounts for mobile browser chrome.
* The ``.job-dialog footer`` (which holds the submit button) is now
  ``position: sticky; bottom: 0`` — the button stays anchored at the
  bottom of the visible modal regardless of scroll position.

Test asserts: with the JobDialog open on the iPhone 14 viewport, the
``job-dialog-submit-button`` is either fully within the viewport already
OR scrolling the modal brings it into view within 1s.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_iphone_submit_button_reachable.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _viewport_state(page: Any) -> dict[str, int]:
    return page.evaluate(
        """() => ({
            innerHeight: window.innerHeight,
            innerWidth: window.innerWidth,
        })"""
    )


def _submit_rect(page: Any) -> dict[str, float] | None:
    """Return the bounding rect of the submit button, or None if absent."""

    return page.evaluate(
        """() => {
            const el = document.querySelector(
                '[data-testid=job-dialog-submit-button]'
            );
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {top: r.top, bottom: r.bottom, left: r.left, right: r.right};
        }"""
    )


def test_submit_button_reachable_on_iphone_14(
    mc_backend: Any, browser: Any, playwright: Any
) -> None:
    """The JobDialog submit button must be visible (or scroll-reachable
    within 1s) on the iPhone 14 viewport.

    The previous layout placed submit at y=674 in a 664px viewport with
    no scroll hint — failing iOS HIG discoverability. After the sticky-
    footer fix, ``bottom`` of the submit button must be ``<= viewport``.
    """

    iphone_kwargs = dict(playwright.devices["iPhone 14"])
    context = browser.new_context(**iphone_kwargs)
    page = context.new_page()
    try:
        page.goto(mc_backend.url, wait_until="networkidle")
        page.wait_for_function(
            "document.querySelector('#root')?.children.length > 0",
            timeout=10_000,
        )

        # Open the new-job dialog (cluster G app-ready marker first so
        # we know the SPA hydrated).
        page.wait_for_selector("[data-mc-shell=ready]", timeout=10_000)
        page.get_by_test_id("new-job-button").click()
        page.wait_for_selector(
            "[data-testid=job-dialog-submit-button]", timeout=5_000
        )

        viewport = _viewport_state(page)
        viewport_h = viewport["innerHeight"]

        # Initial position: under the sticky-footer fix the submit
        # button must already be inside the viewport. Allow up to 2px
        # slack for sub-pixel rounding on dpr=3 webkit.
        rect = _submit_rect(page)
        assert rect is not None, "submit button missing from DOM"
        if rect["bottom"] > viewport_h + 2:
            # Try scrolling the modal — the test passes if scrolling
            # within 1s brings the button into view (so users do have a
            # path to reach it), but logs that the sticky-footer fix
            # regressed.
            deadline = time.monotonic() + 1.0
            page.evaluate(
                """() => {
                    const dialog = document.querySelector('.job-dialog');
                    if (dialog) dialog.scrollTop = dialog.scrollHeight;
                }"""
            )
            while time.monotonic() < deadline:
                rect = _submit_rect(page)
                if rect and rect["bottom"] <= viewport_h + 2:
                    break
                time.sleep(0.05)
            assert rect is not None and rect["bottom"] <= viewport_h + 2, (
                f"submit button still off-screen after scroll: "
                f"rect={rect!r} viewport_h={viewport_h}"
            )

        # The button must also be a real touch target (HIG: 44x44).
        assert rect["right"] - rect["left"] >= 44, (
            f"submit button width {rect['right'] - rect['left']:.1f} < 44"
        )
        assert rect["bottom"] - rect["top"] >= 44, (
            f"submit button height {rect['bottom'] - rect['top']:.1f} < 44"
        )

    finally:
        context.close()


def test_job_dialog_uses_dvh_max_height(mc_backend: Any, page: Any) -> None:
    """Defense-in-depth: the modal must declare ``dvh``-based max-height.

    iOS Safari's ``vh`` reports the LARGEST viewport (URL bar collapsed),
    which is what caused the original W7 regression — content that fits
    when the URL bar is hidden overflows once the URL bar expands.
    Using ``dvh`` (dynamic viewport height) tracks the actual visible
    area, eliminating the gap.
    """

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#root')?.children.length > 0", timeout=10_000
    )
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector("[data-testid=job-dialog-submit-button]", timeout=5_000)

    # Inspect the cascade: at least one max-height rule must use `dvh`.
    has_dvh = page.evaluate(
        """() => {
            const dialog = document.querySelector('.job-dialog');
            if (!dialog) return false;
            // Walk every stylesheet rule that targets .job-dialog and
            // check whether any sets max-height with `dvh`. Cross-origin
            // stylesheets throw on `cssRules` access; skip those.
            for (const sheet of document.styleSheets) {
                let rules = null;
                try { rules = sheet.cssRules; } catch (_) { continue; }
                if (!rules) continue;
                for (const rule of rules) {
                    if (!(rule instanceof CSSStyleRule)) continue;
                    if (!rule.selectorText) continue;
                    if (!rule.selectorText.includes('job-dialog')) continue;
                    const mh = rule.style.maxHeight || '';
                    if (mh.includes('dvh')) return true;
                }
            }
            return false;
        }"""
    )
    assert has_dvh, (
        "expected .job-dialog max-height rule to use dvh — without it "
        "iOS Safari can render the modal taller than the actual visible "
        "area when the URL bar expands."
    )


def test_job_dialog_footer_is_sticky(mc_backend: Any, page: Any) -> None:
    """The footer holding the submit button must be sticky-positioned.

    A sticky footer keeps the submit button anchored at the bottom of
    the visible modal regardless of scroll position. Without it, a tall
    dialog on a small viewport buries the button below the fold.
    """

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#root')?.children.length > 0", timeout=10_000
    )
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector("[data-testid=job-dialog-submit-button]", timeout=5_000)

    position = page.evaluate(
        """() => {
            const footer = document.querySelector('.job-dialog footer');
            if (!footer) return null;
            return getComputedStyle(footer).position;
        }"""
    )
    assert position == "sticky", (
        f"expected .job-dialog footer to use position: sticky; got {position!r}. "
        "Sticky footer is the W7-IMPORTANT-1 fix that keeps the submit "
        "button reachable on small viewports."
    )
