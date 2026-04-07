"""Integration test — end-to-end otto flow with mocked agent."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import yaml
from click.testing import CliRunner

from otto.cli import main
from otto.config import create_config, load_config
from otto.product_planner import PlannedTask, ProductPlan, _parse_planner_output
from otto.runner import run_task_v45
from otto.tasks import add_task, load_tasks


def _commit_otto_config(repo: Path) -> None:
    """Commit otto.yaml after create_config so the tree stays clean."""
    subprocess.run(
        ["git", "add", "otto.yaml"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add otto config"],
        cwd=repo, capture_output=True, check=True,
    )


def _make_fake_result(session_id="test-session"):
    """Create a fake ResultMessage-like object."""
    result = MagicMock()
    result.session_id = session_id
    result.is_error = False
    result.subtype = "success"
    return result


class TestEndToEnd:
    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.query")
    def test_task_passes_and_merges(
        self, mock_query, mock_options_cls, tmp_git_repo
    ):
        """Full flow: add task → run_task_v45 → verify → QA → merge to main."""
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = "true"
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Create hello.py that prints hello",
                 spec=["hello.py exists and prints hello"])

        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "hello.py").write_text("print('hello')\n")
            yield _make_fake_result("test-session-123")

        mock_query.side_effect = fake_query

        tasks = load_tasks(tasks_path)
        task = tasks[0]

        with patch("otto.runner.run_qa", new=AsyncMock(return_value={
            "must_passed": True,
            "verdict": {"must_passed": True, "must_items": []},
            "raw_report": "QA PASS",
            "cost_usd": 0.0,
        })):
            result = asyncio.run(run_task_v45(task, config, tmp_git_repo, tasks_path))
        success = result["success"]

        # Verify
        assert success is True
        tasks = load_tasks(tasks_path)
        assert tasks[0]["status"] == "verified"

    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.query")
    def test_task_fails_and_reverts(
        self, mock_query, mock_options_cls, tmp_git_repo
    ):
        """Task fails verify_cmd → workspace reverted, main untouched."""
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = "true"
        config["max_retries"] = 0
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Do something that fails verification",
                 verify="false", spec=["it works"])

        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "bad.py").write_text("broken\n")
            yield _make_fake_result("s1")

        mock_query.side_effect = fake_query

        tasks = load_tasks(tasks_path)
        task = tasks[0]

        result = asyncio.run(run_task_v45(task, config, tmp_git_repo, tasks_path))
        success = result["success"]

        assert success is False
        tasks = load_tasks(tasks_path)
        assert tasks[0]["status"] == "failed"
        # bad.py should NOT be on main
        assert not (tmp_git_repo / "bad.py").exists()
        # Branch should be cleaned up
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        ).stdout
        assert "otto/" not in branches

    @patch("otto.runner._snapshot_untracked", side_effect=RuntimeError("setup boom"))
    def test_setup_exception_marks_task_failed(
        self, mock_snapshot, tmp_git_repo
    ):
        """Setup failures should not leave the task stuck in running."""
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["max_retries"] = 0
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Task that fails during setup", spec=["it works"])

        task = load_tasks(tasks_path)[0]
        result = asyncio.run(run_task_v45(task, config, tmp_git_repo, tasks_path))
        success = result["success"]

        assert success is False
        failed_task = load_tasks(tasks_path)[0]
        assert failed_task["status"] == "failed"
        assert failed_task["error_code"] == "internal_error"
        assert "setup boom" in failed_task["error"]


class TestBuildCommand:
    def test_build_skips_product_qa_after_inner_failure(
        self,
        tmp_git_repo,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        tasks_path = tmp_git_repo / "tasks.yaml"

        product_spec_path = tmp_git_repo / "product-spec.md"
        product_spec_path.write_text("# Product Spec\n")
        plan = ProductPlan(
            mode="decomposed",
            tasks=[PlannedTask(prompt="Build the actual product")],
            product_spec_path=product_spec_path,
            architecture_path=None,
        )

        from otto.pipeline import BuildResult
        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(passed=False, build_id="test-build", error="build failed",
                               tasks_passed=0, tasks_failed=1)

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = runner.invoke(main, ["build", "demo app", "--no-review"])

        assert result.exit_code == 1

    def test_build_exit_code_tracks_product_qa_result(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        from otto.pipeline import BuildResult
        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(
                passed=False, build_id="test-build", rounds=1,
                journeys=[{"name": "happy path", "passed": False}],
                tasks_passed=1, tasks_failed=0,
            )

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = runner.invoke(main, ["build", "demo app", "--no-review"])

        assert result.exit_code == 1
        assert "Some journeys failed" in result.output

    def test_build_sets_exit_code_when_product_qa_raises(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        async def fake_build(intent, project_dir, config, **kwargs):
            raise RuntimeError("qa boom")

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = runner.invoke(main, ["build", "demo app", "--no-review"])

        assert result.exit_code == 1
        assert "qa boom" in result.output

    def test_build_split_mode_runs(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        from otto.pipeline import BuildResult
        async def fake_split(intent, project_dir, config):
            return BuildResult(passed=True, build_id="test-split", total_cost=0.5)

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v2", side_effect=fake_split):
            result = runner.invoke(
                main,
                ["build", "demo app", "--split", "--no-review"],
            )

        assert result.exit_code == 0



class TestPipelineE2E:
    """E2E tests for the full pipeline: build → certify → fix → verify.

    Mocks only the external boundaries (orchestrator + certifier).
    Everything else runs for real: pipeline.py, verification.py, tasks.yaml, git.
    """

    @pytest.mark.asyncio
    async def test_monolithic_build_all_tasks_pass(self, tmp_git_repo):
        """Full cycle: build -> all tasks pass -> done."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.tasks import load_tasks, update_task

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        async def fake_run_per(config, tasks_path, project_dir):
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            result = await build_product(
                "Build a counter app",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main"},
            )

        assert result.passed is True

        # intent.md was created as grounding
        assert (tmp_git_repo / "intent.md").exists()

    @pytest.mark.asyncio
    async def test_monolithic_build_passes_first_try(self, tmp_git_repo):
        """Happy path: build -> all tasks pass -> done."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.tasks import load_tasks

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        async def fake_run_per(config, tasks_path, project_dir):
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    from otto.tasks import update_task
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            result = await build_product(
                "Build a hello world app",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 1

    @pytest.mark.asyncio
    async def test_build_fails_when_tasks_fail(self, tmp_git_repo):
        """Build fails when tasks report failure."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.tasks import load_tasks

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        async def fake_run_per(config, tasks_path, project_dir):
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    from otto.tasks import update_task
                    update_task(tasks_path, task["key"], status="failed")
            return 1

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            result = await build_product(
                "Build an app",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main", "skip_product_qa": True},
            )

        assert result.passed is False



class TestPlannerConfig:
    def test_decomposed_plan_requires_product_spec_file(self, tmp_path):
        raw = json.dumps({
            "mode": "decomposed",
            "tasks": [{"prompt": "Build feature A"}],
        })

        with pytest.raises(ValueError, match="product-spec.md"):
            _parse_planner_output(raw, tmp_path)


class TestUnifiedCertifierRegressions:
    @pytest.mark.asyncio
    async def test_build_product_forces_skip_qa_and_skip_spec(self, tmp_git_repo):
        from otto.pipeline import build_product

        seen_config = {}
        create_config(tmp_git_repo)
        config = load_config(tmp_git_repo / "otto.yaml")

        async def fake_run_per(config, tasks_path, project_dir):
            seen_config.update(config)
            return 0

        with patch("otto.pipeline._commit_artifacts"), \
             patch("otto.orchestrator.run_per", side_effect=fake_run_per):
            result = await build_product(
                "Build a demo app",
                tmp_git_repo,
                config,
            )

        assert result.passed is True
        assert seen_config["skip_qa"] is True
        assert seen_config["skip_spec"] is True

