"""W3-IMPORTANT-3 regression — improvement-report.md must distinguish
WARN observations from PASS results.

Live W3 dogfood (bench-results/web-as-user/2026-04-26-042240-16857d/W3/
improvement-report.md) showed five rows in the "Results" section all
prefixed with a green ✓ even though the underlying certifier verdict
for three of them was WARN ("non-blocking observation, beyond stated
scope"). An operator scanning the report would conclude every row was
a fully-met requirement — a FALSE PASS that could ship.

Root cause: `_render_results_section` (formerly inline in
`_run_improve_locked`) hard-coded ✓ for any `passed`-truthy row, and
WARN normalises to `passed=True` in the certifier's `_normalize_story_result`
contract (so the fix loop knows the run can land), so all WARN rows
fell into the same bucket as PASS.

Fix: `_render_results_section(journeys)` reads each row's `verdict` tag
and renders distinct glyphs + labels for PASS / WARN / FAIL.

These tests poke the pure renderer — no certify→fix loop, no temp git
repo — so they're cheap and don't depend on the SDK.
"""

from __future__ import annotations

import pytest

from otto.cli_improve import (
    _VERDICT_GLYPHS,
    _journey_verdict,
    _render_results_section,
)


def _journey(name: str, *, verdict: str | None = None, passed: bool | None = None) -> dict[str, object]:
    """Build a minimal journey dict — mirrors `_stories_to_journeys` output."""
    j: dict[str, object] = {"name": name, "story_id": name.lower().replace(" ", "-")}
    if verdict is not None:
        j["verdict"] = verdict
    if passed is not None:
        j["passed"] = passed
    return j


# --------------------------------------------------------------------------- #
# Core requirement: WARN does not render as PASS
# --------------------------------------------------------------------------- #


def test_warn_observation_renders_with_warn_icon() -> None:
    """A single WARN row must NOT use the ✓ glyph (which means PASS)."""
    lines = _render_results_section(
        [_journey("Whitespace-only strings produce ugly output", verdict="WARN")]
    )
    body = "\n".join(lines)
    assert "WARN" in body, f"WARN label missing from rendered row: {body!r}"
    assert "✓" not in body, (
        "WARN row must not use the PASS check glyph — operator would "
        f"misread as success.\nRendered:\n{body}"
    )


def test_warn_observation_does_not_use_success_styling() -> None:
    """Cross-check the glyph table: WARN's semantic class is NOT 'success'."""
    icon, label, css_class = _VERDICT_GLYPHS["WARN"]
    assert icon != "✓", "WARN must not share the PASS glyph"
    assert label == "WARN", f"WARN label must be 'WARN', got {label!r}"
    assert css_class != "success", (
        f"WARN must not use the success class (got {css_class!r}); "
        "operators rely on the colour to triage."
    )


def test_pass_warn_fail_render_distinctly() -> None:
    """PASS / WARN / FAIL each render with a unique icon AND a unique label."""
    lines = _render_results_section(
        [
            _journey("Empty-string returns safe default", verdict="PASS"),
            _journey("Whitespace produces ugly output", verdict="WARN"),
            _journey("Type validation missing", verdict="FAIL"),
        ]
    )
    body = "\n".join(lines)

    # All three labels surface — exactly once each.
    assert body.count("[PASS]") == 1, body
    assert body.count("[WARN]") == 1, body
    assert body.count("[FAIL]") == 1, body

    # All three glyphs surface — exactly once each. (! and ✗ are deliberately
    # distinct so colour-blind operators can still triage by shape.)
    pass_icon, _, _ = _VERDICT_GLYPHS["PASS"]
    warn_icon, _, _ = _VERDICT_GLYPHS["WARN"]
    fail_icon, _, _ = _VERDICT_GLYPHS["FAIL"]
    assert pass_icon != warn_icon != fail_icon != pass_icon, "icons must differ"

    assert body.count(pass_icon) == 1, f"PASS icon count off: {body!r}"
    assert body.count(warn_icon) == 1, f"WARN icon count off: {body!r}"
    assert body.count(fail_icon) == 1, f"FAIL icon count off: {body!r}"


# --------------------------------------------------------------------------- #
# Backwards-compat: legacy journeys without an explicit verdict
# --------------------------------------------------------------------------- #


def test_journey_without_verdict_falls_back_to_passed_flag() -> None:
    """Older serialised state (pre-fix) had only `passed` — must not crash."""
    assert _journey_verdict({"passed": True}) == "PASS"
    assert _journey_verdict({"passed": False}) == "FAIL"
    assert _journey_verdict({}) == "FAIL"  # missing -> FAIL (safer default)


def test_unknown_verdict_string_defaults_safely() -> None:
    """Forward-compat: a verdict the renderer doesn't know about must not
    silently render as a check — that was the original bug class."""
    lines = _render_results_section(
        [_journey("Some new tier", verdict="MYSTERY")]
    )
    body = "\n".join(lines)
    assert "✓" not in body, f"unknown verdict must not get the PASS glyph: {body!r}"


# --------------------------------------------------------------------------- #
# Reproducer: the exact W3 live-findings row set
# --------------------------------------------------------------------------- #


def test_w3_live_findings_repro_warns_now_visible() -> None:
    """Replays the W3 row set verbatim — three of five were WARN.

    Source: bench-results/web-as-user/2026-04-26-042240-16857d/W3/
    improvement-report.md (live dogfood). Before the fix all five rendered
    with a green check; the operator approved the merge based on bad
    signal. After the fix the WARN trio must surface as warnings.
    """
    journeys = [
        _journey(
            "Empty string, None, and missing argument all correctly return the safe default",
            verdict="PASS",
        ),
        _journey(
            "All 4 tests pass including the required empty-string test",
            verdict="PASS",
        ),
        _journey(
            "Whitespace-only strings are not stripped or treated as empty, producing ugly output",
            verdict="WARN",
        ),
        _journey(
            "Using `if not name` catches all falsy values (0, False, []) not just None/empty-string",
            verdict="WARN",
        ),
        _journey(
            "No type validation; arbitrary types accepted via f-string coercion",
            verdict="WARN",
        ),
    ]
    body = "\n".join(_render_results_section(journeys))
    # Two PASS, three WARN, zero FAIL — denominators must match.
    assert body.count("[PASS]") == 2, body
    assert body.count("[WARN]") == 3, body
    assert body.count("[FAIL]") == 0, body


# --------------------------------------------------------------------------- #
# Empty-list guard
# --------------------------------------------------------------------------- #


def test_empty_journeys_renders_nothing() -> None:
    """No stories => no Results section (matches pre-fix behaviour)."""
    assert _render_results_section([]) == []


# --------------------------------------------------------------------------- #
# End-to-end: stories→journeys→render carries verdict through
# --------------------------------------------------------------------------- #


def test_stories_to_journeys_carries_verdict_for_warn() -> None:
    """Verifies the pipeline helper preserves WARN so the renderer sees it."""
    from otto.pipeline import _stories_to_journeys

    stories = [
        {"summary": "ok", "passed": True, "verdict": "PASS"},
        # Realistic certifier shape: passed=True + warn=True + verdict=WARN.
        {"summary": "soft", "passed": True, "warn": True, "verdict": "WARN"},
        {"summary": "broken", "passed": False, "verdict": "FAIL"},
    ]
    journeys = _stories_to_journeys(stories)
    assert [j["verdict"] for j in journeys] == ["PASS", "WARN", "FAIL"]


def test_stories_to_journeys_infers_warn_from_warn_flag_when_verdict_missing() -> None:
    """Defensive: pre-restructure callers may set `warn` without `verdict`."""
    from otto.pipeline import _stories_to_journeys

    journeys = _stories_to_journeys(
        [{"summary": "soft", "passed": True, "warn": True}]
    )
    assert journeys[0]["verdict"] == "WARN"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
