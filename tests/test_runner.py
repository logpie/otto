"""Tests for otto.runner module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import yaml

from otto.runner import (
    check_clean_tree,
    create_task_branch,
    build_candidate_commit,
    merge_to_default,
    cleanup_branch,
    run_task,
)


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

    def test_dirty_tracked_file_fails(self, tmp_git_repo):
        """Modified tracked files (not otto runtime) should still fail."""
        # Create and commit a file, then modify it
        (tmp_git_repo / "real.py").write_text("x = 1")
        subprocess.run(["git", "add", "real.py"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add real"], cwd=tmp_git_repo, capture_output=True)
        (tmp_git_repo / "real.py").write_text("x = 2")
        assert check_clean_tree(tmp_git_repo) is False


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


