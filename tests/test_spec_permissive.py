"""v2.1 permissive parser tests.

Each test corresponds to a finding from docs/intent-to-product-v2.md
that v2.1's permissive parser closes. The pattern: input that v1's
strict validator would have rejected → v2.1 produces a usable Spec
with a specific WarningCode recorded.

Hard rejects in v2.1 are limited to: not-a-dict input, dep cycles,
truly empty (no intent + no slices + no structure).
"""

from __future__ import annotations

import json

import pytest

from otto.spec_compile import (
    SpecValidationError,
    parse_spec,
    spec_from_dict,
)


# ---------------------------------------------------------------------------
# F1: webapp schema fields are advisory, not strict
# ---------------------------------------------------------------------------


def test_webapp_missing_routes_parses_with_warning() -> None:
    """v1 R3/R5/R8: missing routes was a cheap fail. v2.1: parses fine."""
    spec, _warnings = parse_spec({
        "intent": "x",
        "project_kind": "webapp",
        "structure": {"payload": {}},  # no routes, no components
        "slices": [
            {"id": "s", "title": "t", "owned_paths": ["src/**"], "checks": []},
        ],
    })
    assert spec.intent == "x"
    assert len(spec.slices) == 1


# ---------------------------------------------------------------------------
# F2: state_invariant prose generalized to all malformed-payload cases
# ---------------------------------------------------------------------------


def test_state_invariant_prose_parses() -> None:
    """v1 R17/R26: prose state_invariant blocked the slice. v2.1: parses,
    runs as informational PASS at check-execution time."""
    spec, _warnings = parse_spec({
        "intent": "x",
        "project_kind": "webapp",
        "slices": [{
            "id": "s",
            "title": "t",
            "checks": [{
                "kind": "state_invariant",
                "description": "App shell exists",
                "expression": "App shell exists",  # prose, not Python
            }],
        }],
    })
    assert len(spec.slices[0].checks) == 1
    assert spec.slices[0].checks[0].kind == "state_invariant"


# ---------------------------------------------------------------------------
# F3: amendments coercion
# ---------------------------------------------------------------------------


def test_amendment_as_string_coerced_with_warning() -> None:
    """v1 R22: non-dict amendment entries cheap-failed. v2.1: coerced
    into a synthesized Amendment, warning recorded."""
    spec, warnings = parse_spec({
        "intent": "x",
        "project_kind": "webapp",
        "slices": [{"id": "s", "title": "t"}],
        "amendments": ["initial review note"],
    })
    assert len(spec.amendments) == 1
    assert "initial review note" in spec.amendments[0].reason
    assert spec.amendments[0].actor == "parser-coerced"
    assert any(w.code == "spec.coerce.amendment" for w in warnings)


def test_amendments_non_list_coerced() -> None:
    spec, warnings = parse_spec({
        "intent": "x",
        "project_kind": "webapp",
        "slices": [{"id": "s", "title": "t"}],
        "amendments": "not a list",
    })
    assert spec.amendments == []
    assert any(w.code == "spec.coerce.field" and w.path == "amendments" for w in warnings)


# ---------------------------------------------------------------------------
# F4: slice id is free-form; parser slugifies
# ---------------------------------------------------------------------------


def test_slice_id_slugified_with_warning() -> None:
    """v1: regex `^[a-z][a-z0-9_-]*$` rejected `Auth Slice`. v2.1: slugify."""
    spec, warnings = parse_spec({
        "intent": "x",
        "slices": [{"id": "Auth Slice", "title": "t"}],
    })
    assert spec.slices[0].id == "auth-slice"
    assert any(w.code == "spec.coerce.slice_id" for w in warnings)


def test_missing_slice_id_synthesized() -> None:
    spec, warnings = parse_spec({
        "intent": "x",
        "slices": [{"title": "t"}],
    })
    assert spec.slices[0].id == "slice_0"
    assert any(w.code == "spec.coerce.slice_id" for w in warnings)


def test_duplicate_slice_id_auto_disambiguated() -> None:
    spec, warnings = parse_spec({
        "intent": "x",
        "slices": [
            {"id": "auth", "title": "first"},
            {"id": "auth", "title": "second"},
        ],
    })
    ids = [s.id for s in spec.slices]
    assert ids[0] == "auth"
    assert ids[1] == "auth-2"
    assert any(w.code == "spec.coerce.duplicate_id" for w in warnings)


# ---------------------------------------------------------------------------
# F5: empty checks allowed
# ---------------------------------------------------------------------------


def test_slice_with_empty_checks_parses() -> None:
    """v1: rejected. v2.1: parses; slice vacuously passes its own checks
    list. Audit's contract gate verifies the integrated product."""
    spec, _warnings = parse_spec({
        "intent": "x",
        "slices": [{"id": "structural-only", "title": "t", "checks": []}],
    })
    assert spec.slices[0].checks == []


# ---------------------------------------------------------------------------
# F9: project_kind is open-enum
# ---------------------------------------------------------------------------


def test_unknown_project_kind_parses_with_warning() -> None:
    """v1: rejected `mobile`. v2.1: parses, warning records the deviation."""
    spec, warnings = parse_spec({
        "intent": "an iOS app",
        "project_kind": "mobile",
        "slices": [{"id": "s", "title": "t"}],
    })
    assert spec.project_kind == "mobile"
    assert any(w.code == "spec.coerce.project_kind" for w in warnings)


def test_missing_project_kind_defaults_to_webapp() -> None:
    spec, _warnings = parse_spec({
        "intent": "x",
        "slices": [{"id": "s", "title": "t"}],
    })
    assert spec.project_kind == "webapp"


# ---------------------------------------------------------------------------
# Slices coercion: non-list, dict, malformed entries
# ---------------------------------------------------------------------------


def test_slices_as_dict_wrapped_in_list() -> None:
    """v1: type-check failed. v2.1: the single dict is wrapped."""
    spec, warnings = parse_spec({
        "intent": "x",
        "slices": {"id": "only", "title": "t"},
    })
    assert len(spec.slices) == 1
    assert spec.slices[0].id == "only"
    assert any(w.code == "spec.coerce.field" and w.path == "slices" for w in warnings)


def test_slice_entry_not_a_dict_dropped() -> None:
    spec, warnings = parse_spec({
        "intent": "x",
        "slices": [{"id": "real", "title": "t"}, "garbage", 42],
    })
    assert [s.id for s in spec.slices] == ["real"]
    coerce_warnings = [w for w in warnings if w.code == "spec.coerce.slice"]
    assert len(coerce_warnings) == 2


# ---------------------------------------------------------------------------
# Unknown check kinds dropped, not raised
# ---------------------------------------------------------------------------


def test_unknown_check_kind_drops_check_with_warning() -> None:
    spec, warnings = parse_spec({
        "intent": "x",
        "slices": [{
            "id": "s",
            "title": "t",
            "checks": [
                {"kind": "rumor"},
                {"kind": "pytest", "selector": "tests/test_x.py"},
            ],
        }],
    })
    kinds = [c.kind for c in spec.slices[0].checks]
    assert kinds == ["pytest"]
    assert any(w.code == "spec.coerce.unknown_kind" for w in warnings)


# ---------------------------------------------------------------------------
# Hard rejects (parser STILL raises)
# ---------------------------------------------------------------------------


def test_non_dict_input_raises() -> None:
    """Truly unusable: input isn't even a JSON object."""
    with pytest.raises(SpecValidationError):
        parse_spec("not a dict")


def test_empty_input_raises() -> None:
    """No intent, no slices, no structure → nothing to build."""
    with pytest.raises(SpecValidationError):
        parse_spec({})


def test_only_intent_is_enough_to_parse() -> None:
    """Single non-empty signal is enough; the rest defaults."""
    spec, _warnings = parse_spec({"intent": "build something"})
    assert spec.intent == "build something"
    assert spec.slices == []


# ---------------------------------------------------------------------------
# Back-compat: spec_from_dict still returns a Spec (no warnings exposed)
# ---------------------------------------------------------------------------


def test_spec_from_dict_drops_warnings_silently() -> None:
    """Callers that don't care about warnings still get a Spec back."""
    spec = spec_from_dict({
        "intent": "x",
        "slices": [{"id": "s", "title": "t", "checks": [{"kind": "rumor"}]}],
    })
    assert spec.slices[0].checks == []  # unknown kind dropped


# ---------------------------------------------------------------------------
# Round-trip: coerced specs re-serialize cleanly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R26 regression: full cascade — parse + check execution stays unblocked
# ---------------------------------------------------------------------------


def test_r26_cascade_fully_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """R26 (2026-05-03): bench round 0/7 slices landed despite all
    evaluators passing. Cause: compile agent emitted prose
    state_invariant.expression — v1 raised, slice blocked.

    v2.1 closes both halves of the cascade:
    1. Parser: prose state_invariant parses fine.
    2. Runner: prose evaluation returns informational PASS, not block.

    This test verifies the full R26 path with the kind of compile output
    that triggered the failure (multiple slices, mixed checks, prose
    state_invariants throughout).
    """
    from otto.checks import run_check
    from otto.spec_compile import StateInvariant

    r26_shape = {
        "intent": "build microfeed-style social app",
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "slices": [
            {
                "id": "shell",
                "title": "App shell",
                "tasks": ["create_app factory", "blueprint setup"],
                "checks": [{
                    "kind": "state_invariant",
                    "description": "App shell has create_app factory and database setup",
                    "expression": "App shell has create_app factory and database setup",
                }],
            },
            {
                "id": "auth",
                "title": "User auth",
                "deps": ["shell"],
                "checks": [{
                    "kind": "state_invariant",
                    "description": "Auth blueprint registered",
                    "expression": "Auth blueprint registered",
                }],
            },
        ],
    }

    spec, warnings = parse_spec(r26_shape)
    assert len(spec.slices) == 2
    # Parser preserves the prose; runner is what handles it.
    assert spec.slices[0].checks[0].expression.startswith("App shell")

    # Now execute one of those checks — should be informational PASS.
    check = spec.slices[0].checks[0]
    assert isinstance(check, StateInvariant)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert evidence.raw["non_python_expression"] is True

    # Parser emitted no warnings for this input — prose is a runtime
    # concern, not a parse concern. The runner's diagnostic carries the
    # fact that eval was skipped.
    assert all(w.code != "spec.coerce.unknown_kind" for w in warnings)


def test_coerced_spec_roundtrips_through_json() -> None:
    """After permissive parse + serialize, a second parse produces the
    same Spec (warnings empty the second time since coercion already
    happened)."""
    from otto.spec_compile import spec_to_dict

    spec1, warnings1 = parse_spec({
        "intent": "x",
        "project_kind": "mobile",
        "slices": [{"id": "Auth Slice", "title": "t"}],
        "amendments": ["initial note"],
    })
    assert warnings1  # first parse coerced things

    serialized = json.dumps(spec_to_dict(spec1))
    spec2, warnings2 = parse_spec(json.loads(serialized))

    assert spec2.project_kind == "mobile"  # preserved
    assert spec2.slices[0].id == "auth-slice"
    assert len(spec2.amendments) == 1
    # The second parse SHOULD still warn about unknown project_kind because
    # `mobile` remains non-canonical. But slice id and amendment are now
    # canonical-shaped.
    coerce_id_warnings = [w for w in warnings2 if w.code == "spec.coerce.slice_id"]
    coerce_amend_warnings = [w for w in warnings2 if w.code == "spec.coerce.amendment"]
    assert not coerce_id_warnings
    assert not coerce_amend_warnings
