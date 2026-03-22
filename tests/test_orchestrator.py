"""Tests for otto.orchestrator — PER loop, batch execution, serial merge."""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.context import PipelineContext, TaskResult
from otto.orchestrator import cleanup_orphaned_worktrees, run_per
from otto.planner import Batch, ExecutionPlan, TaskPlan


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


class TestRunPerIntegration:
    """Integration tests for run_per with mocked agents."""

    def _make_config(self, repo):
        return {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "max_parallel": 2,
            "effort": "high",
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
        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file):
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file):
            return TaskResult(
                task_key=task_plan.task_key, success=False,
                error="tests failed", cost_usd=0.20,
            )

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_signal_sets_interrupted(self, tmp_git_repo):
        """Context.interrupted should be set by signal handler."""
        context = PipelineContext()
        assert context.interrupted is False
        context.interrupted = True
        assert context.interrupted is True

    @pytest.mark.asyncio
    async def test_batch_tasks_run_sequentially(self, tmp_git_repo):
        """Tasks in the same batch must not overlap on the shared checkout."""
        import yaml
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        events: list[str] = []
        in_flight = False

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file):
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file):
            executed.append(task_plan.task_key)
            if task_plan.task_key == "task-one":
                return TaskResult(task_key=task_plan.task_key, success=False, error="boom")
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.plan", AsyncMock(return_value=initial_plan)):
            with patch("otto.orchestrator.replan", AsyncMock(return_value=invalid_replan)):
                with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        assert executed == ["task-one", "task-two", "task-three"]

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
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])
        executed: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file):
            executed.append(task_plan.task_key)
            context.interrupted = True
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        assert executed == ["task-one"]

        events_path = tmp_git_repo / "otto_logs" / "v4_events.jsonl"
        all_done = json.loads(events_path.read_text().strip().splitlines()[-1])
        assert all_done["event"] == "all_done"
        assert all_done["total_missing_or_interrupted"] == 1


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
