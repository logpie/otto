"""Regression: the smoke-pre repair wall-clock budget must scale with
the number of failing UI journeys.

resume16n proved the cascade-aware repair agent CAN fix the genuine
product gaps — register_and_create_first_issue went fail->pass via real
product commits (a8fc5f5 + 623ce2e fixed the workspace-creation /
onboarding dead-end). But the fixed ~1199s wall killed it mid-work on
the remaining 3 cross-feature journeys (each = diagnose + product fix +
a ~5min cold clean-verify oracle re-run). Root cause = budget too small
for N cross-feature journeys, not "unbuildable". Fix: base 1200s per
failing journey, capped at 6000s; only journey failures scale it
(n=0 -> base, other repair phases unchanged); explicit config override
still wins. Not gate-weakening: journeys must still genuinely pass and
idle/cost/diff-churn budgets + agent escalation still terminate a
non-progressing repair.
"""

from __future__ import annotations

from typing import Any

from otto.v5_runner import _failing_ui_journey_count, _repair_budget_from_config


def _journeys_failed(n: int) -> dict[str, Any]:
    names = ",".join(f"journey_{i}" for i in range(n))
    return {
        "steps": [
            {"id": "npm_build:frontend", "status": "passed"},
            {"id": "start", "status": "passed"},
            {"id": "ui_journeys", "status": "failed", "reason": f"ui journeys failed: {names}"},
        ]
    }


def test_counts_failing_journeys() -> None:
    assert _failing_ui_journey_count(_journeys_failed(4)) == 4
    assert _failing_ui_journey_count(_journeys_failed(1)) == 1


def test_zero_when_no_journey_step() -> None:
    # A clean_deploy/port failure (no ui_journeys step) must not scale —
    # other repair phases keep the base budget.
    assert _failing_ui_journey_count({"steps": [{"id": "start", "status": "failed"}]}) == 0
    assert _failing_ui_journey_count({}) == 0
    assert _failing_ui_journey_count(None) == 0
    # ui_journeys present but PASSED -> 0.
    assert _failing_ui_journey_count(
        {"steps": [{"id": "ui_journeys", "status": "passed"}]}
    ) == 0


def _scaled(n: int) -> float:
    # Mirror the call-site formula.
    return min(6000.0, 1200.0 * max(1, n))


def test_scaling_formula_bounds() -> None:
    assert _scaled(0) == 1200.0  # base, unchanged (other phases)
    assert _scaled(1) == 1200.0
    assert _scaled(4) == 4800.0  # the resume16n case: 4 cross-feature journeys
    assert _scaled(5) == 6000.0  # cap
    assert _scaled(50) == 6000.0  # bounded — never unbounded


def test_explicit_config_override_still_wins() -> None:
    # An operator-set {prefix}_wall_clock_s must still take precedence
    # over the scaled default (the scaled value is only the *default*).
    b = _repair_budget_from_config(
        {"integration_smoke_pre_repair_wall_clock_s": 999.0},
        prefix="integration_smoke_pre_repair",
        default_agent_turns=40,
        default_oracle_invocations=8,
        default_wall_clock_s=4800.0,
    )
    assert b.wall_clock_s == 999.0
    # No override -> the scaled default is used.
    b2 = _repair_budget_from_config(
        {},
        prefix="integration_smoke_pre_repair",
        default_agent_turns=40,
        default_oracle_invocations=8,
        default_wall_clock_s=4800.0,
    )
    assert b2.wall_clock_s == 4800.0
