"""Tests for otto.cli module."""

import subprocess
from pathlib import Path
from unittest.mock import patch

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
        result = runner.invoke(main, ["init"])
        assert "default_branch" in result.output
        assert "max_retries" in result.output


class TestAdd:
    def test_adds_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "--no-rubric", "Build a login page"])
        assert result.exit_code == 0
        assert "Added task" in result.output

    def test_adds_with_verify(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "--no-rubric", "Optimize", "--verify", "python bench.py"])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())["tasks"]
        assert tasks[0]["verify"] == "python bench.py"

    @patch("otto.cli.generate_rubric")
    def test_imports_from_file(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = []
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
        runner.invoke(main, ["add", "--no-rubric", "Some task"])
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
        runner.invoke(main, ["add", "--no-rubric", "Some task"])
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
        runner.invoke(main, ["add", "--no-rubric", "Some task"])
        result = runner.invoke(main, ["retry", "1"])
        assert result.exit_code != 0


class TestRun:
    def test_dry_run(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.config import create_config
        create_config(tmp_git_repo)
        runner.invoke(main, ["add", "--no-rubric", "Task 1"])
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
        runner.invoke(main, ["add", "--no-rubric", "First task"])
        result = runner.invoke(main, ["status"])
        assert "First task" in result.output
        assert "pending" in result.output


class TestReset:
    def test_resets_all_tasks(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "--no-rubric", "Task 1"])
        result = runner.invoke(main, ["reset", "--yes"])
        assert result.exit_code == 0


class TestAddRubric:
    @patch("otto.cli.generate_rubric")
    def test_add_generates_rubric(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = ["criterion 1", "criterion 2"]
        result = runner.invoke(main, ["add", "Add search"])
        assert result.exit_code == 0
        assert "Rubric" in result.output
        assert "criterion 1" in result.output
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["rubric"] == ["criterion 1", "criterion 2"]

    @patch("otto.cli.generate_rubric")
    def test_add_no_rubric_flag(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "--no-rubric", "Fix typo"])
        assert result.exit_code == 0
        mock_gen.assert_not_called()
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert "rubric" not in tasks["tasks"][0]

    @patch("otto.cli.generate_rubric")
    def test_add_empty_rubric_not_stored(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = []
        result = runner.invoke(main, ["add", "Fix typo"])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert "rubric" not in tasks["tasks"][0]


class TestAddImport:
    @patch("otto.cli.generate_rubric")
    def test_import_txt(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = ["auto criterion"]
        txt_file = tmp_git_repo / "tasks.txt"
        txt_file.write_text("Add search\n# comment\nAdd tags\n\n")
        result = runner.invoke(main, ["add", "-f", str(txt_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert len(tasks["tasks"]) == 2
        assert tasks["tasks"][0]["prompt"] == "Add search"
        assert tasks["tasks"][0]["rubric"] == ["auto criterion"]

    @patch("otto.cli.parse_markdown_tasks")
    def test_import_md(self, mock_parse, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_parse.return_value = [
            {"prompt": "Add search", "rubric": ["criterion 1"]},
        ]
        md_file = tmp_git_repo / "features.md"
        md_file.write_text("# Search\nAdd search.\n")
        result = runner.invoke(main, ["add", "-f", str(md_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert len(tasks["tasks"]) == 1
        assert tasks["tasks"][0]["rubric"] == ["criterion 1"]

    @patch("otto.cli.generate_rubric")
    def test_import_yaml_preserves_rubric(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        yaml_file = tmp_git_repo / "import.yaml"
        yaml_file.write_text(yaml.dump({
            "tasks": [
                {"prompt": "Task with rubric", "rubric": ["existing criterion"]},
                {"prompt": "Task without rubric"},
            ]
        }))
        mock_gen.return_value = ["auto criterion"]
        result = runner.invoke(main, ["add", "-f", str(yaml_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["rubric"] == ["existing criterion"]
        # Only called for task without rubric
        mock_gen.assert_called_once()


class TestStatusRubric:
    def test_shows_rubric_count(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task
        add_task(tmp_git_repo / "tasks.yaml", "Task with rubric",
                 rubric=["c1", "c2", "c3"])
        add_task(tmp_git_repo / "tasks.yaml", "Task without rubric")
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        # Should show rubric count column
        assert "Rubric" in result.output
