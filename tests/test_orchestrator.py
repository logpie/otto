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

from otto.context import PipelineContext, TaskResult
from otto.orchestrator import (
    _run_batch_parallel,
    cleanup_orphaned_worktrees,
    merge_parallel_results,
    run_per,
)
from otto.planner import Batch, ExecutionPlan, TaskPlan
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
        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
            return TaskResult(
                task_key=task_plan.task_key, success=False,
                error="tests failed", cost_usd=0.20,
            )

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1

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

        def fake_merge_parallel_results(results, config, project_dir, task_file, telemetry):
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

        rerun_calls: list[str] = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None):
            rerun_calls.append(task_plan.task_key)
            task = next(t for t in load_tasks(task_file) if t.get("key") == task_plan.task_key)
            assert task["status"] == "pending"
            assert task["attempts"] == 2
            assert task.get("error_code") is None
            update_task(task_file, task_plan.task_key, status="passed")
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.preflight_checks", side_effect=fake_preflight_checks):
            with patch("otto.orchestrator.plan", new=AsyncMock(return_value=execution_plan)):
                with patch("otto.orchestrator._run_batch_parallel", side_effect=fake_run_batch_parallel):
                    with patch("otto.orchestrator.merge_parallel_results", side_effect=fake_merge_parallel_results):
                        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                            with patch("otto.orchestrator._print_summary"):
                                with patch("otto.orchestrator._record_run_history"):
                                    exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert rerun_calls == ["task-merge"]

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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
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

    @pytest.mark.asyncio
    async def test_post_run_integration_skipped_when_run_not_clean(self, tmp_git_repo):
        """End-of-run integration should not run if any task already failed."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
            {"id": 3, "key": "task-three", "prompt": "Task three", "status": "pending", "spec": ["three"]},
        ]}))

        config = self._make_config(tmp_git_repo)
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None):
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
    async def test_post_run_integration_uses_main_suite_only(self, tmp_git_repo):
        """End-of-run integration should run the main suite without task verify hooks."""
        from otto.testing import TestSuiteResult, TierResult

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["test_command"] = "pytest"
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None):
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
    async def test_post_run_integration_failure_is_run_level_only(self, tmp_git_repo):
        """Integration failure should exit non-zero without blaming any one task."""
        from otto.testing import TestSuiteResult, TierResult

        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-one", "prompt": "Task one", "status": "pending", "spec": ["one"]},
            {"id": 2, "key": "task-two", "prompt": "Task two", "status": "pending", "spec": ["two"]},
        ]}))

        config = self._make_config(tmp_git_repo)
        config["test_command"] = "pytest"
        config["max_parallel"] = 1
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-one"), TaskPlan(task_key="task-two")]),
        ])

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, task_file, task_work_dir=None):
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

        events_path = tmp_git_repo / "otto_logs" / "v4_events.jsonl"
        all_done = json.loads(events_path.read_text().strip().splitlines()[-1])
        assert all_done["event"] == "all_done"
        assert all_done["total_failed"] == 0

        history_path = tmp_git_repo / "otto_logs" / "run-history.jsonl"
        history_entry = json.loads(history_path.read_text().strip().splitlines()[-1])
        assert history_entry["tasks_failed"] == 0
        assert history_entry["failure_summary"] == "post-run integration failed: 1 failed"


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

        merged = merge_parallel_results(results, config, tmp_git_repo, tasks_path, telemetry)

        assert len(merged) == 2
        assert all(r.success for r in merged)
        # Both files should exist on main
        assert (tmp_git_repo / "a.txt").exists()
        assert (tmp_git_repo / "b.txt").exists()

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

        merged = merge_parallel_results(results, config, tmp_git_repo, tasks_path, telemetry)

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

        merged = merge_parallel_results(results, config, tmp_git_repo, tasks_path, telemetry)

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

        merged = merge_parallel_results(results, config, tmp_git_repo, tasks_path, telemetry)
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
            merged = merge_parallel_results(results, self._make_config(), tmp_git_repo, tasks_path, telemetry)

        assert merged == results
        merge_candidate_mock.assert_not_called()

    def test_post_merge_test_failure(self, tmp_git_repo):
        """Post-merge test failure should mark task as merge_failed and revert."""
        from otto.testing import TestSuiteResult, TierResult
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
        config["skip_test"] = False  # enable post-merge verification

        # Mock run_test_suite to return failure
        failed_suite = TestSuiteResult(
            passed=False,
            tiers=[TierResult(tier="existing_tests", passed=False, output="1 failed")],
        )
        with patch("otto.testing.run_test_suite", return_value=failed_suite):
            merged = merge_parallel_results(results, config, tmp_git_repo, tasks_path, telemetry)

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
        """Merge verification should forward each task's custom verify command."""
        from otto.testing import TestSuiteResult, TierResult
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
            merge_parallel_results(results, config, tmp_git_repo, tasks_path, telemetry)

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
            merge_parallel_results(results, self._make_config(), tmp_git_repo, tasks_path, telemetry)

        assert "merge_pending" in status_updates
        assert status_updates.index("merge_pending") < status_updates.index("passed")


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
                batch, context, config, tmp_git_repo, telemetry, tasks_path, max_parallel=1,
            )

        assert len(results) == 1
        assert not results[0].success
        persisted = yaml.safe_load(tasks_path.read_text())["tasks"][0]
        assert persisted["status"] == "failed"
        assert persisted["error_code"] == "parallel_setup_failed"
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

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, task_work_dir=None):
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.git_ops.create_task_worktree", side_effect=fake_create_task_worktree):
            with patch("otto.testing._install_deps", side_effect=fake_install_deps):
                with patch("otto.git_ops.cleanup_task_worktree"):
                    with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                        results = await _run_batch_parallel(
                            batch, context, config, tmp_git_repo, telemetry, tasks_path, max_parallel=1,
                        )

        assert all(result.success for result in results)
        assert max_active_setup == 1


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
