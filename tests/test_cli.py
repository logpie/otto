"""Tests for otto.cli module."""

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from otto.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestInit:
    def test_creates_config(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["init"])
        assert result.exit_code == 0
        assert (tmp_git_repo / "otto.yaml").exists()

    def test_shows_detected_settings(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        (tmp_git_repo / "tests").mkdir()
        result = runner.invoke(main, ["init"])
        assert "pytest" in result.output or "test_command" in result.output


class TestAdd:
    def test_adds_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "Build a login page"])
        assert result.exit_code == 0
        assert "Added task" in result.output

    def test_adds_with_verify(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "Optimize", "--verify", "python bench.py"])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())["tasks"]
        assert tasks[0]["verify"] == "python bench.py"

    def test_imports_from_file(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        import_file = tmp_git_repo / "import.yaml"
        import_file.write_text(yaml.dump({
            "tasks": [
                {"prompt": "Task A", "id": 99},
                {"prompt": "Task B"},
            ]
        }))
        result = runner.invoke(main, ["add", "-f", str(import_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())["tasks"]
        assert len(tasks) == 2
        # IDs should be auto-assigned (ignoring source IDs)
        assert tasks[0]["id"] == 1
        assert tasks[1]["id"] == 2


class TestRetry:
    def test_resets_failed_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Some task"])
        # Manually set to failed
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        tasks["tasks"][0]["status"] = "failed"
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))
        result = runner.invoke(main, ["retry", "1"])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["status"] == "pending"

    def test_clears_error_code_on_retry(self, runner, tmp_git_repo, monkeypatch):
        """Retry must clear error_code so diverged tasks can re-run."""
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Some task"])
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        tasks["tasks"][0]["status"] = "failed"
        tasks["tasks"][0]["error_code"] = "merge_diverged"
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))
        result = runner.invoke(main, ["retry", "1"])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["status"] == "pending"
        assert tasks["tasks"][0].get("error_code") is None

    def test_rejects_non_failed_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Some task"])
        result = runner.invoke(main, ["retry", "1"])
        assert result.exit_code != 0


class TestRun:
    def test_dry_run(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.config import create_config
        create_config(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["run", "--dry-run"])
        assert result.exit_code == 0
        assert "Pending tasks: 1" in result.output


class TestStatus:
    def test_shows_no_tasks(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No tasks" in result.output or "no tasks" in result.output.lower()

    def test_shows_task_table(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "First task"])
        result = runner.invoke(main, ["status"])
        assert "First task" in result.output
        assert "pending" in result.output


class TestReset:
    def test_resets_all_tasks(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["reset", "--yes"])
        assert result.exit_code == 0
