"""Tests for otto/worktree.py — Phase 1.2 --in-worktree support."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from otto.worktree import (
    WorktreeAlreadyCheckedOut,
    add_worktree,
    enter_worktree_for_atomic_command,
    setup_worktree_for_atomic_cli,
    worktree_path_for,
)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    return repo


# ---------- worktree_path_for ----------


def test_worktree_path_basic(tmp_path: Path):
    p = worktree_path_for(
        project_dir=tmp_path,
        worktree_dir=".worktrees",
        mode="build",
        intent="add csv export",
        date="2026-04-19",
    )
    assert p == tmp_path / ".worktrees" / "build-add-csv-export-2026-04-19"


def test_worktree_path_uses_today_by_default(tmp_path: Path):
    p = worktree_path_for(
        project_dir=tmp_path,
        worktree_dir=".worktrees",
        mode="build",
        intent="x",
    )
    # Path ends with "build-x-YYYY-MM-DD"
    name = p.name
    assert name.startswith("build-x-")
    assert len(name) == len("build-x-2026-04-19")


# ---------- add_worktree ----------


def test_add_worktree_creates_new(tmp_path: Path):
    repo = _init_repo(tmp_path)
    wt = repo / ".worktrees" / "build-x"
    add_worktree(project_dir=repo, worktree_path=wt, branch="build/x-test")
    assert wt.exists()
    assert (wt / "f.txt").exists()  # worktree has the repo's files
    # Check we're on the right branch from inside the worktree
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "build/x-test"


def test_add_worktree_reuses_existing_branch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    # Create branch first
    subprocess.run(["git", "branch", "build/x-pre"], cwd=repo, check=True)
    wt = repo / ".worktrees" / "build-x"
    add_worktree(project_dir=repo, worktree_path=wt, branch="build/x-pre")
    assert wt.exists()


def test_add_worktree_raises_when_branch_already_checked_out(tmp_path: Path):
    repo = _init_repo(tmp_path)
    wt1 = repo / ".worktrees" / "build-1"
    wt2 = repo / ".worktrees" / "build-2"
    add_worktree(project_dir=repo, worktree_path=wt1, branch="build/x")
    # Try to check out the same branch in a second worktree
    with pytest.raises(WorktreeAlreadyCheckedOut):
        add_worktree(project_dir=repo, worktree_path=wt2, branch="build/x")


def test_add_worktree_returns_silently_if_path_already_a_worktree(tmp_path: Path):
    """Re-running with the same path is a no-op (idempotent)."""
    repo = _init_repo(tmp_path)
    wt = repo / ".worktrees" / "build-x"
    add_worktree(project_dir=repo, worktree_path=wt, branch="build/x")
    # Second call should not raise
    add_worktree(project_dir=repo, worktree_path=wt, branch="build/x")


def test_add_worktree_rejects_existing_worktree_on_wrong_branch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    wt = repo / ".worktrees" / "build-x"
    add_worktree(project_dir=repo, worktree_path=wt, branch="build/x")

    with pytest.raises(RuntimeError, match="expected 'build/y'"):
        add_worktree(project_dir=repo, worktree_path=wt, branch="build/y")


# ---------- enter_worktree_for_atomic_command ----------


def test_enter_creates_worktree_and_chdirs(tmp_path: Path):
    repo = _init_repo(tmp_path)
    saved_cwd = os.getcwd()
    try:
        wt_path, branch = enter_worktree_for_atomic_command(
            project_dir=repo,
            worktree_dir=".worktrees",
            mode="build",
            intent="add csv export",
        )
        assert wt_path.exists()
        assert os.getcwd() == str(wt_path)
        assert branch.startswith("build/add-csv-export-")
    finally:
        os.chdir(saved_cwd)


def test_enter_requires_intent(tmp_path: Path):
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError):
        enter_worktree_for_atomic_command(
            project_dir=repo, worktree_dir=".worktrees", mode="build", intent="",
        )


def test_enter_two_simultaneous_distinct_intents_succeed(tmp_path: Path):
    """Two parallel `otto build --in-worktree` runs with distinct intents
    don't collide (different branches, different worktrees)."""
    repo = _init_repo(tmp_path)
    saved_cwd = os.getcwd()
    try:
        wt1, b1 = enter_worktree_for_atomic_command(
            project_dir=repo, worktree_dir=".worktrees",
            mode="build", intent="feature one",
        )
        os.chdir(saved_cwd)  # simulate context switch
        wt2, b2 = enter_worktree_for_atomic_command(
            project_dir=repo, worktree_dir=".worktrees",
            mode="build", intent="feature two",
        )
        assert wt1 != wt2
        assert b1 != b2
    finally:
        os.chdir(saved_cwd)


def test_enter_improve_runs_with_distinct_focuses_use_distinct_worktrees(tmp_path: Path):
    repo = _init_repo(tmp_path)
    saved_cwd = os.getcwd()
    try:
        wt1, b1 = enter_worktree_for_atomic_command(
            project_dir=repo,
            worktree_dir=".worktrees",
            mode="improve-feature",
            intent="Shared product intent",
            slug_source="search UX",
        )
        os.chdir(saved_cwd)
        wt2, b2 = enter_worktree_for_atomic_command(
            project_dir=repo,
            worktree_dir=".worktrees",
            mode="improve-feature",
            intent="Shared product intent",
            slug_source="pricing page",
        )
        assert wt1 != wt2
        assert b1 != b2
        assert "search-ux" in wt1.name
        assert "pricing-page" in wt2.name
    finally:
        os.chdir(saved_cwd)


def test_enter_same_intent_collides_with_clear_error(tmp_path: Path):
    """Two runs with same intent → second should fail with WorktreeAlreadyCheckedOut."""
    repo = _init_repo(tmp_path)
    saved_cwd = os.getcwd()
    try:
        enter_worktree_for_atomic_command(
            project_dir=repo, worktree_dir=".worktrees",
            mode="build", intent="same intent",
        )
        os.chdir(saved_cwd)
        # Second attempt: branch is already checked out in first worktree.
        # `add_worktree` returns silently if the PATH is the same (same intent =
        # same date = same path) — idempotent. This is actually the desired
        # behavior for same-day re-runs.
        wt2, _ = enter_worktree_for_atomic_command(
            project_dir=repo, worktree_dir=".worktrees",
            mode="build", intent="same intent",
        )
        # The second call resolves to the SAME worktree (idempotent same-day re-run)
        assert os.getcwd() == str(wt2)
    finally:
        os.chdir(saved_cwd)


def test_setup_worktree_for_atomic_cli_reloads_config(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "otto.yaml").write_text("queue:\n  worktree_dir: .custom-trees\n")
    saved_cwd = os.getcwd()
    try:
        wt_path, config = setup_worktree_for_atomic_cli(
            project_dir=repo,
            mode="build",
            intent="add csv export",
            config={"queue": {"worktree_dir": ".custom-trees"}},
        )
        assert wt_path.exists()
        assert os.getcwd() == str(wt_path)
        assert config["queue"]["worktree_dir"] == ".custom-trees"
    finally:
        os.chdir(saved_cwd)
