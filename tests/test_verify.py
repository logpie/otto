"""Tests for otto.verify module."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from otto.verify import (
    TierResult,
    VerifyResult,
    _install_deps,
    run_tier1,
    run_tier3,
    run_integration_gate,
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

    @patch("otto.verify.os.killpg")
    @patch("otto.verify.subprocess.Popen")
    def test_timeout_kills_process_group(self, mock_popen, mock_killpg, tmp_git_repo):
        proc = MagicMock()
        proc.pid = 4321
        proc.poll.return_value = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("pytest", 1),
            subprocess.TimeoutExpired("pytest", 5),
            ("", ""),
        ]
        mock_popen.return_value = proc

        result = run_tier1(tmp_git_repo, "pytest", timeout=1)

        assert not result.passed
        assert "timeout" in result.output.lower()
        assert mock_killpg.call_args_list == [
            call(4321, signal.SIGTERM),
            call(4321, signal.SIGKILL),
        ]



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

    @patch("otto.verify.os.killpg")
    @patch("otto.verify.subprocess.Popen")
    def test_timeout_uses_bash_and_terminates_process_group(self, mock_popen, mock_killpg, tmp_git_repo):
        proc = MagicMock()
        proc.pid = 9876
        proc.poll.return_value = None
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("sleep 10", 1),
            ("", ""),
        ]
        mock_popen.return_value = proc

        result = run_tier3(tmp_git_repo, "sleep 10", timeout=1)

        assert not result.passed
        assert "timeout" in result.output.lower()
        assert mock_popen.call_args.kwargs["executable"] == "/bin/bash"
        assert mock_killpg.call_args_list == [call(9876, signal.SIGTERM)]


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

    @patch("otto.verify.run_tier3")
    @patch("otto.verify.run_tier1")
    @patch("otto.verify._install_deps")
    def test_threads_worktree_venv_env_into_tiers(
        self,
        mock_install_deps,
        mock_run_tier1,
        mock_run_tier3,
        tmp_git_repo,
    ):
        head = self._make_commit(tmp_git_repo)
        mock_install_deps.return_value = "/tmp/worktree/.venv/bin"
        mock_run_tier1.return_value = TierResult(tier="existing_tests", passed=True)
        mock_run_tier3.return_value = TierResult(tier="custom_verify", passed=True)

        result = run_verification(
            project_dir=tmp_git_repo,
            candidate_sha=head,
            test_command="pytest",
            verify_cmd="echo ok",
            timeout=60,
        )

        assert result.passed
        tier1_env = mock_run_tier1.call_args.kwargs["env"]
        tier3_env = mock_run_tier3.call_args.kwargs["env"]
        assert tier1_env["PATH"].split(os.pathsep)[0] == "/tmp/worktree/.venv/bin"
        assert tier3_env["PATH"].split(os.pathsep)[0] == "/tmp/worktree/.venv/bin"


class TestInstallDeps:
    @patch("otto.verify.subprocess.run")
    def test_skips_pip_install_when_venv_creation_fails(self, mock_run, tmp_path):
        """When venv can't be created, skip pip install entirely to avoid
        contaminating otto's venv."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[build-system]\nrequires=[]\n")

        venv_bin = _install_deps(worktree, timeout=60)

        assert venv_bin is None
        pip_calls = [
            args[0]
            for args, kwargs in mock_run.call_args_list
            if args and isinstance(args[0], list) and len(args[0]) >= 3 and args[0][1:3] == ["-m", "pip"]
        ]
        # No pip install calls — better to skip than contaminate otto's venv
        assert not pip_calls


class TestIntegrationGate:
    @patch("otto.verify.run_tier1")
    @patch("otto.verify._install_deps")
    def test_installs_deps_and_threads_env(self, mock_install_deps, mock_run_tier1, tmp_git_repo):
        mock_install_deps.return_value = "/tmp/worktree/.venv/bin"
        mock_run_tier1.return_value = TierResult(tier="existing_tests", passed=True)

        result = run_integration_gate(
            project_dir=tmp_git_repo,
            test_command="pytest",
            integration_test_file=None,
            timeout=60,
        )

        assert result.passed
        mock_install_deps.assert_called_once()
        env = mock_run_tier1.call_args.kwargs["env"]
        assert env["PATH"].split(os.pathsep)[0] == "/tmp/worktree/.venv/bin"
