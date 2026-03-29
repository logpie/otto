"""Tests for otto.cli module."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from otto.cli import main


@pytest.fixture
def runner():
    return CliRunner()



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

        result = runner.invoke(main, ["add", "--spec", "Add a dew point indicator panel"])

        assert result.exit_code == 0
        # v4.5: display uses [must]/[should] binding
        assert "Spec" in result.output
        assert "7 criteria" in result.output
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

    def test_retries_conflict_task_and_clears_planner_metadata(self, runner, tmp_git_repo, monkeypatch):
        from otto.tasks import planner_input_fingerprint

        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Conflicting task"])
        runner.invoke(main, ["add", "Other conflicting task"])
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        task = tasks["tasks"][0]
        other = tasks["tasks"][1]
        fingerprint = planner_input_fingerprint(task)
        other_fingerprint = planner_input_fingerprint(other)
        for item in tasks["tasks"]:
            item["status"] = "conflict"
            item["planner_fingerprint"] = planner_input_fingerprint(item)
            item["planner_conflicts"] = [{
                "tasks": [task["key"], other["key"]],
                "description": "same function rewrite",
                "suggestion": "combine tasks",
                "fingerprints": {
                    task["key"]: fingerprint,
                    other["key"]: other_fingerprint,
                },
            }]
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))

        result = runner.invoke(main, ["retry", "1"])

        assert result.exit_code == 0
        persisted = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())["tasks"]
        assert persisted[0]["status"] == "pending"
        assert "planner_conflicts" not in persisted[0]
        assert persisted[1]["status"] == "conflict"


class TestRun:
    def test_dry_run_shows_plan(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.config import create_config
        create_config(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["run", "--dry-run"])
        assert result.exit_code == 0
        assert "Execution Plan" in result.output
        assert "otto run" in result.output

    def test_one_off_mode_routes_through_run_per(
        self,
        runner,
        tmp_git_repo,
        monkeypatch,
    ):
        """One-off mode writes temp tasks.yaml and calls run_per."""
        monkeypatch.chdir(tmp_git_repo)
        from otto.config import create_config

        create_config(tmp_git_repo)

        async def fake_run_per(config, tasks_path, project_dir):
            from otto.tasks import load_tasks
            tasks = load_tasks(tasks_path)
            pending = [t for t in tasks if t.get("status") == "pending"]
            assert len(pending) == 1
            assert "Fix the broken command" in pending[0]["prompt"]
            assert pending[0]["key"].startswith("adhoc-")
            return 0

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            result = runner.invoke(main, ["run", "Fix the broken command"])

        assert result.exit_code == 0


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


class TestDrop:
    def test_drops_pending_task(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["drop", "--yes", "1"])
        assert result.exit_code == 0
        assert "Dropped task" in result.output
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text()) if (tmp_git_repo / "tasks.yaml").exists() else {"tasks": []}
        assert len(tasks.get("tasks", [])) == 0

    def test_drop_passed_warns_about_code(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        tasks["tasks"][0]["status"] = "passed"
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))
        result = runner.invoke(main, ["drop", "--yes", "1"])
        assert result.exit_code == 0
        assert "stays on main" in result.output
        assert "otto revert" in result.output

    def test_drop_running_task_rejected(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        tasks["tasks"][0]["status"] = "running"
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))
        result = runner.invoke(main, ["drop", "1"])
        assert result.exit_code != 0

    def test_drop_all(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        runner.invoke(main, ["add", "Task 2"])
        result = runner.invoke(main, ["drop", "--all", "--yes"])
        assert result.exit_code == 0
        assert "Dropped" in result.output
        assert not (tmp_git_repo / "tasks.yaml").exists()

    def test_drop_not_found(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["drop", "999"])
        assert result.exit_code != 0

    def test_drop_no_args(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["drop"])
        assert result.exit_code != 0


class TestRevert:
    def test_revert_one_task(self, runner, tmp_git_repo, monkeypatch):
        """Revert undoes a specific task's commit and removes from queue."""
        monkeypatch.chdir(tmp_git_repo)
        # Create a commit that looks like otto's
        (tmp_git_repo / "feature.py").write_text("# feature\n")
        subprocess.run(["git", "add", "feature.py"], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "otto: Add feature (#3)"],
            cwd=tmp_git_repo, capture_output=True,
        )
        # Add a task with id=3
        from otto.tasks import add_task
        task = add_task(tmp_git_repo / "tasks.yaml", "Add feature")
        # Manually set id to 3 and status to passed
        tasks_data = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        tasks_data["tasks"][0]["id"] = 3
        tasks_data["tasks"][0]["status"] = "passed"
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks_data))

        result = runner.invoke(main, ["revert", "--yes", "3"])
        assert result.exit_code == 0
        assert "Reverted commit" in result.output
        # feature.py should no longer exist (reverted)
        assert not (tmp_git_repo / "feature.py").exists()

    def test_revert_no_commit_found(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["revert", "--yes", "1"])
        assert result.exit_code != 0
        assert "No git commit found" in result.output

    def test_revert_running_rejected(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        tasks["tasks"][0]["status"] = "running"
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))
        result = runner.invoke(main, ["revert", "1"])
        assert result.exit_code != 0

    def test_revert_all(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["revert", "--all", "--yes"])
        assert result.exit_code == 0

    def test_revert_no_args(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["revert"])
        assert result.exit_code != 0


class TestResetAlias:
    def test_reset_alias_works(self, runner, tmp_git_repo, monkeypatch):
        """'otto reset --yes' should work as alias for 'otto drop --all --yes'."""
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["reset", "--yes"])
        assert result.exit_code == 0

    def test_reset_revert_commits_alias(self, runner, tmp_git_repo, monkeypatch):
        """'otto reset --revert-commits --yes' should work as alias for 'otto revert --all --yes'."""
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["reset", "--revert-commits", "--yes"])
        assert result.exit_code == 0


class TestDeleteAlias:
    def test_delete_alias_works(self, runner, tmp_git_repo, monkeypatch):
        """'otto delete 1' should work as alias for 'otto drop 1'."""
        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Task 1"])
        result = runner.invoke(main, ["delete", "--yes", "1"])
        assert result.exit_code == 0


class TestAddSpec:
    @patch("otto.cli.generate_spec")
    def test_add_generates_spec(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = ["criterion 1", "criterion 2"]
        result = runner.invoke(main, ["add", "--spec", "Add search"])
        assert result.exit_code == 0
        assert "Spec" in result.output
        assert "criterion 1" in result.output
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][0]["spec"] == ["criterion 1", "criterion 2"]

    @patch("otto.cli.generate_spec")
    def test_add_instant_no_spec_by_default(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        """Default add is instant — no spec gen, no LLM call."""
        monkeypatch.chdir(tmp_git_repo)
        result = runner.invoke(main, ["add", "Fix typo"])
        assert result.exit_code == 0
        mock_gen.assert_not_called()
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert "spec" not in tasks["tasks"][0]
        assert "Spec will be generated at run time" in result.output

    @patch("otto.cli.generate_spec")
    def test_add_empty_spec_aborts_with_spec_flag(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        """Empty spec gen with --spec should abort — no ghost task created."""
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = []
        result = runner.invoke(main, ["add", "--spec", "Fix typo"])
        assert result.exit_code != 0
        # Task should NOT have been created
        assert not (tmp_git_repo / "tasks.yaml").exists()


class TestAddImport:
    @patch("otto.cli.generate_spec")
    def test_import_failure_preserves_existing_tasks(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task

        add_task(tmp_git_repo / "tasks.yaml", "Existing task")
        txt_file = tmp_git_repo / "tasks.txt"
        txt_file.write_text("Add search\n")
        mock_gen.side_effect = RuntimeError("spec boom")

        result = runner.invoke(main, ["add", "-f", str(txt_file)])

        assert result.exit_code != 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert [task["prompt"] for task in tasks["tasks"]] == ["Existing task"]

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

    @patch("otto.cli.generate_spec")
    def test_import_yaml_preserves_depends_on(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        yaml_file = tmp_git_repo / "import.yaml"
        yaml_file.write_text(yaml.dump({
            "tasks": [
                {"prompt": "Task A"},
                {"prompt": "Task B", "depends_on": [0]},
            ]
        }))
        mock_gen.return_value = []

        result = runner.invoke(main, ["add", "-f", str(yaml_file)])

        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert tasks["tasks"][1]["depends_on"] == [1]


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

    def test_show_displays_conflict_details(self, runner, tmp_git_repo, monkeypatch):
        from otto.tasks import planner_input_fingerprint

        monkeypatch.chdir(tmp_git_repo)
        runner.invoke(main, ["add", "Rewrite parser A"])
        runner.invoke(main, ["add", "Rewrite parser B"])
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        first = tasks["tasks"][0]
        second = tasks["tasks"][1]
        fingerprints = {
            first["key"]: planner_input_fingerprint(first),
            second["key"]: planner_input_fingerprint(second),
        }
        for item in tasks["tasks"]:
            item["status"] = "conflict"
            item["planner_fingerprint"] = planner_input_fingerprint(item)
            item["planner_conflicts"] = [{
                "tasks": [first["key"], second["key"]],
                "description": "same parser rewrite",
                "suggestion": "choose one approach",
                "fingerprints": fingerprints,
            }]
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))

        result = runner.invoke(main, ["show", "1"])

        assert result.exit_code == 0
        assert "Conflict details" in result.output
        assert "choose one approach" in result.output


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
        assert "Testing" in result.output
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
        # Card layout shows spec count in detail line
        assert "3 specs" in result.output

    def test_status_shows_conflict_and_blocked(self, runner, tmp_git_repo, monkeypatch):
        from otto.tasks import planner_input_fingerprint

        monkeypatch.chdir(tmp_git_repo)
        tasks = {"tasks": [
            {"id": 1, "key": "task-a", "prompt": "Rewrite parser A", "status": "conflict"},
            {"id": 2, "key": "task-b", "prompt": "Rewrite parser B", "status": "conflict"},
            {"id": 3, "key": "task-c", "prompt": "Use parser output", "status": "blocked", "depends_on": [2]},
        ]}
        first = tasks["tasks"][0]
        second = tasks["tasks"][1]
        fingerprints = {
            first["key"]: planner_input_fingerprint(first),
            second["key"]: planner_input_fingerprint(second),
        }
        for item in tasks["tasks"][:2]:
            item["planner_fingerprint"] = planner_input_fingerprint(item)
            item["planner_conflicts"] = [{
                "tasks": [first["key"], second["key"]],
                "description": "same parser rewrite",
                "suggestion": "choose one approach",
                "fingerprints": fingerprints,
            }]
        (tmp_git_repo / "tasks.yaml").write_text(yaml.dump(tasks))

        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "conflict" in result.output
        assert "blocked" in result.output


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

    def test_shows_review_ref_in_show(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        from otto.tasks import add_task, load_tasks, update_task
        add_task(tmp_git_repo / "tasks.yaml", "Task with review ref")
        tasks = load_tasks(tmp_git_repo / "tasks.yaml")
        update_task(
            tmp_git_repo / "tasks.yaml",
            tasks[0]["key"],
            status="failed",
            review_ref="refs/otto/candidates/abc123/attempt-2",
        )
        result = runner.invoke(main, ["show", "1"])
        assert "Review ref" in result.output
        assert "refs/otto/candidates/abc123/attempt-2" in result.output
