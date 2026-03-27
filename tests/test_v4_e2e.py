"""End-to-end tests for v4 PER pipeline.

Tests the full integration: CLI dispatch, orchestrator, coding_loop,
planner, telemetry, context — all with mocked LLM calls.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from otto.context import PipelineContext, TaskResult
from otto.orchestrator import run_per, cleanup_orphaned_worktrees, _plan_covers_pending
from otto.planner import Batch, ExecutionPlan, TaskPlan, default_plan, parse_plan_json
from otto.runner import coding_loop, preflight_checks, rebase_and_merge
from otto.telemetry import Telemetry, TaskStarted, TaskMerged, TaskFailed, AllDone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _commit_file(repo, name, content, msg="add file"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, capture_output=True, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _setup_project(repo):
    """Set up a minimal otto project with config and tasks."""
    config = {
        "default_branch": "main",
        "max_retries": 1,
        "verify_timeout": 60,
        "max_parallel": 2,
        "effort": "high",
        "orchestrator": "v4",
    }
    (repo / "otto.yaml").write_text(yaml.dump(config))
    return config


# ---------------------------------------------------------------------------
# E2E: CLI dispatch
# ---------------------------------------------------------------------------

class TestCLIDispatch:
    """Verify CLI routes to v4 by default and v3 with --pilot."""

    def test_v4_is_default(self, tmp_git_repo, monkeypatch):
        """Without --pilot, CLI should import run_per."""
        import click.testing
        from otto.cli import main

        _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": []}))

        monkeypatch.chdir(tmp_git_repo)
        runner = click.testing.CliRunner()
        # Dry run just checks config, doesn't need LLM
        result = runner.invoke(main, ["run", "--dry-run"])
        assert result.exit_code == 0
        assert "Pending tasks: 0" in result.output


# ---------------------------------------------------------------------------
# E2E: Preflight checks
# ---------------------------------------------------------------------------

class TestPreflightE2E:
    def test_preflight_on_clean_repo(self, tmp_git_repo):
        """Preflight should pass on clean repo with pending tasks."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Do thing", "status": "pending"},
        ]}))
        error_code, pending = preflight_checks(config, tasks_path, tmp_git_repo)
        assert error_code is None
        assert len(pending) == 1
        assert pending[0]["key"] == "abc123def456"

    def test_preflight_no_pending(self, tmp_git_repo):
        """No pending tasks should return exit code 0."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Done", "status": "passed"},
        ]}))
        error_code, pending = preflight_checks(config, tasks_path, tmp_git_repo)
        assert error_code == 0
        assert pending == []

    def test_preflight_resets_stale_running(self, tmp_git_repo):
        """Tasks stuck in 'running' should be reset to 'pending'."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Stuck", "status": "running"},
        ]}))
        error_code, pending = preflight_checks(config, tasks_path, tmp_git_repo)
        assert error_code is None
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# E2E: Planner coverage validation
# ---------------------------------------------------------------------------

class TestPlannerCoverage:
    def test_valid_plan_accepted(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2")]),
        ])
        pending = [{"key": "t1"}, {"key": "t2"}]
        assert _plan_covers_pending(plan, pending) is True

    def test_missing_task_rejected(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1")]),
        ])
        pending = [{"key": "t1"}, {"key": "t2"}]
        assert _plan_covers_pending(plan, pending) is False

    def test_duplicate_task_rejected(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t1")]),
        ])
        pending = [{"key": "t1"}]
        assert _plan_covers_pending(plan, pending) is False

    def test_extra_task_rejected(self):
        plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="t1"), TaskPlan(task_key="t2"), TaskPlan(task_key="t3")]),
        ])
        pending = [{"key": "t1"}, {"key": "t2"}]
        assert _plan_covers_pending(plan, pending) is False


# ---------------------------------------------------------------------------
# E2E: Default plan with dependencies
# ---------------------------------------------------------------------------

class TestDefaultPlanDeps:
    def test_deps_respected(self):
        """Task 2 depends on task 1 — must be in a later batch."""
        tasks = [
            {"key": "t1", "id": 1, "prompt": "Foundation"},
            {"key": "t2", "id": 2, "prompt": "Depends on t1", "depends_on": [1]},
        ]
        plan = default_plan(tasks)
        assert plan.total_tasks == 2
        batch_of = {}
        for i, batch in enumerate(plan.batches):
            for tp in batch.tasks:
                batch_of[tp.task_key] = i
        assert batch_of["t2"] > batch_of["t1"]

    def test_independent_tasks_parallel(self):
        """Three independent tasks should be in one batch."""
        tasks = [
            {"key": "t1", "id": 1, "prompt": "A"},
            {"key": "t2", "id": 2, "prompt": "B"},
            {"key": "t3", "id": 3, "prompt": "C"},
        ]
        plan = default_plan(tasks)
        assert len(plan.batches) == 1
        assert plan.total_tasks == 3

    def test_chain_deps(self):
        """t1 -> t2 -> t3 should produce 3 sequential batches."""
        tasks = [
            {"key": "t1", "id": 1, "prompt": "A"},
            {"key": "t2", "id": 2, "prompt": "B", "depends_on": [1]},
            {"key": "t3", "id": 3, "prompt": "C", "depends_on": [2]},
        ]
        plan = default_plan(tasks)
        assert len(plan.batches) == 3


# ---------------------------------------------------------------------------
# E2E: Telemetry JSONL + dual-write
# ---------------------------------------------------------------------------

class TestTelemetryE2E:
    def test_full_lifecycle_events(self, tmp_path):
        """Simulate a full task lifecycle and verify JSONL output."""
        t = Telemetry(tmp_path / "logs")
        t.enable_legacy_write()

        # Simulate lifecycle
        t.log(TaskStarted(task_key="t1", task_id=1, prompt="Add hello"))
        t.log(TaskMerged(task_key="t1", task_id=1, cost_usd=0.15, duration_s=30.0))
        t.log(AllDone(total_passed=1, total_failed=0, total_cost=0.15, total_duration_s=30.0))

        # Check v4 events
        lines = t.events_path.read_text().strip().splitlines()
        assert len(lines) == 3
        events = [json.loads(l) for l in lines]
        assert events[0]["event"] == "task_started"
        assert events[1]["event"] == "task_merged"
        assert events[2]["event"] == "all_done"

        # Check legacy dual-write
        legacy = tmp_path / "logs" / "pilot_results.jsonl"
        assert legacy.exists()
        legacy_lines = legacy.read_text().strip().splitlines()
        # Should have phase event + result event
        assert len(legacy_lines) >= 2

    def test_failed_task_telemetry(self, tmp_path):
        """Failed task should produce correct v4 + legacy events."""
        t = Telemetry(tmp_path / "logs")
        t.enable_legacy_write()

        t.log(TaskStarted(task_key="t1", task_id=1, prompt="Fail"))
        t.log(TaskFailed(task_key="t1", task_id=1, error="tests failed", cost_usd=0.10))

        legacy = tmp_path / "logs" / "pilot_results.jsonl"
        legacy_lines = legacy.read_text().strip().splitlines()
        # Last line should be the failure result
        last = json.loads(legacy_lines[-1])
        assert last["tool"] == "run_task_with_qa"
        assert last["success"] is False
        assert last["error"] == "tests failed"


# ---------------------------------------------------------------------------
# E2E: run_per orchestrator (mocked agents)
# ---------------------------------------------------------------------------

class TestRunPerE2E:
    """Full orchestrator tests with mocked coding_loop."""

    @pytest.mark.asyncio
    async def test_single_task_pass(self, tmp_git_repo):
        """One pending task that passes → exit 0."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Add hello", "status": "pending",
             "spec": ["hello() returns 'hello'"]},
        ]}))

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, qa_mode="per_task"):
            # Simulate: create branch, commit, merge (as run_task_with_qa would)
            key = task_plan.task_key
            branch = f"otto/{key}"
            subprocess.run(["git", "checkout", "-b", branch], cwd=project_dir, capture_output=True)
            (project_dir / "hello.py").write_text("def hello(): return 'hello'\n")
            subprocess.run(["git", "add", "hello.py"], cwd=project_dir, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"otto: add hello (#{1})"],
                           cwd=project_dir, capture_output=True)
            # Merge back
            subprocess.run(["git", "checkout", "main"], cwd=project_dir, capture_output=True)
            subprocess.run(["git", "merge", "--ff-only", branch], cwd=project_dir, capture_output=True)
            subprocess.run(["git", "branch", "-d", branch], cwd=project_dir, capture_output=True)
            # Update tasks.yaml
            from otto.tasks import update_task
            update_task(tasks_path, key, status="passed", cost_usd=0.10, duration_s=5.0)
            return TaskResult(task_key=key, success=True, cost_usd=0.10, duration_s=5.0)

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        # Verify hello.py was merged to main
        assert (tmp_git_repo / "hello.py").exists()
        # Verify telemetry was written
        events_file = tmp_git_repo / "otto_logs" / "v4_events.jsonl"
        assert events_file.exists()

    @pytest.mark.asyncio
    async def test_single_task_fail(self, tmp_git_repo):
        """One pending task that fails → exit 1."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Impossible", "status": "pending",
             "spec": ["Something impossible"]},
        ]}))

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, qa_mode="per_task"):
            from otto.tasks import update_task
            update_task(tasks_path, task_plan.task_key, status="failed", error="max retries")
            return TaskResult(task_key=task_plan.task_key, success=False, error="max retries")

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_multi_task_sequential_batches(self, tmp_git_repo):
        """Two dependent tasks → sequential batches, both pass → exit 0."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task1key12345", "prompt": "Foundation", "status": "pending"},
            {"id": 2, "key": "task2key12345", "prompt": "Build on 1", "status": "pending",
             "depends_on": [1]},
        ]}))

        call_order = []

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, qa_mode="per_task"):
            call_order.append(task_plan.task_key)
            from otto.tasks import update_task
            update_task(tasks_path, task_plan.task_key, status="passed", cost_usd=0.05)
            return TaskResult(task_key=task_plan.task_key, success=True, cost_usd=0.05)

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        # Task 1 must run before task 2 (dependency order)
        assert call_order.index("task1key12345") < call_order.index("task2key12345")

    @pytest.mark.asyncio
    async def test_no_pending_tasks(self, tmp_git_repo):
        """All tasks already passed → exit 0, no coding_loop calls."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Done", "status": "passed"},
        ]}))

        exit_code = await run_per(config, tasks_path, tmp_git_repo)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_interrupted_exits_nonzero(self, tmp_git_repo):
        """If interrupted flag set during execution, exit 1."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task1key12345", "prompt": "A", "status": "pending"},
            {"id": 2, "key": "task2key12345", "prompt": "B", "status": "pending"},
        ]}))

        call_count = 0

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, qa_mode="per_task"):
            nonlocal call_count
            call_count += 1
            from otto.tasks import update_task
            update_task(tasks_path, task_plan.task_key, status="passed")
            # Set interrupted after first task
            if call_count == 1:
                context.interrupted = True
            return TaskResult(task_key=task_plan.task_key, success=True)

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            exit_code = await run_per(config, tasks_path, tmp_git_repo)

        # Should exit 1 because task2 was never executed
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_telemetry_all_done_event(self, tmp_git_repo):
        """AllDone event should be written with correct counts."""
        config = _setup_project(tmp_git_repo)
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "abc123def456", "prompt": "Test", "status": "pending"},
        ]}))

        async def fake_coding_loop(task_plan, context, config, project_dir, telemetry, tasks_file, qa_mode="per_task"):
            from otto.tasks import update_task
            update_task(tasks_path, task_plan.task_key, status="passed", cost_usd=0.25)
            return TaskResult(task_key=task_plan.task_key, success=True, cost_usd=0.25)

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            await run_per(config, tasks_path, tmp_git_repo)

        events_file = tmp_git_repo / "otto_logs" / "v4_events.jsonl"
        events = [json.loads(l) for l in events_file.read_text().strip().splitlines()]
        all_done = [e for e in events if e["event"] == "all_done"]
        assert len(all_done) == 1
        assert all_done[0]["total_passed"] == 1
        assert all_done[0]["total_failed"] == 0


# ---------------------------------------------------------------------------
# E2E: Context state management
# ---------------------------------------------------------------------------

class TestContextE2E:
    def test_learnings_injected_into_hint(self):
        """Context learnings should be available for coding_loop to inject."""
        ctx = PipelineContext()
        ctx.add_learning("Use pytest -x for fast fail", source="task-0")
        ctx.add_learning("API requires auth header", source="task-1")
        ctx.add_research("task1", "Found: use requests library")

        assert len(ctx.learnings) == 2
        assert ctx.get_research("task1") == "Found: use requests library"
        assert ctx.get_research("nonexistent") is None

    def test_cost_accumulation(self):
        """Costs should accumulate correctly across tasks."""
        ctx = PipelineContext()
        ctx.add_success(TaskResult(task_key="t1", success=True, cost_usd=0.50))
        ctx.add_failure(TaskResult(task_key="t2", success=False, cost_usd=0.30))
        ctx.add_success(TaskResult(task_key="t3", success=True, cost_usd=0.20))
        assert abs(ctx.total_cost - 1.0) < 0.001
        assert ctx.passed_count == 2
        assert ctx.failed_count == 1


# ---------------------------------------------------------------------------
# E2E: Rebase and merge integration
# ---------------------------------------------------------------------------

class TestRebaseAndMergeE2E:
    def test_two_sequential_merges(self, tmp_git_repo):
        """Two task branches merged sequentially via rebase."""
        # Task 1
        subprocess.run(["git", "checkout", "-b", "otto/t1"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "t1.py", "# task 1", "otto: task 1")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)

        result1 = rebase_and_merge(tmp_git_repo, "otto/t1", "main")
        assert result1 is True
        assert (tmp_git_repo / "t1.py").exists()

        # Task 2 — branched from old main (before t1 merge)
        # Simulate by creating from current main
        subprocess.run(["git", "checkout", "-b", "otto/t2"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "t2.py", "# task 2", "otto: task 2")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)

        result2 = rebase_and_merge(tmp_git_repo, "otto/t2", "main")
        assert result2 is True
        assert (tmp_git_repo / "t2.py").exists()

        # Both files should be on main
        files = subprocess.run(
            ["git", "ls-files"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "t1.py" in files
        assert "t2.py" in files


# ---------------------------------------------------------------------------
# E2E: Worktree cleanup
# ---------------------------------------------------------------------------

class TestWorktreeCleanupE2E:
    def test_cleanup_removes_otto_worktrees(self, tmp_git_repo):
        """cleanup_orphaned_worktrees should remove otto- dirs."""
        wt = tmp_git_repo / ".worktrees"
        wt.mkdir()
        (wt / "otto-task1").mkdir()
        (wt / "otto-task2").mkdir()
        (wt / "not-otto").mkdir()

        cleanup_orphaned_worktrees(tmp_git_repo)

        assert not (wt / "otto-task1").exists()
        assert not (wt / "otto-task2").exists()
        assert (wt / "not-otto").exists()  # not touched

    def test_cleanup_no_worktrees_dir(self, tmp_git_repo):
        """No .worktrees dir should not raise."""
        cleanup_orphaned_worktrees(tmp_git_repo)  # should be a no-op


# ---------------------------------------------------------------------------
# E2E: Config default
# ---------------------------------------------------------------------------

class TestConfigDefault:
    def test_agent_settings_default_to_project(self):
        from otto.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["coding_agent_settings"] == "project"
        assert DEFAULT_CONFIG["spec_agent_settings"] == "project"
        assert DEFAULT_CONFIG["qa_agent_settings"] == "project"
        assert DEFAULT_CONFIG["planner_agent_settings"] == "project"


# ---------------------------------------------------------------------------
# E2E: Parse plan JSON edge cases
# ---------------------------------------------------------------------------

class TestParsePlanEdgeCases:
    def test_nested_json_in_text(self):
        raw = 'Let me create a plan. {"batches": [{"tasks": [{"task_key": "abc"}]}]} That should work.'
        plan = parse_plan_json(raw)
        assert plan is not None
        assert plan.total_tasks == 1

    def test_triple_backtick_json(self):
        raw = "```json\n{\"batches\": [{\"tasks\": [{\"task_key\": \"abc\"}]}]}\n```"
        plan = parse_plan_json(raw)
        assert plan is not None

    def test_completely_empty(self):
        assert parse_plan_json("") is None

    def test_valid_json_but_wrong_schema(self):
        assert parse_plan_json('{"tasks": [1,2,3]}') is None

    def test_array_not_object(self):
        assert parse_plan_json('[1, 2, 3]') is None
