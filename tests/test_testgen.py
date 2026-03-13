"""Tests for otto.testgen module."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from otto.testgen import (
    build_testgen_prompt,
    detect_test_framework,
    test_file_path,
    generate_tests,
)


class TestDetectTestFramework:
    def test_detects_pytest(self, tmp_git_repo):
        (tmp_git_repo / "tests").mkdir()
        assert detect_test_framework(tmp_git_repo) == "pytest"

    def test_detects_jest(self, tmp_git_repo):
        (tmp_git_repo / "package.json").write_text('{"devDependencies":{"jest":"*"}}')
        assert detect_test_framework(tmp_git_repo) == "jest"

    def test_returns_none_when_unknown(self, tmp_git_repo):
        assert detect_test_framework(tmp_git_repo) is None


class TestTestFilePath:
    def test_pytest_path(self):
        p = test_file_path("pytest", "abc123def456")
        assert p == Path("tests/otto_verify_abc123def456.py")

    def test_jest_path(self):
        p = test_file_path("jest", "abc123def456")
        assert p == Path("__tests__/otto_verify_abc123def456.test.js")


class TestBuildTestgenPrompt:
    def test_contains_task_prompt(self):
        prompt = build_testgen_prompt("Add auth", "file1.py\nfile2.py", "pytest")
        assert "Add auth" in prompt
        assert "file1.py" in prompt
        assert "pytest" in prompt

    def test_instructs_hermetic_tests(self):
        prompt = build_testgen_prompt("Do stuff", "app.py", "pytest")
        assert "mock" in prompt.lower() or "hermetic" in prompt.lower()


class TestGenerateTests:
    @patch("otto.testgen.subprocess.run")
    def test_generates_test_file(self, mock_run, tmp_git_repo):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="file1.py\nfile2.py\n"),  # git ls-files
            MagicMock(returncode=0, stdout='def test_hello():\n    assert True\n'),  # claude -p
        ]
        key = "abc123def456"
        result = generate_tests(
            task_prompt="Add hello function",
            project_dir=tmp_git_repo,
            key=key,
        )
        assert result is not None
        assert result.exists()
        assert "test_hello" in result.read_text()
        # Verify stored under <git-common-dir>/otto/testgen/
        assert "otto/testgen/" in str(result)

    @patch("otto.testgen.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, tmp_git_repo):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="file1.py\n"),  # git ls-files
            MagicMock(returncode=1, stdout="", stderr="error"),  # claude -p
        ]
        result = generate_tests(
            task_prompt="Do something",
            project_dir=tmp_git_repo,
            key="abc123def456",
        )
        assert result is None
