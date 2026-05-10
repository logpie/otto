"""Regression: noise-file conflicts on merge auto-resolve, don't block.

The finance-dashboard live decomp run produced 2 merge_blocked verdicts on
otherwise-passing children because Playwright wrote test-results/results.json
into both branches. The work was good; the artifact was transient. Test that
the merge step recognises this and completes the merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from otto.v5_branching import (
    _is_noise_path,
    child_branch_name,
    merge_child_into_integration,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )


def test_is_noise_path_recognises_common_artifacts() -> None:
    assert _is_noise_path("test-results/results.json")
    assert _is_noise_path("web/test-results/foo.json")
    assert _is_noise_path("playwright-report/index.html")
    assert _is_noise_path("__pycache__/foo.cpython-311.pyc")
    assert _is_noise_path("api/__pycache__/main.cpython-314.pyc")
    assert _is_noise_path("foo.pyc")
    assert _is_noise_path("debug.log")
    assert _is_noise_path("dist/index.html")
    assert _is_noise_path("node_modules/react/package.json")

    # Real source files — not noise.
    assert not _is_noise_path("src/App.tsx")
    assert not _is_noise_path("api/main.py")
    assert not _is_noise_path("tests/test_api.py")
    assert not _is_noise_path("README.md")


def test_merge_auto_resolves_noise_conflict(tmp_path: Path) -> None:
    """End-to-end: two branches commit different versions of test-results/foo.json,
    merge should succeed with auto-resolution."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")

    # Initial commit on main.
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")

    # Set up an integration branch.
    _git(repo, "branch", "main")  # ensure named main
    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "main")

    # Branch A: makes a real change + commits a test-results file.
    child_task = "v5-feature-a"
    branch_a = child_branch_name(child_task)  # i2p/build/v5-feature-a
    _git(repo, "checkout", "-q", "-b", branch_a, "main")
    (repo / "feature-a.txt").write_text("A\n")
    (repo / "test-results").mkdir(exist_ok=True)
    (repo / "test-results" / "results.json").write_text('{"run": "A"}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat A + results")

    # Integration branch already has a different test-results file (e.g. from a
    # sibling that landed first).
    _git(repo, "checkout", "-q", integration)
    (repo / "test-results").mkdir(exist_ok=True)
    (repo / "test-results" / "results.json").write_text('{"run": "earlier"}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "earlier results")

    # Now merge child into integration. Without the noise auto-resolve,
    # this would conflict on test-results/results.json.
    ok, detail = merge_child_into_integration(
        project_dir=repo,
        child_task_id=child_task,
        parent_integration_branch=integration,
    )
    assert ok, f"merge should succeed via noise auto-resolve, got: {detail}"
    assert "auto-resolved noise" in detail, detail

    # The child's real change must have landed.
    assert (repo / "feature-a.txt").read_text() == "A\n"


def test_merge_blocks_on_real_source_conflict(tmp_path: Path) -> None:
    """If the conflict involves real source code, the merge must NOT auto-resolve."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")

    (repo / "src.py").write_text("a = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")

    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "HEAD")

    child = "v5-conflict"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child), "HEAD")
    (repo / "src.py").write_text("a = 2\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "child change")

    _git(repo, "checkout", "-q", integration)
    (repo / "src.py").write_text("a = 3\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "integration change")

    ok, detail = merge_child_into_integration(
        project_dir=repo,
        child_task_id=child,
        parent_integration_branch=integration,
    )
    assert not ok, "real source conflict should block merge"
    assert "src.py" in detail
