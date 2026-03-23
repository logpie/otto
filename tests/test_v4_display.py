"""Tests for v4 display wiring — verify coding_loop creates TaskDisplay and routes events.

These tests mock run_task_with_qa to simulate progress events and verify
they reach the TaskDisplay. Without this wiring, the user sees a blank
terminal during v4 runs.
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import yaml

from otto.context import PipelineContext, TaskResult
from otto.planner import TaskPlan
from otto.runner import coding_loop
from otto.telemetry import Telemetry


def _setup_task(repo, key="abc123def456", task_id=1, prompt="Add hello"):
    """Create a minimal task in tasks.yaml."""
    tasks_path = repo / "tasks.yaml"
    tasks_path.write_text(yaml.dump({"tasks": [
        {"id": task_id, "key": key, "prompt": prompt, "status": "pending",
         "spec": ["Function hello() returns 'hello'"]},
    ]}))
    return tasks_path


class TestCodingLoopDisplay:
    """Verify that coding_loop creates a TaskDisplay and routes events to it."""

    @pytest.mark.asyncio
    async def test_display_created_and_started(self, tmp_git_repo):
        """TaskDisplay should be created and started when coding_loop runs."""
        tasks_path = _setup_task(tmp_git_repo)
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="abc123def456")

        mock_display = MagicMock()
        mock_display.stop.return_value = "5s"

        async def fake_run_task_with_qa(task, config, project_dir, tasks_file, hint=None, on_progress=None):
            # Simulate a successful run
            from otto.tasks import update_task
            update_task(tasks_path, "abc123def456", status="passed", cost_usd=0.10)
            return {"success": True, "cost_usd": 0.10, "status": "passed",
                    "error": "", "diff_summary": "", "qa_report": ""}

        with patch("otto.runner.run_task_with_qa", side_effect=fake_run_task_with_qa):
            with patch("otto.display.TaskDisplay", return_value=mock_display) as mock_cls:
                result = await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        # TaskDisplay was created and started
        mock_cls.assert_called_once()
        mock_display.start.assert_called_once()
        # TaskDisplay was stopped
        mock_display.stop.assert_called_once()
        assert result.success is True

    @pytest.mark.asyncio
    async def test_phase_events_routed_to_display(self, tmp_git_repo):
        """Phase events from on_progress should reach display.update_phase."""
        tasks_path = _setup_task(tmp_git_repo)
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="abc123def456")

        mock_display = MagicMock()
        mock_display.stop.return_value = "10s"
        phase_calls = []

        async def fake_run_task_with_qa(task, config, project_dir, tasks_file, hint=None, on_progress=None):
            # Simulate phase events
            if on_progress:
                on_progress("phase", {"name": "prepare", "status": "running"})
                on_progress("phase", {"name": "prepare", "status": "done", "time_s": 1.0})
                on_progress("phase", {"name": "coding", "status": "running"})
                on_progress("phase", {"name": "coding", "status": "done", "time_s": 5.0, "cost": 0.10})
            from otto.tasks import update_task
            update_task(tasks_path, "abc123def456", status="passed", cost_usd=0.10)
            return {"success": True, "cost_usd": 0.10, "status": "passed",
                    "error": "", "diff_summary": "", "qa_report": ""}

        with patch("otto.runner.run_task_with_qa", side_effect=fake_run_task_with_qa):
            with patch("otto.display.TaskDisplay", return_value=mock_display):
                await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        # Verify update_phase was called for each phase event
        update_phase_calls = mock_display.update_phase.call_args_list
        assert len(update_phase_calls) == 4
        assert update_phase_calls[0].kwargs["name"] == "prepare"
        assert update_phase_calls[0].kwargs["status"] == "running"
        assert update_phase_calls[3].kwargs["name"] == "coding"
        assert update_phase_calls[3].kwargs["status"] == "done"

    @pytest.mark.asyncio
    async def test_tool_events_routed_to_display(self, tmp_git_repo):
        """Agent tool calls from on_progress should reach display.add_tool."""
        tasks_path = _setup_task(tmp_git_repo)
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="abc123def456")

        mock_display = MagicMock()
        mock_display.stop.return_value = "5s"

        async def fake_run_task_with_qa(task, config, project_dir, tasks_file, hint=None, on_progress=None):
            if on_progress:
                on_progress("agent_tool", {"name": "Write", "detail": "hello.py"})
                on_progress("agent_tool", {"name": "Bash", "detail": "pytest"})
            from otto.tasks import update_task
            update_task(tasks_path, "abc123def456", status="passed", cost_usd=0.10)
            return {"success": True, "cost_usd": 0.10, "status": "passed",
                    "error": "", "diff_summary": "", "qa_report": ""}

        with patch("otto.runner.run_task_with_qa", side_effect=fake_run_task_with_qa):
            with patch("otto.display.TaskDisplay", return_value=mock_display):
                await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        assert mock_display.add_tool.call_count == 2
        first_call = mock_display.add_tool.call_args_list[0]
        assert first_call.kwargs["data"]["name"] == "Write"

    @pytest.mark.asyncio
    async def test_qa_events_routed_to_display(self, tmp_git_repo):
        """QA findings and summary should reach display."""
        tasks_path = _setup_task(tmp_git_repo)
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="abc123def456")

        mock_display = MagicMock()
        mock_display.stop.return_value = "5s"

        async def fake_run_task_with_qa(task, config, project_dir, tasks_file, hint=None, on_progress=None):
            if on_progress:
                on_progress("qa_finding", {"text": "### Spec 1: PASS"})
                on_progress("qa_finding", {"text": "### Spec 2: FAIL"})
                on_progress("qa_summary", {"total": 2, "passed": 1, "failed": 1})
            from otto.tasks import update_task
            update_task(tasks_path, "abc123def456", status="failed", error="QA failed")
            return {"success": False, "cost_usd": 0.10, "status": "failed",
                    "error": "QA failed", "diff_summary": "", "qa_report": ""}

        with patch("otto.runner.run_task_with_qa", side_effect=fake_run_task_with_qa):
            with patch("otto.display.TaskDisplay", return_value=mock_display):
                await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        assert mock_display.add_finding.call_count == 2
        mock_display.set_qa_summary.assert_called_once_with(total=2, passed=1, failed=1)

    @pytest.mark.asyncio
    async def test_display_stopped_on_failure(self, tmp_git_repo):
        """Display should be stopped even when task fails."""
        tasks_path = _setup_task(tmp_git_repo)
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="abc123def456")

        mock_display = MagicMock()
        mock_display.stop.return_value = "3s"

        async def fake_run_task_with_qa(task, config, project_dir, tasks_file, hint=None, on_progress=None):
            from otto.tasks import update_task
            update_task(tasks_path, "abc123def456", status="failed", error="tests failed")
            return {"success": False, "cost_usd": 0.05, "status": "failed",
                    "error": "tests failed", "diff_summary": "", "qa_report": ""}

        with patch("otto.runner.run_task_with_qa", side_effect=fake_run_task_with_qa):
            with patch("otto.display.TaskDisplay", return_value=mock_display):
                result = await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        mock_display.stop.assert_called_once()
        assert result.success is False

    @pytest.mark.asyncio
    async def test_display_stopped_on_exception(self, tmp_git_repo):
        """Display should be stopped even when run_task_with_qa crashes."""
        tasks_path = _setup_task(tmp_git_repo)
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="abc123def456")

        mock_display = MagicMock()
        mock_display.stop.return_value = "1s"

        async def fake_crash(*args, **kwargs):
            raise RuntimeError("agent SDK crashed")

        with patch("otto.runner.run_task_with_qa", side_effect=fake_crash):
            with patch("otto.display.TaskDisplay", return_value=mock_display):
                result = await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        mock_display.stop.assert_called_once()
        assert result.success is False
        assert "agent SDK crashed" in result.error

    @pytest.mark.asyncio
    async def test_no_display_for_missing_task(self, tmp_git_repo):
        """If task not found in tasks.yaml, no display should be created."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}))
        ctx = PipelineContext()
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60,
                   "effort": "high"}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        tp = TaskPlan(task_key="nonexistent123")

        with patch("otto.display.TaskDisplay") as mock_cls:
            result = await coding_loop(tp, ctx, config, tmp_git_repo, telemetry, tasks_path)

        # TaskDisplay should NOT be created for a missing task
        mock_cls.assert_not_called()
        assert result.success is False
        assert "not found" in result.error
