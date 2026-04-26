"""Shared test fixtures for otto tests."""

import os
import subprocess
from unittest.mock import MagicMock

import pytest


def make_mock_query(text, cost=0.50, session_id="test-session", assistant_messages=None):
    """Build a drop-in replacement for `otto.agent.run_agent_query`.

    Returns an async callable that yields canned text/cost/ResultMessage.
    Used by tests that patch ``otto.agent.run_agent_query`` to exercise the
    pipeline without hitting the real SDK.

    Invokes the caller-supplied ``on_message`` callback with synthetic
    assistant messages plus a ResultMessage so the session logger
    populates narrative.log and messages.jsonl realistically.
    """
    result_msg = MagicMock()
    result_msg.session_id = session_id
    result_msg.subtype = "success"
    result_msg.is_error = False
    result_msg.result = None
    result_msg.total_cost_usd = cost
    result_msg.usage = None
    async def mock_query(prompt, options, **kwargs):
        from otto.agent import AssistantMessage, ResultMessage, TextBlock
        on_message = kwargs.get("on_message")
        emitted_assistant_messages = assistant_messages
        if emitted_assistant_messages is None:
            emitted_assistant_messages = [AssistantMessage(content=[TextBlock(text=text)])]
        if on_message is not None:
            for message in emitted_assistant_messages:
                on_message(message)
            on_message(ResultMessage(
                subtype="success", is_error=False, session_id=session_id,
                result=None, total_cost_usd=cost, usage=None,
            ))
        return text, cost, result_msg

    return mock_query


@pytest.fixture(autouse=True)
def block_real_claude_sdk_calls(monkeypatch):
    """Fail fast if a unit test accidentally reaches the real Claude SDK.

    Tests that need SDK-shaped events should monkeypatch ``otto.agent._sdk_query``
    with a fake stream, as the integration fixtures do. Real provider calls
    remain opt-in for explicit dogfood scripts via OTTO_ALLOW_REAL_COST.
    """
    if (
        os.environ.get("OTTO_ALLOW_REAL_COST") == "1"
        or os.environ.get("OTTO_ALLOW_REAL_PROVIDER_IN_TESTS") == "1"
    ):
        return

    try:
        import otto.agent as agent
    except Exception:
        return
    if getattr(agent, "_sdk_query", None) is None:
        return

    async def blocked_sdk_query(*, prompt, options):
        del prompt, options
        test_name = os.environ.get("PYTEST_CURRENT_TEST", "unknown test")
        raise AssertionError(
            "test attempted to call the real Claude SDK without installing a fake "
            f"agent stream: {test_name}"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(agent, "_sdk_query", blocked_sdk_query)


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
    subprocess.run(["git", "add", "otto.yaml"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add config"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    return tmp_path
