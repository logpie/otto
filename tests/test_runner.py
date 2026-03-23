"""Tests for otto.runner module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import yaml

from otto.runner import (
    _restore_workspace_state,
    _build_coding_prompt,
    _setup_task_worktree,
    _teardown_task_worktree,
    build_project_map,
    check_clean_tree,
    create_task_branch,
    build_candidate_commit,
    merge_to_default,
    cleanup_branch,
    rebase_and_merge,
    preflight_checks,
)


def _commit_all(repo: Path, message: str = "test commit") -> None:
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)


class TestCheckCleanTree:
    def test_clean_repo_passes(self, tmp_git_repo):
        assert check_clean_tree(tmp_git_repo) is True

    def test_dirty_repo_auto_stashes(self, tmp_git_repo):
        """Dirty tracked files get auto-stashed, returning True."""
        (tmp_git_repo / "dirty.txt").write_text("dirty")
        subprocess.run(["git", "add", "dirty.txt"], cwd=tmp_git_repo, capture_output=True)
        assert check_clean_tree(tmp_git_repo) is True
        # Verify stash was created
        stash = subprocess.run(
            ["git", "stash", "list"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        )
        assert "otto: auto-stash" in stash.stdout

    def test_ignores_tasks_yaml(self, tmp_git_repo):
        """Modified tasks.yaml should not count as dirty."""
        (tmp_git_repo / "tasks.yaml").write_text("tasks: []\n")
        assert check_clean_tree(tmp_git_repo) is True

    def test_ignores_tasks_lock(self, tmp_git_repo):
        """Untracked .tasks.lock should not count as dirty."""
        (tmp_git_repo / ".tasks.lock").write_text("")
        assert check_clean_tree(tmp_git_repo) is True

    def test_ignores_untracked_files(self, tmp_git_repo):
        """Untracked files (like user scratch files) should not count as dirty."""
        (tmp_git_repo / "scratch.py").write_text("x = 1")
        assert check_clean_tree(tmp_git_repo) is True

    def test_dirty_tracked_file_auto_stashes(self, tmp_git_repo):
        """Modified tracked files get auto-stashed, returning True."""
        (tmp_git_repo / "real.py").write_text("x = 1")
        subprocess.run(["git", "add", "real.py"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add real"], cwd=tmp_git_repo, capture_output=True)
        (tmp_git_repo / "real.py").write_text("x = 2")
        assert check_clean_tree(tmp_git_repo) is True
        stash = subprocess.run(
            ["git", "stash", "list"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        )
        assert "otto: auto-stash" in stash.stdout


class TestCreateTaskBranch:
    def test_creates_branch(self, tmp_git_repo):
        base_sha = create_task_branch(tmp_git_repo, "abc123def456", "main")
        # Verify we're on the new branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "otto/abc123def456"
        assert len(base_sha) == 40  # full SHA

    def test_recreates_stale_branch(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        # Should not raise — deletes and recreates
        base_sha = create_task_branch(tmp_git_repo, "abc123def456", "main")
        assert len(base_sha) == 40


class TestBuildProjectMap:
    def test_prioritizes_manifests_and_renders_shallow_tree(self, tmp_git_repo):
        (tmp_git_repo / "package.json").write_text('{"name": "otto"}\n')
        (tmp_git_repo / "Dockerfile").write_text("FROM python:3.12\n")
        (tmp_git_repo / "zeta.txt").write_text("zeta\n")
        (tmp_git_repo / "src" / "app").mkdir(parents=True)
        (tmp_git_repo / "src" / "lib").mkdir(parents=True)
        (tmp_git_repo / "src" / "app" / "layout.tsx").write_text("export default null\n")
        (tmp_git_repo / "src" / "app" / "my file.ts").write_text("export const x = 1\n")
        (tmp_git_repo / "src" / "lib" / "api.ts").write_text("export const api = {}\n")
        _commit_all(tmp_git_repo, "add project tree")

        project_map = build_project_map(tmp_git_repo)
        lines = project_map.splitlines()

        assert lines[:4] == ["package.json", "README.md", "Dockerfile", "zeta.txt"]
        assert "src/" in lines
        assert "  app/" in lines
        assert "    layout.tsx" in lines
        assert "    my file.ts" in lines
        assert "  lib/" in lines
        assert "    api.ts" in lines

    def test_collapses_large_directories(self, tmp_git_repo):
        vendor_dir = tmp_git_repo / "vendor"
        vendor_dir.mkdir()
        for index in range(21):
            (vendor_dir / f"file_{index:02d}.txt").write_text(f"{index}\n")
        _commit_all(tmp_git_repo, "add vendor files")

        project_map = build_project_map(tmp_git_repo)

        assert "vendor/  (21 files)" in project_map

    def test_caps_output_and_reports_remaining_files(self, tmp_git_repo):
        for index in range(200):
            (tmp_git_repo / f"file_{index:03d}.txt").write_text(f"{index}\n")
        _commit_all(tmp_git_repo, "add many files")

        project_map = build_project_map(tmp_git_repo)
        lines = project_map.splitlines()

        assert len(lines) == 150
        assert lines[-1] == "... and 52 more files"


class TestBuildCodingPrompt:
    @patch("otto.runner.build_project_map", return_value="PROJECT MAP")
    def test_uses_shared_project_map(self, mock_project_map, tmp_git_repo):
        prompt = _build_coding_prompt(
            {"prompt": "Add feature", "key": "abc123"},
            {},
            tmp_git_repo,
            tmp_git_repo,
        )

        mock_project_map.assert_called_once_with(tmp_git_repo)
        assert "PROJECT FILES:\nPROJECT MAP" in prompt


class TestBuildCandidateCommit:
    def test_creates_candidate_with_changes(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        # Simulate agent changes
        (tmp_git_repo / "new_file.py").write_text("print('hello')\n")
        candidate = build_candidate_commit(tmp_git_repo, base_sha, testgen_file=None)
        assert candidate != base_sha
        assert len(candidate) == 40

    def test_includes_testgen_file(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        (tmp_git_repo / "new_file.py").write_text("print('hello')\n")
        # Create a fake testgen file (in the git metadata dir)
        git_common_dir_raw = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_common_dir = Path(git_common_dir_raw)
        if not git_common_dir.is_absolute():
            git_common_dir = (tmp_git_repo / git_common_dir).resolve()
        testgen_dir = git_common_dir / "otto" / "testgen" / "abc123def456"
        testgen_dir.mkdir(parents=True)
        testgen_file = testgen_dir / "test_otto_abc123def456.py"
        testgen_file.write_text("def test_verify(): assert True\n")
        candidate = build_candidate_commit(tmp_git_repo, base_sha, testgen_file=testgen_file)
        # Verify test file is in the candidate
        show = subprocess.run(
            ["git", "show", f"{candidate}:tests/test_otto_abc123def456.py"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        assert show.returncode == 0


class TestMergeToDefault:
    def test_fast_forward_merge(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        (tmp_git_repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "otto: add feature (#1)"],
            cwd=tmp_git_repo, capture_output=True,
        )
        success = merge_to_default(tmp_git_repo, "abc123def456", "main")
        assert success
        # Should be on main now
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "main"


class TestCleanupBranch:
    def test_deletes_branch(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        cleanup_branch(tmp_git_repo, "abc123def456", "main")
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/abc123def456" not in branches


class TestWorkspaceCleanup:
    @patch("otto.runner._log_warn")
    @patch("otto.runner.subprocess.run")
    def test_restore_workspace_logs_warning_on_git_reset_failure(self, mock_run, mock_warn, tmp_git_repo):
        mock_run.return_value = MagicMock(returncode=1, stderr="reset failed", stdout="")

        _restore_workspace_state(tmp_git_repo)

        mock_warn.assert_called_once()
        assert "git reset --hard" in mock_warn.call_args.args[0]


class TestTaskWorktree:
    def test_teardown_removes_worktree_branch(self, tmp_git_repo):
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

        wt_dir = _setup_task_worktree(tmp_git_repo, "abc123def456", base_sha)
        _teardown_task_worktree(tmp_git_repo, "abc123def456")

        assert not wt_dir.exists()
        branches = subprocess.run(
            ["git", "branch"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "otto/abc123def456" not in branches


class TestTamperDetection:
    def test_detects_modified_test_file(self, tmp_git_repo):
        test_file = tmp_git_repo / "tests" / "test_otto_abc.py"
        test_file.parent.mkdir(exist_ok=True)
        test_file.write_text("def test_a(): assert False\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "tests"], cwd=tmp_git_repo, capture_output=True)
        original_sha = subprocess.run(
            ["git", "hash-object", str(test_file)],
            capture_output=True, text=True,
        ).stdout.strip()

        # Tamper with file
        test_file.write_text("def test_a(): assert True\n")
        current_sha = subprocess.run(
            ["git", "hash-object", str(test_file)],
            capture_output=True, text=True,
        ).stdout.strip()

        assert current_sha != original_sha

        # Restore
        subprocess.run(
            ["git", "checkout", "HEAD", "--", "tests/test_otto_abc.py"],
            cwd=tmp_git_repo, capture_output=True,
        )
        restored_sha = subprocess.run(
            ["git", "hash-object", str(test_file)],
            capture_output=True, text=True,
        ).stdout.strip()
        assert restored_sha == original_sha


def _commit_file(repo, name, content, msg="add file"):
    """Helper: write, stage, commit a file."""
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, capture_output=True, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestRebaseAndMerge:
    def test_simple_rebase_and_merge(self, tmp_git_repo):
        """Task branch rebases onto main and merges ff-only."""
        # Create a commit on main
        _commit_file(tmp_git_repo, "base.txt", "base", "base commit")

        # Create task branch from main
        subprocess.run(["git", "checkout", "-b", "otto/task1"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "task.txt", "task work", "task commit")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)

        # Add another commit on main (diverge)
        _commit_file(tmp_git_repo, "main_new.txt", "main work", "main diverge")

        # Rebase and merge should succeed
        result = rebase_and_merge(tmp_git_repo, "otto/task1", "main")
        assert result is True

        # Verify task file exists on main
        assert (tmp_git_repo / "task.txt").exists()
        assert (tmp_git_repo / "main_new.txt").exists()

        # Verify branch was deleted
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/task1" not in branches

    def test_rebase_conflict_returns_false(self, tmp_git_repo):
        """Conflicting changes should return False."""
        _commit_file(tmp_git_repo, "conflict.txt", "base content", "base")
        subprocess.run(["git", "checkout", "-b", "otto/task2"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "conflict.txt", "task content", "task edit")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "conflict.txt", "main content", "main edit")

        result = rebase_and_merge(tmp_git_repo, "otto/task2", "main")
        assert result is False

        # Branch should still exist (rebase was aborted)
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/task2" in branches

    def test_fast_forward_when_no_diverge(self, tmp_git_repo):
        """No divergence = simple ff merge."""
        subprocess.run(["git", "checkout", "-b", "otto/task3"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "simple.txt", "simple", "simple commit")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)

        result = rebase_and_merge(tmp_git_repo, "otto/task3", "main")
        assert result is True
        assert (tmp_git_repo / "simple.txt").exists()
