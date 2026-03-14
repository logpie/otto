"""Tests for otto.verify module."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from otto.verify import (
    TierResult,
    VerifyResult,
    run_tier1,
    run_tier2,
    run_tier3,
    run_verification,
)


class TestRunTier1:
    def test_passes_when_tests_pass(self, tmp_git_repo):
        # Create a passing test
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_basic.py").write_text("def test_ok(): assert True\n")
        result = run_tier1(tmp_git_repo, "pytest", timeout=60)
        assert result.passed

    def test_fails_when_tests_fail(self, tmp_git_repo):
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_basic.py").write_text("def test_bad(): assert False\n")
        result = run_tier1(tmp_git_repo, "pytest", timeout=60)
        assert not result.passed
        assert result.output  # Should capture error output

    def test_skips_when_no_command(self, tmp_git_repo):
        result = run_tier1(tmp_git_repo, None, timeout=60)
        assert result.passed  # Skip = not a failure
        assert result.skipped


class TestRunTier2:
    def test_passes_with_passing_test(self, tmp_git_repo):
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_otto_test123.py"
        test_file.write_text("def test_ok(): assert True\n")
        result = run_tier2(tmp_git_repo, test_file, "pytest", timeout=60)
        assert result.passed

    def test_fails_with_failing_test(self, tmp_git_repo):
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_otto_test123.py"
        test_file.write_text("def test_bad(): assert False\n")
        result = run_tier2(tmp_git_repo, test_file, "pytest", timeout=60)
        assert not result.passed
        assert result.output

    def test_skips_when_no_file(self, tmp_git_repo):
        result = run_tier2(tmp_git_repo, None, "pytest", timeout=60)
        assert result.passed
        assert result.skipped


class TestRunTier3:
    def test_passes_on_exit_zero(self, tmp_git_repo):
        result = run_tier3(tmp_git_repo, "true", timeout=60)
        assert result.passed

    def test_fails_on_nonzero_exit(self, tmp_git_repo):
        result = run_tier3(tmp_git_repo, "false", timeout=60)
        assert not result.passed

    def test_fails_on_timeout(self, tmp_git_repo):
        result = run_tier3(tmp_git_repo, "sleep 10", timeout=1)
        assert not result.passed
        assert "timeout" in result.output.lower()


class TestRunVerification:
    def _make_commit(self, repo):
        """Helper: create a commit and return its SHA."""
        (repo / "hello.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True,
                        capture_output=True)
        subprocess.run(["git", "commit", "-m", "add hello"],
                        cwd=repo, check=True, capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_creates_and_cleans_up_worktree(self, tmp_git_repo):
        """Verify that a disposable worktree is created and removed."""
        head = self._make_commit(tmp_git_repo)
        result = run_verification(
            project_dir=tmp_git_repo,
            candidate_sha=head,
            test_command=None,
            verify_cmd=None,
            timeout=60,
        )
        assert result.passed
        # Worktree should be cleaned up
        wt_list = subprocess.run(
            ["git", "worktree", "list"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        ).stdout
        assert "otto-verify-" not in wt_list

    def test_first_failure_stops_chain(self, tmp_git_repo):
        """Tier 1 failure should prevent Tier 2 and Tier 3 from running."""
        head = self._make_commit(tmp_git_repo)
        # Create a failing test in the committed tree
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_fail.py").write_text("def test_bad(): assert False\n")
        subprocess.run(["git", "add", "tests/test_fail.py"], cwd=tmp_git_repo,
                        check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add failing test"],
                        cwd=tmp_git_repo, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        result = run_verification(
            project_dir=tmp_git_repo,
            candidate_sha=head,
            test_command="pytest",
            verify_cmd="echo should_not_run",
            timeout=60,
        )
        assert not result.passed
        # Only Tier 1 should have run
        assert len(result.tiers) == 1
        assert result.tiers[0].tier == "existing_tests"
        assert result.failure_output  # Should have meaningful content
