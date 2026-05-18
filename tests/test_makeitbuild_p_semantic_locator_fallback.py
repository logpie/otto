"""Hybrid journey resolver — Phase 1b (literal-miss semantic fallback).

`_semantic_locator(page, spec)` snapshots the live page's interactive
controls and deterministically resolves the step's intent to the best
one when the literal Playwright selector missed (count==0). Literal-
first means zero behavior change when the label already matches; this
only fires on a miss. Fails CLOSED (no match / no intent / evaluate
error → (None, "")) so a wrong/ambiguous resolution stays a real
failure — anti-false-pass preserved.
"""

from __future__ import annotations

from typing import Any

from otto.journey_ui_executor import _semantic_locator


class _FakeLocator:
    def __init__(self, selector: str) -> None:
        self.selector = selector

    @property
    def first(self) -> "_FakeLocator":
        return self


class _FakePage:
    def __init__(self, candidates: Any, *, raise_eval: bool = False) -> None:
        self._candidates = candidates
        self._raise_eval = raise_eval
        self.located: list[str] = []

    def evaluate(self, _js: str) -> Any:
        if self._raise_eval:
            raise RuntimeError("evaluate boom")
        return self._candidates

    def locator(self, selector: str) -> _FakeLocator:
        self.located.append(selector)
        return _FakeLocator(selector)


_PAGE_DOM = [
    {"role": "button", "name": "Cancel", "selector": "button:nth-of-type(1)"},
    {"role": "button", "name": "Create workspace", "selector": "[data-testid=\"ws-create\"]"},
    {"role": "textbox", "label": "Email", "selector": "#email"},
    {"role": "textbox", "label": "Name", "selector": "form input:nth-of-type(1)"},
]


def test_drifted_button_resolves_via_fallback() -> None:
    page = _FakePage(_PAGE_DOM)
    loc, label = _semantic_locator(
        page, {"role": "button", "name": "Create your workspace"}
    )
    assert loc is not None
    assert label == 'semantic:[data-testid="ws-create"]'
    assert page.located == ['[data-testid="ws-create"]']


def test_unassociated_name_input_resolves_via_fallback() -> None:
    page = _FakePage(_PAGE_DOM)
    loc, label = _semantic_locator(
        page, {"role": "textbox", "field_label": "Name"}
    )
    assert loc is not None
    assert label == "semantic:form input:nth-of-type(1)"


def test_no_match_fails_closed() -> None:
    page = _FakePage(_PAGE_DOM)
    loc, label = _semantic_locator(
        page, {"role": "button", "name": "Delete everything forever"}
    )
    assert loc is None and label == ""
    assert page.located == []  # never fabricates a locator


def test_no_intent_is_noop() -> None:
    page = _FakePage(_PAGE_DOM)
    assert _semantic_locator(page, {"role": "button"}) == (None, "")
    assert page.located == []


def test_evaluate_error_fails_closed() -> None:
    page = _FakePage(_PAGE_DOM, raise_eval=True)
    assert _semantic_locator(
        page, {"role": "button", "name": "Create your workspace"}
    ) == (None, "")


def test_non_list_candidates_fails_closed() -> None:
    assert _semantic_locator(
        _FakePage({"not": "a list"}), {"name": "Create workspace"}
    ) == (None, "")
