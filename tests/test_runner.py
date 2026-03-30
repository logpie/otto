"""Tests for otto.runner module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from otto.runner import (
    _audit_proof_sufficiency,
    _build_qa_retry_error,
    _restore_workspace_state,
    check_clean_tree,
    build_candidate_commit,
)


def _commit_all(repo: Path, message: str = "test commit") -> None:
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)


class TestCheckCleanTree:
    def test_clean_repo_passes(self, tmp_git_repo):
        assert check_clean_tree(tmp_git_repo) is True

    def test_dirty_repo_refuses(self, tmp_git_repo):
        """Dirty tracked user files cause check to return False (no stash)."""
        (tmp_git_repo / "dirty.txt").write_text("dirty")
        subprocess.run(["git", "add", "dirty.txt"], cwd=tmp_git_repo, capture_output=True)
        assert check_clean_tree(tmp_git_repo) is False

    def test_otto_owned_files_ignored(self, tmp_git_repo):
        """Changes to otto_logs/ don't block runs."""
        # Create and track an otto_logs file, then modify it
        otto_dir = tmp_git_repo / "otto_logs"
        otto_dir.mkdir()
        (otto_dir / "test.log").write_text("original")
        subprocess.run(["git", "add", "otto_logs/"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add logs"], cwd=tmp_git_repo, capture_output=True)
        (otto_dir / "test.log").write_text("modified")
        assert check_clean_tree(tmp_git_repo) is True

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

    def test_dirty_tracked_user_file_refuses(self, tmp_git_repo):
        """Modified tracked user files cause check to return False."""
        (tmp_git_repo / "real.py").write_text("x = 1")
        subprocess.run(["git", "add", "real.py"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add real"], cwd=tmp_git_repo, capture_output=True)
        (tmp_git_repo / "real.py").write_text("x = 2")
        assert check_clean_tree(tmp_git_repo) is False


class TestBuildCandidateCommit:
    def test_creates_candidate_with_changes(self, tmp_git_repo):
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        # Simulate agent changes
        (tmp_git_repo / "new_file.py").write_text("print('hello')\n")
        candidate = build_candidate_commit(tmp_git_repo, base_sha)
        assert candidate != base_sha
        assert len(candidate) == 40


class TestWorkspaceCleanup:
    @patch("otto.git_ops._log_warn")
    @patch("otto.git_ops.subprocess.run")
    def test_restore_workspace_logs_warning_on_git_reset_failure(self, mock_run, mock_warn, tmp_git_repo):
        mock_run.return_value = MagicMock(returncode=1, stderr="reset failed", stdout="")

        _restore_workspace_state(tmp_git_repo)

        mock_warn.assert_called_once()
        assert "git reset --hard" in mock_warn.call_args.args[0]


class TestQaProofHelpers:
    def test_builds_retry_error_with_proof_gap_context(self):
        message = _build_qa_retry_error([
            {
                "criterion": "API returns JSON",
                "status": "fail",
                "evidence": "response was plain text",
                "proof": [],
            },
            {
                "criterion": "Rate limiting works",
                "status": "fail",
                "evidence": "11th request returned 200",
                "proof": ["curl loop returned 200 on request 11"],
            },
        ], "QA FAIL")

        assert "QA did not record proof" in message  # no-proof item
        assert "why it failed: response was plain text" in message  # evidence shown
        assert "why it failed: 11th request returned 200" in message  # evidence for item with proof

    @patch("otto.runner._log_warn")
    def test_audits_missing_proof_and_visual_screenshot_without_blocking(self, mock_warn, tmp_path):
        proofs_dir = tmp_path / "qa-proofs"
        proofs_dir.mkdir(parents=True)
        emit_events = []
        verdict = {
            "must_items": [
                {"criterion": "API returns JSON", "status": "pass", "proof": []},
                {"criterion": "Layout matches mock", "status": "pass", "proof": ["checked in browser"]},
            ]
        }
        qa_spec = [
            {"text": "API returns JSON", "binding": "must", "verifiable": True},
            {"text": "Layout matches mock", "binding": "must", "verifiable": False},
        ]

        warnings = _audit_proof_sufficiency(
            verdict,
            qa_spec,
            proofs_dir,
            lambda event, **data: emit_events.append((event, data)),
        )

        report = (proofs_dir / "proof-report.md").read_text()
        assert len(warnings) == 2
        assert "Passed [must] missing proof: API returns JSON" in report
        assert "Passed [must ◈] missing screenshot in qa-proofs/: Layout matches mock" in report
        assert mock_warn.call_count == 2
        assert ("qa_finding", {"text": "[warning] Passed [must] missing proof: API returns JSON"}) in emit_events


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


def _commit_file(repo, name, content, msg="add file"):
    """Helper: write, stage, commit a file."""
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, capture_output=True, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()



class TestShouldStageUntracked:
    """Staging policy: project source files in, otto runtime and artifacts out."""

    def test_stages_source_files(self):
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked("src/components/NewPanel.tsx") is True
        assert _should_stage_untracked("lib/utils.py") is True
        assert _should_stage_untracked("README.md") is True
        assert _should_stage_untracked("src/logo.png") is True

    def test_excludes_otto_runtime(self):
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked("otto_logs/abc123/agent.log") is False
        assert _should_stage_untracked("otto_arch/file-plan.md") is False
        assert _should_stage_untracked("tasks.yaml") is False
        assert _should_stage_untracked(".tasks.lock") is False

    def test_excludes_build_artifacts(self):
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked("__pycache__/module.cpython-311.pyc") is False
        assert _should_stage_untracked("node_modules/lodash/index.js") is False
        assert _should_stage_untracked(".next/server/app.js") is False
        assert _should_stage_untracked("dist/bundle.js") is False
        assert _should_stage_untracked(".pytest_cache/v/cache/nodeids") is False

    def test_excludes_compiled_files(self):
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked("module.pyc") is False
        assert _should_stage_untracked("lib.so") is False

    def test_excludes_otto_runtime(self):
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked(".otto-worktrees/otto-task-abc/file.py") is False
        assert _should_stage_untracked("otto_logs/task/agent.log") is False
        assert _should_stage_untracked("tasks.yaml") is False

    def test_stages_test_files(self):
        """Test files are staged — prompt guidance handles test hygiene, not the gate."""
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked("__tests__/myFeature.test.tsx") is True
        assert _should_stage_untracked("tests/test_calculator.py") is True
        assert _should_stage_untracked("src/utils.test.ts") is True


class TestTaskWorktrees:
    """Tests for parallel execution worktree lifecycle (git_ops.py)."""

    def test_create_and_cleanup_worktree(self, tmp_git_repo):
        """Create a task worktree, verify it exists, then clean it up."""
        from otto.git_ops import create_task_worktree, cleanup_task_worktree

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        wt_path = create_task_worktree(tmp_git_repo, "test-key", base_sha)
        assert wt_path.exists()
        assert wt_path.is_dir()
        # Worktree should have the README from the base commit
        assert (wt_path / "README.md").exists()
        # Worktree is in detached HEAD
        head_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt_path, capture_output=True, text=True,
        ).stdout.strip()
        assert head_ref == base_sha

        cleanup_task_worktree(tmp_git_repo, "test-key")
        assert not wt_path.exists()

    def test_create_worktree_at_specific_sha(self, tmp_git_repo):
        """Worktree should be at the specified base SHA, not necessarily HEAD."""
        from otto.git_ops import create_task_worktree, cleanup_task_worktree

        # Get the first commit SHA
        first_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        # Make another commit
        (tmp_git_repo / "extra.txt").write_text("extra content")
        subprocess.run(["git", "add", "extra.txt"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add extra"], cwd=tmp_git_repo, capture_output=True)

        # Create worktree at first SHA — should NOT have extra.txt
        wt_path = create_task_worktree(tmp_git_repo, "old-sha-key", first_sha)
        assert wt_path.exists()
        assert not (wt_path / "extra.txt").exists()
        assert (wt_path / "README.md").exists()

        cleanup_task_worktree(tmp_git_repo, "old-sha-key")

    def test_multiple_worktrees_isolated(self, tmp_git_repo):
        """Multiple worktrees should be independent of each other."""
        from otto.git_ops import create_task_worktree, cleanup_task_worktree

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        wt1 = create_task_worktree(tmp_git_repo, "key-1", base_sha)
        wt2 = create_task_worktree(tmp_git_repo, "key-2", base_sha)

        assert wt1.exists() and wt2.exists()
        assert wt1 != wt2

        # Write a file in wt1, verify it doesn't appear in wt2
        (wt1 / "only_in_wt1.txt").write_text("isolated")
        assert not (wt2 / "only_in_wt1.txt").exists()

        cleanup_task_worktree(tmp_git_repo, "key-1")
        cleanup_task_worktree(tmp_git_repo, "key-2")
        assert not wt1.exists()
        assert not wt2.exists()

    def test_cleanup_all_worktrees(self, tmp_git_repo):
        """cleanup_all_worktrees should remove all otto task worktrees."""
        from otto.git_ops import create_task_worktree, cleanup_all_worktrees

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        wt1 = create_task_worktree(tmp_git_repo, "all-1", base_sha)
        wt2 = create_task_worktree(tmp_git_repo, "all-2", base_sha)
        assert wt1.exists() and wt2.exists()

        cleanup_all_worktrees(tmp_git_repo)
        assert not wt1.exists()
        assert not wt2.exists()

    def test_cleanup_all_no_worktrees_is_noop(self, tmp_git_repo):
        """cleanup_all_worktrees when no worktrees exist should not raise."""
        from otto.git_ops import cleanup_all_worktrees
        cleanup_all_worktrees(tmp_git_repo)
        # Should not raise

    def test_create_worktree_replaces_stale(self, tmp_git_repo):
        """Creating a worktree that already exists should replace it."""
        from otto.git_ops import create_task_worktree, cleanup_task_worktree

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        wt1 = create_task_worktree(tmp_git_repo, "stale-key", base_sha)
        (wt1 / "stale_file.txt").write_text("stale")

        # Create again — should replace
        wt2 = create_task_worktree(tmp_git_repo, "stale-key", base_sha)
        assert wt2.exists()
        # Stale file should be gone (fresh worktree)
        assert not (wt2 / "stale_file.txt").exists()

        cleanup_task_worktree(tmp_git_repo, "stale-key")

    def test_worktrees_in_otto_worktrees_dir(self, tmp_git_repo):
        """Worktrees should be created under .otto-worktrees/."""
        from otto.git_ops import create_task_worktree, cleanup_task_worktree

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()

        wt_path = create_task_worktree(tmp_git_repo, "path-check", base_sha)
        assert ".otto-worktrees" in str(wt_path)
        assert "otto-task-path-check" in wt_path.name

        cleanup_task_worktree(tmp_git_repo, "path-check")
