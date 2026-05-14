"""Shared fixtures for the 0% LLM v6.5 smoke matrix."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.smoke


def git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and cp.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
        )
    return cp


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main", check=True)
    git(repo, "config", "user.email", "smoke@example.invalid", check=True)
    git(repo, "config", "user.name", "Smoke Test", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n", encoding="utf-8")
    (repo / "README.md").write_text("initial repo\n", encoding="utf-8")
    git(repo, "add", ".gitignore", "README.md", check=True)
    git(repo, "commit", "-q", "-m", "init", check=True)
    return repo

