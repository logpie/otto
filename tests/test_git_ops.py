"""Tests for git operations — merge, cleanup, staging, candidate commits.

Uses real git repos (tmp_path + git init). No mocking of git commands.

Covers:
- merge_candidate: success, conflict, cleanup
- _abort_merge_and_cleanup: MERGE_HEAD cleanup, branch restoration
- check_clean_tree: clean, staged, modified, untracked
- build_candidate_commit: staging, otto-owned exclusion
- _is_otto_owned: path classification
- _should_stage_untracked: source vs artifact filtering
"""

import subprocess
from pathlib import Path

import pytest

from otto.git_ops import (
    _abort_merge_and_cleanup,
    _is_otto_owned,
    _should_stage_untracked,
    build_candidate_commit,
    check_clean_tree,
    merge_candidate,
)


# ── Helpers ──────────────────────────────────────────────────────────────


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


def _current_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit_file(repo: Path, filename: str, content: str, message: str = "test") -> str:
    filepath = repo / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)
    return _head_sha(repo)


def _default_branch(repo: Path) -> str:
    """Get the default branch name (main or master depending on git config)."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ── _is_otto_owned ──────────────────────────────────────────────────────


class TestIsOttoOwned:
    def test_tasks_yaml_is_owned(self):
        assert _is_otto_owned("tasks.yaml") is True

    def test_tasks_lock_is_owned(self):
        assert _is_otto_owned(".tasks.lock") is True

    def test_otto_logs_prefix_is_owned(self):
        assert _is_otto_owned("otto_logs/foo.log") is True
        assert _is_otto_owned("otto_logs/task-abc/attempt-1-agent.log") is True

    def test_otto_scratch_prefix_is_owned(self):
        assert _is_otto_owned(".otto-scratch/tmp") is True

    def test_otto_worktrees_prefix_is_owned(self):
        assert _is_otto_owned(".otto-worktrees/otto-task-abc/README.md") is True

    def test_source_file_is_not_owned(self):
        assert _is_otto_owned("src/main.py") is False

    def test_root_file_is_not_owned(self):
        assert _is_otto_owned("package.json") is False

    def test_test_file_is_not_owned(self):
        assert _is_otto_owned("tests/test_foo.py") is False

    def test_otto_arch_prefix_is_owned(self):
        assert _is_otto_owned("otto_arch/plan.md") is True

    def test_dot_otto_prefix_is_owned(self):
        assert _is_otto_owned(".otto/config") is True


# ── _should_stage_untracked ─────────────────────────────────────────────


class TestShouldStageUntracked:
    def test_source_file_staged(self):
        assert _should_stage_untracked("src/app.py") is True

    def test_config_file_staged(self):
        assert _should_stage_untracked("tsconfig.json") is True

    def test_otto_logs_not_staged(self):
        assert _should_stage_untracked("otto_logs/run.jsonl") is False

    def test_tasks_yaml_not_staged(self):
        assert _should_stage_untracked("tasks.yaml") is False

    def test_node_modules_not_staged(self):
        assert _should_stage_untracked("node_modules/foo/index.js") is False

    def test_venv_not_staged(self):
        assert _should_stage_untracked(".venv/bin/python") is False

    def test_pycache_not_staged(self):
        assert _should_stage_untracked("__pycache__/foo.pyc") is False

    def test_compiled_extension_not_staged(self):
        assert _should_stage_untracked("lib/binding.so") is False
        assert _should_stage_untracked("module.pyc") is False

    def test_pytest_cache_not_staged(self):
        assert _should_stage_untracked(".pytest_cache/v/cache/stepwise") is False

    def test_next_dir_not_staged(self):
        assert _should_stage_untracked(".next/build-manifest.json") is False

    def test_egg_info_not_staged(self):
        assert _should_stage_untracked("otto.egg-info/PKG-INFO") is False

    def test_otto_worktrees_not_staged(self):
        assert _should_stage_untracked(".otto-worktrees/otto-task-abc/file.py") is False

    def test_tasks_lock_not_staged(self):
        assert _should_stage_untracked(".tasks.lock") is False

    def test_otto_lock_not_staged(self):
        assert _should_stage_untracked("otto.lock") is False


# ── check_clean_tree ────────────────────────────────────────────────────


class TestCheckCleanTree:
    def test_clean_repo(self, tmp_git_repo):
        assert check_clean_tree(tmp_git_repo) is True

    def test_staged_changes_dirty(self, tmp_git_repo):
        (tmp_git_repo / "new.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "new.py"], cwd=tmp_git_repo, capture_output=True)
        assert check_clean_tree(tmp_git_repo) is False

    def test_modified_tracked_file_dirty(self, tmp_git_repo):
        (tmp_git_repo / "README.md").write_text("changed\n")
        assert check_clean_tree(tmp_git_repo) is False

    def test_untracked_files_clean(self, tmp_git_repo):
        """Untracked files don't make the tree dirty (uses -uno flag)."""
        (tmp_git_repo / "untracked.txt").write_text("I'm untracked\n")
        assert check_clean_tree(tmp_git_repo) is True

    def test_otto_owned_modifications_clean(self, tmp_git_repo):
        """Otto-owned files are ignored even if modified/staged."""
        # Track tasks.yaml first so it shows as modified
        tasks_file = tmp_git_repo / "tasks.yaml"
        tasks_file.write_text("key: val\n")
        subprocess.run(["git", "add", "tasks.yaml"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add tasks"], cwd=tmp_git_repo, capture_output=True)
        # Now modify it — should still be clean
        tasks_file.write_text("key: changed\n")
        assert check_clean_tree(tmp_git_repo) is True


# ── merge_candidate ─────────────────────────────────────────────────────


class TestMergeCandidate:
    def test_successful_merge(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)
        base_sha = _head_sha(tmp_git_repo)

        # Create a candidate commit on a side branch
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_git_repo, capture_output=True, check=True)
        candidate_sha = _commit_file(tmp_git_repo, "feature.py", "print('feature')\n", "add feature")
        subprocess.run(["git", "checkout", branch], cwd=tmp_git_repo, capture_output=True, check=True)

        success, new_sha = merge_candidate(tmp_git_repo, candidate_sha, branch)

        assert success is True
        assert new_sha != ""
        assert new_sha != base_sha
        assert _current_branch(tmp_git_repo) == branch

    def test_merge_conflict_returns_false(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)

        # Create conflicting changes
        subprocess.run(["git", "checkout", "-b", "conflict-branch"], cwd=tmp_git_repo, capture_output=True, check=True)
        _commit_file(tmp_git_repo, "README.md", "conflict version A\n", "conflict A")
        conflict_sha = _head_sha(tmp_git_repo)

        subprocess.run(["git", "checkout", branch], cwd=tmp_git_repo, capture_output=True, check=True)
        _commit_file(tmp_git_repo, "README.md", "conflict version B\n", "conflict B")

        success, new_sha = merge_candidate(tmp_git_repo, conflict_sha, branch)

        assert success is False
        assert new_sha == ""
        # Should be back on default branch, cleanly
        assert _current_branch(tmp_git_repo) == branch

    def test_temp_branch_cleaned_up_after_success(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)

        subprocess.run(["git", "checkout", "-b", "feat2"], cwd=tmp_git_repo, capture_output=True, check=True)
        candidate_sha = _commit_file(tmp_git_repo, "feat2.py", "pass\n", "add feat2")
        subprocess.run(["git", "checkout", branch], cwd=tmp_git_repo, capture_output=True, check=True)

        merge_candidate(tmp_git_repo, candidate_sha, branch)

        # No otto/_merge_temp_ branches should remain
        result = subprocess.run(
            ["git", "branch"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        assert "otto/_merge_temp_" not in result.stdout

    def test_temp_branch_cleaned_up_after_conflict(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)

        subprocess.run(["git", "checkout", "-b", "conflict2"], cwd=tmp_git_repo, capture_output=True, check=True)
        _commit_file(tmp_git_repo, "README.md", "A\n", "A")
        conflict_sha = _head_sha(tmp_git_repo)

        subprocess.run(["git", "checkout", branch], cwd=tmp_git_repo, capture_output=True, check=True)
        _commit_file(tmp_git_repo, "README.md", "B\n", "B")

        merge_candidate(tmp_git_repo, conflict_sha, branch)

        result = subprocess.run(
            ["git", "branch"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        assert "otto/_merge_temp_" not in result.stdout

    def test_invalid_candidate_ref_returns_false(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)
        success, new_sha = merge_candidate(tmp_git_repo, "deadbeef00000000", branch)
        assert success is False
        assert new_sha == ""


# ── _abort_merge_and_cleanup ────────────────────────────────────────────


class TestAbortMergeAndCleanup:
    def test_cleans_up_merge_state(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)

        # Set up a merge conflict manually
        subprocess.run(["git", "checkout", "-b", "conflict-src"], cwd=tmp_git_repo, capture_output=True, check=True)
        _commit_file(tmp_git_repo, "README.md", "version A\n", "A")
        subprocess.run(["git", "checkout", branch], cwd=tmp_git_repo, capture_output=True, check=True)
        _commit_file(tmp_git_repo, "README.md", "version B\n", "B")

        # Create temp branch and trigger conflict
        temp_branch = "otto/_test_temp"
        subprocess.run(["git", "checkout", "-b", temp_branch], cwd=tmp_git_repo, capture_output=True, check=True)

        conflict_sha = subprocess.run(
            ["git", "rev-parse", "conflict-src"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        # This merge will fail with conflict
        subprocess.run(
            ["git", "merge", "--no-edit", conflict_sha],
            cwd=tmp_git_repo, capture_output=True,
        )

        # MERGE_HEAD should exist now
        merge_head_path = tmp_git_repo / ".git" / "MERGE_HEAD"
        assert merge_head_path.exists()

        _abort_merge_and_cleanup(tmp_git_repo, branch, temp_branch)

        # Postconditions: on default branch, no MERGE_HEAD, temp branch gone
        assert _current_branch(tmp_git_repo) == branch
        assert not merge_head_path.exists()

        branches = subprocess.run(
            ["git", "branch"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert temp_branch not in branches

    def test_returns_to_default_branch(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)
        temp_branch = "otto/_test_cleanup"
        subprocess.run(["git", "checkout", "-b", temp_branch], cwd=tmp_git_repo, capture_output=True, check=True)

        _abort_merge_and_cleanup(tmp_git_repo, branch, temp_branch)

        assert _current_branch(tmp_git_repo) == branch

    def test_deletes_temp_branch(self, tmp_git_repo):
        branch = _default_branch(tmp_git_repo)
        temp_branch = "otto/_test_delete"
        subprocess.run(["git", "checkout", "-b", temp_branch], cwd=tmp_git_repo, capture_output=True, check=True)
        subprocess.run(["git", "checkout", branch], cwd=tmp_git_repo, capture_output=True, check=True)

        _abort_merge_and_cleanup(tmp_git_repo, branch, temp_branch)

        branches = subprocess.run(
            ["git", "branch"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert temp_branch not in branches


# ── build_candidate_commit ──────────────────────────────────────────────


class TestBuildCandidateCommit:
    def test_creates_candidate_commit_from_staged_changes(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)

        # Make a change (simulate agent work)
        (tmp_git_repo / "app.py").write_text("print('hello')\n")

        candidate_sha = build_candidate_commit(tmp_git_repo, base_sha)

        assert candidate_sha != base_sha
        # The commit should exist
        result = subprocess.run(
            ["git", "cat-file", "-t", candidate_sha],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "commit"

    def test_otto_owned_files_excluded_from_commit(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)

        # Create both user and otto-owned files
        (tmp_git_repo / "app.py").write_text("print('app')\n")
        tasks_file = tmp_git_repo / "tasks.yaml"
        tasks_file.write_text("key: val\n")

        candidate_sha = build_candidate_commit(tmp_git_repo, base_sha)

        # Check what files are in the candidate diff
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", base_sha, candidate_sha],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        changed_files = diff_result.stdout.strip().splitlines()
        assert "app.py" in changed_files
        assert "tasks.yaml" not in changed_files

    def test_squashes_agent_commits(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)

        # Simulate agent making multiple commits
        _commit_file(tmp_git_repo, "file1.py", "one\n", "agent commit 1")
        _commit_file(tmp_git_repo, "file2.py", "two\n", "agent commit 2")

        candidate_sha = build_candidate_commit(tmp_git_repo, base_sha)

        # Should be a single commit on top of base
        parent = subprocess.run(
            ["git", "rev-parse", f"{candidate_sha}^"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert parent == base_sha

    def test_stages_new_untracked_source_files(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)

        (tmp_git_repo / "new_module.py").write_text("# new\n")

        candidate_sha = build_candidate_commit(tmp_git_repo, base_sha)

        diff_result = subprocess.run(
            ["git", "diff", "--name-only", base_sha, candidate_sha],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        assert "new_module.py" in diff_result.stdout

    def test_excludes_node_modules_from_commit(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)

        nm_dir = tmp_git_repo / "node_modules" / "foo"
        nm_dir.mkdir(parents=True)
        (nm_dir / "index.js").write_text("module.exports = {}\n")
        (tmp_git_repo / "app.js").write_text("const foo = require('./foo')\n")

        candidate_sha = build_candidate_commit(tmp_git_repo, base_sha)

        diff_result = subprocess.run(
            ["git", "diff", "--name-only", base_sha, candidate_sha],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        changed = diff_result.stdout.strip()
        assert "app.js" in changed
        assert "node_modules" not in changed

    def test_excludes_venv_from_commit(self, tmp_git_repo):
        base_sha = _head_sha(tmp_git_repo)

        venv_dir = tmp_git_repo / ".venv" / "bin"
        venv_dir.mkdir(parents=True)
        (venv_dir / "python").write_text("#!/usr/bin/env python3\n")
        (tmp_git_repo / "main.py").write_text("print('main')\n")

        candidate_sha = build_candidate_commit(tmp_git_repo, base_sha)

        diff_result = subprocess.run(
            ["git", "diff", "--name-only", base_sha, candidate_sha],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        changed = diff_result.stdout.strip()
        assert "main.py" in changed
        assert ".venv" not in changed
