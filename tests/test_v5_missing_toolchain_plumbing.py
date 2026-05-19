"""Regression: the P0 ``missing_toolchain`` thin-slice (Codex Plan Gate R3#2).

The Otto-seeded start.sh exits 86 (with a ``missing_toolchain`` sentinel)
when the host has no usable uv / Python 3.12. That is an ENVIRONMENT
failure, not a product defect or an Otto crash. R3#2 required the full thin
slice so it is never half-plumbed: a distinct clean-verify kind, a distinct
preflight legacy kind (NOT collapsed into generic start/smoke), non-repairable
at the repair gate (never architect re-entry / bounded child repair), and a
distinct non-success CLI exit (not reported as a product merge_blocked / not
conflated with catastrophic).
"""

from __future__ import annotations

import typing

from otto.repair_gates import NO_REPAIR, REPAIR_NOW, repair_gate_for_verdict
from otto.v5_clean_verify import CleanOracleIssue, FailureKind
from otto.v5_preflight import preflight_issues_from_clean_oracle


def test_missing_toolchain_is_a_first_class_clean_verify_kind() -> None:
    assert "missing_toolchain" in typing.get_args(FailureKind)


class _Result:
    """Minimal stand-in for CleanOracleResult (preflight only reads
    .passed and .issues)."""

    def __init__(self, issues: list[CleanOracleIssue]) -> None:
        self.passed = False
        self.issues = issues


def test_preflight_keeps_missing_toolchain_distinct_not_collapsed() -> None:
    issue = CleanOracleIssue(
        kind="missing_toolchain",
        severity="block",
        message="start.sh could not find a usable uv / Python 3.12 toolchain",
        step_id="start",
    )
    mapped = preflight_issues_from_clean_oracle(
        _Result([issue]), surface="clean_deploy"
    )
    assert len(mapped) == 1
    # distinct kind — NOT clean_deploy_start_failed / _smoke_error
    assert mapped[0].kind == "clean_deploy_missing_toolchain"
    assert mapped[0].kind not in {
        "clean_deploy_start_failed",
        "clean_deploy_smoke_error",
        "clean_deploy_ports_not_listening",
    }


def test_repair_gate_treats_missing_toolchain_non_repairable() -> None:
    for code in ("missing_toolchain", "clean_deploy_missing_toolchain"):
        decision = repair_gate_for_verdict(
            {"verdict": "failed", "failure_kind": code}
        )
        assert decision.action == NO_REPAIR
        assert decision.actionable is False
        assert "toolchain" in decision.reason.lower()


def test_repair_gate_still_repairs_a_real_product_failure() -> None:
    """Guard: the non-repairable addition must not over-block real defects."""
    decision = repair_gate_for_verdict(
        {
            "verdict": "failed",
            "failure_kind": "ui_journey_failed",
            "failing_evidence": [{"journey": "j1", "detail": "500 on submit"}],
        }
    )
    assert decision.action == REPAIR_NOW
    assert decision.actionable is True


def test_cli_v5_exits_distinctly_for_missing_toolchain() -> None:
    """The CLI must give the environment failure a distinct non-success exit
    (3), separate from catastrophic (1) and product success/merge_blocked."""
    src = __import__("pathlib").Path(
        __import__("otto.cli_v5", fromlist=["__file__"]).__file__
    ).read_text()
    assert 'if "missing_toolchain" in (result.failure_reason or ""):' in src
    assert "sys.exit(3)" in src
    # the catastrophic exit(1) must still be a separate, later branch
    assert src.index("sys.exit(3)") < src.index(
        'if result.verdict == "catastrophic":'
    )
