"""Tests for otto.orchestrator — PER loop, batch execution, serial merge."""

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from otto.context import PipelineContext, QAMode, TaskResult
from otto.orchestrator import (
    _build_sibling_context,
    _fixed_execution_plan,
    _record_run_history,
    _refresh_task_live_state,
    _run_batch_qa,
    _run_integrated_unit_in_worktree,
    _unit_prompt,
    _run_batch_parallel,
    cleanup_orphaned_worktrees,
    merge_batch_results,
    run_per,
)
from otto.verification import run_product_verification
from otto.planner import Batch, BatchUnit, ExecutionPlan, TaskPlan
from otto.tasks import load_tasks, update_task
from otto.telemetry import Telemetry


class TestCleanupOrphanedWorktrees:
    def test_no_worktrees_dir(self, tmp_git_repo):
        """No .worktrees dir = no-op."""
        cleanup_orphaned_worktrees(tmp_git_repo)
        # Should not raise

    def test_removes_otto_worktrees(self, tmp_git_repo):
        """Should remove directories starting with otto-."""
        wt_dir = tmp_git_repo / ".worktrees"
        wt_dir.mkdir()
        (wt_dir / "otto-task1").mkdir()
        (wt_dir / "otto-task2").mkdir()
        (wt_dir / "other-thing").mkdir()  # should NOT be removed

        cleanup_orphaned_worktrees(tmp_git_repo)

        assert not (wt_dir / "otto-task1").exists()
        assert not (wt_dir / "otto-task2").exists()
        assert (wt_dir / "other-thing").exists()


class TestIntegratedUnitPrompt:
    def test_unit_prompt_includes_other_project_tasks_and_planner_analysis(self):
        prompt = _unit_prompt(
            [TaskPlan(task_key="task-a"), TaskPlan(task_key="task-b")],
            {
                "task-a": {"id": 1, "prompt": "Build data layer"},
                "task-b": {"id": 2, "prompt": "Build service layer"},
                "task-c": {"id": 3, "prompt": "Build CLI"},
            },
            analysis=[
                {
                    "task_a": "task-b",
                    "task_b": "task-c",
                    "relationship": "LAYERED",
                    "reason": "CLI depends on service interfaces",
                }
            ],
        )

        assert "Tasks:" in prompt
        assert "#1 Build data layer" in prompt
        assert "#2 Build service layer" in prompt
        assert "Other tasks in this project" in prompt
        assert "#3 Build CLI" in prompt
        assert "Planner analysis" in prompt
        assert "LAYERED" in prompt

    def test_project_prompt_literal_concatenates_raw_prompts(self):
        from otto.orchestrator import _project_prompt_literal

        prompt = _project_prompt_literal({
            "task-a": {"id": 1, "prompt": "Build data layer."},
            "task-b": {"id": 2, "prompt": "Build service layer."},
            "task-c": {"id": 3, "prompt": "Build CLI."},
        })

        assert prompt == "Build data layer. Build service layer. Build CLI."


class TestFixedExecutionPlan:
    def test_resolves_task_ids_to_keys(self, tmp_path):
        pending = [
            {"id": 1, "key": "task-a", "prompt": "A"},
            {"id": 2, "key": "task-b", "prompt": "B"},
            {"id": 3, "key": "task-c", "prompt": "C"},
        ]
        config = {
            "fixed_plan": {
                "batches": [
                    {"units": [{"task_ids": [1, 2]}]},
                    {"units": [{"task_ids": [3]}]},
                ]
            }
        }
        plan = _fixed_execution_plan(config, pending, tmp_path)
        assert plan is not None
        assert len(plan.batches) == 2
        assert [tp.task_key for tp in plan.batches[0].units[0].tasks] == ["task-a", "task-b"]
        assert [tp.task_key for tp in plan.batches[1].units[0].tasks] == ["task-c"]

    def test_rejects_unknown_task_id(self, tmp_path):
        pending = [{"id": 1, "key": "task-a", "prompt": "A"}]
        config = {"fixed_plan": {"batches": [{"units": [{"task_ids": [99]}]}]}}
        assert _fixed_execution_plan(config, pending, tmp_path) is None

    @pytest.mark.asyncio
    async def test_integrated_unit_execution_passes_enriched_prompt(self, tmp_path):
        captured = {}

        async def fake_run_task_v45(task, config, project_dir, tasks_file=None, task_work_dir=None, qa_mode="per_task", on_progress=None, **kwargs):
            captured["prompt"] = task["prompt"]
            captured["full_project_brief"] = kwargs.get("full_project_brief")
            captured["feedback"] = task.get("feedback", "")
            captured["attempts"] = task.get("attempts")
            captured["spec"] = task.get("spec")
            return {
                "success": False,
                "cost_usd": 0.0,
                "token_usage": {},
                "duration_s": 0.0,
                "attempts": 1,
                "phase_timings": {},
                "diff_summary": "",
                "qa_report": "",
                "error": "stop",
                "error_code": "stop",
            }

        unit = BatchUnit(tasks=[TaskPlan(task_key="task-a"), TaskPlan(task_key="task-b")])
        pending_by_key = {
            "task-a": {"id": 1, "prompt": "Build data layer", "attempts": 2, "feedback": "Fix data bug", "spec": [{"text": "Data works", "binding": "must"}]},
            "task-b": {"id": 2, "prompt": "Build service layer", "attempts": 1, "feedback": "Fix service bug", "spec": [{"text": "Service works", "binding": "must"}]},
            "task-c": {"id": 3, "prompt": "Build CLI"},
        }
        with patch("otto.git_ops.create_task_worktree", return_value=tmp_path), \
             patch("otto.git_ops.cleanup_task_worktree"), \
             patch("otto.testing._install_deps"), \
             patch("otto.runner.run_task_v45", side_effect=fake_run_task_v45):
            await _run_integrated_unit_in_worktree(
                unit,
                pending_by_key,
                MagicMock(),
                {},
                tmp_path,
                MagicMock(),
                tmp_path / "tasks.yaml",
                "base-sha",
                planner_analysis=[{
                    "task_a": "task-b",
                    "task_b": "task-c",
                    "relationship": "LAYERED",
                    "reason": "CLI depends on service interfaces",
                }],
                full_project_brief="1. #1 Build data layer\n2. #2 Build service layer\n3. #3 Build CLI",
            )

        prompt = captured["prompt"]
        assert "Build data layer" in prompt
        assert "Build service layer" in prompt
        assert "Other tasks in this project" in prompt
        assert "Build CLI" in prompt
        assert "Planner analysis" in prompt
        assert "Build data layer" in captured["full_project_brief"]
        assert "Build service layer" in captured["full_project_brief"]
        assert "Build CLI" in captured["full_project_brief"]
        assert captured["feedback"].startswith("Task #1")
        assert "Fix data bug" in captured["feedback"]
        assert "Fix service bug" in captured["feedback"]
        assert captured["attempts"] == 2
        assert captured["spec"][0]["text"].startswith("[Task #1]")
        assert captured["spec"][1]["text"].startswith("[Task #2]")

    def test_single_task_context_includes_other_project_tasks(self):
        batch = Batch(units=[BatchUnit(tasks=[TaskPlan(task_key="task-a")])])
        context = _build_sibling_context(
            "task-a",
            batch,
            {
                "task-a": {"id": 1, "prompt": "Build data layer"},
                "task-b": {"id": 2, "prompt": "Build service layer"},
                "task-c": {"id": 3, "prompt": "Build CLI"},
            },
            [],
        )

        assert context is not None
        assert "OTHER TASKS IN THIS PROJECT" in context
        assert "Build service layer" in context
        assert "Build CLI" in context


class TestRefreshTaskLiveState:
    def test_populates_duration_cost_and_phase_timings(self, tmp_path):
        task_dir = tmp_path / "otto_logs" / "task-1"
        task_dir.mkdir(parents=True)

        _refresh_task_live_state(
            tmp_path,
            "task-1",
            status="merged",
            merge_status="done",
            completed=False,
            token_usage={"input_tokens": 10},
            elapsed_s=12.3,
            cost_available=False,
            cost_usd=0.0,
            phase_timings={"prepare": 1.0, "coding": 8.5, "test": 2.8},
            attempts=2,
        )

        live_state = json.loads((task_dir / "live-state.json").read_text())
        assert live_state["status"] == "merged"
        assert live_state["elapsed_s"] == 12.3
        assert live_state["cost_available"] is False
        assert live_state["cost_usd"] == 0.0
        assert live_state["token_usage"] == {"input_tokens": 10}
        assert live_state["phases"]["prepare"]["time_s"] == 1.0
        assert live_state["phases"]["coding"]["time_s"] == 8.5
        assert live_state["phases"]["test"]["time_s"] == 2.8
        assert live_state["phases"]["merge"]["status"] == "done"

        summary = json.loads((task_dir / "task-summary.json").read_text())
        assert summary["status"] == "merged"
        assert summary["total_duration_s"] == 12.3
        assert summary["cost_available"] is False
        assert summary["total_cost_usd"] == 0.0
        assert summary["attempts"] == 2
        assert summary["phase_timings"]["coding"] == 8.5


class TestRunPerIntegration:
    """Integration tests for run_per with mocked agents."""

    def _make_config(self, repo):
        return {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "max_parallel": 2,
            "effort": "high",
            "execution_mode": "planned",
        }

    @pytest.mark.asyncio
    async def test_no_pending_tasks(self, tmp_git_repo):
        """Should exit 0 when no pending tasks."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123", "prompt": "done", "status": "passed"},
        ]}))

        config = self._make_config(tmp_git_repo)
        exit_code = await run_per(config, tasks_path, tmp_git_repo)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_build_scope_runs_only_matching_pending_tasks(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "user-task", "prompt": "Unrelated backlog", "status": "pending"},
            {"id": 2, "key": "build-task", "prompt": "Current build task", "status": "pending", "build_id": "build-123"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["build_id"] = "build-123"
        seen_task_keys = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            seen_task_keys.append(task_plan.task_key)
            return TaskResult(
                task_key=task_plan.task_key,
                success=True,
                commit_sha="abc",
                cost_usd=0.10,
                worktree=None,
            )

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert seen_task_keys == ["build-task"]
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        assert persisted["user-task"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_monolithic_mode_bypasses_planner_and_runs_one_integrated_unit(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-a", "prompt": "Task A", "status": "pending"},
            {"id": 2, "key": "task-b", "prompt": "Task B", "status": "pending"},
            {"id": 3, "key": "task-c", "prompt": "Task C", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["execution_mode"] = "monolithic"

        async def fake_run_integrated_unit(unit, pending_by_key, context, config, project_dir, telemetry, task_file, base_sha, qa_mode="per_task", **kwargs):
            assert [tp.task_key for tp in unit.tasks] == ["task-a", "task-b", "task-c"]
            assert qa_mode == QAMode.BATCH
            for key in ["task-a", "task-b", "task-c"]:
                update_task(task_file, key, status="verified")
            return [
                TaskResult(task_key=key, success=True, unit_key="unit-abc", unit_task_keys=["task-a", "task-b", "task-c"])
                for key in ["task-a", "task-b", "task-c"]
            ]

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for result in results:
                update_task(task_file, result.task_key, status="merged")
            return results

        async def fake_run_batch_qa(*args, **kwargs):
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [], "regressions": []},
                "raw_report": "ok",
                "failed_task_keys": [],
                "cost_usd": 0.0,
            }

        with patch("otto.orchestrator.plan", AsyncMock(side_effect=AssertionError("planner should not run"))):
            with patch("otto.orchestrator._run_integrated_unit_in_worktree", side_effect=fake_run_integrated_unit) as integrated_mock:
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                    with patch("otto.orchestrator._run_batch_qa", side_effect=fake_run_batch_qa):
                        with patch("otto.orchestrator._print_summary"):
                            with patch("otto.orchestrator._record_run_history"):
                                exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        integrated_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_single_task_success(self, tmp_git_repo):
        """Single task that succeeds should exit 0."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Add hello", "status": "pending",
             "spec": ["Function hello() returns 'hello'"]},
        ]}))

        config = self._make_config(tmp_git_repo)

        # Mock coding_loop to succeed — create a branch with commit for merge
        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            branch = f"otto/{task_plan.task_key}"
            subprocess.run(["git", "checkout", "-b", branch], cwd=project_dir, capture_output=True)
            (project_dir / "hello.py").write_text("def hello(): return 'hello'\n")
            subprocess.run(["git", "add", "hello.py"], cwd=project_dir, capture_output=True)
            subprocess.run(["git", "commit", "-m", "otto: add hello (#1)"], cwd=project_dir, capture_output=True)
            subprocess.run(["git", "checkout", "main"], cwd=project_dir, capture_output=True)
            return TaskResult(
                task_key=task_plan.task_key, success=True,
                commit_sha="abc", cost_usd=0.10,
                worktree=None,  # no worktree — branch already on main repo
            )

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        # No worktree to merge, so tasks stay as coding_loop returned them
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_single_task_failure(self, tmp_git_repo):
        """Single task that fails should exit 1."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Impossible task", "status": "pending",
             "spec": ["Something impossible"]},
        ]}))

        config = self._make_config(tmp_git_repo)

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            return TaskResult(
                task_key=task_plan.task_key, success=False,
                error="tests failed", cost_usd=0.20,
            )

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_conflicting_tasks_are_skipped_and_downstream_blocked(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-a", "prompt": "Rewrite parser with approach A", "status": "pending"},
            {"id": 2, "key": "task-b", "prompt": "Rewrite parser with approach B", "status": "pending"},
            {"id": 3, "key": "task-c", "prompt": "Use parser output", "status": "pending", "depends_on": [2]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["skip_qa"] = True
        execution_plan = ExecutionPlan(
            batches=[],
            conflicts=[{
                "tasks": ["task-a", "task-b"],
                "description": "Both rewrite the parser incompatibly",
                "suggestion": "choose one parser approach",
            }],
            analysis=[{
                "task_a": "task-a",
                "task_b": "task-b",
                "relationship": "CONTRADICTORY",
                "reason": "same parser rewrite",
            }],
        )

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=AssertionError("coding_loop should not run")):
                with patch("otto.orchestrator._print_summary"):
                    with patch("otto.orchestrator._record_run_history"):
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        assert persisted["task-a"]["status"] == "conflict"
        assert persisted["task-b"]["status"] == "conflict"
        assert persisted["task-c"]["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_parallel_merge_retry_uses_error_code_and_preserves_attempts(self, tmp_git_repo):
        """Merge retries should use structured TaskResult codes and keep prior attempts."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {
                "id": 1,
                "key": "task-merge",
                "prompt": "Retry merge task",
                "status": "pending",
                "attempts": 2,
            },
            {
                "id": 2,
                "key": "task-pass",
                "prompt": "Pass task",
                "status": "pending",
            },
        ]}))

        config = self._make_config(tmp_git_repo)
        config["skip_qa"] = True
        execution_plan = ExecutionPlan(batches=[Batch(tasks=[
            TaskPlan(task_key="task-merge"),
            TaskPlan(task_key="task-pass"),
        ])])

        def fake_preflight_checks(*args, **kwargs):
            return None, load_tasks(tasks_path)

        async def fake_run_batch_parallel(*args, **kwargs):
            return [
                TaskResult(task_key="task-merge", success=True),
                TaskResult(task_key="task-pass", success=True),
            ]

        merge_calls = 0

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            nonlocal merge_calls
            merge_calls += 1
            if merge_calls == 1:
                update_task(
                    task_file,
                    "task-merge",
                    status="merge_failed",
                    error="post-merge tests failed",
                    error_code="post_merge_test_fail",
                )
                update_task(task_file, "task-pass", status="passed")
                return [
                    TaskResult(
                        task_key="task-merge",
                        success=False,
                        error="post-merge tests failed",
                        error_code="post_merge_test_fail",
                    ),
                    TaskResult(task_key="task-pass", success=True),
                ]

            update_task(task_file, "task-merge", status="merged" if qa_mode == QAMode.BATCH else "passed")
            return [TaskResult(task_key="task-merge", success=True)]

        rerun_calls: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            rerun_calls.append(task_plan.task_key)
            task = next(t for t in load_tasks(task_file) if t.get("key") == task_plan.task_key)
            assert task["status"] == "pending"
            assert task["attempts"] == 2
            assert task.get("error_code") is None
            update_task(task_file, task_plan.task_key, status="verified" if qa_mode == QAMode.BATCH else "passed")
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.preflight_checks", side_effect=fake_preflight_checks):
            with patch("otto.orchestrator.plan", new=AsyncMock(return_value=execution_plan)):
                with patch("otto.orchestrator._run_batch_parallel", side_effect=fake_run_batch_parallel):
                    with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                            with patch("otto.orchestrator._print_summary"):
                                with patch("otto.orchestrator._record_run_history"):
                                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert rerun_calls == ["task-merge"]

    @pytest.mark.asyncio
    async def test_batch_qa_retry_reruns_integrated_unit_once(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-a", "prompt": "Task A", "status": "pending"},
            {"id": 2, "key": "task-b", "prompt": "Task B", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1

        execution_plan = ExecutionPlan(batches=[
            Batch(units=[BatchUnit(tasks=[TaskPlan(task_key="task-a"), TaskPlan(task_key="task-b")])]),
        ])

        def fake_preflight_checks(*args, **kwargs):
            return None, load_tasks(tasks_path)

        async def fake_run_integrated_unit(unit, pending_by_key, context, config, project_dir, telemetry, task_file, base_sha, qa_mode="per_task", **kwargs):
            for key in ["task-a", "task-b"]:
                update_task(task_file, key, status="verified")
            return [
                TaskResult(task_key="task-a", success=True, unit_key="unit-ab", unit_task_keys=["task-a", "task-b"]),
                TaskResult(task_key="task-b", success=True, unit_key="unit-ab", unit_task_keys=["task-a", "task-b"]),
            ]

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for result in results:
                update_task(task_file, result.task_key, status="merged")
            return results

        batch_qa_calls = 0

        async def fake_run_batch_qa(merged_tasks, config, project_dir, tasks_file, telemetry, context, **kwargs):
            nonlocal batch_qa_calls
            batch_qa_calls += 1
            if batch_qa_calls == 1:
                return {
                    "must_passed": False,
                    "verdict": {"must_passed": False, "must_items": [], "regressions": []},
                    "raw_report": "task-a failed",
                    "failed_task_keys": ["task-a"],
                    "cost_usd": 0.0,
                }
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [], "regressions": []},
                "raw_report": "all good",
                "failed_task_keys": [],
                "cost_usd": 0.0,
            }

        with patch("otto.orchestrator.preflight_checks", side_effect=fake_preflight_checks):
            with patch("otto.orchestrator.plan", new=AsyncMock(return_value=execution_plan)):
                with patch("otto.orchestrator._run_integrated_unit_in_worktree", side_effect=fake_run_integrated_unit) as integrated_mock:
                    with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                        with patch("otto.orchestrator._run_batch_qa", side_effect=fake_run_batch_qa):
                            with patch("otto.orchestrator._print_summary"):
                                with patch("otto.orchestrator._record_run_history"):
                                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert integrated_mock.call_count == 2  # initial run + one unit retry

    @pytest.mark.asyncio
    async def test_run_per_executes_integrated_unit_once(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-a", "prompt": "Task A", "status": "pending"},
            {"id": 2, "key": "task-b", "prompt": "Task B", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(units=[BatchUnit(tasks=[TaskPlan(task_key="task-a"), TaskPlan(task_key="task-b")])]),
        ])

        def fake_preflight_checks(*args, **kwargs):
            return None, load_tasks(tasks_path)

        async def fake_run_integrated_unit(unit, pending_by_key, context, config, project_dir, telemetry, task_file, base_sha, qa_mode="per_task", **kwargs):
            for key in ["task-a", "task-b"]:
                update_task(task_file, key, status="verified")
            return [
                TaskResult(task_key="task-a", success=True, unit_key="unit-ab", unit_task_keys=["task-a", "task-b"]),
                TaskResult(task_key="task-b", success=True, unit_key="unit-ab", unit_task_keys=["task-a", "task-b"]),
            ]

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for result in results:
                update_task(task_file, result.task_key, status="merged" if qa_mode == QAMode.BATCH else "passed")
            return results

        async def fake_run_batch_qa(*args, **kwargs):
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [], "regressions": []},
                "raw_report": "all good",
                "failed_task_keys": [],
                "cost_usd": 0.0,
            }

        with patch("otto.orchestrator.preflight_checks", side_effect=fake_preflight_checks):
            with patch("otto.orchestrator.plan", new=AsyncMock(return_value=execution_plan)):
                with patch("otto.orchestrator._run_integrated_unit_in_worktree", side_effect=fake_run_integrated_unit) as integrated_mock:
                    with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                        with patch("otto.orchestrator._run_batch_qa", side_effect=fake_run_batch_qa):
                            with patch("otto.orchestrator._print_summary"):
                                with patch("otto.orchestrator._record_run_history"):
                                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert integrated_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_signal_sets_interrupted(self, tmp_git_repo):
        """Context.interrupted should be set by signal handler."""
        context = PipelineContext()
        assert context.interrupted is False
        context.interrupted = True
        assert context.interrupted is True

    @pytest.mark.asyncio
    async def test_batch_tasks_run_sequentially_when_max_parallel_1(self, tmp_git_repo):
        """With max_parallel=1, tasks in the same batch run sequentially."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1  # force serial execution
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        events: list[str] = []
        in_flight = False

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            nonlocal in_flight
            assert in_flight is False
            in_flight = True
            events.append(f"start:{task_plan.task_key}")
            await asyncio.sleep(0)
            events.append(f"end:{task_plan.task_key}")
            in_flight = False
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert events == [
            "start:task-one",
            "end:task-one",
            "start:task-two",
            "end:task-two",
        ]

    @pytest.mark.asyncio
    async def test_batch_tasks_run_parallel_when_max_parallel_gt_1(self, tmp_git_repo):
        """With max_parallel>1 and multiple tasks, tasks run in parallel with worktrees.

        The mock coding_loop creates a real commit in the worktree and anchors
        it as a candidate ref, so the serial merge phase can merge it.
        """
        import yaml
        from otto.git_ops import _anchor_candidate_ref
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 2
        config["skip_test"] = True  # skip post-merge verification for unit test speed
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        executed_keys: list[str] = []
        received_work_dirs: dict[str, str] = {}

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            executed_keys.append(task_plan.task_key)
            if task_work_dir:
                received_work_dirs[task_plan.task_key] = str(task_work_dir)
                # Create a real commit in the worktree so the merge phase has something to merge
                filename = f"{task_plan.task_key}.txt"
                (task_work_dir / filename).write_text(f"content for {task_plan.task_key}\n")
                subprocess.run(["git", "add", filename], cwd=task_work_dir, capture_output=True, check=True)
                subprocess.run(
                    ["git", "commit", "-m", f"otto: {task_plan.task_key}"],
                    cwd=task_work_dir, capture_output=True, check=True,
                )
                sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=task_work_dir, capture_output=True, text=True, check=True,
                ).stdout.strip()
                # Anchor the commit as a candidate ref (stored in shared git object store)
                _anchor_candidate_ref(task_work_dir, task_plan.task_key, 1, sha)
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert set(executed_keys) == {"task-one", "task-two"}
        # Each task should have received a separate worktree path
        assert len(received_work_dirs) == 2
        assert received_work_dirs["task-one"] != received_work_dirs["task-two"]
        # Worktree paths should contain the task key
        assert "task-one" in received_work_dirs["task-one"]
        assert "task-two" in received_work_dirs["task-two"]
        # Worktrees should be cleaned up after
        wt_dir = tmp_git_repo / ".otto-worktrees"
        if wt_dir.exists():
            remaining = [c for c in wt_dir.iterdir() if c.name.startswith("otto-task-")]
            assert remaining == [], f"Worktrees not cleaned up: {remaining}"

    @pytest.mark.asyncio
    async def test_invalid_replan_falls_back_to_remaining_plan(self, tmp_git_repo):
        """Invalid replan coverage should preserve the pre-replan remaining plan."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
            {"id": 3, "key": "task-three", "prompt": "Task three", "status": "pending", "spec": ["three"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        initial_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one")]),
            Batch(tasks=[TaskPlan(task_key="task-two"), TaskPlan(task_key="task-three")]),
        ])
        invalid_replan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-two")]),
        ])
        executed: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            executed.append(task_plan.task_key)
            if task_plan.task_key == "task-one":
                return TaskResult(task_key=task_plan.task_key, success=False, error="boom")
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.plan", AsyncMock(return_value=initial_plan)):
            with patch("otto.orchestrator.replan", AsyncMock(return_value=invalid_replan)):
                with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        # Batch 1 runs task-one (serial, single task). Batch 2 runs task-two and task-three
        # (parallel, order non-deterministic). All 3 must execute.
        assert executed[0] == "task-one"
        assert set(executed) == {"task-one", "task-two", "task-three"}

    @pytest.mark.asyncio
    async def test_all_done_reports_missing_or_interrupted_tasks(self, tmp_git_repo):
        """Interrupted runs should report unfinished tasks in AllDone telemetry."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1  # serial for interrupt testing
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        executed: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            executed.append(task_plan.task_key)
            context.interrupted = True
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        assert executed == ["task-one"]

        events_path = tmp_git_repo / "otto_logs" / "events.jsonl"
        all_done = json.loads(events_path.read_text().strip().splitlines()[-1])
        assert all_done["event"] == "all_done"
        assert all_done["total_missing_or_interrupted"] == 1

    @pytest.mark.asyncio
    async def test_post_run_suite_skipped_when_skip_qa_run_not_clean(self, tmp_git_repo):
        """Skip-QA mode should skip the final suite if the run is already not clean."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
            {"id": 3, "key": "task-three", "prompt": "Task three", "status": "pending", "spec": ["three"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["skip_qa"] = True
        config["test_command"] = "pytest"
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[
                TaskPlan(task_key="task-one"),
                TaskPlan(task_key="task-two"),
                TaskPlan(task_key="task-three"),
            ]),
        ])
        outcomes = {
            "task-one": True,
            "task-two": True,
            "task-three": False,
        }

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            success = outcomes[task_plan.task_key]
            if success:
                update_task(task_file, task_plan.task_key, status="passed")
                return TaskResult(task_key=task_plan.task_key, success=True)
            update_task(task_file, task_plan.task_key, status="failed", error="boom")
            return TaskResult(task_key=task_plan.task_key, success=False, error="boom")

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.testing.run_test_suite") as run_suite_mock:
                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        run_suite_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_qa_runs_post_run_suite_on_head_only(self, tmp_git_repo):
        """Skip-QA mode should still run the deterministic final suite on HEAD."""
        from otto.testing import TestSuiteResult, TierResult

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["skip_qa"] = True
        config["test_command"] = "pytest"
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            update_task(task_file, task_plan.task_key, status="passed")
            return TaskResult(task_key=task_plan.task_key, success=True)

        passed_suite = TestSuiteResult(
            passed=True,
            tiers=[TierResult(tier="existing_tests", passed=True, output="ok")],
        )
        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.testing.run_test_suite", return_value=passed_suite) as run_suite_mock:
                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        run_suite_mock.assert_called_once()
        assert run_suite_mock.call_args.kwargs["candidate_sha"] == "HEAD"
        assert run_suite_mock.call_args.kwargs["custom_test_cmd"] is None

    @pytest.mark.asyncio
    async def test_skip_qa_post_run_suite_failure_is_run_level_only(self, tmp_git_repo):
        """Skip-QA post-run suite failure should exit non-zero without blaming one task."""
        from otto.testing import TestSuiteResult, TierResult

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["skip_qa"] = True
        config["test_command"] = "pytest"
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            update_task(task_file, task_plan.task_key, status="passed")
            return TaskResult(task_key=task_plan.task_key, success=True)

        failed_suite = TestSuiteResult(
            passed=False,
            tiers=[TierResult(tier="existing_tests", passed=False, output="1 failed")],
        )
        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.testing.run_test_suite", return_value=failed_suite):
                    with patch("otto.orchestrator._print_summary") as print_summary_mock:
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        assert persisted["task-one"]["status"] == "passed"
        assert persisted["task-two"]["status"] == "passed"
        assert persisted["task-one"].get("error_code") is None
        assert persisted["task-two"].get("error_code") is None
        print_summary_mock.assert_called_once()
        assert print_summary_mock.call_args.kwargs["integration_passed"] is False

        events_path = tmp_git_repo / "otto_logs" / "events.jsonl"
        all_done = json.loads(events_path.read_text().strip().splitlines()[-1])
        assert all_done["event"] == "all_done"
        assert all_done["total_failed"] == 0

        history_path = tmp_git_repo / "otto_logs" / "run-history.jsonl"
        history_entry = json.loads(history_path.read_text().strip().splitlines()[-1])
        assert history_entry["tasks_failed"] == 0
        assert history_entry["failure_summary"] == "post-run test suite failed: 1 failed"

    @pytest.mark.asyncio
    async def test_multi_task_run_uses_batch_qa_mode_and_batch_qa(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending"},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        seen_modes: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            seen_modes.append(qa_mode)
            update_task(task_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True)

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            assert qa_mode == QAMode.BATCH
            for result in results:
                update_task(task_file, result.task_key, status="merged")
            return results

        batch_qa_mock = AsyncMock(return_value={
            "must_passed": True,
            "verdict": {"must_passed": True, "must_items": []},
            "raw_report": "batch qa pass",
            "failed_task_keys": [],
            "cost_usd": 0.0,
        })

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                    with patch("otto.orchestrator._run_batch_qa", batch_qa_mock):
                        with patch("otto.testing.run_test_suite") as run_suite_mock:
                            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert seen_modes == [QAMode.BATCH, QAMode.BATCH]
        batch_qa_mock.assert_awaited_once()
        run_suite_mock.assert_not_called()
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        assert persisted["task-one"]["status"] == "passed"
        assert persisted["task-two"]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_batch_qa_failure_retries_task_and_reqa_passes(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending"},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        call_counts = {"task-one": 0, "task-two": 0}
        merge_seen_statuses: list[dict[str, str]] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            call_counts[task_plan.task_key] += 1
            if task_plan.task_key == "task-one" and call_counts["task-one"] == 2:
                task = next(t for t in load_tasks(task_file) if t["key"] == "task-one")
                assert "returns widget" in task.get("feedback", "")
                assert "broken response shape" in task.get("feedback", "")
            update_task(task_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True, cost_usd=0.1)

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            assert qa_mode == QAMode.BATCH
            for result in results:
                update_task(task_file, result.task_key, status="merged")
            return results

        batch_qa_mock = AsyncMock(side_effect=[
            {
                "must_passed": False,
                "verdict": {
                    "must_items": [
                        {
                            "task_key": "task-one",
                            "spec_id": 1,
                            "criterion": "returns widget",
                            "status": "fail",
                            "evidence": "broken response shape",
                            "proof": ["pytest tests/test_widget.py::test_widget"],
                        },
                    ],
                    "integration_findings": [],
                    "regressions": [],
                    "test_suite_passed": True,
                },
                "raw_report": "initial batch QA failed",
                "failed_task_keys": ["task-one"],
                "cost_usd": 0.0,
            },
            {
                "must_passed": True,
                "verdict": {
                    "must_items": [
                        {"task_key": "task-one", "spec_id": 1, "criterion": "returns widget", "status": "pass"},
                    ],
                    "integration_findings": [],
                    "regressions": [],
                    "test_suite_passed": True,
                },
                "raw_report": "retry batch QA passed",
                "failed_task_keys": [],
                "cost_usd": 0.0,
            },
        ])

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                    with patch("otto.orchestrator._run_batch_qa", batch_qa_mock):
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert call_counts == {"task-one": 2, "task-two": 1}
        assert batch_qa_mock.await_count == 2
        assert batch_qa_mock.await_args_list[1].kwargs["focus_task_keys"] == {"task-one"}
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        assert persisted["task-one"]["status"] == "passed"
        assert persisted["task-two"]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_batch_qa_retry_stops_after_two_rounds_and_marks_failed(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending"},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1
        config["max_retries"] = 0  # no retries within a batch — just fail and continue
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        call_counts = {"task-one": 0, "task-two": 0}
        merge_seen_statuses: list[dict[str, str]] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            from otto.git_ops import _anchor_candidate_ref
            call_counts[task_plan.task_key] += 1
            filename = f"{task_plan.task_key}.txt"
            (task_work_dir / filename).write_text(f"{task_plan.task_key}-{call_counts[task_plan.task_key]}\n")
            subprocess.run(["git", "add", filename], cwd=task_work_dir, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"otto: {task_plan.task_key}-{call_counts[task_plan.task_key]}"],
                cwd=task_work_dir, capture_output=True, check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=task_work_dir, capture_output=True, text=True, check=True,
            ).stdout.strip()
            _anchor_candidate_ref(task_work_dir, task_plan.task_key, call_counts[task_plan.task_key], sha)
            update_task(task_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True)

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            merge_seen_statuses.append({
                result.task_key: next(
                    task["status"] for task in load_tasks(task_file) if task["key"] == result.task_key
                )
                for result in results
            })
            for result in results:
                update_task(task_file, result.task_key, status="merged")
            return results

        qa_fail_task_one = {
            "must_passed": False,
            "verdict": {
                "must_items": [
                    {
                        "task_key": "task-one",
                        "spec_id": 1,
                        "criterion": "returns widget",
                        "status": "fail",
                        "evidence": "broken response shape",
                        "proof": ["pytest tests/test_widget.py::test_widget"],
                    },
                ],
                "integration_findings": [],
                "regressions": [],
                "test_suite_passed": True,
            },
            "raw_report": "batch QA failed",
            "failed_task_keys": ["task-one"],
            "cost_usd": 0.0,
        }
        qa_pass = {
            "must_passed": True,
            "verdict": {"must_items": [], "integration_findings": [], "regressions": [], "test_suite_passed": True},
            "raw_report": "batch QA passed",
            "cost_usd": 0.0,
        }
        # max_retries=0: no retry rounds. Batch 1 QA fails → rollback → continue.
        # Batch 2 (task-two only, task-one failed permanently) → QA passes.
        batch_qa_mock = AsyncMock(side_effect=[
            qa_fail_task_one,  # batch 1 initial QA (fails, no retries)
            qa_pass,           # batch 2 (task-two only) QA passes
        ])

        # Replan returns task-two (the rolled-back task) in a new batch
        replan_result = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-two")]),
        ])

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.replan", AsyncMock(return_value=replan_result)):
                with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                    with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                        with patch("otto.orchestrator._run_batch_qa", batch_qa_mock):
                            with patch(
                                "otto.git_ops._find_best_candidate_ref",
                                side_effect=lambda repo, key: (
                                    "refs/otto/candidates/task-two/attempt-1"
                                    if key == "task-two"
                                    else None
                                ),
                            ):
                                with patch("otto.orchestrator._rollback_main_to_sha", return_value=True) as rollback_mock:
                                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        # task-one: initial coding only (max_retries=0, no batch retry)
        # task-two: initial coding in batch 1, then candidate ref is reused in replanned batch 2
        assert call_counts["task-one"] == 1
        assert call_counts["task-two"] == 1
        assert merge_seen_statuses[1]["task-two"] == "verified"
        # 2 batch QA calls: batch 1 (fail) + replanned batch 2 (pass)
        assert batch_qa_mock.await_count == 2
        # Rollback called once for batch 1 failure
        rollback_mock.assert_called_once()
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        # task-one permanently failed after batch QA rejection
        assert persisted["task-one"]["status"] == "failed"
        assert persisted["task-one"]["error_code"] == "batch_qa_failed"
        # task-two was rolled back from batch 1, then passed in replanned batch 2
        assert persisted["task-two"]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_batch_qa_infrastructure_error_rolls_back_without_failing_tasks(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending"},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            update_task(task_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True)

        def fake_merge_batch_results(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for result in results:
                update_task(task_file, result.task_key, status="merged")
            return results

        batch_qa_mock = AsyncMock(return_value={
            "must_passed": False,
            "verdict": {
                "must_items": [],
                "integration_findings": [],
                "regressions": [],
                "test_suite_passed": True,
            },
            "raw_report": "stream closed",
            "failed_task_keys": [],
            "cost_usd": 0.0,
            "infrastructure_error": True,
        })

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge_batch_results):
                    with patch("otto.orchestrator._run_batch_qa", batch_qa_mock):
                        with patch("otto.orchestrator._rollback_main_to_sha", return_value=True) as rollback_mock:
                            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        rollback_mock.assert_called_once()
        persisted = {task["key"]: task for task in load_tasks(tasks_path)}
        assert persisted["task-one"]["status"] == "pending"
        assert persisted["task-two"]["status"] == "pending"
        assert persisted["task-one"].get("error_code") is None
        assert persisted["task-two"].get("error_code") is None

    @pytest.mark.asyncio
    async def test_skip_qa_mode_does_not_run_batch_qa(self, tmp_git_repo):
        from otto.testing import TestSuiteResult, TierResult

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending"},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending"},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["skip_qa"] = True
        config["max_parallel"] = 1
        config["test_command"] = "pytest"
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        seen_modes: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            seen_modes.append(qa_mode)
            update_task(task_file, task_plan.task_key, status="passed")
            return TaskResult(task_key=task_plan.task_key, success=True)

        passed_suite = TestSuiteResult(
            passed=True,
            tiers=[TierResult(tier="existing_tests", passed=True, output="ok")],
        )
        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator._run_batch_qa", AsyncMock()) as batch_qa_mock:
                    with patch("otto.testing.run_test_suite", return_value=passed_suite) as run_suite_mock:
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert seen_modes == [QAMode.SKIP, QAMode.SKIP]
        batch_qa_mock.assert_not_awaited()
        run_suite_mock.assert_called_once()
        assert run_suite_mock.call_args.kwargs["candidate_sha"] == "HEAD"


class TestOuterLoop:
    @pytest.mark.asyncio
    async def test_skips_product_qa_when_fix_execution_fails(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "fix-task", "prompt": "Fix the product", "status": "pending"},
        ]}))
        product_spec_path = tmp_git_repo / "product-spec.md"
        product_spec_path.write_text("# Product Spec\n")

        async def fake_run_per(config, tasks_path, project_dir):
            update_task(tasks_path, "fix-task", status="failed", error="tests failed")
            return 1

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            with patch("otto.certifier.run_unified_certifier", side_effect=AssertionError("Certifier should not run")):
                result = await run_product_verification(
                    product_spec_path=product_spec_path,
                    project_dir=tmp_git_repo,
                    tasks_path=tasks_path,
                    config={},
                    intent="test product",
                )

        assert result["product_passed"] is False
        assert result["build_failed"] is True

    @pytest.mark.asyncio
    async def test_stops_when_certifier_makes_no_progress(self, tmp_git_repo):
        from otto.certifier.report import (
            CertificationOutcome, CertificationReport, Finding, TierResult, TierStatus,
        )

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}))
        product_spec_path = tmp_git_repo / "product-spec.md"
        product_spec_path.write_text("# Product Spec\n")

        def make_failing_report(cost):
            return CertificationReport(
                product_type="web", interaction="http",
                tiers=[
                    TierResult(tier=1, name="structural", status=TierStatus.PASSED),
                    TierResult(tier=4, name="journeys", status=TierStatus.FAILED,
                        findings=[Finding(tier=4, severity="critical", category="journey",
                            description="Story failed: checkout",
                            diagnosis="button broken", fix_suggestion="fix button",
                            story_id="checkout")]),
                ],
                findings=[Finding(tier=4, severity="critical", category="journey",
                    description="Story failed: checkout",
                    diagnosis="button broken", fix_suggestion="fix button",
                    story_id="checkout")],
                outcome=CertificationOutcome.FAILED,
                cost_usd=cost, duration_s=10.0,
            )

        certifier_results = [make_failing_report(0.1), make_failing_report(0.2)]

        async def fake_run_per(config, tasks_path, project_dir):
            for task in load_tasks(tasks_path):
                if task.get("status") == "pending":
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        certifier_mock = MagicMock(side_effect=certifier_results)
        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            with patch("otto.certifier.run_unified_certifier", certifier_mock):
                result = await run_product_verification(
                    product_spec_path=product_spec_path,
                    project_dir=tmp_git_repo,
                    tasks_path=tasks_path,
                    config={},
                    intent="test product",
                    max_rounds=3,
                )

        assert result["product_passed"] is False
        assert result["rounds"] == 2
        assert result["fix_tasks_created"] == 1
        assert certifier_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_fix_prompts_use_passed_grounding_file(self, tmp_git_repo):
        from otto.certifier.report import (
            CertificationOutcome, CertificationReport, Finding, TierResult, TierStatus,
        )

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}))
        intent_path = tmp_git_repo / "intent.md"
        intent_path.write_text("# Intent\n")

        def make_failing_report():
            return CertificationReport(
                product_type="web", interaction="http",
                tiers=[
                    TierResult(tier=1, name="structural", status=TierStatus.PASSED),
                    TierResult(tier=4, name="journeys", status=TierStatus.FAILED,
                        findings=[Finding(tier=4, severity="critical", category="journey",
                            description="Story failed: checkout",
                            diagnosis="submit button is disabled",
                            fix_suggestion="enable the button after form validation",
                            story_id="checkout",
                            evidence={"steps": [
                                {"action": "Click Submit", "outcome": "fail",
                                 "diagnosis": "button never enables",
                                 "fix_suggestion": "enable after valid input"},
                            ]})]),
                ],
                findings=[Finding(tier=4, severity="critical", category="journey",
                    description="Story failed: checkout",
                    diagnosis="submit button is disabled",
                    fix_suggestion="enable the button after form validation",
                    story_id="checkout",
                    evidence={"steps": [
                        {"action": "Click Submit", "outcome": "fail",
                         "diagnosis": "button never enables",
                         "fix_suggestion": "enable after valid input"},
                    ]})],
                outcome=CertificationOutcome.FAILED,
                cost_usd=0.1, duration_s=10.0,
            )

        certifier_results = [make_failing_report(), make_failing_report()]

        async def fake_run_per(config, tasks_path, project_dir):
            for task in load_tasks(tasks_path):
                if task.get("status") == "pending":
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        certifier_mock = MagicMock(side_effect=certifier_results)
        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            with patch("otto.certifier.run_unified_certifier", certifier_mock):
                result = await run_product_verification(
                    product_spec_path=intent_path,
                    project_dir=tmp_git_repo,
                    tasks_path=tasks_path,
                    config={},
                    intent="test product",
                    max_rounds=3,
                )

        assert result["product_passed"] is False
        prompts = [task["prompt"] for task in load_tasks(tasks_path)]
        assert any("See intent.md for the full product definition." in prompt for prompt in prompts)


class TestOrchestrationLogic:
    """Unit tests for plan-execute-replan logic."""

    def test_plan_remaining_after_removes_completed(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
            Batch(tasks=[TaskPlan(task_key="t3")]),
        ])
        remaining = plan.remaining_after({"t1"})
        assert remaining.total_tasks == 2
        keys = [tp.task_key for b in remaining.batches for tp in b.tasks]
        assert "t1" not in keys
        assert "t2" in keys
        assert "t3" in keys

    def test_empty_after_all_completed(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1")]),
        ])
        remaining = plan.remaining_after({"t1"})
        assert remaining.is_empty

    def test_context_tracks_results(self):
        ctx = PipelineContext()
        ctx.add_success(TaskResult(task_key="t1", success=True, cost_usd=0.5))
        ctx.add_failure(TaskResult(task_key="t2", success=False, cost_usd=0.3))
        assert ctx.passed_count == 1
        assert ctx.failed_count == 1
        assert abs(ctx.total_cost - 0.8) < 0.001


class TestMergeParallelResults:
    """Tests for the serial merge phase after parallel task execution."""

    def _make_config(self):
        return {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "skip_test": True,  # skip post-merge verification for unit tests
        }

    def _create_candidate_commit(self, repo, task_key, filename, content):
        """Create a commit and anchor it as a candidate ref. Returns the SHA."""
        from otto.git_ops import _anchor_candidate_ref
        (repo / filename).write_text(content)
        subprocess.run(["git", "add", filename], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"otto: {task_key}"],
            cwd=repo, capture_output=True, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Reset back to main to simulate worktree behavior
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo, capture_output=True)
        _anchor_candidate_ref(repo, task_key, 1, sha)
        return sha

    def test_passed_result_with_candidate_ref_still_merges(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-aaa", "prompt": "Task A", "status": "passed"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        sha = self._create_candidate_commit(tmp_git_repo, "task-aaa", "a.txt", "aaa\n")
        config = self._make_config()

        merged = merge_batch_results(
            [TaskResult(task_key="task-aaa", success=True, commit_sha=sha)],
            config,
            tmp_git_repo,
            tasks_path,
            telemetry,
            qa_mode=QAMode.SKIP,
        )

        assert len(merged) == 1
        assert merged[0].success is True
        assert (tmp_git_repo / "a.txt").exists()

    def test_both_tasks_merge_no_conflict(self, tmp_git_repo):
        """Two tasks touching different files should both merge successfully."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-aaa", "prompt": "Task A", "status": "verified"},
            {"id": 2, "key": "task-bbb", "prompt": "Task B", "status": "verified"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        self._create_candidate_commit(tmp_git_repo, "task-aaa", "a.txt", "aaa\n")
        self._create_candidate_commit(tmp_git_repo, "task-bbb", "b.txt", "bbb\n")

        results = [
            TaskResult(task_key="task-aaa", success=True, cost_usd=0.1),
            TaskResult(task_key="task-bbb", success=True, cost_usd=0.2),
        ]
        config = self._make_config()

        merged = merge_batch_results(results, config, tmp_git_repo, tasks_path, telemetry)

        assert len(merged) == 2
        assert all(r.success for r in merged)
        # Both files should exist on main
        assert (tmp_git_repo / "a.txt").exists()
        assert (tmp_git_repo / "b.txt").exists()
        orchestrator_log = (tmp_git_repo / "otto_logs" / "orchestrator.log").read_text()
        assert "merge attempt" in orchestrator_log
        assert "task=task-aaa merge result=success" in orchestrator_log
        assert "task=task-bbb merge result=success" in orchestrator_log

    def test_conflict_detected_and_queued_for_reapply(self, tmp_git_repo):
        """Merge conflict should mark task for re-apply (not abort permanently)."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-aaa", "prompt": "Task A", "status": "verified"},
            {"id": 2, "key": "task-bbb", "prompt": "Task B", "status": "verified"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        # Both tasks modify the same file with different content
        self._create_candidate_commit(tmp_git_repo, "task-aaa", "shared.txt", "content from A\n")
        self._create_candidate_commit(tmp_git_repo, "task-bbb", "shared.txt", "content from B\n")

        results = [
            TaskResult(task_key="task-aaa", success=True),
            TaskResult(task_key="task-bbb", success=True),
        ]
        config = self._make_config()

        merged = merge_batch_results(results, config, tmp_git_repo, tasks_path, telemetry)

        # First task should merge, second should be queued for re-apply
        passed = [r for r in merged if r.success]
        failed = [r for r in merged if not r.success]
        assert len(passed) == 1
        assert len(failed) == 1
        assert passed[0].task_key == "task-aaa"
        assert failed[0].task_key == "task-bbb"
        assert failed[0].error_code == "merge_conflict"
        assert "re-apply" in (failed[0].error or "").lower()

    def test_failed_tasks_carried_through(self, tmp_git_repo):
        """Failed tasks from the parallel phase should be carried through unchanged."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-ok", "prompt": "Good task", "status": "verified"},
            {"id": 2, "key": "task-bad", "prompt": "Bad task", "status": "failed"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        self._create_candidate_commit(tmp_git_repo, "task-ok", "ok.txt", "ok\n")

        results = [
            TaskResult(task_key="task-ok", success=True),
            TaskResult(task_key="task-bad", success=False, error="tests failed"),
        ]
        config = self._make_config()

        merged = merge_batch_results(results, config, tmp_git_repo, tasks_path, telemetry)

        passed = [r for r in merged if r.success]
        failed = [r for r in merged if not r.success]
        assert len(passed) == 1
        assert len(failed) == 1
        assert passed[0].task_key == "task-ok"
        assert failed[0].task_key == "task-bad"
        assert failed[0].error == "tests failed"  # original error preserved

    def test_no_verified_tasks_returns_unchanged(self, tmp_git_repo):
        """If no tasks are verified, return results unchanged."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        results = [
            TaskResult(task_key="t1", success=False, error="boom"),
        ]
        config = self._make_config()

        merged = merge_batch_results(results, config, tmp_git_repo, tasks_path, telemetry)
        assert merged == results

    def test_already_passed_parallel_result_skips_merge_phase(self, tmp_git_repo):
        """Parallel no-change passes should bypass the merge phase entirely."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-pass", "prompt": "No-op", "status": "passed"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)
        results = [TaskResult(task_key="task-pass", success=True)]

        with patch("otto.git_ops.merge_candidate") as merge_candidate_mock:
            merged = merge_batch_results(results, self._make_config(), tmp_git_repo, tasks_path, telemetry)

        assert merged == results
        merge_candidate_mock.assert_not_called()

    def test_post_merge_test_failure_in_skip_qa_mode(self, tmp_git_repo):
        """Post-merge test failure in --no-qa mode should mark task as merge_failed."""
        from otto.testing import TestSuiteResult, TierResult
        from otto.context import QAMode
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-fail", "prompt": "Fails post-merge verification", "status": "verified"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        self._create_candidate_commit(tmp_git_repo, "task-fail", "fail.txt", "content\n")

        # Record HEAD before merge attempt
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

        results = [TaskResult(task_key="task-fail", success=True)]
        config = self._make_config()
        config["skip_test"] = False

        # Mock run_test_suite to return failure
        failed_suite = TestSuiteResult(
            passed=False,
            tiers=[TierResult(tier="existing_tests", passed=False, output="1 failed")],
        )
        with patch("otto.testing.run_test_suite", return_value=failed_suite):
            merged = merge_batch_results(
                results, config, tmp_git_repo, tasks_path, telemetry,
                qa_mode=QAMode.SKIP,
            )

        assert len(merged) == 1
        assert not merged[0].success
        assert merged[0].error_code == "post_merge_test_fail"
        assert "post-merge" in (merged[0].error or "").lower()

        # Main should remain at the original HEAD after the failed verification
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head_after == head_before

    def test_post_merge_verification_uses_task_verify_command(self, tmp_git_repo):
        """Merge verification in --no-qa mode should forward custom verify command."""
        from otto.testing import TestSuiteResult, TierResult
        from otto.context import QAMode
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {
                "id": 1,
                "key": "task-verify",
                "prompt": "Verify task",
                "status": "verified",
                "verify": "bin/task-verify",
            },
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        self._create_candidate_commit(tmp_git_repo, "task-verify", "verify.txt", "content\n")
        results = [TaskResult(task_key="task-verify", success=True)]
        config = self._make_config()
        config["skip_test"] = False

        passed_suite = TestSuiteResult(
            passed=True,
            tiers=[TierResult(tier="existing_tests", passed=True, output="ok")],
        )
        with patch("otto.testing.run_test_suite", return_value=passed_suite) as run_suite_mock:
            merge_batch_results(
                results, config, tmp_git_repo, tasks_path, telemetry,
                qa_mode=QAMode.SKIP,
            )

        assert run_suite_mock.call_args.kwargs["custom_test_cmd"] == "bin/task-verify"

    def test_merge_marks_task_merge_pending_before_passing(self, tmp_git_repo):
        """Tasks should persist merge_pending while actively merging."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-merge", "prompt": "Task merge", "status": "verified"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        self._create_candidate_commit(tmp_git_repo, "task-merge", "merge.txt", "ok\n")
        results = [TaskResult(task_key="task-merge", success=True)]

        from otto.tasks import update_task as real_update_task
        status_updates: list[str] = []

        def tracking_update_task(*args, **kwargs):
            if "status" in kwargs:
                status_updates.append(kwargs["status"])
            return real_update_task(*args, **kwargs)

        with patch("otto.orchestrator.update_task", side_effect=tracking_update_task):
            merge_batch_results(results, self._make_config(), tmp_git_repo, tasks_path, telemetry)

        assert "merge_pending" in status_updates
        assert status_updates.index("merge_pending") < status_updates.index("passed")

    def test_batch_mode_merge_marks_task_merged(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-merge", "prompt": "Task merge", "status": "verified"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir(exist_ok=True)
        telemetry = Telemetry(log_dir)

        self._create_candidate_commit(tmp_git_repo, "task-merge", "merge.txt", "ok\n")
        results = [TaskResult(task_key="task-merge", success=True)]

        merged = merge_batch_results(
            results, self._make_config(), tmp_git_repo, tasks_path, telemetry, qa_mode=QAMode.BATCH,
        )

        assert merged[0].success is True
        assert load_tasks(tasks_path)[0]["status"] == "merged"

    def test_merge_updates_live_state_to_passed(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-live", "prompt": "Task live", "status": "verified"},
        ]}))
        log_dir = tmp_git_repo / "otto_logs" / "task-live"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "live-state.json").write_text(json.dumps({
            "task_key": "task-live",
            "status": "verified",
            "completed": True,
            "phases": {"merge": {"status": "pending", "time_s": 0.0}},
        }))
        (log_dir / "task-summary.json").write_text(json.dumps({
            "task_key": "task-live",
            "status": "verified",
        }))
        telemetry = Telemetry(tmp_git_repo / "otto_logs")

        self._create_candidate_commit(tmp_git_repo, "task-live", "live.txt", "ok\n")
        results = [TaskResult(task_key="task-live", success=True)]

        merged = merge_batch_results(results, self._make_config(), tmp_git_repo, tasks_path, telemetry)

        assert merged[0].success is True
        live_state = json.loads((log_dir / "live-state.json").read_text())
        summary = json.loads((log_dir / "task-summary.json").read_text())
        assert live_state["status"] == "passed"
        assert live_state["phases"]["merge"]["status"] == "done"
        assert summary["status"] == "passed"

    def test_record_run_history_marks_codex_cost_unavailable(self, tmp_git_repo):
        config = self._make_config()
        config["provider"] = "codex"

        _record_run_history(
            tmp_git_repo,
            config,
            results=[({"id": 1, "error": ""}, True)],
            run_duration=12.3,
            total_cost=0.0,
        )

        history_path = tmp_git_repo / "otto_logs" / "run-history.jsonl"
        entry = json.loads(history_path.read_text().strip().splitlines()[-1])
        assert entry["cost_available"] is False


class TestMergeCandidate:
    """Tests for merge_candidate git operation."""

    def test_clean_merge(self, tmp_git_repo):
        """Merge a candidate ref onto a temp branch with no conflicts."""
        from otto.git_ops import merge_candidate, _anchor_candidate_ref

        # Create a candidate commit
        (tmp_git_repo / "new_file.txt").write_text("hello\n")
        subprocess.run(["git", "add", "new_file.txt"], cwd=tmp_git_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "candidate"], cwd=tmp_git_repo, capture_output=True, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=tmp_git_repo, capture_output=True)
        _anchor_candidate_ref(tmp_git_repo, "test-task", 1, sha)

        success, new_sha = merge_candidate(
            tmp_git_repo, f"refs/otto/candidates/test-task/attempt-1", "main",
        )
        assert success
        assert new_sha != ""
        assert not (tmp_git_repo / "new_file.txt").exists()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head != new_sha

    def test_merge_conflict_without_llm_returns_false(self, tmp_git_repo):
        """Merge conflict with LLM disabled should return (False, '') and leave main clean."""
        from otto.git_ops import merge_candidate, _anchor_candidate_ref

        # Create a file on main
        (tmp_git_repo / "shared.txt").write_text("main content\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=tmp_git_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "main has shared"], cwd=tmp_git_repo, capture_output=True, check=True)

        # Create a candidate that modifies the same file from the parent of main's commit
        head_before_main_commit = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "checkout", head_before_main_commit], cwd=tmp_git_repo, capture_output=True)
        (tmp_git_repo / "shared.txt").write_text("candidate content\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=tmp_git_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "candidate"], cwd=tmp_git_repo, capture_output=True, check=True)
        candidate_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        _anchor_candidate_ref(tmp_git_repo, "conflict-task", 1, candidate_sha)

        success, new_sha = merge_candidate(
            tmp_git_repo, "refs/otto/candidates/conflict-task/attempt-1", "main",
        )
        assert not success
        assert new_sha == ""
        # Should be back on main
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "main"


class TestRunBatchParallel:
    @pytest.mark.asyncio
    async def test_setup_failures_persist_to_tasks_file(self, tmp_git_repo):
        """Parallel setup failures should be recorded on disk."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-boom", "prompt": "boom", "status": "pending"},
        ]}))
        batch = Batch(tasks=[TaskPlan(task_key="task-boom")])
        config = {"default_branch": "main", "verify_timeout": 60}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        context = PipelineContext()

        with patch("otto.git_ops.create_task_worktree", side_effect=RuntimeError("setup boom")):
            results = await _run_batch_parallel(
                batch, {"task-boom": {"key": "task-boom", "prompt": "boom"}}, context, config, tmp_git_repo, telemetry, tasks_path, max_parallel=1,
            )

        assert len(results) == 1
        assert not results[0].success
        persisted = yaml.safe_load(tasks_path.read_text())["tasks"][0]
        assert persisted["status"] == "failed"
        assert persisted["error_code"] == "worktree_setup_failed"
        assert "setup boom" in persisted["error"]

    @pytest.mark.asyncio
    async def test_semaphore_bounds_setup_concurrency(self, tmp_git_repo):
        """Worktree creation and install should respect max_parallel."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "one", "status": "pending"},
            {"id": 2, "key": "task-two", "prompt": "two", "status": "pending"},
        ]}))
        batch = Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")])
        config = {"default_branch": "main", "verify_timeout": 60}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        context = PipelineContext()

        active_setup = 0
        max_active_setup = 0
        lock = threading.Lock()

        def fake_create_task_worktree(project_dir, task_key, base_sha):
            nonlocal active_setup, max_active_setup
            with lock:
                active_setup += 1
                max_active_setup = max(max_active_setup, active_setup)
            time.sleep(0.05)
            worktree = project_dir / ".otto-worktrees" / task_key
            worktree.mkdir(parents=True, exist_ok=True)
            with lock:
                active_setup -= 1
            return worktree

        def fake_install_deps(worktree_path, timeout):
            nonlocal active_setup, max_active_setup
            with lock:
                active_setup += 1
                max_active_setup = max(max_active_setup, active_setup)
            time.sleep(0.05)
            with lock:
                active_setup -= 1
            return None

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None, qa_mode="per_task", sibling_context=None):
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.git_ops.create_task_worktree", side_effect=fake_create_task_worktree):
            with patch("otto.testing._install_deps", side_effect=fake_install_deps):
                with patch("otto.git_ops.cleanup_task_worktree"):
                    with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                        results = await _run_batch_parallel(
                            batch, {
                                "task-one": {"key": "task-one", "prompt": "one"},
                                "task-two": {"key": "task-two", "prompt": "two"},
                            }, context, config, tmp_git_repo, telemetry, tasks_path, max_parallel=1,
                        )

        assert all(result.success for result in results)
        assert max_active_setup == 1

    @pytest.mark.asyncio
    async def test_integrated_unit_runs_once_and_expands_results(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-a", "prompt": "A", "status": "pending"},
            {"id": 2, "key": "task-b", "prompt": "B", "status": "pending"},
            {"id": 3, "key": "task-c", "prompt": "C", "status": "pending"},
        ]}))
        batch = Batch(units=[
            BatchUnit(tasks=[TaskPlan(task_key="task-a"), TaskPlan(task_key="task-b")]),
            BatchUnit(tasks=[TaskPlan(task_key="task-c")]),
        ])
        config = {"default_branch": "main", "verify_timeout": 60}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        context = PipelineContext()

        async def fake_run_integrated_unit(*args, **kwargs):
            return [
                TaskResult(task_key="task-a", success=True, unit_key="unit-ab", unit_task_keys=["task-a", "task-b"]),
                TaskResult(task_key="task-b", success=True, unit_key="unit-ab", unit_task_keys=["task-a", "task-b"]),
            ]

        async def fake_run_task(*args, **kwargs):
            return TaskResult(task_key="task-c", success=True)

        with patch("otto.orchestrator._run_integrated_unit_in_worktree", side_effect=fake_run_integrated_unit) as integrated_mock:
            with patch("otto.orchestrator._run_task_in_worktree", side_effect=fake_run_task) as task_mock:
                results = await _run_batch_parallel(
                    batch,
                    {
                        "task-a": {"key": "task-a", "prompt": "A"},
                        "task-b": {"key": "task-b", "prompt": "B"},
                        "task-c": {"key": "task-c", "prompt": "C"},
                    },
                    context,
                    config,
                    tmp_git_repo,
                    telemetry,
                    tasks_path,
                    max_parallel=2,
                )

        assert integrated_mock.call_count == 1
        assert task_mock.call_count == 1
        assert [r.task_key for r in results] == ["task-a", "task-b", "task-c"]
        assert all(r.success for r in results)


class TestCrashRecovery:
    """Tests for crash recovery — stale state reset on startup."""

    @pytest.mark.asyncio
    async def test_stale_verified_reset_to_pending(self, tmp_git_repo):
        """Tasks stuck in 'verified' should be reset to 'pending' on startup."""
        from otto.tasks import load_tasks
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "stuck-task", "prompt": "Stuck", "status": "verified"},
        ]}))

        config = {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "max_parallel": 1,
        }

        # Run preflight checks which should reset the stale state
        from otto.runner import preflight_checks
        error_code, pending = preflight_checks(config, tasks_path, tmp_git_repo)

        assert error_code is None
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_stale_merge_pending_reset_to_pending(self, tmp_git_repo):
        """Tasks stuck in 'merge_pending' should be reset to 'pending' on startup."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "mp-task", "prompt": "Merge pending", "status": "merge_pending"},
        ]}))

        config = {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "max_parallel": 1,
        }

        from otto.runner import preflight_checks
        error_code, pending = preflight_checks(config, tasks_path, tmp_git_repo)

        assert error_code is None
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_merged_tasks_not_reset_on_startup(self, tmp_git_repo):
        """Tasks in 'merged' state should NOT be reset — code already landed on main."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "merged-task", "prompt": "Merged", "status": "merged"},
        ]}))

        config = {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "max_parallel": 1,
        }

        from otto.runner import preflight_checks
        error_code, pending = preflight_checks(config, tasks_path, tmp_git_repo)

        assert error_code == 0  # no pending tasks → clean exit
        # merged task should NOT appear in pending list
        assert len(pending) == 0
        # verify it stayed merged in the file
        from otto.tasks import load_tasks
        persisted = {t["key"]: t for t in load_tasks(tasks_path)}
        assert persisted["merged-task"]["status"] == "merged"


class TestInstallTimeout:
    """Tests for separate install_timeout config."""

    def test_install_timeout_defaults_from_verify_timeout(self):
        """install_timeout should fall back to verify_timeout when not set."""
        from otto.config import load_config, DEFAULT_CONFIG
        assert "install_timeout" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["install_timeout"] == 120

    def test_install_timeout_independent(self):
        """install_timeout and verify_timeout should be separate config values."""
        from otto.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["install_timeout"] != DEFAULT_CONFIG["verify_timeout"]


# ── Parallel QA ─────────────────────────────────────────────────────────


class TestParallelQA:
    """Tests for parallel per-task QA via asyncio.gather."""

    def _make_tasks(self, visual=False):
        """Create task list. If visual=True, add a non-verifiable ◈ spec."""
        spec1 = [{"text": "A works", "binding": "must", "verifiable": True}]
        spec2 = [{"text": "B works", "binding": "must", "verifiable": True}]
        if visual:
            spec1.append({"text": "A looks good", "binding": "must", "verifiable": False})
        return [
            {"key": "t1", "prompt": "Task A", "spec": spec1},
            {"key": "t2", "prompt": "Task B", "spec": spec2},
        ]

    @pytest.mark.asyncio
    async def test_parallel_dispatches_per_task_sessions(self, tmp_path):
        """parallel_qa=True with code-only tasks dispatches per-task run_qa calls."""
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": tasks[0]["key"], "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks()
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
            )

        # Should dispatch 2 separate per-task calls, not 1 batch call
        assert len(call_args) == 2
        assert call_args[0][0] == ["t1"]
        assert call_args[1][0] == ["t2"]
        assert call_args[0][1]["light_batch_qa"] is True
        assert call_args[1][1]["light_batch_qa"] is True
        assert call_args[0][1]["require_full_test_suite"] is False
        assert call_args[1][1]["require_full_test_suite"] is False
        assert result["must_passed"] is True
        assert result["cost_usd"] == 1.0  # 2 × $0.50

    @pytest.mark.asyncio
    async def test_parallel_works_with_visual_specs(self, tmp_path):
        """parallel_qa=True with ◈ specs still parallelizes (agent-browser handles concurrency)."""
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": tasks[0]["key"], "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks(visual=True)  # has ◈ spec
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
            )

        # agent-browser handles concurrent sessions — should still parallelize
        assert len(call_args) == 2
        assert call_args[0][0] == ["t1"]
        assert call_args[1][0] == ["t2"]
        assert call_args[0][1]["light_batch_qa"] is True
        assert call_args[1][1]["light_batch_qa"] is True
        assert call_args[0][1]["require_full_test_suite"] is False
        assert call_args[1][1]["require_full_test_suite"] is False

    @pytest.mark.asyncio
    async def test_parallel_merge_propagates_failure(self, tmp_path):
        """When one parallel session fails, merged result reflects it."""
        call_count = 0

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            nonlocal call_count
            call_count += 1
            key = tasks[0]["key"]
            if key == "t2":
                return {
                    "must_passed": False,
                    "verdict": {"must_passed": False, "must_items": [
                        {"task_key": "t2", "spec_id": 1, "status": "fail",
                         "evidence": "B broken"},
                    ]},
                    "raw_report": "",
                    "cost_usd": 0.40,
                }
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": "t1", "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks()
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
            )

        assert result["must_passed"] is False
        assert "t2" in result["failed_task_keys"]
        assert len(result["verdict"]["must_items"]) == 2

    @pytest.mark.asyncio
    async def test_parallel_propagates_infrastructure_error(self, tmp_path):
        """Infrastructure error in one session propagates to merged result."""
        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            key = tasks[0]["key"]
            if key == "t1":
                return {
                    "must_passed": None,
                    "verdict": {},
                    "raw_report": "",
                    "cost_usd": 0.0,
                    "infrastructure_error": True,
                }
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": "t2", "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks()
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
            )

        assert result["infrastructure_error"] is True

    @pytest.mark.asyncio
    async def test_parallel_exception_in_one_session_doesnt_crash(self, tmp_path):
        """If one per-task QA session throws, others still complete."""
        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            key = tasks[0]["key"]
            if key == "t1":
                raise RuntimeError("SDK crashed")
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": "t2", "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks()
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
            )

        # t1 crashed → must_passed=False, t2 passed
        assert result["must_passed"] is False
        assert "t1" in result["failed_task_keys"]

    @pytest.mark.asyncio
    async def test_parallel_disabled_by_default(self, tmp_path):
        """Without parallel_qa config, uses flat batch QA."""
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": []},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {}  # no parallel_qa
        tasks = self._make_tasks()
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
            )

        # Should be 1 batch call with both tasks
        assert len(call_args) == 1
        assert sorted(call_args[0][0]) == ["t1", "t2"]
        assert call_args[0][1]["light_batch_qa"] is False
        assert call_args[0][1]["require_full_test_suite"] is True

    @pytest.mark.asyncio
    async def test_parallel_focused_retries_filter_tasks(self, tmp_path):
        """Focused retries only dispatch sessions for focused task keys."""
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": tasks[0]["key"], "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks()
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
                focus_task_keys={"t2"},
            )

        # Should only dispatch session for t2, not t1
        assert len(call_args) == 1
        assert call_args[0][0] == ["t2"]
        assert call_args[0][1]["light_batch_qa"] is True
        assert call_args[0][1]["require_full_test_suite"] is False

    @pytest.mark.asyncio
    async def test_integrated_unit_runs_one_qa_session(self, tmp_path):
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            return {
                "must_passed": True,
                "verdict": {
                    "must_passed": True,
                    "must_items": [
                        {"task_key": "t1", "spec_id": 1, "status": "pass"},
                        {"task_key": "t2", "spec_id": 1, "status": "pass"},
                    ],
                },
                "raw_report": "",
                "cost_usd": 0.50,
                "failed_task_keys": [],
            }

        config = {"parallel_qa": True}
        tasks = self._make_tasks()
        batch = Batch(units=[
            BatchUnit(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
        ])
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
                batch=batch,
            )

        assert len(call_args) == 1
        assert call_args[0][0] == ["t1", "t2"]
        assert call_args[0][1]["light_batch_qa"] is False
        assert call_args[0][1]["require_full_test_suite"] is False
        assert result["must_passed"] is True
        assert call_args[0][1].get("retried_task_keys") is None

    @pytest.mark.asyncio
    async def test_mixed_units_dispatch_by_unit_not_by_task(self, tmp_path):
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            must_items = [
                {"task_key": task["key"], "spec_id": 1, "status": "pass"}
                for task in tasks
            ]
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": must_items},
                "raw_report": "",
                "cost_usd": 0.50,
                "failed_task_keys": [],
            }

        config = {"parallel_qa": True}
        tasks = [
            {"key": "t1", "prompt": "Task A", "spec": [{"text": "A works", "binding": "must", "verifiable": True}]},
            {"key": "t2", "prompt": "Task B", "spec": [{"text": "B works", "binding": "must", "verifiable": True}]},
            {"key": "t3", "prompt": "Task C", "spec": [{"text": "C works", "binding": "must", "verifiable": True}]},
        ]
        batch = Batch(units=[
            BatchUnit(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
            BatchUnit(tasks=[TaskPlan(task_key="t3")]),
        ])
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
                batch=batch,
            )

        assert len(call_args) == 2
        assert call_args[0][0] == ["t1", "t2"]
        assert call_args[0][1]["light_batch_qa"] is False
        assert call_args[0][1]["require_full_test_suite"] is False
        assert call_args[1][0] == ["t3"]
        assert call_args[1][1]["light_batch_qa"] is True
        assert call_args[1][1]["require_full_test_suite"] is False
        assert result["must_passed"] is True

    @pytest.mark.asyncio
    async def test_mixed_units_propagate_focus_to_unit_tasks(self, tmp_path):
        call_args = []

        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            call_args.append(([t["key"] for t in tasks], kw))
            must_items = [
                {"task_key": task["key"], "spec_id": 1, "status": "pass"}
                for task in tasks
            ]
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": must_items},
                "raw_report": "",
                "cost_usd": 0.50,
                "failed_task_keys": [],
            }

        config = {"parallel_qa": True}
        tasks = [
            {"key": "t1", "prompt": "Task A", "spec": [{"text": "A works", "binding": "must", "verifiable": True}]},
            {"key": "t2", "prompt": "Task B", "spec": [{"text": "B works", "binding": "must", "verifiable": True}]},
            {"key": "t3", "prompt": "Task C", "spec": [{"text": "C works", "binding": "must", "verifiable": True}]},
        ]
        batch = Batch(units=[
            BatchUnit(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
            BatchUnit(tasks=[TaskPlan(task_key="t3")]),
        ])
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
                batch=batch,
                focus_task_keys={"t2"},
        )

        assert len(call_args) == 1
        assert call_args[0][0] == ["t1", "t2"]
        assert call_args[0][1].get("retried_task_keys") == {"t2"}
        assert call_args[0][1]["require_full_test_suite"] is False

    @pytest.mark.asyncio
    async def test_parallel_merge_preserves_integration_findings(self, tmp_path):
        async def fake_run_qa(tasks, config, project_dir, diff, **kw):
            if len(tasks) == 2:
                return {
                    "must_passed": False,
                    "verdict": {
                        "must_passed": False,
                        "must_items": [
                            {"task_key": "t1", "spec_id": 1, "status": "pass"},
                            {"task_key": "t2", "spec_id": 1, "status": "pass"},
                        ],
                        "integration_findings": [
                            {"description": "combined failure", "status": "fail", "tasks_involved": ["t1", "t2"]},
                        ],
                        "regressions": ["regression detail"],
                    },
                    "raw_report": "",
                    "cost_usd": 0.50,
                    "failed_task_keys": ["t1", "t2"],
                }
            return {
                "must_passed": True,
                "verdict": {"must_passed": True, "must_items": [
                    {"task_key": "t3", "spec_id": 1, "status": "pass"},
                ]},
                "raw_report": "",
                "cost_usd": 0.50,
                "failed_task_keys": [],
            }

        config = {"parallel_qa": True}
        tasks = [
            {"key": "t1", "prompt": "Task A", "spec": [{"text": "A works", "binding": "must", "verifiable": True}]},
            {"key": "t2", "prompt": "Task B", "spec": [{"text": "B works", "binding": "must", "verifiable": True}]},
            {"key": "t3", "prompt": "Task C", "spec": [{"text": "C works", "binding": "must", "verifiable": True}]},
        ]
        batch = Batch(units=[
            BatchUnit(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
            BatchUnit(tasks=[TaskPlan(task_key="t3")]),
        ])
        with patch("otto.orchestrator.run_qa", side_effect=fake_run_qa):
            result = await _run_batch_qa(
                tasks, config, tmp_path, tmp_path / "tasks.yaml",
                MagicMock(), MagicMock(),
                batch=batch,
            )

        assert result["must_passed"] is False
        assert result["verdict"]["integration_findings"][0]["description"] == "combined failure"
        assert result["verdict"]["regressions"] == ["regression detail"]
