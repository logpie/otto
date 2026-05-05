"""Shared test helpers for otto's pytest suite.

These are plain functions (not pytest fixtures) so call sites stay
explicit: `init_repo(tmp_path, ...)` reads more naturally than injecting
a `init_repo` factory fixture into every test signature.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def init_repo(
    tmp_path: Path,
    *,
    subdir: str | None = "repo",
    commit_file: str = "f.txt",
    commit_content: str = "x",
    commit_msg: str = "i",
    initial_commit: bool = True,
) -> Path:
    """Create a tiny git repo on `main` with one commit. Returns the repo path.

    Defaults match the most-common pattern across tests/. Override for
    variants:
    - `subdir=None` to init in `tmp_path` directly (instead of `tmp_path/"repo"`)
    - `initial_commit=False` for tests that need an empty repo
    - `commit_content`/`commit_msg` for tests that assert on the exact content
    """
    repo = tmp_path / subdir if subdir else tmp_path
    if not repo.exists():
        repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo,
        check=True,
    )
    if initial_commit:
        (repo / commit_file).write_text(commit_content)
        subprocess.run(
            ["git", "add", commit_file],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", commit_msg],
            cwd=repo,
            check=True,
        )
    return repo
