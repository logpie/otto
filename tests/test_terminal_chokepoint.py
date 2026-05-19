"""v5 one-hard-gate keystone — terminal chokepoint.

The single place that decides land-vs-terminal. Locked design
2026-05-19 (research-linkboard-overconstraint.md): only INFRA_CORRUPT
refuses; everything else lands + is annotated. Unmapped origin → PRODUCT
(LAND) is the SAFE default in the inverted design (refusal lives at the
git/merge layer, not in these recording helpers).
"""
import inspect

import pytest

from otto.v5_runner import (
    TerminalAction,
    TerminalCause,
    _cause_from_origin,
    resolve_terminal_outcome,
)


def test_resolve_has_no_default_cause():
    """Codex R2#3 intent preserved: a missing classification must be a
    hard error, never a silent default."""
    sig = inspect.signature(resolve_terminal_outcome)
    assert sig.parameters["cause"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "cause,action",
    [
        (TerminalCause.PRODUCT, TerminalAction.LAND_CONTINUE),
        (TerminalCause.VERIFICATION, TerminalAction.LAND_CONTINUE),
        (TerminalCause.CONFLICT_RESIDUAL, TerminalAction.LAND_CONTINUE),
        (TerminalCause.BUDGET_EXHAUSTED, TerminalAction.LAND_STOP),
        (TerminalCause.ENV_UNMEASURED, TerminalAction.LAND_STOP),
        (TerminalCause.INFRA_CORRUPT, TerminalAction.HONEST_TERMINAL),
    ],
)
def test_action_mapping(cause, action):
    assert resolve_terminal_outcome(cause=cause) is action


def test_only_infra_corrupt_refuses():
    """The invariant: exactly one cause yields a non-landing action."""
    refusing = [
        c for c in TerminalCause
        if resolve_terminal_outcome(cause=c) is TerminalAction.HONEST_TERMINAL
    ]
    assert refusing == [TerminalCause.INFRA_CORRUPT]


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("verification", TerminalCause.VERIFICATION),
        ("subtree_propagation", TerminalCause.CONFLICT_RESIDUAL),
        ("merge_repair_helper", TerminalCause.CONFLICT_RESIDUAL),
        ("foundation_clean_boot", TerminalCause.VERIFICATION),
        ("budget", TerminalCause.BUDGET_EXHAUSTED),
        ("missing_toolchain", TerminalCause.ENV_UNMEASURED),
    ],
)
def test_cause_from_known_origin(origin, expected):
    assert _cause_from_origin(origin, None) is expected


def test_cause_from_unknown_origin_is_safe_land():
    """Unmapped origin must land (PRODUCT), never refuse — the safe
    fail direction in the inverted design."""
    c = _cause_from_origin("some_origin_nobody_mapped_yet", None)
    assert c is TerminalCause.PRODUCT
    assert resolve_terminal_outcome(cause=c) is TerminalAction.LAND_CONTINUE
