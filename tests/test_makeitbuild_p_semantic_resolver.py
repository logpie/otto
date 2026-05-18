"""Hybrid journey resolver — Phase 1 (deterministic semantic match).

Root cause (generic, every multi-feature web product): compile-spec
emits literal Playwright selectors; independent leaf agents build the
UI with their own reasonable wording, so a strict string match fails a
functionally-correct product (resume16j/k: journey 1 fails
`required input absent: label='Name'` then journeys 2-4 cascade).

`_semantic_match` resolves the journey step's INTENT to the best page
element by token-recall + role compatibility, and fails CLOSED on
ambiguity/absence — never guesses — preserving the no-false-pass
invariant (a wrong resolution yields a real failure, not a fake pass;
the post-action assertion stays deterministic regardless).
"""

from __future__ import annotations

from otto.journey_ui_executor import _semantic_match


def test_drifted_button_name_resolves() -> None:
    # Journey intent "Create your workspace"; product renders "Create
    # workspace" (the exact resume16j drift). Must resolve.
    best = _semantic_match(
        {"role": "button", "name": "Create your workspace"},
        [
            {"role": "button", "name": "Cancel"},
            {"role": "button", "name": "Create workspace"},
        ],
    )
    assert best is not None and best["name"] == "Create workspace"


def test_drifted_searchbox_label_resolves_with_role_synonym() -> None:
    # Intent label "Search"; product aria-label "Search query", role
    # searchbox while intent said textbox (role synonym).
    best = _semantic_match(
        {"role": "textbox", "label": "Search"},
        [{"role": "searchbox", "aria_label": "Search query"}],
    )
    assert best is not None and best["aria_label"] == "Search query"


def test_unassociated_name_label_input_resolves() -> None:
    # The resume16k journey-1 killer: <label>Name</label> not associated
    # with the input (no for/id); strict getByLabel('Name') failed.
    # A candidate synthesized from the nearby label text still resolves.
    best = _semantic_match(
        {"role": "textbox", "label": "Name"},
        [
            {"role": "textbox", "label": "Email", "placeholder": "you@example.com"},
            {"role": "textbox", "label": "Name", "selector": "form input:nth-of-type(1)"},
        ],
    )
    assert best is not None and best["selector"] == "form input:nth-of-type(1)"


def test_ambiguous_fails_closed() -> None:
    # Intent "Create" matches BOTH "Create workspace" and "Create issue"
    # equally → must NOT guess (anti-false-pass).
    assert (
        _semantic_match(
            {"role": "button", "name": "Create"},
            [
                {"role": "button", "name": "Create workspace"},
                {"role": "button", "name": "Create issue"},
            ],
        )
        is None
    )


def test_absent_control_fails_closed() -> None:
    assert (
        _semantic_match(
            {"role": "button", "name": "Create your workspace"},
            [
                {"role": "button", "name": "Sign out"},
                {"role": "link", "name": "Documentation"},
            ],
        )
        is None
    )


def test_role_incompatible_candidate_filtered() -> None:
    # A textbox named "Submit" must NOT satisfy a button intent "Submit".
    assert (
        _semantic_match(
            {"role": "button", "name": "Submit"},
            [{"role": "textbox", "name": "Submit", "label": "Submit"}],
        )
        is None
    )


def test_empty_intent_or_candidates_is_none() -> None:
    assert _semantic_match({}, [{"role": "button", "name": "X"}]) is None
    assert _semantic_match({"name": "X"}, []) is None
    assert _semantic_match({"role": "button"}, [{"role": "button", "name": "X"}]) is None


def test_partial_recall_below_threshold_fails_closed() -> None:
    # Intent "Add a comment to the issue" vs a control "Add label" —
    # only 1/5 intent tokens overlap → below recall threshold → None.
    assert (
        _semantic_match(
            {"role": "textbox", "name": "Add a comment to the issue"},
            [{"role": "textbox", "name": "Add label"}],
        )
        is None
    )
