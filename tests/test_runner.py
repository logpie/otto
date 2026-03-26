"""Tests for otto.runner module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from otto.runner import (
    _audit_proof_sufficiency,
    _build_qa_retry_error,
    _run_durable_regression,
    _restore_workspace_state,
    check_clean_tree,
    create_task_branch,
    build_candidate_commit,
    merge_to_default,
    cleanup_branch,
    rebase_and_merge,
)


def _commit_all(repo: Path, message: str = "test commit") -> None:
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)


class TestCheckCleanTree:
    def test_clean_repo_passes(self, tmp_git_repo):
        assert check_clean_tree(tmp_git_repo) is True

    def test_dirty_repo_auto_stashes(self, tmp_git_repo):
        """Dirty tracked files get auto-stashed, returning True."""
        (tmp_git_repo / "dirty.txt").write_text("dirty")
        subprocess.run(["git", "add", "dirty.txt"], cwd=tmp_git_repo, capture_output=True)
        assert check_clean_tree(tmp_git_repo) is True
        # Verify stash was created
        stash = subprocess.run(
            ["git", "stash", "list"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        )
        assert "otto: auto-stash" in stash.stdout

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

    def test_dirty_tracked_file_auto_stashes(self, tmp_git_repo):
        """Modified tracked files get auto-stashed, returning True."""
        (tmp_git_repo / "real.py").write_text("x = 1")
        subprocess.run(["git", "add", "real.py"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add real"], cwd=tmp_git_repo, capture_output=True)
        (tmp_git_repo / "real.py").write_text("x = 2")
        assert check_clean_tree(tmp_git_repo) is True
        stash = subprocess.run(
            ["git", "stash", "list"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        )
        assert "otto: auto-stash" in stash.stdout


class TestCreateTaskBranch:
    def test_creates_branch(self, tmp_git_repo):
        base_sha = create_task_branch(tmp_git_repo, "abc123def456", "main")
        # Verify we're on the new branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "otto/abc123def456"
        assert len(base_sha) == 40  # full SHA

    def test_recreates_stale_branch(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        # Should not raise — deletes and recreates
        base_sha = create_task_branch(tmp_git_repo, "abc123def456", "main")
        assert len(base_sha) == 40


class TestBuildCandidateCommit:
    def test_creates_candidate_with_changes(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        # Simulate agent changes
        (tmp_git_repo / "new_file.py").write_text("print('hello')\n")
        candidate = build_candidate_commit(tmp_git_repo, base_sha)
        assert candidate != base_sha
        assert len(candidate) == 40


class TestMergeToDefault:
    def test_fast_forward_merge(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        (tmp_git_repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "otto: add feature (#1)"],
            cwd=tmp_git_repo, capture_output=True,
        )
        success = merge_to_default(tmp_git_repo, "abc123def456", "main")
        assert success
        # Should be on main now
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "main"


class TestCleanupBranch:
    def test_deletes_branch(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        cleanup_branch(tmp_git_repo, "abc123def456", "main")
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/abc123def456" not in branches


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

        assert "proof gap: QA did not record proof" in message
        assert "QA tested this and it failed" in message
        assert "curl loop returned 200 on request 11" in message

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

    def test_runs_durable_regression_script(self, tmp_path):
        log_dir = tmp_path / "logs"
        proofs_dir = log_dir / "qa-proofs"
        proofs_dir.mkdir(parents=True)
        script = proofs_dir / "durable-regression.sh"
        script.write_text("#!/bin/bash\nset -e\n\necho replay\nfalse\n")

        result = _run_durable_regression(tmp_path, log_dir, timeout=5, attempt_num=2)

        assert result is not None
        assert result[0] is False
        assert "replay" in result[1]
        assert (log_dir / "attempt-2-durable-regression.log").exists()


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


class TestRebaseAndMerge:
    def test_simple_rebase_and_merge(self, tmp_git_repo):
        """Task branch rebases onto main and merges ff-only."""
        # Create a commit on main
        _commit_file(tmp_git_repo, "base.txt", "base", "base commit")

        # Create task branch from main
        subprocess.run(["git", "checkout", "-b", "otto/task1"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "task.txt", "task work", "task commit")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)

        # Add another commit on main (diverge)
        _commit_file(tmp_git_repo, "main_new.txt", "main work", "main diverge")

        # Rebase and merge should succeed
        result = rebase_and_merge(tmp_git_repo, "otto/task1", "main")
        assert result is True

        # Verify task file exists on main
        assert (tmp_git_repo / "task.txt").exists()
        assert (tmp_git_repo / "main_new.txt").exists()

        # Verify branch was deleted
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/task1" not in branches

    def test_rebase_conflict_returns_false(self, tmp_git_repo):
        """Conflicting changes should return False."""
        _commit_file(tmp_git_repo, "conflict.txt", "base content", "base")
        subprocess.run(["git", "checkout", "-b", "otto/task2"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "conflict.txt", "task content", "task edit")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "conflict.txt", "main content", "main edit")

        result = rebase_and_merge(tmp_git_repo, "otto/task2", "main")
        assert result is False

        # Branch should still exist (rebase was aborted)
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/task2" in branches

    def test_fast_forward_when_no_diverge(self, tmp_git_repo):
        """No divergence = simple ff merge."""
        subprocess.run(["git", "checkout", "-b", "otto/task3"], cwd=tmp_git_repo, capture_output=True)
        _commit_file(tmp_git_repo, "simple.txt", "simple", "simple commit")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)

        result = rebase_and_merge(tmp_git_repo, "otto/task3", "main")
        assert result is True
        assert (tmp_git_repo / "simple.txt").exists()


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

    def test_excludes_scratch_area(self):
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked(".otto-scratch/test_verify.py") is False
        assert _should_stage_untracked(".otto-scratch/probes/check.sh") is False

    def test_stages_test_files(self):
        """Test files are staged — prompt guidance handles test hygiene, not the gate."""
        from otto.runner import _should_stage_untracked
        assert _should_stage_untracked("__tests__/myFeature.test.tsx") is True
        assert _should_stage_untracked("tests/test_calculator.py") is True
        assert _should_stage_untracked("src/utils.test.ts") is True
