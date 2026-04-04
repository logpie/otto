"""Tests for otto.pipeline — build pipeline dataclasses and helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from otto.pipeline import (
    BuildMode,
    BuildResult,
    _plan_fingerprint,
    resolve_build_mode,
)


def test_resolve_build_mode_defaults():
    """Monolithic mode: no planner, no parallel, grounding=intent."""
    mode = resolve_build_mode({})
    assert mode.use_planner is False
    assert mode.parallel is False
    assert mode.grounding == "intent"

    # Explicit monolithic
    mode = resolve_build_mode({"execution_mode": "monolithic"})
    assert mode.use_planner is False
    assert mode.parallel is False
    assert mode.grounding == "intent"


def test_resolve_build_mode_planned():
    """Planned mode: planner enabled, parallel when max_parallel > 1."""
    mode = resolve_build_mode({"execution_mode": "planned"})
    assert mode.use_planner is True
    assert mode.parallel is False  # max_parallel defaults to 1
    assert mode.grounding == "spec"

    mode = resolve_build_mode({"execution_mode": "planned", "max_parallel": 4})
    assert mode.use_planner is True
    assert mode.parallel is True
    assert mode.grounding == "spec"


def test_resolve_build_mode_flag_override():
    """use_planner flag overrides execution_mode default."""
    # Force planner on monolithic
    mode = resolve_build_mode({"execution_mode": "monolithic", "use_planner": True})
    assert mode.use_planner is True
    assert mode.grounding == "spec"

    # Force planner off planned
    mode = resolve_build_mode({"execution_mode": "planned", "use_planner": False})
    assert mode.use_planner is False


def test_build_mode_finalize():
    """Finalize disables parallel for single_task plans."""
    mode = BuildMode(use_planner=True, parallel=True, grounding="spec")

    finalized = mode.finalize("single_task")
    assert finalized.parallel is False
    assert finalized.use_planner is True

    finalized = mode.finalize("decomposed")
    assert finalized.parallel is True


def test_build_result_dataclass():
    """BuildResult fields are accessible and have sane defaults."""
    result = BuildResult(passed=True, build_id="build-123-456")
    assert result.passed is True
    assert result.build_id == "build-123-456"
    assert result.rounds == 1
    assert result.total_cost == 0.0
    assert result.journeys == []
    assert result.error == ""
    assert result.tasks_passed == 0
    assert result.tasks_failed == 0

    # With all fields
    result = BuildResult(
        passed=False,
        build_id="build-999-1",
        rounds=3,
        total_cost=1.23,
        journeys=[{"name": "login"}],
        error="build failed",
        tasks_passed=2,
        tasks_failed=1,
    )
    assert result.rounds == 3
    assert result.total_cost == 1.23
    assert len(result.journeys) == 1
    assert result.error == "build failed"
    assert result.tasks_passed == 2
    assert result.tasks_failed == 1


def test_plan_fingerprint_deterministic(tmp_path: Path):
    """Same input produces the same fingerprint."""
    spec_path = tmp_path / "product-spec.md"
    spec_path.write_text("# My Product\nFeatures: login, dashboard")

    plan = SimpleNamespace(
        tasks=[
            SimpleNamespace(prompt="Build login", depends_on=[]),
            SimpleNamespace(prompt="Build dashboard", depends_on=[0]),
        ],
        product_spec_path=spec_path,
    )

    fp1 = _plan_fingerprint(plan, tmp_path)
    fp2 = _plan_fingerprint(plan, tmp_path)
    assert fp1 == fp2
    assert len(fp1) == 16  # sha256 hex truncated to 16


def test_plan_fingerprint_changes_with_content(tmp_path: Path):
    """Different plan content produces different fingerprints."""
    spec_path = tmp_path / "product-spec.md"
    spec_path.write_text("# V1")

    plan_a = SimpleNamespace(
        tasks=[SimpleNamespace(prompt="Build A", depends_on=[])],
        product_spec_path=spec_path,
    )
    fp_a = _plan_fingerprint(plan_a, tmp_path)

    spec_path.write_text("# V2")
    fp_b = _plan_fingerprint(plan_a, tmp_path)

    assert fp_a != fp_b  # spec content changed


def test_plan_fingerprint_with_architecture(tmp_path: Path):
    """Architecture.md is included in the fingerprint when present."""
    spec_path = tmp_path / "product-spec.md"
    spec_path.write_text("# Spec")

    plan = SimpleNamespace(
        tasks=[SimpleNamespace(prompt="Build it", depends_on=[])],
        product_spec_path=spec_path,
    )

    fp_without = _plan_fingerprint(plan, tmp_path)

    arch_path = tmp_path / "architecture.md"
    arch_path.write_text("# Architecture\nMonolith")

    fp_with = _plan_fingerprint(plan, tmp_path)

    assert fp_without != fp_with
