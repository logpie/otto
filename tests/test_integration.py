"""Integration test — end-to-end otto flow with mocked agent."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from otto.config import create_config, load_config
from otto.runner import run_all
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
    @patch("otto.runner.generate_tests", new_callable=AsyncMock)
    @patch("otto.runner.query")
    def test_task_passes_and_merges(
        self, mock_query, mock_testgen, mock_options_cls, tmp_git_repo
    ):
        """Full flow: add task → run → verify → merge to main."""
        # Setup: create config, commit it so tree is clean
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = None  # Skip baseline and tier 1
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Create hello.py that prints hello")

        # Mock agent: simulate creating a file
        # query() is async generator — mock it as such
        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "hello.py").write_text("print('hello')\n")
            yield _make_fake_result("test-session-123")

        mock_query.side_effect = fake_query
        mock_testgen.return_value = None  # Skip testgen

        # Run
        exit_code = asyncio.run(run_all(config, tasks_path, tmp_git_repo))

        # Verify
        assert exit_code == 0
        tasks = load_tasks(tasks_path)
        assert tasks[0]["status"] == "passed"
        # hello.py should be on main
        assert (tmp_git_repo / "hello.py").exists()
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "main"

    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.generate_tests", new_callable=AsyncMock)
    @patch("otto.runner.query")
    def test_task_fails_and_reverts(
        self, mock_query, mock_testgen, mock_options_cls, tmp_git_repo
    ):
        """Task fails verify_cmd → branch deleted, main untouched.

        Uses a per-task verify command ('false') rather than test_command so
        the global baseline check is skipped (test_command=None).
        """
        # Setup: create config, commit it so tree is clean
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = None  # Skip baseline; no tier-1 test suite
        config["max_retries"] = 0
        tasks_path = tmp_git_repo / "tasks.yaml"
        # Per-task verify command that always fails
        add_task(tasks_path, "Do something that fails verification", verify="false")

        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "bad.py").write_text("broken\n")
            yield _make_fake_result("s1")

        mock_query.side_effect = fake_query
        mock_testgen.return_value = None

        exit_code = asyncio.run(run_all(config, tasks_path, tmp_git_repo))

        assert exit_code == 1
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


class TestRubricEndToEnd:
    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.generate_tests", new_callable=AsyncMock)
    @patch("otto.testgen.validate_generated_tests")
    @patch("otto.testgen.run_testgen_agent")
    @patch("otto.testgen.build_blackbox_context")
    @patch("otto.runner.query")
    def test_rubric_uses_adversarial_testgen(
        self, mock_query, mock_blackbox, mock_testgen_agent,
        mock_validate, mock_testgen, mock_options_cls, tmp_git_repo,
    ):
        """Task with rubric uses adversarial testgen (build_blackbox_context +
        run_testgen_agent + validate_generated_tests), not generate_tests.

        Also verifies the agent prompt includes the acceptance tests instruction.
        """
        # Setup: create config, commit it so tree is clean
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = "true"  # Always-pass command — exercises verify without real tests
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(
            tasks_path,
            "Add search",
            rubric=["search is case-insensitive"],
        )

        # Capture prompt passed to query
        captured_prompts = []

        async def fake_query(*, prompt, options=None):
            captured_prompts.append(prompt)
            (tmp_git_repo / "search.py").write_text("def search(): pass\n")
            yield _make_fake_result("rubric-session")

        mock_query.side_effect = fake_query
        mock_blackbox.return_value = "FILE TREE:\nREADME.md"

        # run_testgen_agent returns a test file path
        # Test checks that search.py is importable (the agent will create it)
        test_file = tmp_git_repo / "tests" / "test_otto_search.py"

        async def fake_testgen_agent(rubric, key, ctx, project_dir, **kw):
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text("def test_search():\n    assert True\n")
            return test_file, []

        mock_testgen_agent.side_effect = fake_testgen_agent

        # Validation returns tdd_ok (some tests fail — expected pre-implementation)
        mock_validate_result = MagicMock()
        mock_validate_result.status = "tdd_ok"
        mock_validate_result.passed = 0
        mock_validate_result.failed = 1
        mock_validate.return_value = mock_validate_result

        mock_testgen.return_value = None  # Should not be called (non-rubric path)

        # Run
        exit_code = asyncio.run(run_all(config, tasks_path, tmp_git_repo))

        # Verify
        assert exit_code == 0

        # Adversarial testgen functions should be called
        mock_blackbox.assert_called_once()
        mock_testgen_agent.assert_called_once()
        mock_validate.assert_called_once()

        # generate_tests should NOT be called (non-rubric path)
        mock_testgen.assert_not_called()

        # Agent prompt should include acceptance tests instruction
        assert len(captured_prompts) >= 1
        assert "ACCEPTANCE TESTS" in captured_prompts[0]
        assert "Do NOT modify this file" in captured_prompts[0]
