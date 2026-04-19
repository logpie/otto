"""Shared test fixtures for otto tests."""

import subprocess
from unittest.mock import MagicMock

import pytest


def make_mock_query(text, cost=0.50, session_id="test-session"):
    """Build a drop-in replacement for `otto.agent.run_agent_query`.

    Returns an async callable that yields canned text/cost/ResultMessage.
    Used by tests that patch ``otto.agent.run_agent_query`` to exercise the
    pipeline without hitting the real SDK.
    """
    result_msg = MagicMock()
    result_msg.session_id = session_id

    async def mock_query(prompt, options, **kwargs):
        return text, cost, result_msg

    return mock_query


def pytest_collection_modifyitems(items):
    """Remove collected items that are imported from non-test modules.

    Prevents functions like `test_file_path` (imported from otto modules)
    from being collected as tests when imported into test modules.
    """
    filtered = []
    for item in items:
        # Only filter Function items (not class-based tests)
        if hasattr(item, "function"):
            func = item.function
            func_module = getattr(func, "__module__", None)
            # If the function's __module__ doesn't start with "tests.", skip it
            if func_module and not func_module.startswith("tests.") and func_module != item.module.__name__:
                continue
        filtered.append(item)
    items[:] = filtered


@pytest.fixture
def tmp_bare_git_repo(tmp_path):
    """Temp git repo with an initial README commit — no otto.yaml.

    Used by ``tests/test_config.py`` where the config helpers need a git
    repo but don't want otto.yaml pre-populated (the tests create it).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True, check=True,
    )
    readme = repo / "README.md"
    readme.write_text("# Test repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo, capture_output=True, check=True,
    )
    return repo


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Temp git repo with otto.yaml committed — what the CLI expects on entry.

    This is the default across test_hardening.py and test_v3_pipeline.py;
    use ``tmp_bare_git_repo`` if you need a repo without otto.yaml.
    """
    from otto.config import create_config
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    create_config(tmp_path)
    subprocess.run(["git", "add", "otto.yaml"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add config"],
        cwd=tmp_path, capture_output=True,
    )
    return tmp_path
