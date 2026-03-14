"""Tests for otto.testgen module."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from otto.testgen import (
    build_testgen_prompt,
    detect_test_framework,
    test_file_path,
    generate_tests,
    generate_tests_from_rubric,
    _validate_test_output,
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
            MagicMock(returncode=1, stdout="", stderr="error"),  # claude -p (attempt 1)
            MagicMock(returncode=1, stdout="", stderr="error"),  # claude -p (retry 1)
            MagicMock(returncode=1, stdout="", stderr="error"),  # claude -p (retry 2)
        ]
        result = generate_tests(
            task_prompt="Do something",
            project_dir=tmp_git_repo,
            key="abc123def456",
        )
        assert result is None


class TestValidateTestOutput:
    def test_valid_pytest(self):
        code = "import pytest\n\ndef test_something():\n    assert True"
        assert _validate_test_output(code, "pytest") is True

    def test_invalid_pytest_syntax(self):
        assert _validate_test_output("this is not python{{{", "pytest") is False

    def test_invalid_pytest_no_test_func(self):
        assert _validate_test_output("x = 1", "pytest") is False

    def test_prose_rejected(self):
        assert _validate_test_output("Here are some tests I wrote:", "pytest") is False

    def test_valid_jest(self):
        code = "describe('test', () => { it('works', () => {}) })"
        assert _validate_test_output(code, "jest") is True

    def test_empty_rejected(self):
        assert _validate_test_output("", "pytest") is False


class TestGenerateTestsFromRubric:
    @patch("otto.testgen.subprocess.run")
    def test_creates_test_file(self, mock_run, tmp_path):
        import subprocess as real_subprocess
        real_subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        real_subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path, capture_output=True,
        )
        (tmp_path / "tests").mkdir()

        test_code = (
            'from app import search\n\n'
            'def test_case_insensitive():\n'
            '    assert search("PYTHON") == search("python")\n'
        )

        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "ls-files" in str(args):
                m.stdout = "app.py"
            elif "claude" in str(args):
                m.stdout = test_code
            return m

        mock_run.side_effect = side_effect

        result = generate_tests_from_rubric(
            ["search is case-insensitive"], "Add search", tmp_path, "testkey123"
        )
        assert result is not None
        assert result.exists()
        assert "def test_case_insensitive" in result.read_text()

    @patch("otto.testgen.subprocess.run")
    def test_returns_none_on_prose(self, mock_run, tmp_path):
        import subprocess as real_subprocess
        real_subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        real_subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path, capture_output=True,
        )
        (tmp_path / "tests").mkdir()

        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "Here is what I would test..."
            return m

        mock_run.side_effect = side_effect

        result = generate_tests_from_rubric(
            ["criterion"], "task", tmp_path, "key123"
        )
        assert result is None
