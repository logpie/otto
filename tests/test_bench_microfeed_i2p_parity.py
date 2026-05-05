"""Unit-style tests for the Microfeed i2p bench parity verdict logic.

Covers gap A14 from `docs/codex-followups.md`: previously the bench
emitted `verdict=i2p_passed` even when `wall_s` exceeded the parity
ceiling (1500s). The verdict now honors all parity criteria from
plan.md Step 11 and reports an explicit per-criterion decomposition in
`summary.parity`.

These tests exercise the pure verdict function only — no real-cost
`otto run` invocation is performed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bench_microfeed_i2p as bench  # noqa: E402


def _passing_summary(**overrides) -> bench.I2pSummary:
    s = bench.I2pSummary(
        audit_verdict="passed",
        hidden_result="pass",
        browser_result="pass",
        slices_blocked=0,
        wall_s=900.0,
        quality_score=4,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def test_verdict_all_green_returns_i2p_passed() -> None:
    s = _passing_summary()
    assert bench._verdict(s) == "i2p_passed"
    assert s.parity["wall_s"]["passed"] is True
    assert all(s.parity[k]["passed"] for k in s.parity)


def test_wall_excess_blocks_i2p_passed() -> None:
    """A14 regression: wall_s above ceiling must NOT emit i2p_passed."""
    s = _passing_summary(wall_s=2123.0)  # the actual failing-bench value
    verdict = bench._verdict(s)
    assert verdict == "i2p_partial_wall_exceeded"
    assert s.parity["wall_s"]["passed"] is False
    assert s.parity["wall_s"]["value"] == 2123.0
    assert s.parity["wall_s"]["ceiling"] == bench._WALL_CEILING_S


def test_wall_at_ceiling_passes() -> None:
    s = _passing_summary(wall_s=bench._WALL_CEILING_S)
    assert bench._verdict(s) == "i2p_passed"
    assert s.parity["wall_s"]["passed"] is True


def test_audit_failed_dominates_wall_excess() -> None:
    """Functional failure outranks wall-excess in the verdict ladder."""
    s = _passing_summary(audit_verdict="failed", wall_s=2123.0)
    assert bench._verdict(s) == "i2p_failed"
    assert s.parity["audit_verdict_passed"]["passed"] is False
    assert s.parity["wall_s"]["passed"] is False


def test_quality_low_dominates_wall_excess() -> None:
    s = _passing_summary(quality_score=2, wall_s=2123.0)
    assert bench._verdict(s) == "i2p_quality_low"


def test_quality_zero_treated_as_no_signal() -> None:
    """quality_score=0 means audit didn't assess — not a failure."""
    s = _passing_summary(quality_score=0)
    assert bench._verdict(s) == "i2p_passed"
    assert s.parity["quality_score"]["passed"] is True


def test_blocked_slices_fail_functional() -> None:
    s = _passing_summary(slices_blocked=1)
    assert bench._verdict(s) == "i2p_failed"
    assert s.parity["zero_slices_blocked"]["passed"] is False


def test_hidden_eval_fail_blocks_pass() -> None:
    s = _passing_summary(hidden_result="fail")
    assert bench._verdict(s) == "i2p_failed"


def test_browser_eval_fail_blocks_pass() -> None:
    s = _passing_summary(browser_result="fail")
    assert bench._verdict(s) == "i2p_failed"
