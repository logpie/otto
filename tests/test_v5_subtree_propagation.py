"""Regression: when a decomposed child's integration Lead returns pass/partial,
its integration branch must merge UP to the parent's integration branch.

Source bug: chat-platform decomp shipped a broken product because the web
client's source code stayed on i2p/integ/<web_id> and never reached main.
Root claimed verdict=pass anyway. This is a correctness defect, not polish.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from otto.v5_branching import (
    child_branch_name,
    integration_branch_name,
    merge_branch_into,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\nnode_modules/\n")
    (repo / "README.md").write_text("init\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def test_subtree_integration_merges_up_to_parent_branch(tmp_path: Path) -> None:
    """Simulate: parent task's integ branch has subtree work; merge it to main."""
    repo = tmp_path / "r"
    _init_repo(repo)

    # Parent task = "v5-web". Its integration branch = i2p/integ/v5-web.
    # Set it up off main with grandchild work merged in.
    parent_id = "v5-web"
    parent_integ = integration_branch_name(parent_id)  # "i2p/integ/v5-web"

    _git(repo, "branch", parent_integ, "main")
    _git(repo, "checkout", "-q", parent_integ)
    (repo / "web").mkdir()
    (repo / "web" / "App.tsx").write_text("export default function App(){}\n")
    (repo / "web" / "package.json").write_text('{"name":"web"}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(web): scaffold + login + chat view")

    # main has API/WS/CLI work merged from sibling top-level tasks.
    _git(repo, "checkout", "-q", "main")
    (repo / "api").mkdir()
    (repo / "api" / "main.py").write_text("# api server\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(api): rest endpoints")

    # Now propagate web's subtree integration UP to main.
    ok, detail = merge_branch_into(
        project_dir=repo,
        source_branch=parent_integ,
        target_branch="main",
    )
    assert ok, f"propagation should succeed: {detail}"

    # Verify both web AND api landed on main.
    _git(repo, "checkout", "-q", "main")
    assert (repo / "web" / "App.tsx").exists(), "web subtree work missing on main!"
    assert (repo / "api" / "main.py").exists(), "api work missing on main!"


def test_propagation_blocks_on_real_source_conflict(tmp_path: Path) -> None:
    """If a sibling top-level task already touched the same file, the
    propagation surfaces a real conflict."""
    repo = tmp_path / "r"
    _init_repo(repo)

    parent_id = "v5-web"
    parent_integ = integration_branch_name(parent_id)
    _git(repo, "branch", parent_integ, "main")

    # Web subtree commits a shared App.tsx
    _git(repo, "checkout", "-q", parent_integ)
    (repo / "App.tsx").write_text("// web version\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "web App.tsx")

    # API task on main also writes App.tsx (unusual but exercises conflict).
    _git(repo, "checkout", "-q", "main")
    (repo / "App.tsx").write_text("// api version\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "api App.tsx")

    ok, detail = merge_branch_into(
        project_dir=repo,
        source_branch=parent_integ,
        target_branch="main",
    )
    assert not ok
    assert "App.tsx" in detail


def test_merge_child_into_integration_still_works(tmp_path: Path) -> None:
    """The thin wrapper (build branch → parent integ) keeps its old behaviour
    after the refactor that introduced merge_branch_into.
    """
    from otto.v5_branching import merge_child_into_integration

    repo = tmp_path / "r"
    _init_repo(repo)

    integ = "i2p/integ/parent"
    _git(repo, "branch", integ, "main")

    child_id = "v5-feat"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child_id), "main")
    (repo / "feature.txt").write_text("A\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "feat A")

    ok, detail = merge_child_into_integration(
        project_dir=repo,
        child_task_id=child_id,
        parent_integration_branch=integ,
    )
    assert ok, detail
    _git(repo, "checkout", "-q", integ)
    assert (repo / "feature.txt").read_text() == "A\n"
