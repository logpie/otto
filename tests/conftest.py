"""Shared test fixtures for otto tests."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml


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
def tmp_git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
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
    # Initial commit so we have a HEAD
    readme = repo / "README.md"
    readme.write_text("# Test repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo, capture_output=True, check=True,
    )
    return repo


@pytest.fixture
def sample_config(tmp_git_repo):
    """Create a sample otto.yaml in the temp repo."""
    config_path = tmp_git_repo / "otto.yaml"
    config = {
        "test_command": "pytest",
        "max_retries": 3,
        "model": "sonnet",
        "project_dir": ".",
        "default_branch": "main",
        "verify_timeout": 300,
    }
    config_path.write_text(yaml.dump(config))
    return config_path


@pytest.fixture
def sample_tasks_file(tmp_git_repo):
    """Create a sample tasks.yaml in the temp repo."""
    tasks_path = tmp_git_repo / "tasks.yaml"
    tasks = {
        "tasks": [
            {
                "id": 1,
                "key": "a1b2c3d4e5f6",
                "prompt": "Add a hello function",
                "status": "pending",
            },
        ]
    }
    tasks_path.write_text(yaml.dump(tasks))
    return tasks_path
