"""Regression: noise-file conflicts on merge auto-resolve, don't block.

Approach: the project's own ``.gitignore`` is the source of truth — Otto
delegates to ``git check-ignore``. Whatever the project considers
ignorable is auto-resolvable. No hardcoded list to keep in sync.

Source: finance-dashboard live decomp run had 2 children verified pass
but recorded merge_blocked because Playwright wrote test-results/results.json
into both branches. The work was good; the artifact was transient.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from otto.v5_branching import (
    _gitignored_paths,
    child_branch_name,
    commit_worktree,
    merge_child_into_integration,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )


def _init_repo_with_ignore(repo: Path, ignore_lines: str = "test-results/\n*.pyc\nnode_modules/\n") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(ignore_lines)
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def test_gitignored_paths_uses_repo_gitignore(tmp_path: Path) -> None:
    """Whatever the repo's gitignore says is ignored → returns those paths."""
    repo = tmp_path / "r"
    _init_repo_with_ignore(repo, "test-results/\nbuild/\n*.log\n")
    candidates = [
        "test-results/foo.json",
        "src/test-results-summary.tsx",  # NOT inside test-results/, just similar name
        "build/index.html",
        "src/build-helpers.ts",  # NOT inside build/, just similar name
        "debug.log",
        "src/main.py",
    ]
    ignored = _gitignored_paths(repo, candidates)
    assert "test-results/foo.json" in ignored
    assert "build/index.html" in ignored
    assert "debug.log" in ignored
    # Substring matches must NOT be flagged — these are real source files
    # whose names happen to share a prefix with ignored directories.
    assert "src/test-results-summary.tsx" not in ignored
    assert "src/build-helpers.ts" not in ignored
    assert "src/main.py" not in ignored


def test_gitignored_paths_empty_input(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _init_repo_with_ignore(repo)
    assert _gitignored_paths(repo, []) == set()


def test_merge_auto_resolves_when_gitignore_says_so(tmp_path: Path) -> None:
    """If the project's gitignore matches the conflicting paths, merge succeeds."""
    repo = tmp_path / "r"
    _init_repo_with_ignore(repo, "test-results/\n")

    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "HEAD")

    # Child branch: real change + a gitignored artifact.
    child_task = "v5-feat"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child_task), "HEAD")
    (repo / "feature.txt").write_text("A\n")
    (repo / "test-results").mkdir(exist_ok=True)
    (repo / "test-results" / "results.json").write_text('{"run": "A"}\n')
    # Force-add despite gitignore so the commit has it (simulates the bug
    # where agents push past gitignore via -f or stale ignore state).
    _git(repo, "add", "-f", "test-results/results.json")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feat + results")

    # Integration branch has a different version of the same file.
    _git(repo, "checkout", "-q", integration)
    (repo / "test-results").mkdir(exist_ok=True)
    (repo / "test-results" / "results.json").write_text('{"run": "earlier"}\n')
    _git(repo, "add", "-f", "test-results/results.json")
    _git(repo, "commit", "-q", "-m", "earlier")

    ok, detail = merge_child_into_integration(
        project_dir=repo,
        child_task_id=child_task,
        parent_integration_branch=integration,
    )
    assert ok, f"merge should succeed when gitignore covers the conflict: {detail}"
    assert "noise" in detail
    assert (repo / "feature.txt").read_text() == "A\n"


def test_merge_blocks_on_unignored_source_conflict(tmp_path: Path) -> None:
    """A conflict on a path NOT in gitignore must still block."""
    repo = tmp_path / "r"
    _init_repo_with_ignore(repo, "test-results/\n")

    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "HEAD")

    child = "v5-conflict"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child), "HEAD")
    (repo / "src.py").write_text("a = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "child")

    _git(repo, "checkout", "-q", integration)
    (repo / "src.py").write_text("a = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "integ")

    ok, detail = merge_child_into_integration(
        project_dir=repo,
        child_task_id=child,
        parent_integration_branch=integration,
    )
    assert not ok
    assert "src.py" in detail


def test_commit_worktree_seeds_default_gitignore(tmp_path: Path) -> None:
    """commit_worktree writes the otto-managed default ignore block when missing."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    # No .gitignore initially.
    assert not (repo / ".gitignore").exists()

    (repo / "feature.txt").write_text("x\n")
    ok, _ = commit_worktree(worktree_path=repo, message="add feature")
    assert ok
    text = (repo / ".gitignore").read_text()
    assert "otto v5 default ignores" in text
    assert "__pycache__/" in text
    assert "test-results/" in text


def test_commit_worktree_untracks_now_ignored_files(tmp_path: Path) -> None:
    """If a file was previously committed but the seeded gitignore now matches it,
    commit_worktree untracks it via git rm --cached."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    # Initial: track a file that the seeded .gitignore will later cover.
    (repo / "test-results").mkdir()
    (repo / "test-results" / "results.json").write_text("old\n")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init w/ artifact")

    # Now run commit_worktree — should seed gitignore AND untrack the artifact.
    (repo / "feature.txt").write_text("y\n")
    ok, _ = commit_worktree(worktree_path=repo, message="add feature")
    assert ok

    # The file remains on disk (rm --cached keeps the working tree).
    assert (repo / "test-results" / "results.json").exists()
    # But should no longer be tracked.
    ls = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True,
    ).stdout
    assert "test-results/results.json" not in ls
