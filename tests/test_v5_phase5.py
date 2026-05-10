"""Phase 5 tests — real verify wiring, build/test split, branching."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from otto.lead_verify import (
    _detect_browser_runner,
    _detect_test_command,
    _filter_journeys,
    _unverified,
    run_verify_for_lead,
)
from otto.v5_branching import (
    child_branch_name,
    child_worktree_path,
    commit_worktree,
    ensure_branch_exists,
    integration_branch_name,
    merge_child_into_integration,
    setup_child_worktree,
)


# ---------------------------------------------------------------------------
# Test detection
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_project(tmp_path: Path) -> Path:
    (tmp_path / "otto_logs").mkdir()
    return tmp_path


@pytest.fixture
def npm_project(tmp_path: Path) -> Path:
    (tmp_path / "otto_logs").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({
            "name": "test",
            "scripts": {"test": "echo 'ran tests' && exit 0"},
        }),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    (tmp_path / "otto_logs").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    return tmp_path


class TestDetectTestCommand:
    def test_no_runner_returns_none(self, empty_project: Path) -> None:
        assert _detect_test_command(empty_project) is None

    def test_npm_test_detected(self, npm_project: Path) -> None:
        cmd = _detect_test_command(npm_project)
        assert cmd is not None
        assert "npm test" in cmd

    def test_pytest_detected(self, py_project: Path) -> None:
        # Skip if pytest isn't on PATH.
        import shutil
        if not shutil.which("pytest"):
            pytest.skip("pytest not on PATH")
        cmd = _detect_test_command(py_project)
        assert cmd is not None
        assert "pytest" in cmd


class TestDetectBrowserRunner:
    def test_no_runner_returns_none(self, empty_project: Path) -> None:
        assert _detect_browser_runner(empty_project) is None

    def test_python_browser_runner_detected(self, empty_project: Path) -> None:
        runner_path = empty_project / "tests" / "run_browser_journey.py"
        runner_path.parent.mkdir(parents=True, exist_ok=True)
        runner_path.write_text("# stub\n", encoding="utf-8")
        cmd = _detect_browser_runner(empty_project)
        assert cmd is not None
        assert "run_browser_journey.py" in cmd

    def test_npm_browser_script_detected(self, empty_project: Path) -> None:
        (empty_project / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"browser": "playwright test"}}),
            encoding="utf-8",
        )
        cmd = _detect_browser_runner(empty_project)
        assert cmd == "npm run browser"


class TestRunVerifyForLead:
    @pytest.mark.asyncio
    async def test_no_spec_returns_unverified(self, empty_project: Path) -> None:
        # No spec.json under session_dir.
        session_dir = empty_project / "otto_logs" / "sessions" / "s1"
        session_dir.mkdir(parents=True, exist_ok=True)
        result = await run_verify_for_lead(
            task_id="t1",
            project_dir=empty_project,
            session_dir=session_dir,
            feature_scope_ids=[],
        )
        assert result["verdict"] == "unverified"
        assert "spec" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_no_test_runner_returns_unverified(
        self, empty_project: Path
    ) -> None:
        session_dir = empty_project / "otto_logs" / "sessions" / "s1"
        spec_dir = session_dir / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.json").write_text(
            json.dumps({
                "schema_version": 1,
                "intent": "x",
                "behavior_journeys": [{"id": "j1", "description": "user does x"}],
            }),
            encoding="utf-8",
        )
        result = await run_verify_for_lead(
            task_id="t1",
            project_dir=empty_project,
            session_dir=session_dir,
            feature_scope_ids=[],
        )
        # No test runner detected and no browser runner.
        assert result["verdict"] == "unverified"

    @pytest.mark.asyncio
    async def test_npm_passing_test_yields_pass(self, npm_project: Path) -> None:
        session_dir = npm_project / "otto_logs" / "sessions" / "s1"
        spec_dir = session_dir / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.json").write_text(
            json.dumps({
                "schema_version": 1,
                "intent": "x",
                "behavior_journeys": [{"id": "j1", "description": "tests pass"}],
            }),
            encoding="utf-8",
        )
        result = await run_verify_for_lead(
            task_id="t1",
            project_dir=npm_project,
            session_dir=session_dir,
            feature_scope_ids=[],
            timeout_s=30,
        )
        assert result["verdict"] == "pass"
        assert result["test_outcome"]["status"] == "pass"
        assert len(result["journeys"]) == 1
        assert result["journeys"][0]["passed"] is True


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------


class TestBranching:
    def test_integration_branch_for_root(self) -> None:
        assert integration_branch_name(None) == "main"
        assert integration_branch_name("root") == "main"

    def test_integration_branch_for_child(self) -> None:
        assert integration_branch_name("v5-abc123") == "i2p/integ/v5-abc123"

    def test_child_branch_name_sanitizes(self) -> None:
        assert child_branch_name("v5-abc123") == "i2p/build/v5-abc123"
        # Special chars sanitized (the function should not crash).
        result = child_branch_name("v5/abc 123")
        # Sibling-namespace prefix means at most 2 slashes (i2p/build/<id>).
        assert result.count("/") == 2

    def test_integration_and_build_namespaces_dont_collide(self) -> None:
        # i2p/<id>/integration vs i2p/<id> would collide as ref paths;
        # i2p/build/<id> vs i2p/integ/<id> are siblings.
        tid = "v5-foo"
        assert child_branch_name(tid).split("/")[1] != integration_branch_name(tid).split("/")[1]


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    """A minimal git repo with one commit."""
    (tmp_path / "otto_logs").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


class TestEnsureBranchExists:
    def test_creates_branch_idempotent(self, git_project: Path) -> None:
        created = ensure_branch_exists(git_project, "i2p/foo/integration", base_ref="main")
        assert created is True
        # Idempotent: second call returns False (already existed).
        created = ensure_branch_exists(git_project, "i2p/foo/integration", base_ref="main")
        assert created is False

    def test_falls_back_when_base_ref_missing(self, git_project: Path) -> None:
        # Non-existent base ref → falls back to HEAD.
        created = ensure_branch_exists(git_project, "i2p/bar/integration", base_ref="no-such-ref")
        assert created is True


class TestSetupChildWorktree:
    def test_creates_worktree_off_integration_branch(self, git_project: Path) -> None:
        # First create the parent integration branch.
        ensure_branch_exists(git_project, "i2p/parent/integration", base_ref="main")
        wt = setup_child_worktree(
            project_dir=git_project,
            child_task_id="v5-child001",
            parent_integration_branch="i2p/parent/integration",
        )
        assert wt is not None
        assert wt.exists()
        assert (wt / ".git").exists()
        assert (wt / "README.md").exists()


class TestCommitWorktree:
    def test_commits_changes(self, git_project: Path) -> None:
        ensure_branch_exists(git_project, "i2p/parent/integration", base_ref="main")
        wt = setup_child_worktree(
            project_dir=git_project,
            child_task_id="v5-commit001",
            parent_integration_branch="i2p/parent/integration",
        )
        assert wt is not None
        # Make a change.
        (wt / "new_file.txt").write_text("hello\n", encoding="utf-8")
        ok, detail = commit_worktree(worktree_path=wt, message="add new_file")
        assert ok is True
        assert "committed" in detail or "no-op" in detail

    def test_no_op_when_clean(self, git_project: Path) -> None:
        ensure_branch_exists(git_project, "i2p/parent2/integration", base_ref="main")
        wt = setup_child_worktree(
            project_dir=git_project,
            child_task_id="v5-clean001",
            parent_integration_branch="i2p/parent2/integration",
        )
        assert wt is not None
        # First call may commit a seeded .gitignore if one wasn't there;
        # the SECOND call against an unchanged worktree must be a no-op.
        commit_worktree(worktree_path=wt, message="seed")
        ok, detail = commit_worktree(worktree_path=wt, message="empty")
        assert ok is True
        assert "no-op" in detail


class TestMergeChildIntoIntegration:
    def test_clean_merge(self, git_project: Path) -> None:
        # Set up parent integration branch + child worktree.
        ensure_branch_exists(git_project, "i2p/p3/integration", base_ref="main")
        wt = setup_child_worktree(
            project_dir=git_project,
            child_task_id="v5-merge001",
            parent_integration_branch="i2p/p3/integration",
        )
        assert wt is not None
        (wt / "added.txt").write_text("from child\n", encoding="utf-8")
        ok, _detail = commit_worktree(worktree_path=wt, message="add file")
        assert ok is True
        # Merge.
        ok, detail = merge_child_into_integration(
            project_dir=git_project,
            child_task_id="v5-merge001",
            parent_integration_branch="i2p/p3/integration",
        )
        assert ok is True
        assert "merged" in detail
