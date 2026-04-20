"""Tests for otto/merge/git_ops.py — git wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from otto.merge import git_ops


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


def _commit_on_branch(repo: Path, branch: str, file: str, content: str, msg: str):
    subprocess.run(["git", "checkout", "-b", branch], cwd=repo, capture_output=True, check=True)
    (repo / file).write_text(content)
    subprocess.run(["git", "add", "--", file], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)


def test_head_sha_returns_40_hex(tmp_path: Path):
    repo = _init_repo(tmp_path)
    sha = git_ops.head_sha(repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_head_parents_initial_has_zero(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert git_ops.head_parents(repo) == []


def test_current_branch_returns_main(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert git_ops.current_branch(repo) == "main"


def test_branch_exists(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert git_ops.branch_exists(repo, "main") is True
    assert git_ops.branch_exists(repo, "nonexistent") is False


def test_resolve_branch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    sha1 = git_ops.head_sha(repo)
    sha2 = git_ops.resolve_branch(repo, "main")
    assert sha1 == sha2


def test_working_tree_clean(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert git_ops.working_tree_clean(repo) is True
    (repo / "f.txt").write_text("dirty\n")
    assert git_ops.working_tree_clean(repo) is False


def test_clean_merge_no_conflicts(tmp_path: Path):
    """Two branches modifying different files merge cleanly."""
    repo = _init_repo(tmp_path)
    _commit_on_branch(repo, "feat-a", "a.txt", "A\n", "add a")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    _commit_on_branch(repo, "feat-b", "b.txt", "B\n", "add b")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    r = git_ops.merge_no_ff(repo, "feat-a", message="merge a")
    assert r.ok
    assert git_ops.is_merge_commit(repo, "HEAD")
    assert git_ops.conflicted_files(repo) == []


def test_conflicted_files_detected(tmp_path: Path):
    """Two branches modifying same line on the same file produce a conflict."""
    repo = _init_repo(tmp_path)
    _commit_on_branch(repo, "feat-a", "f.txt", "A's content\n", "a")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    _commit_on_branch(repo, "feat-b", "f.txt", "B's content\n", "b")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    git_ops.merge_no_ff(repo, "feat-a")  # clean
    r = git_ops.merge_no_ff(repo, "feat-b")
    assert not r.ok
    conflicts = git_ops.conflicted_files(repo)
    assert "f.txt" in conflicts


def test_merge_in_progress_after_conflict(tmp_path: Path):
    """After a conflicted merge, merge_in_progress reports True."""
    repo = _init_repo(tmp_path)
    _commit_on_branch(repo, "feat-a", "f.txt", "A\n", "a")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    _commit_on_branch(repo, "feat-b", "f.txt", "B\n", "b")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    git_ops.merge_no_ff(repo, "feat-a")
    git_ops.merge_no_ff(repo, "feat-b")  # conflicts
    assert git_ops.merge_in_progress(repo) is True
    git_ops.merge_abort(repo)
    assert git_ops.merge_in_progress(repo) is False


def test_diff_check_catches_conflict_markers(tmp_path: Path):
    """git diff --check returns non-zero when markers are present."""
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text(
        "<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> branch\n"
    )
    r = git_ops.diff_check(repo)
    assert not r.ok


def test_files_in_branch_diff(tmp_path: Path):
    """files changed by a branch relative to target."""
    repo = _init_repo(tmp_path)
    _commit_on_branch(repo, "feat-x", "x.txt", "X\n", "x")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    files = git_ops.files_in_branch_diff(repo, "feat-x", "main")
    assert "x.txt" in files


def test_is_merge_commit_after_clean_merge(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit_on_branch(repo, "feat-a", "a.txt", "A\n", "a")
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    git_ops.merge_no_ff(repo, "feat-a", message="merge a")
    assert git_ops.is_merge_commit(repo)
    parents = git_ops.head_parents(repo)
    assert len(parents) == 2
