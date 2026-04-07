"""Tests for otto.pipeline — build pipeline dataclasses and helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from otto.pipeline import (
    BuildMode,
    BuildResult,
    _plan_fingerprint,
    build_product,
    resolve_build_mode,
)
from otto.tasks import add_task, load_tasks, update_task


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


@pytest.mark.asyncio
async def test_build_product_no_qa_fails_on_partial_build_and_preserves_unrelated_pending(tmp_git_repo: Path):
    tasks_path = tmp_git_repo / "tasks.yaml"
    add_task(tasks_path, "stale pending task")
    historical = add_task(tasks_path, "historical completed task")
    update_task(tasks_path, historical["key"], status="passed")

    product_spec_path = tmp_git_repo / "product-spec.md"
    product_spec_path.write_text("# Product Spec\n")

    async def fake_plan(intent, project_dir, config):
        return SimpleNamespace(
            mode="decomposed",
            tasks=[
                SimpleNamespace(prompt="Build API", depends_on=[]),
                SimpleNamespace(prompt="Build UI", depends_on=[]),
            ],
            product_spec_path=product_spec_path,
            cost_usd=0.0,
        )

    async def fake_run_per(config, tasks_path, project_dir):
        tasks = load_tasks(tasks_path)
        prompts = {task["prompt"]: task for task in tasks}
        assert "stale pending task" in prompts
        assert "historical completed task" in prompts
        assert prompts["stale pending task"].get("build_id") is None
        assert prompts["Build API"]["build_id"] == config["build_id"]
        assert prompts["Build UI"]["build_id"] == config["build_id"]
        update_task(tasks_path, prompts["Build API"]["key"], status="passed")
        update_task(tasks_path, prompts["Build UI"]["key"], status="failed")
        return 1

    with patch("otto.product_planner.run_product_planner", side_effect=fake_plan):
        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            result = await build_product(
                "Build a product",
                tmp_git_repo,
                {"execution_mode": "planned", "skip_product_qa": True},
            )

    assert result.passed is False
    assert result.tasks_passed == 1
    assert result.tasks_failed == 1

    final_prompts = {task["prompt"] for task in load_tasks(tasks_path)}
    assert "stale pending task" in final_prompts
    assert "historical completed task" in final_prompts


@pytest.mark.asyncio
@pytest.mark.skip(reason="Verification now runs as subprocess — internal mocking doesn't apply")
async def test_build_product_verification_can_override_partial_build_failure(tmp_git_repo: Path):
    product_spec_path = tmp_git_repo / "product-spec.md"
    product_spec_path.write_text("# Product Spec\n")

    async def fake_plan(intent, project_dir, config):
        return SimpleNamespace(
            mode="decomposed",
            tasks=[
                SimpleNamespace(prompt="Build backend", depends_on=[]),
                SimpleNamespace(prompt="Build frontend", depends_on=[]),
            ],
            product_spec_path=product_spec_path,
            cost_usd=0.0,
        )

    async def fake_run_per(config, tasks_path, project_dir):
        tasks = {task["prompt"]: task for task in load_tasks(tasks_path)}
        update_task(tasks_path, tasks["Build backend"]["key"], status="passed")
        update_task(tasks_path, tasks["Build frontend"]["key"], status="failed")
        return 1

    with patch("otto.product_planner.run_product_planner", side_effect=fake_plan):
        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            fake_sp_result = MagicMock()
            fake_sp_result.returncode = 0
            fake_sp_result.stdout = json.dumps({
                "product_passed": True, "rounds": 2, "total_cost": 0.25,
                "journeys": [{"name": "happy path", "passed": True}],
            })
            fake_sp_result.stderr = ""
            with patch("subprocess.run", return_value=fake_sp_result) as verify_sync:
                result = await build_product(
                    "Build a product",
                    tmp_git_repo,
                    {"execution_mode": "planned"},
                )

    assert result.passed is True
    assert result.tasks_passed == 1
    assert result.tasks_failed == 1
    assert verify_sync.call_args.args[1] == product_spec_path


@pytest.mark.asyncio
@pytest.mark.skip(reason="Verification now runs as subprocess — internal mocking doesn't apply")
async def test_build_product_counts_verification_fix_tasks_for_same_build(tmp_git_repo: Path):
    product_spec_path = tmp_git_repo / "product-spec.md"
    product_spec_path.write_text("# Product Spec\n")

    async def fake_plan(intent, project_dir, config):
        return SimpleNamespace(
            mode="decomposed",
            tasks=[SimpleNamespace(prompt="Build backend", depends_on=[])],
            product_spec_path=product_spec_path,
            cost_usd=0.0,
        )

    async def fake_run_per(config, tasks_path, project_dir):
        tasks = {task["prompt"]: task for task in load_tasks(tasks_path)}
        update_task(tasks_path, tasks["Build backend"]["key"], status="passed")
        return 0

    def fake_verify(intent, grounding_path, project_dir, tasks_path, config):
        fix_task = add_task(
            tasks_path,
            "Fix checkout flow",
            spec=[{"text": "Checkout flow works", "binding": "must"}],
            build_id=config["build_id"],
        )
        update_task(tasks_path, fix_task["key"], status="passed")
        return {
            "product_passed": True,
            "rounds": 2,
            "total_cost": 0.25,
            "journeys": [{"name": "happy path", "passed": True}],
        }

    fake_sp_result2 = MagicMock()
    fake_sp_result2.returncode = 0
    fake_sp_result2.stdout = json.dumps({
        "product_passed": True, "rounds": 2, "total_cost": 0.25,
        "journeys": [{"name": "happy path", "passed": True}],
    })
    fake_sp_result2.stderr = ""

    with patch("otto.product_planner.run_product_planner", side_effect=fake_plan):
        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            with patch("subprocess.run", return_value=fake_sp_result2):
                result = await build_product(
                    "Build a product",
                    tmp_git_repo,
                    {"execution_mode": "planned"},
                )

    assert result.passed is True
    assert result.tasks_passed == 2
    assert result.tasks_failed == 0
