"""Regression: the candidate label-recovery heuristic must NOT bleed a
form field's <label> onto adjacent buttons/links.

resume16m proved P1a+P1b+base_url all work (journeys connect and run
assertions) but the semantic fallback still returned None for every
control. Root cause, found via a real Playwright repro on the saved
register DOM: `<div class=mb-4><label>Password</label><input
type=password></div>` ... `<button>Create account</button>` —
`_CANDIDATE_JS`'s previousElementSibling label walk reached back past
the button to the Password field's <label>, giving the submit button
`label='Password'`. `_semantic_match({label:'Password'})` then saw TWO
recall-1.0 candidates (the real password textbox AND the mislabeled
button) and fail-closed on false ambiguity -> None -> every journey
`required input/control absent`.

Fix: only LABELABLE controls (input[not button-ish]/textarea/select/
contenteditable/role=textbox|searchbox|combobox|spinbutton) get the
unassociated/sibling <label> recovery; buttons/links derive their name
from own text/aria-label/value (per the HTML spec). Generic — this
`<div><label/><input/></div>` + nearby button shape is ubiquitous.
"""

from __future__ import annotations

import pytest

from otto.journey_ui_executor import _CANDIDATE_JS, _semantic_match

pytest.importorskip("playwright.sync_api")

_REGISTER_HTML = """
<!doctype html><html><body><div><div><div>
<h1>Create your account</h1>
<form>
  <div class="mb-4"><label>Name</label><input type="text" value="Alice"></div>
  <div class="mb-4"><label>Email</label><input type="email" value="a@b.com"></div>
  <div class="mb-4"><label>Password</label><input type="password"></div>
  <button type="submit">Create account</button>
</form>
</div></div></div>
<div><p>Already have an account? <button>Sign in</button></p></div>
</body></html>
"""


def _candidates(html: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            pg = b.new_page()
            pg.set_content(html, wait_until="domcontentloaded")
            cands = pg.evaluate(_CANDIDATE_JS)
        finally:
            b.close()
    return [c for c in cands if isinstance(c, dict)]


def test_button_does_not_inherit_adjacent_field_label() -> None:
    cands = _candidates(_REGISTER_HTML)
    buttons = [c for c in cands if c.get("role") == "button"]
    assert buttons, "expected the submit/sign-in buttons as candidates"
    for b in buttons:
        assert not b.get("label"), (
            f"button {b.get('name')!r} bled an adjacent field label "
            f"{b.get('label')!r}"
        )
    # The button's own name is still recovered from its text.
    assert any(b.get("name") == "Create account" for b in buttons)


def test_semantic_match_resolves_password_textbox_not_ambiguous() -> None:
    cands = _candidates(_REGISTER_HTML)
    m = _semantic_match({"role": "", "label": "Password"}, cands)
    assert m is not None, "Password input must resolve (was false-ambiguous)"
    assert m.get("role") == "textbox" and m.get("label") == "Password"
    # And the other fields/buttons still resolve.
    assert _semantic_match({"role": "textbox", "label": "Name"}, cands) is not None
    assert (
        _semantic_match({"role": "button", "name": "Create account"}, cands)
        is not None
    )
