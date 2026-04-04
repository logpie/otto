"""Tests for worktree-based task execution — the unified serial + parallel path.

Covers:
- git_ops: create_task_worktree, cleanup_task_worktree, cleanup_all_worktrees
- orchestrator: _run_task_in_worktree, serial via worktree, merge_batch_results
- cli: one-off path via temp tasks.yaml
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from otto.git_ops import (
    create_task_worktree,
    cleanup_task_worktree,
    cleanup_all_worktrees,
)
from otto.planner import Batch, ExecutionPlan, TaskPlan
from otto.orchestrator import (
    _run_task_in_worktree,
    merge_batch_results,
    run_per,
)
from otto.context import PipelineContext, TaskResult
from otto.telemetry import Telemetry
from otto.tasks import load_tasks, update_task


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True, check=True,
    )
    readme = repo / "README.md"
    readme.write_text("# Test repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo, capture_output=True, check=True,
    )
    return repo


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit_file(repo: Path, filename: str, content: str, message: str = "test") -> str:
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)
    return _head_sha(repo)


# ── git_ops: Worktree creation/cleanup ────────────────────────────────────


class TestCreateTaskWorktree:
    def test_creates_worktree_at_correct_path(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)
        wt = create_task_worktree(tmp_git_repo, "task-abc", base_sha)

        assert wt.exists()
        assert wt.name == "otto-task-task-abc"
        assert wt.parent.name == ".otto-worktrees"
        # Worktree has the repo content
        assert (wt / "README.md").exists()

        # Cleanup
        cleanup_task_worktree(tmp_git_repo, "task-abc")

    def test_worktree_is_detached_head(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)
        wt = create_task_worktree(tmp_git_repo, "task-detach", base_sha)

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head == base_sha

        # Should be on detached HEAD, not a branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=wt, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == ""  # detached HEAD has no current branch

        cleanup_task_worktree(tmp_git_repo, "task-detach")

    def test_worktree_at_specific_sha(self, tmp_git_repo):
        first_sha = _head_sha(tmp_git_repo)
        _commit_file(tmp_git_repo, "new.txt", "hello", "second commit")

        # Create worktree at first commit — should NOT have new.txt
        wt = create_task_worktree(tmp_git_repo, "task-old", first_sha)
        assert not (wt / "new.txt").exists()
        assert (wt / "README.md").exists()

        cleanup_task_worktree(tmp_git_repo, "task-old")

    def test_stale_worktree_removed_on_create(self, tmp_git_repo):
        """If a worktree from a crashed run exists, it's cleaned up."""
        base_sha = _head_sha(tmp_git_repo)
        wt = create_task_worktree(tmp_git_repo, "task-stale", base_sha)
        assert wt.exists()

        # Create again — should succeed without error
        wt2 = create_task_worktree(tmp_git_repo, "task-stale", base_sha)
        assert wt2.exists()
        assert wt2 == wt  # same path

        cleanup_task_worktree(tmp_git_repo, "task-stale")

    def test_changes_in_worktree_dont_affect_main(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)
        wt = create_task_worktree(tmp_git_repo, "task-iso", base_sha)

        # Write a file in the worktree
        (wt / "worktree-only.txt").write_text("from worktree")
        subprocess.run(["git", "add", "worktree-only.txt"], cwd=wt, capture_output=True)
        subprocess.run(["git", "commit", "-m", "wt commit"], cwd=wt, capture_output=True)

        # Main repo should NOT see this file
        assert not (tmp_git_repo / "worktree-only.txt").exists()
        main_sha = _head_sha(tmp_git_repo)
        assert main_sha == base_sha  # main hasn't moved

        cleanup_task_worktree(tmp_git_repo, "task-iso")


class TestCleanupTaskWorktree:
    def test_cleanup_removes_directory(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)
        wt = create_task_worktree(tmp_git_repo, "task-clean", base_sha)
        assert wt.exists()

        cleanup_task_worktree(tmp_git_repo, "task-clean")
        assert not wt.exists()

    def test_cleanup_nonexistent_is_noop(self, tmp_git_repo):
        """Cleaning up a worktree that doesn't exist should not error."""
        cleanup_task_worktree(tmp_git_repo, "task-nonexistent")

    def test_worktree_pruned_from_git(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)
        create_task_worktree(tmp_git_repo, "task-prune", base_sha)

        cleanup_task_worktree(tmp_git_repo, "task-prune")

        # Verify git worktree list doesn't show it
        wt_list = subprocess.run(
            ["git", "worktree", "list"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto-task-task-prune" not in wt_list


class TestCleanupAllWorktrees:
    def test_removes_all_otto_worktrees(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)
        wt1 = create_task_worktree(tmp_git_repo, "task-a", base_sha)
        wt2 = create_task_worktree(tmp_git_repo, "task-b", base_sha)
        assert wt1.exists()
        assert wt2.exists()

        cleanup_all_worktrees(tmp_git_repo)

        assert not wt1.exists()
        assert not wt2.exists()

    def test_no_worktrees_is_noop(self, tmp_git_repo):
        cleanup_all_worktrees(tmp_git_repo)  # should not error


# ── orchestrator: _run_task_in_worktree ───────────────────────────────────


class TestRunTaskInWorktree:
    @pytest.mark.asyncio
    async def test_creates_and_cleans_up_worktree(self, tmp_git_repo):
        """Worktree should be created for the task and cleaned up after."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-wt", "prompt": "Test", "status": "pending"},
        ]}))
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        context = PipelineContext()
        base_sha = _head_sha(tmp_git_repo)
        task_plan = TaskPlan(task_key="task-wt")

        worktree_used = []

        async def fake_coding_loop(task_plan, context, config, project_dir,
                                    telemetry, tasks_file, task_work_dir=None,
                                    qa_mode="per_task", sibling_context=None):
            # Record that coding_loop received a worktree path
            worktree_used.append(task_work_dir)
            assert task_work_dir is not None
            assert task_work_dir != project_dir  # NOT project_dir
            assert task_work_dir.exists()
            assert (task_work_dir / "README.md").exists()
            update_task(tasks_file, "task-wt", status="verified")
            return TaskResult(task_key="task-wt", success=True)

        with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
            with patch("otto.testing._install_deps"):
                result = await _run_task_in_worktree(
                    task_plan, context, config, tmp_git_repo,
                    telemetry, tasks_path, base_sha,
                )

        assert result.success
        assert len(worktree_used) == 1
        # Worktree should be cleaned up after
        wt_path = tmp_git_repo / ".otto-worktrees" / "otto-task-task-wt"
        assert not wt_path.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_worktree_on_failure(self, tmp_git_repo):
        """Worktree should be cleaned up even if coding_loop raises."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-fail", "prompt": "Test", "status": "pending"},
        ]}))
        config = {"default_branch": "main", "max_retries": 1, "verify_timeout": 60}
        telemetry = Telemetry(tmp_git_repo / "otto_logs")
        context = PipelineContext()
        base_sha = _head_sha(tmp_git_repo)
        task_plan = TaskPlan(task_key="task-fail")

        async def boom(*args, **kwargs):
            raise RuntimeError("coding exploded")

        with patch("otto.orchestrator.coding_loop", side_effect=boom):
            with patch("otto.testing._install_deps"):
                result = await _run_task_in_worktree(
                    task_plan, context, config, tmp_git_repo,
                    telemetry, tasks_path, base_sha,
                )

        assert not result.success
        assert "coding exploded" in result.error
        # Worktree should still be cleaned up
        wt_path = tmp_git_repo / ".otto-worktrees" / "otto-task-task-fail"
        assert not wt_path.exists()


# ── orchestrator: Serial execution via worktree ───────────────────────────


class TestSerialViaWorktree:
    def _make_config(self, repo):
        return {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "max_parallel": 1,
            "execution_mode": "planned",
        }

    @pytest.mark.asyncio
    async def test_serial_task_uses_worktree_not_project_dir(self, tmp_git_repo):
        """Serial tasks should use worktrees, not work in project_dir."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "serial-wt", "prompt": "Add hello", "status": "pending"},
        ]}))
        config = self._make_config(tmp_git_repo)
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="serial-wt")]),
        ])

        worktree_dirs = []

        async def fake_coding_loop(task_plan, context, config, project_dir,
                                    telemetry, tasks_file, task_work_dir=None,
                                    qa_mode="per_task", sibling_context=None):
            worktree_dirs.append(task_work_dir)
            # Verify it's a worktree, not project_dir
            assert task_work_dir is not None
            assert ".otto-worktrees" in str(task_work_dir)
            update_task(tasks_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True)

        def fake_merge(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for r in results:
                update_task(task_file, r.task_key, status="passed")
            return results

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge):
                    with patch("otto.testing._install_deps"):
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert len(worktree_dirs) == 1
        assert worktree_dirs[0] is not None
        assert str(worktree_dirs[0]) != str(tmp_git_repo)

    @pytest.mark.asyncio
    async def test_serial_multi_task_each_gets_own_worktree(self, tmp_git_repo):
        """Each serial task should get its own worktree."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-a", "prompt": "Task A", "status": "pending"},
            {"id": 2, "key": "task-b", "prompt": "Task B", "status": "pending"},
        ]}))
        config = self._make_config(tmp_git_repo)
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="task-a"), TaskPlan(task_key="task-b")]),
        ])

        worktree_dirs = {}

        async def fake_coding_loop(task_plan, context, config, project_dir,
                                    telemetry, tasks_file, task_work_dir=None,
                                    qa_mode="per_task", sibling_context=None):
            worktree_dirs[task_plan.task_key] = task_work_dir
            update_task(tasks_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True)

        def fake_merge(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for r in results:
                update_task(task_file, r.task_key, status="passed")
            return results

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge):
                    with patch("otto.testing._install_deps"):
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 0
        assert len(worktree_dirs) == 2
        # Each task got a different worktree
        assert worktree_dirs["task-a"] != worktree_dirs["task-b"]
        assert "task-a" in str(worktree_dirs["task-a"])
        assert "task-b" in str(worktree_dirs["task-b"])

    @pytest.mark.asyncio
    async def test_tasks_yaml_not_corrupted_after_serial_failure(self, tmp_git_repo):
        """Failed tasks should not corrupt tasks.yaml — the bug 6 regression test."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "ok-task", "prompt": "OK", "status": "pending"},
            {"id": 2, "key": "bad-task", "prompt": "Fail", "status": "pending"},
        ]}))
        config = self._make_config(tmp_git_repo)
        execution_plan = ExecutionPlan(batches=[
            Batch(tasks=[TaskPlan(task_key="ok-task"), TaskPlan(task_key="bad-task")]),
        ])

        call_count = {"ok-task": 0, "bad-task": 0}

        async def fake_coding_loop(task_plan, context, config, project_dir,
                                    telemetry, tasks_file, task_work_dir=None,
                                    qa_mode="per_task", sibling_context=None):
            call_count[task_plan.task_key] += 1
            if task_plan.task_key == "bad-task":
                update_task(tasks_file, task_plan.task_key, status="failed",
                           error="test failure", error_code="test_failed")
                return TaskResult(task_key=task_plan.task_key, success=False, error="test failure")
            update_task(tasks_file, task_plan.task_key, status="verified")
            return TaskResult(task_key=task_plan.task_key, success=True)

        def fake_merge(results, config, project_dir, task_file, telemetry, qa_mode="per_task"):
            for r in results:
                if r.success:
                    update_task(task_file, r.task_key, status="passed")
            return results

        with patch("otto.orchestrator.plan", AsyncMock(return_value=execution_plan)):
            with patch("otto.orchestrator.coding_loop", side_effect=fake_coding_loop):
                with patch("otto.orchestrator.merge_batch_results", side_effect=fake_merge):
                    with patch("otto.testing._install_deps"):
                        exit_code = await run_per(config, tasks_path, tmp_git_repo)

        assert exit_code == 1
        # Verify tasks.yaml is correct — BOTH tasks should have their final status
        persisted = {t["key"]: t for t in load_tasks(tasks_path)}
        assert persisted["ok-task"]["status"] == "passed"
        assert persisted["bad-task"]["status"] == "failed"


# ── merge_batch_results: git-level integration ────────────────────────────


class TestMergeBatchResultsGit:
    """Integration tests that verify actual git merges via merge_batch_results."""

    def _make_config(self):
        return {
            "default_branch": "main",
            "max_retries": 1,
            "verify_timeout": 60,
            "skip_test": True,
        }

    def _create_candidate_commit(self, repo, task_key, filename, content):
        """Create a commit in a worktree and anchor it as a candidate ref."""
        from otto.git_ops import _anchor_candidate_ref
        base_sha = _head_sha(repo)
        wt = create_task_worktree(repo, task_key, base_sha)
        (wt / filename).write_text(content)
        subprocess.run(["git", "add", filename], cwd=wt, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"otto: {task_key}"],
            cwd=wt, capture_output=True, check=True,
        )
        sha = _head_sha(wt)
        _anchor_candidate_ref(repo, task_key, 1, sha)
        cleanup_task_worktree(repo, task_key)
        return sha

    def test_single_task_merges_to_main(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-m1", "prompt": "test", "status": "verified"},
        ]}))
        config = self._make_config()
        telemetry = Telemetry(tmp_git_repo / "otto_logs")

        sha = self._create_candidate_commit(tmp_git_repo, "task-m1", "hello.txt", "hello")
        result = TaskResult(task_key="task-m1", success=True, commit_sha=sha)

        merged = merge_batch_results([result], config, tmp_git_repo, tasks_path, telemetry)

        assert len(merged) == 1
        assert merged[0].success
        # File should now be on main
        assert (tmp_git_repo / "hello.txt").exists()
        assert (tmp_git_repo / "hello.txt").read_text() == "hello"

    def test_two_non_conflicting_tasks_merge_sequentially(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-x", "prompt": "test", "status": "verified"},
            {"id": 2, "key": "task-y", "prompt": "test", "status": "verified"},
        ]}))
        config = self._make_config()
        telemetry = Telemetry(tmp_git_repo / "otto_logs")

        sha_x = self._create_candidate_commit(tmp_git_repo, "task-x", "x.txt", "x content")
        sha_y = self._create_candidate_commit(tmp_git_repo, "task-y", "y.txt", "y content")

        results = [
            TaskResult(task_key="task-x", success=True, commit_sha=sha_x),
            TaskResult(task_key="task-y", success=True, commit_sha=sha_y),
        ]
        merged = merge_batch_results(results, config, tmp_git_repo, tasks_path, telemetry)

        assert all(r.success for r in merged)
        assert (tmp_git_repo / "x.txt").read_text() == "x content"
        assert (tmp_git_repo / "y.txt").read_text() == "y content"

    def test_conflicting_tasks_detected(self, tmp_git_repo):
        """Two tasks modifying the same file should result in merge conflict."""
        tasks_path = tmp_git_repo / "tasks.yaml"
        tasks_path.write_text(yaml.dump({"tasks": [
            {"id": 1, "key": "task-c1", "prompt": "test", "status": "verified"},
            {"id": 2, "key": "task-c2", "prompt": "test", "status": "verified"},
        ]}))
        config = self._make_config()
        telemetry = Telemetry(tmp_git_repo / "otto_logs")

        # Both tasks modify README.md (from same base)
        sha_c1 = self._create_candidate_commit(tmp_git_repo, "task-c1", "README.md", "version A\n")
        sha_c2 = self._create_candidate_commit(tmp_git_repo, "task-c2", "README.md", "version B\n")

        results = [
            TaskResult(task_key="task-c1", success=True, commit_sha=sha_c1),
            TaskResult(task_key="task-c2", success=True, commit_sha=sha_c2),
        ]
        merged = merge_batch_results(results, config, tmp_git_repo, tasks_path, telemetry)

        # First should succeed, second should conflict
        assert merged[0].success
        assert not merged[1].success
        assert merged[1].error_code in ("merge_conflict", "merge_failed")
