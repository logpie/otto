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
        result = runner.invoke(main, ["add", "--no-spec", "Build a login page"])
        assert result.exit_code == 0
        assert "Added task" in result.output

    def test_adds_with_verify(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "--no-spec", "Optimize", "--verify", "python bench.py"])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())["tasks"]
        assert tasks[0]["verify"] == "python bench.py"

    @patch("otto.cli.generate_spec")
    def test_filters_generated_spec_preamble_before_display_and_storage(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = [
            "Acceptance Spec: Dew Point Indicator Panel",
            "Existing card style: bg-white/15 backdrop-blur-lg rounded-2xl border border-white/20",
            "Temperature always arrives in Celsius from API; convertTemp/formatTemp in src/lib/units.ts",
            "current.temperature and current.humidity are available on WeatherData",
            "Tests live in __tests__/ using Jest + @testing-library/react",
            "---",
            "Dew point temperature is calculated from current.temperature and current.humidity.",
            "The panel displays exactly one of four comfort level labels.",
            "The dew point temperature is displayed respecting the user's temperature unit preference.",
            "The panel is rendered in the WeatherApp layout alongside existing panels.",
            {"text": "The panel matches the existing card style used elsewhere in the UI.", "verifiable": False},
            "Tests cover: dew point calculation, comfort classification, and rendering.",
            "The dew point calculation and comfort classification logic are in a separate utility module.",
        ]

        result = runner.invoke(main, ["add", "Add a dew point indicator panel"])

        assert result.exit_code == 0
        assert "Spec (7 criteria — 6 verifiable, 1 visual)" in result.output
        assert "Acceptance Spec: Dew Point Indicator Panel" not in result.output
        assert "Existing card style:" not in result.output
        assert "Tests live in __tests__/" not in result.output
        assert "Tests cover:" in result.output

        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())["tasks"]
        assert len(tasks[0]["spec"]) == 7
        stored_texts = [item["text"] if isinstance(item, dict) else item for item in tasks[0]["spec"]]
        assert "Acceptance Spec: Dew Point Indicator Panel" not in stored_texts
        assert any(text.startswith("Tests cover:") for text in stored_texts)

    @patch("otto.cli.generate_spec")
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
        runner.invoke(main, ["add", "--no-spec", "Some task"])
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
        runner.invoke(main, ["add", "--no-spec", "Some task"])
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
        runner.invoke(main, ["add", "--no-spec", "Some task"])
        result = runner.invoke(main, ["retry", "1"])
        assert result.exit_code != 0


class TestRun:
    def test_dry_run(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.config import create_config
        create_config(tmp_git_repo)
        runner.invoke(main, ["add", "--no-spec", "Task 1"])
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
        runner.invoke(main, ["add", "--no-spec", "First task"])
        result = runner.invoke(main, ["status"])
        assert "First task" in result.output
        assert "pending" in result.output


class TestReset:
    def test_resets_all_tasks(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "--no-spec", "Task 1"])
        result = runner.invoke(main, ["reset", "--yes"])
        assert result.exit_code == 0


class TestAddSpec:
    @patch("otto.cli.generate_spec")
    def test_add_generates_spec(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = ["criterion 1", "criterion 2"]
        result = runner.invoke(main, ["add", "Add search"])
        assert result.exit_code == 0
        assert "Spec" in result.output
        assert "criterion 1" in result.output
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["spec"] == ["criterion 1", "criterion 2"]

    @patch("otto.cli.generate_spec")
    def test_add_no_spec_flag(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "--no-spec", "Fix typo"])
        assert result.exit_code == 0
        mock_gen.assert_not_called()
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert "spec" not in tasks["tasks"][0]

    @patch("otto.cli.generate_spec")
    def test_add_empty_spec_aborts(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        """Empty spec gen should abort — no ghost task created."""
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = []
        result = runner.invoke(main, ["add", "Fix typo"])
        assert result.exit_code != 0
        # Task should NOT have been created
        assert not (tmp_git_repo / "tasks.yaml").exists()


class TestAddImport:
    @patch("otto.cli.generate_spec")
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
        assert tasks["tasks"][0]["spec"] == ["auto criterion"]

    @patch("otto.cli.parse_markdown_tasks")
    def test_import_md(self, mock_parse, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_parse.return_value = [
            {"prompt": "Add search", "spec": ["criterion 1"]},
        ]
        md_file = tmp_git_repo / "features.md"
        md_file.write_text("# Search\nAdd search.\n")
        result = runner.invoke(main, ["add", "-f", str(md_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert len(tasks["tasks"]) == 1
        assert tasks["tasks"][0]["spec"] == ["criterion 1"]

    @patch("otto.cli.generate_spec")
    def test_import_yaml_preserves_spec(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        yaml_file = tmp_git_repo / "import.yaml"
        yaml_file.write_text(yaml.dump({
            "tasks": [
                {"prompt": "Task with spec", "spec": ["existing criterion"]},
                {"prompt": "Task without spec"},
            ]
        }))
        mock_gen.return_value = ["auto criterion"]
        result = runner.invoke(main, ["add", "-f", str(yaml_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["spec"] == ["existing criterion"]
        # Only called for task without spec
        mock_gen.assert_called_once()


class TestDiffAndShow:
    def test_show_displays_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task
        add_task(tmp_git_repo / "tasks.yaml", "Test task", spec=["criterion 1"])
        result = runner.invoke(main, ["show", "1"])
        assert result.exit_code == 0
        assert "Test task" in result.output
        assert "criterion 1" in result.output

    def test_show_not_found(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["show", "999"])
        assert result.exit_code != 0

    def test_show_with_phase_timings(self, runner, tmp_git_repo, monkeypatch):
        """Show should display per-phase timing from progress events."""
        import json
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks, update_task
        add_task(tmp_git_repo / "tasks.yaml", "Timed task", spec=["c1"])
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        key = tasks[0]["key"]
        update_task(tmp_git_repo / "tasks.yaml", key,
                    status="passed", duration_s=120.0, cost_usd=0.50)

        # Create progress events
        log_dir = tmp_git_repo / "otto_logs"
        log_dir.mkdir()
        results_file = log_dir / "pilot_results.jsonl"
        events = [
            {"tool": "progress", "event": "phase", "task_key": key,
             "name": "prepare", "status": "done", "time_s": 2.0},
            {"tool": "progress", "event": "phase", "task_key": key,
             "name": "coding", "status": "done", "time_s": 45.0},
            {"tool": "progress", "event": "phase", "task_key": key,
             "name": "verify", "status": "done", "time_s": 10.0},
            {"tool": "progress", "event": "phase", "task_key": key,
             "name": "qa", "status": "done", "time_s": 60.0},
            {"tool": "progress", "event": "phase", "task_key": key,
             "name": "merge", "status": "done", "time_s": 3.0},
        ]
        results_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        # Create task log dir
        task_log = log_dir / key
        task_log.mkdir()

        result = runner.invoke(main, ["show", "1"])
        assert result.exit_code == 0
        assert "2m00s" in result.output  # total duration
        assert "prepare" in result.output
        assert "coding" in result.output
        assert "$0.50" in result.output

    def test_show_with_verify_logs(self, runner, tmp_git_repo, monkeypatch):
        """Show should display verify summary from log files."""
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks, update_task
        add_task(tmp_git_repo / "tasks.yaml", "Verify task")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        key = tasks[0]["key"]
        update_task(tmp_git_repo / "tasks.yaml", key, status="passed")

        log_dir = tmp_git_repo / "otto_logs" / key
        log_dir.mkdir(parents=True)
        (log_dir / "verify.log").write_text("PASSED")

        result = runner.invoke(main, ["show", "1"])
        assert result.exit_code == 0
        assert "PASSED" in result.output

    def test_show_with_agent_log(self, runner, tmp_git_repo, monkeypatch):
        """Show should display agent log highlights."""
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks, update_task
        add_task(tmp_git_repo / "tasks.yaml", "Agent task")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        key = tasks[0]["key"]
        update_task(tmp_git_repo / "tasks.yaml", key, status="passed")

        log_dir = tmp_git_repo / "otto_logs" / key
        log_dir.mkdir(parents=True)
        agent_log = "\n".join([f"line {i}" for i in range(20)])
        (log_dir / "attempt-1-agent.log").write_text(agent_log)

        result = runner.invoke(main, ["show", "1"])
        assert result.exit_code == 0
        assert "Agent log" in result.output
        assert "line 0" in result.output  # first line shown


class TestHistory:
    def test_no_history(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["history"])
        assert result.exit_code == 0
        assert "No run history" in result.output

    def test_shows_history_entries(self, runner, tmp_git_repo, monkeypatch):
        import json
        monkeypatch.chdir(tmp_git_repo)
        history_dir = tmp_git_repo / "otto_logs"
        history_dir.mkdir()
        history_file = history_dir / "run-history.jsonl"
        entries = [
            {"timestamp": "2026-03-20T23:00:00", "tasks_total": 3,
             "tasks_passed": 2, "tasks_failed": 1, "cost_usd": 1.50,
             "time_s": 300.0, "commit": "abc123", "failure_summary": "task #5 failed: timeout"},
            {"timestamp": "2026-03-20T22:00:00", "tasks_total": 1,
             "tasks_passed": 1, "tasks_failed": 0, "cost_usd": 0.19,
             "time_s": 120.0, "commit": "def456", "failure_summary": ""},
        ]
        history_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        result = runner.invoke(main, ["history"])
        assert result.exit_code == 0
        assert "2026-03-20" in result.output
        assert "$1.50" in result.output
        assert "$0.19" in result.output

    def test_history_limit(self, runner, tmp_git_repo, monkeypatch):
        import json
        monkeypatch.chdir(tmp_git_repo)
        history_dir = tmp_git_repo / "otto_logs"
        history_dir.mkdir()
        history_file = history_dir / "run-history.jsonl"
        entries = [
            {"timestamp": f"2026-03-{20+i:02d}T10:00:00", "tasks_total": 1,
             "tasks_passed": 1, "tasks_failed": 0, "cost_usd": 0.1,
             "time_s": 60.0}
            for i in range(5)
        ]
        history_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        result = runner.invoke(main, ["history", "-n", "2"])
        assert result.exit_code == 0
        # Should only show 2 most recent
        assert "2026-03-24" in result.output
        assert "2026-03-23" in result.output


class TestLogs:
    def test_logs_no_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["logs", "999"])
        assert result.exit_code != 0

    def test_logs_no_logs(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task
        add_task(tmp_git_repo / "tasks.yaml", "Task")
        result = runner.invoke(main, ["logs", "1"])
        assert result.exit_code == 0
        assert "No logs" in result.output

    def test_logs_raw_mode(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks
        add_task(tmp_git_repo / "tasks.yaml", "Task")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        key = tasks[0]["key"]
        log_dir = tmp_git_repo / "otto_logs" / key
        log_dir.mkdir(parents=True)
        (log_dir / "verify.log").write_text("PASSED")
        (log_dir / "attempt-1-agent.log").write_text("tool call here")

        result = runner.invoke(main, ["logs", "--raw", "1"])
        assert result.exit_code == 0
        assert "verify.log" in result.output
        assert "PASSED" in result.output
        assert "agent.log" in result.output
        assert "tool call here" in result.output

    def test_logs_structured_mode(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks
        add_task(tmp_git_repo / "tasks.yaml", "Task")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        key = tasks[0]["key"]
        log_dir = tmp_git_repo / "otto_logs" / key
        log_dir.mkdir(parents=True)
        (log_dir / "attempt-1-verify.log").write_text(
            "test_command: PASS\nOutput here\n12 passed"
        )
        (log_dir / "attempt-1-agent.log").write_text(
            "thinking about things\n"
            "● Write  src/main.py\n"
            "● Bash  python test.py\n"
            "done reasoning\n"
        )

        result = runner.invoke(main, ["logs", "1"])
        assert result.exit_code == 0
        assert "Verification" in result.output
        assert "PASS" in result.output
        assert "Agent Activity" in result.output
        assert "2 tool calls" in result.output


class TestStatusWatch:
    def test_status_watch_help(self, runner, tmp_git_repo, monkeypatch):
        """Watch flag should be recognized."""
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["status", "--help"])
        assert "--watch" in result.output


class TestStatusSpec:
    def test_shows_spec_count(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task
        add_task(tmp_git_repo / "tasks.yaml", "Task with spec",
                 spec=["c1", "c2", "c3"])
        add_task(tmp_git_repo / "tasks.yaml", "Task without spec")
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        # Should show spec count column
        assert "Spec" in result.output


class TestStatusCost:
    def test_shows_cost(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks, update_task
        add_task(tmp_git_repo / "tasks.yaml", "Task with cost")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        update_task(tmp_git_repo / "tasks.yaml",
                    tasks[0]["key"],
                    cost_usd=0.15)
        result = runner.invoke(main, ["status"])
        assert "$0.15" in result.output

    def test_shows_cost_in_show(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks, update_task
        add_task(tmp_git_repo / "tasks.yaml", "Task with cost")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        update_task(tmp_git_repo / "tasks.yaml",
                    tasks[0]["key"],
                    cost_usd=0.42)
        result = runner.invoke(main, ["show", "1"])
        assert "$0.42" in result.output
