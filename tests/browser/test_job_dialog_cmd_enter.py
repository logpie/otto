"""W8-IMPORTANT-1: JobDialog must submit on Cmd+Enter / Ctrl+Enter
from the intent textarea.

Source: live W8 dogfood run (mc-audit/live-findings.md, search
"W8-IMPORTANT-1").

Reproduction summary
--------------------

The documented power-user shortcut to submit a long intent without
releasing the keyboard was dead. Two of three keyboard-only submit
attempts in the W8 run logged ``Cmd+Enter / Enter ignored``. Only the
Tab-to-submit-button + Enter path worked.

Fix verified
------------

The intent ``<textarea>`` now has an ``onKeyDown`` handler that
intercepts ``(metaKey || ctrlKey) && key === 'Enter'`` and forwards to
the form's ``requestSubmit()`` — going through the form so the existing
validation gating (grace window, dirty-target confirm, prior-run
requirement) stays in one place.

Test asserts: focusing the textarea, typing intent, then pressing
Cmd+Enter (mac) submits the form and triggers the queue POST. The
test also verifies the shortcut is GATED by the same disabled rules as
the submit button — pressing Cmd+Enter when the intent is empty must
NOT fire a POST.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_job_dialog_cmd_enter.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import platform
import time
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _modifier() -> str:
    """Return the Cmd/Ctrl modifier name expected by the platform.

    Webkit on macOS exposes ``Meta`` as Cmd; Chromium/Firefox on Linux
    map ``Control`` to the same keyboard slot. Playwright accepts
    ``Meta`` and ``Control`` strings in its key DSL.
    """

    return "Meta" if platform.system() == "Darwin" else "Control"


def _open_dialog(page: Any, mc_backend: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)
    page.get_by_test_id("new-job-button").click()
    page.wait_for_selector("[data-testid=job-dialog-submit-button]", timeout=5_000)


def _install_post_recorder(page: Any) -> None:
    """Patch window.fetch to record POSTs against /api/queue/build.

    We don't intercept — the real backend is fine to receive the POST and
    the dialog already shows the queued banner. We just need a reliable
    signal that the POST was issued (the SPA may not yet have closed the
    dialog by the time the test polls).
    """

    page.evaluate(
        """() => {
            window.__queuePosts = [];
            const origFetch = window.fetch.bind(window);
            window.fetch = async (input, init) => {
                try {
                    const url = typeof input === 'string' ? input : input.url;
                    const method = (init && init.method) || (typeof input === 'object' && input.method) || 'GET';
                    if (url && url.includes('/api/queue/') && method === 'POST') {
                        window.__queuePosts.push({url, method, ts: Date.now()});
                    }
                } catch (_) { /* swallow — don't break the page */ }
                return origFetch(input, init);
            };
        }"""
    )


def _post_count(page: Any) -> int:
    return page.evaluate("() => (window.__queuePosts || []).length") or 0


def test_cmd_enter_in_textarea_submits_dialog(
    mc_backend: Any, page: Any
) -> None:
    """Cmd+Enter (or Ctrl+Enter) from the intent textarea must submit.

    The textarea would otherwise eat Enter as a newline. The fix wires
    onKeyDown to forward to the form's requestSubmit().
    """

    _open_dialog(page, mc_backend)
    _install_post_recorder(page)

    textarea = page.get_by_test_id("job-dialog-intent")
    textarea.click()
    textarea.fill("Add a /api/health endpoint that returns ok")

    # Submit via Cmd+Enter from the focused textarea.
    page.keyboard.press(f"{_modifier()}+Enter")

    # The dialog uses a 3s grace window before the actual POST fires —
    # wait up to 6s for the POST to land.
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if _post_count(page) >= 1:
            return
        time.sleep(0.05)

    pytest.fail(
        f"Cmd+Enter from textarea did NOT submit the dialog within 6s; "
        f"posts={page.evaluate('() => window.__queuePosts')!r}"
    )


def test_cmd_enter_does_not_submit_when_intent_empty(
    mc_backend: Any, page: Any
) -> None:
    """The Cmd+Enter shortcut must respect the same disabled gating as
    the submit button. Pressing it when the intent is empty must NOT
    fire a queue POST.
    """

    _open_dialog(page, mc_backend)
    _install_post_recorder(page)

    textarea = page.get_by_test_id("job-dialog-intent")
    textarea.click()  # focus, but no fill — intent stays empty

    page.keyboard.press(f"{_modifier()}+Enter")

    # No grace window should ever start. Wait a couple seconds to be sure.
    time.sleep(2.0)
    assert _post_count(page) == 0, (
        f"Cmd+Enter with empty intent erroneously fired a queue POST; "
        f"posts={page.evaluate('() => window.__queuePosts')!r}"
    )
    # Dialog must still be open — empty submission should not close it.
    assert page.locator("[data-testid=job-dialog-submit-button]").count() == 1, (
        "JobDialog disappeared after Cmd+Enter on empty intent — the "
        "shortcut must respect the validation gate."
    )


def test_textarea_enter_alone_inserts_newline(mc_backend: Any, page: Any) -> None:
    """Plain Enter (no modifier) inside the textarea must remain a newline.

    Without this guard, the Cmd+Enter handler could regress and start
    swallowing every Enter keystroke. We type two lines using Enter as
    the separator and assert the textarea value contains the newline.
    """

    _open_dialog(page, mc_backend)
    _install_post_recorder(page)

    textarea = page.get_by_test_id("job-dialog-intent")
    textarea.click()
    textarea.type("first line")
    page.keyboard.press("Enter")
    textarea.type("second line")

    value = textarea.input_value()
    assert "\n" in value, (
        f"plain Enter in textarea must insert a newline (regression guard); "
        f"value={value!r}"
    )

    # And no POST should have fired from the lone Enter.
    assert _post_count(page) == 0, (
        f"plain Enter must not submit the dialog; "
        f"posts={page.evaluate('() => window.__queuePosts')!r}"
    )
