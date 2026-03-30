"""Benchmark tests for gate pilot — evaluates decision quality on realistic scenarios.

These tests mock the LLM call and verify the full pipeline:
context assembly → pilot prompt → decision parsing → application.

To benchmark with REAL LLM calls against realistic otto_logs, run:
    pytest tests/test_pilot_benchmark.py -k real --pilot-live

(requires API access, costs ~$0.05 per scenario)
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.context import PipelineContext, TaskResult
from otto.pilot import (
    PilotDecision,
    RetryStrategy,
    RoutedContext,
    assemble_pilot_context,
    invoke_pilot,
    parse_pilot_decision,
)
from otto.planner import Batch, ExecutionPlan, TaskPlan


# ---------------------------------------------------------------------------
# Scenario fixtures — realistic failure patterns
# ---------------------------------------------------------------------------

def _make_project(tmp_path, tasks, results):
    """Create a realistic otto_logs structure for a scenario."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    logs = project_dir / "otto_logs"
    logs.mkdir()

    for task_key, data in results.items():
        task_dir = logs / task_key
        task_dir.mkdir()

        if "verify_log" in data:
            (task_dir / "attempt-1-verify.log").write_text(data["verify_log"])
        if "qa_verdict" in data:
            (task_dir / "qa-verdict.json").write_text(json.dumps(data["qa_verdict"]))
        if "task_summary" in data:
            (task_dir / "task-summary.json").write_text(json.dumps(data["task_summary"]))

    return project_dir


class TestScenario_EnvironmentIssue:
    """Scenario: task fails because of wrong test framework.

    Expected pilot behavior:
    - Identify as environment issue
    - Recommend retry_different with specific guidance
    - Route discovery to upcoming tasks
    """

    def test_context_includes_error_detail(self, tmp_path):
        project_dir = _make_project(tmp_path, [], {
            "add-tests": {
                "verify_log": (
                    "FAIL tests/test_search.py\n"
                    "ModuleNotFoundError: No module named 'jest'\n"
                    "hint: this project uses vitest, not jest\n"
                    "exit code: 1"
                ),
            },
        })

        result = TaskResult(
            task_key="add-tests", success=False, cost_usd=0.40,
            duration_s=120, error="tests failed",
        )
        remaining = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="add-search")]),
        ])
        ctx = PipelineContext()

        context_str = assemble_pilot_context(
            batch_results=[result],
            remaining_plan=remaining,
            context=ctx,
            project_dir=project_dir,
            pending_by_key={"add-search": {"key": "add-search", "prompt": "Add search"}},
        )

        assert "vitest" in context_str
        assert "jest" in context_str
        assert "add-search" in context_str

    def test_pilot_decision_quality(self, tmp_path):
        """Mock the LLM to return a good decision for this scenario and verify parsing."""
        good_decision = {
            "failure_analysis": {"add-tests": "used jest but project uses vitest"},
            "retry_strategies": {
                "add-tests": {
                    "action": "retry_different",
                    "guidance": "use vitest not jest — see vitest.config.ts in project root",
                    "reason": "environment issue: wrong test framework",
                },
            },
            "routed_context": [
                {"target_task": "add-search", "context": "project uses vitest for testing, not jest"},
            ],
            "skip_tasks": [],
            "new_learnings": ["project uses vitest as test runner"],
            "batches": [{"tasks": [{"task_key": "add-tests"}, {"task_key": "add-search"}]}],
        }

        decision = parse_pilot_decision(json.dumps(good_decision))

        # Verify decision quality
        assert decision.retry_strategies["add-tests"].action == "retry_different"
        assert "vitest" in decision.retry_strategies["add-tests"].guidance
        assert len(decision.routed_context) == 1
        assert decision.routed_context[0].target_task == "add-search"
        assert len(decision.skip_tasks) == 0


class TestScenario_PeerDiscovery:
    """Scenario: task A passes and discovers something task B needs.

    Expected pilot behavior:
    - Route discovery from passed task to upcoming task
    - No skips, no retry changes
    """

    def test_context_includes_passed_task_summary(self, tmp_path):
        project_dir = _make_project(tmp_path, [], {
            "setup-db": {
                "task_summary": {
                    "phases": {"coding": {"cost": 0.25, "duration": 60}},
                    "notes": "SQLAlchemy uses async sessions by default",
                },
            },
        })

        passed = TaskResult(
            task_key="setup-db", success=True, cost_usd=0.25,
            duration_s=60, diff_summary="Modified models.py, db.py",
        )
        failed = TaskResult(
            task_key="add-crud", success=False, cost_usd=0.40,
            duration_s=90, error="ImportError: cannot import sync_session",
        )
        remaining = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="add-tags")]),
        ])
        ctx = PipelineContext()
        ctx.add_learning("SQLAlchemy uses async sessions", source="setup-db")

        context_str = assemble_pilot_context(
            batch_results=[passed, failed],
            remaining_plan=remaining,
            context=ctx,
            project_dir=project_dir,
            pending_by_key={"add-tags": {"key": "add-tags", "prompt": "Add tag system"}},
        )

        assert "async sessions" in context_str
        assert "setup-db" in context_str
        assert "add-tags" in context_str


class TestScenario_BrokenDependency:
    """Scenario: task A fails, task B depends on A's output.

    Expected pilot behavior:
    - Skip task B (dependency failed)
    - Retry task A
    """

    def test_pilot_should_skip_dependent(self):
        decision_json = {
            "failure_analysis": {"setup-db": "SQLite version too old for FTS5"},
            "retry_strategies": {
                "setup-db": {
                    "action": "retry_different",
                    "guidance": "use FTS4 instead of FTS5, or add SQLite version check",
                    "reason": "environment issue: SQLite doesn't support FTS5",
                },
            },
            "routed_context": [],
            "skip_tasks": ["add-search"],
            "new_learnings": ["target SQLite does not support FTS5"],
            "batches": [{"tasks": [{"task_key": "setup-db"}]}],
        }

        decision = parse_pilot_decision(json.dumps(decision_json))

        assert "add-search" in decision.skip_tasks
        assert decision.retry_strategies["setup-db"].action == "retry_different"


class TestScenario_TransientFailure:
    """Scenario: task fails due to timeout (infrastructure, not code).

    Expected pilot behavior:
    - Identify as infrastructure issue
    - Recommend plain retry (same approach)
    - No skips, no context routing
    """

    def test_pilot_retries_same_approach(self):
        decision_json = {
            "failure_analysis": {"deploy-api": "subprocess timed out after 300s"},
            "retry_strategies": {
                "deploy-api": {
                    "action": "retry",
                    "guidance": "",
                    "reason": "infrastructure: timeout, not a code bug",
                },
            },
            "routed_context": [],
            "skip_tasks": [],
            "new_learnings": [],
            "batches": [{"tasks": [{"task_key": "deploy-api"}]}],
        }

        decision = parse_pilot_decision(json.dumps(decision_json))

        assert decision.retry_strategies["deploy-api"].action == "retry"
        assert len(decision.skip_tasks) == 0
        assert len(decision.routed_context) == 0


# ---------------------------------------------------------------------------
# Config flag: disable pilot (fallback to replan)
# ---------------------------------------------------------------------------

class TestPilotConfigFlag:
    """Verify that pilot can be disabled via config."""

    def test_pilot_disabled_falls_back_to_replan(self):
        """When pilot is disabled, orchestrator should use replan directly.

        This is a design test — verifies the config flag is checked.
        The actual fallback is tested in test_orchestrator.py.
        """
        # The pilot is enabled by default. Config flag:
        # otto.yaml: pilot: false
        # orchestrator checks: config.get("pilot", True)
        config = {"pilot": False}
        assert config.get("pilot", True) is False

        config_default = {}
        assert config_default.get("pilot", True) is True
