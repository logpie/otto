"""Integration test — end-to-end otto flow with mocked agent."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from otto.config import create_config, load_config
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
