"""Unit tests for otto/v5_preflight.py — deterministic graph checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from otto.v5_preflight import (
    PreflightIssue,
    check_scaffold_compiles,
    filter_blocked_descendants,
    run_preflight,
    smoke_clean_deploy,
)
from otto.v5_clean_verify import CleanOracleIssue, CleanOracleResult, CleanOracleStepResult, Scope


def _graph(tasks: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "tasks": tasks}


def _clean_oracle_result(
    tmp_path: Path,
    *,
    passed: bool,
    scope: Scope,
    kind: str = "build_failed",
    message: str = "failed",
) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="check",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command_identity="python -m otto.cli clean-verify",
        command=["python", "-m", "otto.cli", "clean-verify"],
        cwd=str(tmp_path),
        env={},
    )
    issue = CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message,
        step_id=step.id,
        command_identity=step.command_identity,
        return_code=step.return_code,
    )
    return CleanOracleResult.from_parts(
        passed=passed,
        scope=scope,
        issues=[] if passed else [issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=tmp_path,
        temp_dir=None,
    )


def test_architect_sub_decomposed_flagged(tmp_path: Path):
    g = _graph({
        "root": {"intent": "Build X", "depends_on": [], "decomposition": "emit"},
        "v5-arch": {
            "intent": "Architect for X. Set up CHARTER.md...",
            "depends_on": [],
            "decomposition": "emit",  # <- bug: sub-decomposed
        },
        "v5-feat-a": {"intent": "Build feature A", "depends_on": ["v5-arch"]},
    })
    issues = run_preflight(tmp_path, g, [])
    kinds = [i.kind for i in issues]
    assert "architect_sub_decomposed" in kinds
    bad = next(i for i in issues if i.kind == "architect_sub_decomposed")
    assert bad.severity == "block"
    assert bad.task_id == "v5-arch"


def test_architect_inline_ok(tmp_path: Path):
    g = _graph({
        "v5-arch": {
            "intent": "Architect for X.",
            "depends_on": [],
            "decomposition": "inline",
            "verdict": "pass",
        },
    })
    (tmp_path / "CHARTER.md").write_text("# x\n")
    issues = run_preflight(tmp_path, g, [])
    assert all(i.kind != "architect_sub_decomposed" for i in issues)


def test_charter_missing_after_architect_pass(tmp_path: Path):
    g = _graph({
        "v5-arch": {
            "intent": "Architect: set up CHARTER",
            "depends_on": [],
            "decomposition": "inline",
            "verdict": "pass",
        },
    })
    # No CHARTER.md at tmp_path
    issues = run_preflight(tmp_path, g, [])
    kinds = [i.kind for i in issues]
    assert "charter_missing" in kinds


def test_charter_present_ok(tmp_path: Path):
    g = _graph({
        "v5-arch": {
            "intent": "Architect: set up CHARTER",
            "depends_on": [],
            "decomposition": "inline",
            "verdict": "pass",
        },
    })
    (tmp_path / "CHARTER.md").write_text("# spec\n")
    issues = run_preflight(tmp_path, g, [])
    assert all(i.kind != "charter_missing" for i in issues)


def test_charter_check_skipped_when_architect_not_done(tmp_path: Path):
    g = _graph({
        "v5-arch": {
            "intent": "Architect: set up CHARTER",
            "depends_on": [],
            "decomposition": "inline",
            "verdict": None,  # still running
        },
    })
    # No CHARTER.md, but architect not done yet, so no issue.
    issues = run_preflight(tmp_path, g, [])
    assert all(i.kind != "charter_missing" for i in issues)


def test_dag_cycle_detected(tmp_path: Path):
    g = _graph({
        "a": {"depends_on": ["b"]},
        "b": {"depends_on": ["c"]},
        "c": {"depends_on": ["a"]},
    })
    issues = run_preflight(tmp_path, g, [])
    kinds = [i.kind for i in issues]
    assert "dag_cycle" in kinds
    cyc = next(i for i in issues if i.kind == "dag_cycle")
    assert cyc.severity == "block"


def test_dag_no_cycle(tmp_path: Path):
    g = _graph({
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["a", "b"]},
    })
    issues = run_preflight(tmp_path, g, [])
    assert all(i.kind != "dag_cycle" for i in issues)


def test_duplicate_task_ids_in_pending(tmp_path: Path):
    g = _graph({})
    pending = [
        {"task_id": "v5-x", "intent": "first"},
        {"task_id": "v5-y", "intent": "other"},
        {"task_id": "v5-x", "intent": "duplicate!"},
    ]
    issues = run_preflight(tmp_path, g, pending)
    kinds = [i.kind for i in issues]
    assert "duplicate_task_id" in kinds


def test_filter_blocked_descendants_removes_grandchildren(tmp_path: Path):
    g = _graph({
        "v5-arch": {"depends_on": []},
        "v5-grand-a": {"depends_on": ["v5-arch"]},
        "v5-grand-b": {"depends_on": ["v5-arch"]},
        "v5-feat": {"depends_on": ["v5-arch"]},
    })
    pending = [
        {"task_id": "v5-grand-a", "depends_on": ["v5-arch"]},
        {"task_id": "v5-grand-b", "depends_on": ["v5-arch"]},
        {"task_id": "v5-feat", "depends_on": ["v5-arch"]},
    ]
    blocking = [
        PreflightIssue(
            kind="architect_sub_decomposed",
            severity="block",
            message="...",
            task_id="v5-arch",
        )
    ]
    filtered, blocked = filter_blocked_descendants(g, pending, blocking)
    # Children of architect should be removed
    filtered_ids = {e["task_id"] for e in filtered}
    assert "v5-grand-a" not in filtered_ids
    assert "v5-grand-b" not in filtered_ids
    assert "v5-feat" not in filtered_ids
    assert "v5-arch" in blocked


def test_clean_graph_no_issues(tmp_path: Path):
    g = _graph({
        "v5-arch": {
            "intent": "Architect for X.",
            "depends_on": [],
            "decomposition": "inline",
            "verdict": "pass",
        },
        "v5-feat-a": {
            "intent": "Build feature A",
            "depends_on": ["v5-arch"],
            "decomposition": "inline",
        },
    })
    (tmp_path / "CHARTER.md").write_text("# x\n")
    issues = run_preflight(tmp_path, g, [])
    assert issues == []


def test_check_scaffold_compiles_maps_script_valid_failure_to_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify_from_clean(*_args, **_kwargs) -> CleanOracleResult:
        return _clean_oracle_result(
            tmp_path,
            passed=False,
            scope="scaffold",
            kind="script_valid_failed",
            message="start.sh uses bash-4-only expansion",
        )

    monkeypatch.setattr(
        "otto.v5_clean_verify.verify_from_clean_oracle",
        fake_verify_from_clean,
    )

    issues = check_scaffold_compiles(tmp_path, architect_task_id="v5-arch")

    assert issues == [
        PreflightIssue(
            kind="script_valid_failed",
            severity="block",
            message="start.sh uses bash-4-only expansion",
            task_id="v5-arch",
        )
    ]


def test_smoke_clean_deploy_maps_port_busy_to_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "start.sh").write_text("#!/usr/bin/env bash\necho ok\n")

    def fake_verify_from_clean(*_args, **_kwargs) -> CleanOracleResult:
        return _clean_oracle_result(
            tmp_path,
            passed=False,
            scope="subtree",
            kind="port_busy",
            message="Declared ports [18080] already bound",
        )

    monkeypatch.setattr(
        "otto.v5_clean_verify.verify_from_clean_oracle",
        fake_verify_from_clean,
    )

    issues = smoke_clean_deploy(tmp_path)

    assert issues == [
        PreflightIssue(
            kind="clean_deploy_port_busy",
            severity="block",
            message="Declared ports [18080] already bound",
        )
    ]
