# Otto Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `otto`, a CLI tool that runs autonomous Claude Code agents against a task queue with tiered verification, branch-per-task isolation, and a candidate-commit verification pattern.

**Architecture:** Six Python modules with a clean dependency DAG: `cli → runner → verify → testgen`, with `runner → tasks` and `verify → config`. Each task gets its own git branch, verification runs in a disposable worktree against a candidate commit, and the exact verified tree is promoted on success.

**Tech Stack:** Python 3.11, click (CLI), PyYAML (config/tasks), claude-agent-sdk (agent execution), claude CLI (testgen via `claude -p`), pytest (testing)

**Working directory:** `/Users/yuxuan/work/cc-autonomous/.worktrees/v3/`

**Spec:** `docs/superpowers/specs/2026-03-13-otto-design.md`

---

## File Structure

```
otto/
  __init__.py       # Package marker, version string
  config.py         # Load/create otto.yaml, auto-detect test command + default branch
  tasks.py          # CRUD on tasks.yaml with flock, state transitions, key generation
  testgen.py        # Generate integration tests via claude -p, store in .git/otto/
  verify.py         # Tiered verification in disposable worktree
  runner.py         # Core loop: branch → agent → candidate commit → verify → merge/revert
  cli.py            # Click entrypoint: init, add, run, status, logs, retry, reset

tests/
  conftest.py       # Shared fixtures (temp git repos, sample configs)
  test_config.py    # Config loading, detection, defaults
  test_tasks.py     # Task CRUD, state transitions, key generation, file locking
  test_testgen.py   # Testgen prompt building, file output
  test_verify.py    # Tiered verification, disposable worktree, timeout handling
  test_runner.py    # Core loop, branch management, candidate commit, retry logic
  test_cli.py       # CLI argument parsing, subcommand wiring
```

---

## Chunk 1: Foundation

### Task 1: Project scaffolding and dependencies

**Files:**
- Create: `otto/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `pyproject.toml`

- [ ] **Step 1: Create pyproject.toml with dependencies**

```toml
[project]
name = "otto"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "click>=8.0",
    "pyyaml>=6.0",
    "claude-agent-sdk>=0.1.0",
]

[project.scripts]
otto = "otto.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create otto/__init__.py**

```python
"""Otto — Autonomous Claude Code agent runner."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Create tests/__init__.py (empty)**

- [ ] **Step 4: Create tests/conftest.py with shared fixtures**

```python
"""Shared test fixtures for otto tests."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml


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
```

- [ ] **Step 5: Install dependencies**

Run: `uv pip install pyyaml --python /Users/yuxuan/work/cc-autonomous/.venv/bin/python`
Run: `uv pip install -e . --python /Users/yuxuan/work/cc-autonomous/.venv/bin/python`

- [ ] **Step 6: Verify pytest runs with no tests**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -v`
Expected: "no tests ran" or similar (no errors)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml otto/__init__.py tests/__init__.py tests/conftest.py
git commit -m "feat: scaffold otto project with dependencies and test fixtures"
```

---

### Task 2: config.py — Configuration loading and detection

**Files:**
- Create: `otto/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for config loading**

```python
"""Tests for otto.config module."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from otto.config import (
    DEFAULT_CONFIG,
    create_config,
    detect_default_branch,
    detect_test_command,
    load_config,
)


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_git_repo):
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"test_command": "pytest", "max_retries": 5}))
        cfg = load_config(config_path)
        assert cfg["test_command"] == "pytest"
        assert cfg["max_retries"] == 5

    def test_fills_defaults_for_missing_keys(self, tmp_git_repo):
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"test_command": "pytest"}))
        cfg = load_config(config_path)
        assert cfg["max_retries"] == DEFAULT_CONFIG["max_retries"]
        assert cfg["model"] == DEFAULT_CONFIG["model"]
        assert cfg["verify_timeout"] == DEFAULT_CONFIG["verify_timeout"]

    def test_returns_defaults_when_file_missing(self, tmp_git_repo):
        cfg = load_config(tmp_git_repo / "otto.yaml")
        assert cfg == DEFAULT_CONFIG

    def test_loads_empty_file(self, tmp_git_repo):
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text("")
        cfg = load_config(config_path)
        assert cfg == DEFAULT_CONFIG


class TestDetectTestCommand:
    def test_detects_pytest(self, tmp_git_repo):
        (tmp_git_repo / "tests").mkdir()
        (tmp_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        result = detect_test_command(tmp_git_repo)
        assert result == "pytest"

    def test_detects_npm_test(self, tmp_git_repo):
        pkg = {"scripts": {"test": "jest"}}
        (tmp_git_repo / "package.json").write_text(json.dumps(pkg))
        result = detect_test_command(tmp_git_repo)
        assert result == "npm test"

    def test_returns_none_when_nothing_found(self, tmp_git_repo):
        result = detect_test_command(tmp_git_repo)
        assert result is None

    def test_returns_none_when_ambiguous(self, tmp_git_repo):
        (tmp_git_repo / "tests").mkdir()
        (tmp_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        pkg = {"scripts": {"test": "jest"}}
        (tmp_git_repo / "package.json").write_text(json.dumps(pkg))
        result = detect_test_command(tmp_git_repo)
        assert result is None


class TestDetectDefaultBranch:
    def test_detects_main(self, tmp_git_repo):
        result = detect_default_branch(tmp_git_repo)
        # git init creates 'main' by default on modern git
        assert result in ("main", "master")

    def test_returns_main_as_fallback(self, tmp_path):
        # Non-git directory
        result = detect_default_branch(tmp_path)
        assert result == "main"


class TestCreateConfig:
    def test_creates_config_file(self, tmp_git_repo):
        config_path = create_config(tmp_git_repo)
        assert config_path.exists()
        cfg = yaml.safe_load(config_path.read_text())
        assert "test_command" in cfg
        assert "default_branch" in cfg

    def test_updates_git_info_exclude(self, tmp_git_repo):
        create_config(tmp_git_repo)
        exclude_path = tmp_git_repo / ".git" / "info" / "exclude"
        content = exclude_path.read_text()
        assert "tasks.yaml" in content
        assert "otto_logs/" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_config' from 'otto.config'`

- [ ] **Step 3: Implement config.py**

```python
"""Otto configuration — load/create otto.yaml, auto-detect project settings."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "test_command": None,
    "max_retries": 3,
    "model": "sonnet",
    "project_dir": ".",
    "default_branch": "main",
    "verify_timeout": 300,
}


def load_config(config_path: Path) -> dict[str, Any]:
    """Load otto.yaml, filling missing keys with defaults."""
    if not config_path.exists():
        return dict(DEFAULT_CONFIG)
    raw = yaml.safe_load(config_path.read_text()) or {}
    return {**DEFAULT_CONFIG, **raw}


def detect_test_command(project_dir: Path) -> str | None:
    """Auto-detect the project's test command. Returns None if ambiguous or not found."""
    candidates = []

    # pytest
    tests_dir = project_dir / "tests"
    if tests_dir.is_dir() or (project_dir / "test").is_dir():
        candidates.append("pytest")

    # npm test
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            if "test" in pkg.get("scripts", {}):
                candidates.append("npm test")
        except (json.JSONDecodeError, KeyError):
            pass

    # go test
    if (project_dir / "go.mod").exists():
        candidates.append("go test ./...")

    # cargo test
    if (project_dir / "Cargo.toml").exists():
        candidates.append("cargo test")

    # make test
    makefile = project_dir / "Makefile"
    if makefile.exists() and "test:" in makefile.read_text():
        candidates.append("make test")

    if len(candidates) == 1:
        return candidates[0]
    return None  # Ambiguous or not found


def detect_default_branch(project_dir: Path) -> str:
    """Detect the default branch name. Fallback to 'main'."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # refs/remotes/origin/main → main
            return result.stdout.strip().split("/")[-1]
    except FileNotFoundError:
        pass

    # Fallback: check current branch name
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass

    return "main"


def create_config(project_dir: Path) -> Path:
    """Create otto.yaml with auto-detected settings. Updates .git/info/exclude."""
    test_cmd = detect_test_command(project_dir)
    default_branch = detect_default_branch(project_dir)

    config = {
        "test_command": test_cmd,
        "max_retries": DEFAULT_CONFIG["max_retries"],
        "model": DEFAULT_CONFIG["model"],
        "project_dir": str(DEFAULT_CONFIG["project_dir"]),
        "default_branch": default_branch,
        "verify_timeout": DEFAULT_CONFIG["verify_timeout"],
    }

    config_path = project_dir / "otto.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))

    # Update .git/info/exclude for runtime files
    exclude_path = project_dir / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    entries = ["tasks.yaml", "otto_logs/", "otto.lock"]
    to_add = [e for e in entries if e not in existing]
    if to_add:
        with open(exclude_path, "a") as f:
            f.write("\n# otto runtime files\n")
            for entry in to_add:
                f.write(f"{entry}\n")

    return config_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_config.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add otto/config.py tests/test_config.py
git commit -m "feat: add config module — load/create otto.yaml, auto-detect settings"
```

---

### Task 3: tasks.py — Task file CRUD with locking

**Files:**
- Create: `otto/tasks.py`
- Create: `tests/test_tasks.py`

- [ ] **Step 1: Write failing tests for task management**

```python
"""Tests for otto.tasks module."""

import threading
from pathlib import Path

import pytest
import yaml

from otto.tasks import (
    add_task,
    generate_key,
    load_tasks,
    save_tasks,
    update_task,
)


class TestGenerateKey:
    def test_returns_12_char_hex(self):
        key = generate_key(set())
        assert len(key) == 12
        assert all(c in "0123456789abcdef" for c in key)

    def test_unique_against_existing(self):
        existing = {generate_key(set()) for _ in range(100)}
        new_key = generate_key(existing)
        assert new_key not in existing


class TestLoadSaveTasks:
    def test_load_empty_file(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        tasks = load_tasks(path)
        assert tasks == []

    def test_round_trip(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        tasks = [{"id": 1, "key": "abc123def456", "prompt": "hello", "status": "pending"}]
        save_tasks(path, tasks)
        loaded = load_tasks(path)
        assert loaded == tasks


class TestAddTask:
    def test_adds_task_with_auto_id_and_key(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        task = add_task(path, "Build a login page")
        assert task["id"] == 1
        assert len(task["key"]) == 12
        assert task["prompt"] == "Build a login page"
        assert task["status"] == "pending"

    def test_increments_id(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        add_task(path, "First task")
        task2 = add_task(path, "Second task")
        assert task2["id"] == 2

    def test_custom_verify_and_retries(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        task = add_task(path, "Optimize", verify="python bench.py", max_retries=5)
        assert task["verify"] == "python bench.py"
        assert task["max_retries"] == 5


class TestUpdateTask:
    def test_updates_status(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        task = add_task(path, "Do something")
        updated = update_task(path, task["key"], status="running", attempts=1)
        assert updated["status"] == "running"
        assert updated["attempts"] == 1

    def test_raises_on_unknown_key(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        add_task(path, "Do something")
        with pytest.raises(KeyError):
            update_task(path, "nonexistent123", status="running")


class TestConcurrentAccess:
    def test_concurrent_adds_dont_lose_data(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        errors = []

        def add_one(i):
            try:
                add_task(path, f"Task {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_one, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        tasks = load_tasks(path)
        assert len(tasks) == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_tasks.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement tasks.py**

```python
"""Otto task management — CRUD on tasks.yaml with file locking."""

import fcntl
import os
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml


def generate_key(existing_keys: set[str]) -> str:
    """Generate a unique 12-char hex key."""
    while True:
        key = uuid.uuid4().hex[:12]
        if key not in existing_keys:
            return key


def load_tasks(tasks_path: Path) -> list[dict[str, Any]]:
    """Load tasks from tasks.yaml. Returns empty list if file doesn't exist."""
    if not tasks_path.exists():
        return []
    data = yaml.safe_load(tasks_path.read_text())
    if data is None or "tasks" not in data:
        return []
    return data["tasks"]


def save_tasks(tasks_path: Path, tasks: list[dict[str, Any]]) -> None:
    """Atomically write tasks to tasks.yaml."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(tasks_path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            yaml.dump({"tasks": tasks}, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, tasks_path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def _locked_rw(tasks_path: Path, mutator):
    """Read-modify-write tasks.yaml under flock."""
    lock_path = tasks_path.parent / ".tasks.lock"
    lock_path.touch()
    with open(lock_path, "r") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = load_tasks(tasks_path)
        result = mutator(tasks)
        save_tasks(tasks_path, tasks)
        return result


def add_task(
    tasks_path: Path,
    prompt: str,
    verify: str | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Add a new task to tasks.yaml. Thread-safe via flock."""
    def _add(tasks):
        existing_keys = {t["key"] for t in tasks if "key" in t}
        max_id = max((t.get("id", 0) for t in tasks), default=0)
        task: dict[str, Any] = {
            "id": max_id + 1,
            "key": generate_key(existing_keys),
            "prompt": prompt,
            "status": "pending",
        }
        if verify is not None:
            task["verify"] = verify
        if max_retries is not None:
            task["max_retries"] = max_retries
        tasks.append(task)
        return task

    return _locked_rw(tasks_path, _add)


def update_task(tasks_path: Path, key: str, **updates) -> dict[str, Any]:
    """Update a task by key. Thread-safe via flock. Raises KeyError if not found."""
    result = {}

    def _update(tasks):
        for task in tasks:
            if task.get("key") == key:
                task.update(updates)
                result.update(task)
                return
        raise KeyError(f"Task with key '{key}' not found")

    _locked_rw(tasks_path, _update)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_tasks.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add otto/tasks.py tests/test_tasks.py
git commit -m "feat: add tasks module — CRUD with flock, key generation, state transitions"
```

---

## Chunk 2: Verification

### Task 4: testgen.py — Integration test generation via claude -p

**Files:**
- Create: `otto/testgen.py`
- Create: `tests/test_testgen.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for otto.testgen module."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from otto.testgen import (
    build_testgen_prompt,
    detect_test_framework,
    test_file_path,
    generate_tests,
)


class TestDetectTestFramework:
    def test_detects_pytest(self, tmp_git_repo):
        (tmp_git_repo / "tests").mkdir()
        assert detect_test_framework(tmp_git_repo) == "pytest"

    def test_detects_jest(self, tmp_git_repo):
        (tmp_git_repo / "package.json").write_text('{"devDependencies":{"jest":"*"}}')
        assert detect_test_framework(tmp_git_repo) == "jest"

    def test_returns_none_when_unknown(self, tmp_git_repo):
        assert detect_test_framework(tmp_git_repo) is None


class TestTestFilePath:
    def test_pytest_path(self):
        p = test_file_path("pytest", "abc123def456")
        assert p == Path("tests/otto_verify_abc123def456.py")

    def test_jest_path(self):
        p = test_file_path("jest", "abc123def456")
        assert p == Path("__tests__/otto_verify_abc123def456.test.js")


class TestBuildTestgenPrompt:
    def test_contains_task_prompt(self):
        prompt = build_testgen_prompt("Add auth", "file1.py\nfile2.py", "pytest")
        assert "Add auth" in prompt
        assert "file1.py" in prompt
        assert "pytest" in prompt

    def test_instructs_hermetic_tests(self):
        prompt = build_testgen_prompt("Do stuff", "app.py", "pytest")
        assert "mock" in prompt.lower() or "hermetic" in prompt.lower()


class TestGenerateTests:
    @patch("otto.testgen.subprocess.run")
    def test_generates_test_file(self, mock_run, tmp_git_repo):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="file1.py\nfile2.py\n"),  # git ls-files
            MagicMock(returncode=0, stdout='def test_hello():\n    assert True\n'),  # claude -p
        ]
        key = "abc123def456"
        result = generate_tests(
            task_prompt="Add hello function",
            project_dir=tmp_git_repo,
            key=key,
        )
        assert result is not None
        assert result.exists()
        assert "test_hello" in result.read_text()
        # Verify stored under .git/otto/testgen/
        assert ".git/otto/testgen/" in str(result)

    @patch("otto.testgen.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, tmp_git_repo):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="file1.py\n"),  # git ls-files
            MagicMock(returncode=1, stdout="", stderr="error"),  # claude -p
        ]
        result = generate_tests(
            task_prompt="Do something",
            project_dir=tmp_git_repo,
            key="abc123def456",
        )
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_testgen.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement testgen.py**

```python
"""Otto test generation — generate integration tests via claude -p."""

import json
import re
import subprocess
from pathlib import Path

TESTGEN_TIMEOUT = 120  # seconds


def detect_test_framework(project_dir: Path) -> str | None:
    """Detect which test framework the project uses."""
    if (project_dir / "tests").is_dir() or (project_dir / "test").is_dir():
        return "pytest"
    if (project_dir / "package.json").exists():
        try:
            pkg = json.loads((project_dir / "package.json").read_text())
            deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
            if "jest" in deps or "vitest" in deps or "mocha" in deps:
                return "jest"
        except (json.JSONDecodeError, KeyError):
            pass
    if (project_dir / "go.mod").exists():
        return "go"
    if (project_dir / "Cargo.toml").exists():
        return "cargo"
    return None


def test_file_path(framework: str, key: str) -> Path:
    """Return the relative path for a generated test file."""
    match framework:
        case "pytest":
            return Path(f"tests/otto_verify_{key}.py")
        case "jest":
            return Path(f"__tests__/otto_verify_{key}.test.js")
        case "go":
            return Path(f"otto_verify_{key}_test.go")
        case "cargo":
            return Path(f"tests/otto_verify_{key}.rs")
        case _:
            return Path(f"tests/otto_verify_{key}.py")


def build_testgen_prompt(task_prompt: str, file_tree: str, framework: str) -> str:
    """Build the prompt for test generation."""
    return f"""You are a QA engineer writing integration tests for a coding task.

TASK: {task_prompt}

PROJECT FILES:
{file_tree}

TEST FRAMEWORK: {framework}

Write integration tests that verify the task was completed correctly.

Rules:
- Write behavioral tests that exercise the REAL system (build, run, check output)
- Tests must be hermetic and deterministic — no external network calls
- Mocks/fakes ONLY if the project already provides test fixtures for them
- Do NOT grep source code for strings — test actual behavior
- Output ONLY the test file contents, no explanation or markdown fences
- The tests should be runnable with the standard test command for {framework}
"""


def generate_tests(
    task_prompt: str,
    project_dir: Path,
    key: str,
) -> Path | None:
    """Generate integration tests via claude -p. Returns path to generated test file or None."""
    # Capture file tree
    try:
        tree_result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        file_tree = tree_result.stdout if tree_result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        file_tree = ""

    framework = detect_test_framework(project_dir) or "pytest"
    prompt = build_testgen_prompt(task_prompt, file_tree, framework)

    # Run claude -p via stdin (avoids ARG_MAX on large file trees)
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TESTGEN_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    # Extract code from markdown fences if present
    output = result.stdout.strip()
    fence_match = re.search(r"```(?:\w*)\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    # Write to .git/otto/testgen/<key>/
    testgen_dir = project_dir / ".git" / "otto" / "testgen" / key
    testgen_dir.mkdir(parents=True, exist_ok=True)

    rel_path = test_file_path(framework, key)
    out_file = testgen_dir / rel_path.name
    out_file.write_text(output)

    return out_file
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_testgen.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add otto/testgen.py tests/test_testgen.py
git commit -m "feat: add testgen module — generate integration tests via claude -p"
```

---

### Task 5: verify.py — Tiered verification in disposable worktree

**Files:**
- Create: `otto/verify.py`
- Create: `tests/test_verify.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for otto.verify module."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from otto.verify import (
    TierResult,
    VerifyResult,
    run_tier1,
    run_tier2,
    run_tier3,
    run_verification,
)


class TestRunTier1:
    def test_passes_when_tests_pass(self, tmp_git_repo):
        # Create a passing test
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_basic.py").write_text("def test_ok(): assert True\n")
        result = run_tier1(tmp_git_repo, "pytest", timeout=60)
        assert result.passed

    def test_fails_when_tests_fail(self, tmp_git_repo):
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_basic.py").write_text("def test_bad(): assert False\n")
        result = run_tier1(tmp_git_repo, "pytest", timeout=60)
        assert not result.passed
        assert result.output  # Should capture error output

    def test_skips_when_no_command(self, tmp_git_repo):
        result = run_tier1(tmp_git_repo, None, timeout=60)
        assert result.passed  # Skip = not a failure
        assert result.skipped


class TestRunTier2:
    def test_passes_with_passing_test(self, tmp_git_repo):
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "otto_verify_test123.py"
        test_file.write_text("def test_ok(): assert True\n")
        result = run_tier2(tmp_git_repo, test_file, "pytest", timeout=60)
        assert result.passed

    def test_fails_with_failing_test(self, tmp_git_repo):
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "otto_verify_test123.py"
        test_file.write_text("def test_bad(): assert False\n")
        result = run_tier2(tmp_git_repo, test_file, "pytest", timeout=60)
        assert not result.passed
        assert result.output

    def test_skips_when_no_file(self, tmp_git_repo):
        result = run_tier2(tmp_git_repo, None, "pytest", timeout=60)
        assert result.passed
        assert result.skipped


class TestRunTier3:
    def test_passes_on_exit_zero(self, tmp_git_repo):
        result = run_tier3(tmp_git_repo, "true", timeout=60)
        assert result.passed

    def test_fails_on_nonzero_exit(self, tmp_git_repo):
        result = run_tier3(tmp_git_repo, "false", timeout=60)
        assert not result.passed

    def test_fails_on_timeout(self, tmp_git_repo):
        result = run_tier3(tmp_git_repo, "sleep 10", timeout=1)
        assert not result.passed
        assert "timeout" in result.output.lower()


class TestRunVerification:
    def _make_commit(self, repo):
        """Helper: create a commit and return its SHA."""
        (repo / "hello.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True,
                        capture_output=True)
        subprocess.run(["git", "commit", "-m", "add hello"],
                        cwd=repo, check=True, capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_creates_and_cleans_up_worktree(self, tmp_git_repo):
        """Verify that a disposable worktree is created and removed."""
        head = self._make_commit(tmp_git_repo)
        result = run_verification(
            project_dir=tmp_git_repo,
            candidate_sha=head,
            test_command=None,
            testgen_file=None,
            verify_cmd=None,
            timeout=60,
        )
        assert result.passed
        # Worktree should be cleaned up
        wt_list = subprocess.run(
            ["git", "worktree", "list"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        ).stdout
        assert "otto-verify-" not in wt_list

    def test_first_failure_stops_chain(self, tmp_git_repo):
        """Tier 1 failure should prevent Tier 2 and Tier 3 from running."""
        head = self._make_commit(tmp_git_repo)
        # Create a failing test in the committed tree
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_fail.py").write_text("def test_bad(): assert False\n")
        subprocess.run(["git", "add", "tests/test_fail.py"], cwd=tmp_git_repo,
                        check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add failing test"],
                        cwd=tmp_git_repo, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        result = run_verification(
            project_dir=tmp_git_repo,
            candidate_sha=head,
            test_command="pytest",
            testgen_file=None,
            verify_cmd="echo should_not_run",
            timeout=60,
        )
        assert not result.passed
        # Only Tier 1 should have run
        assert len(result.tiers) == 1
        assert result.tiers[0].tier == "existing_tests"
        assert result.failure_output  # Should have meaningful content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_verify.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement verify.py**

```python
"""Otto verification — tiered verification in disposable worktree."""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TierResult:
    tier: str
    passed: bool
    output: str = ""
    skipped: bool = False


@dataclass
class VerifyResult:
    passed: bool
    tiers: list[TierResult] = field(default_factory=list)

    @property
    def failure_output(self) -> str:
        """Combined output from failed tiers, for feeding back to the agent."""
        parts = []
        for t in self.tiers:
            if not t.passed and not t.skipped:
                parts.append(f"=== {t.tier} FAILED ===\n{t.output}")
        return "\n\n".join(parts)


def run_tier1(workdir: Path, test_command: str | None, timeout: int) -> TierResult:
    """Run existing test suite."""
    if not test_command:
        return TierResult(tier="existing_tests", passed=True, skipped=True)
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,  # own process group for clean kill
        )
        return TierResult(
            tier="existing_tests",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="existing_tests",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


def run_tier2(
    workdir: Path,
    testgen_file: Path | None,
    test_command: str,
    timeout: int,
) -> TierResult:
    """Run generated integration tests in the worktree."""
    if not testgen_file or not testgen_file.exists():
        return TierResult(tier="generated_tests", passed=True, skipped=True)

    # Copy test file into worktree at the correct relative path
    # Use the file's name to place it in the right directory (e.g., tests/)
    from otto.testgen import test_file_path, detect_test_framework
    framework = detect_test_framework(workdir) or "pytest"
    # Extract key from filename (otto_verify_<key>.py)
    fname = testgen_file.name
    rel_path = None
    for fw in ("pytest", "jest", "go", "cargo"):
        candidate = test_file_path(fw, "PLACEHOLDER")
        if candidate.suffix == Path(fname).suffix:
            # Use the parent directory from the framework-specific path
            rel_path = candidate.parent / fname
            break
    if rel_path is None:
        rel_path = Path("tests") / fname

    dest = workdir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(testgen_file, dest)

    try:
        # Run just this test file
        if "pytest" in test_command:
            cmd = f"pytest {dest.relative_to(workdir)} -v"
        else:
            cmd = test_command
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return TierResult(
            tier="generated_tests",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="generated_tests",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


def run_tier3(workdir: Path, verify_cmd: str | None, timeout: int) -> TierResult:
    """Run custom verify command."""
    if not verify_cmd:
        return TierResult(tier="custom_verify", passed=True, skipped=True)
    try:
        result = subprocess.run(
            verify_cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            executable="/bin/bash",
            start_new_session=True,  # own process group for clean kill
        )
        return TierResult(
            tier="custom_verify",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="custom_verify",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


def run_verification(
    project_dir: Path,
    candidate_sha: str,
    test_command: str | None,
    testgen_file: Path | None,
    verify_cmd: str | None,
    timeout: int,
) -> VerifyResult:
    """Run all verification tiers in a disposable worktree."""
    tiers: list[TierResult] = []
    worktree_path = Path(tempfile.mkdtemp(prefix="otto-verify-"))

    try:
        # Create disposable worktree with detached HEAD
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), candidate_sha],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )

        # Tier 1: Existing tests
        t1 = run_tier1(worktree_path, test_command, timeout)
        tiers.append(t1)
        if not t1.passed and not t1.skipped:
            return VerifyResult(passed=False, tiers=tiers)

        # Tier 2: Generated tests
        t2 = run_tier2(worktree_path, testgen_file, test_command or "pytest", timeout)
        tiers.append(t2)
        if not t2.passed and not t2.skipped:
            return VerifyResult(passed=False, tiers=tiers)

        # Tier 3: Custom verify
        t3 = run_tier3(worktree_path, verify_cmd, timeout)
        tiers.append(t3)

        all_passed = all(t.passed for t in tiers)
        return VerifyResult(passed=all_passed, tiers=tiers)

    finally:
        # Always clean up the worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_dir,
            capture_output=True,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_verify.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add otto/verify.py tests/test_verify.py
git commit -m "feat: add verify module — tiered verification in disposable worktree"
```

---

## Chunk 3: Core Loop

### Task 6: runner.py — Core execution loop

**Files:**
- Create: `otto/runner.py`
- Create: `tests/test_runner.py`

- [ ] **Step 1: Write failing tests for git operations**

```python
"""Tests for otto.runner module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import yaml

from otto.runner import (
    check_clean_tree,
    create_task_branch,
    build_candidate_commit,
    merge_to_default,
    cleanup_branch,
    run_task,
)


class TestCheckCleanTree:
    def test_clean_repo_passes(self, tmp_git_repo):
        assert check_clean_tree(tmp_git_repo) is True

    def test_dirty_repo_fails(self, tmp_git_repo):
        (tmp_git_repo / "dirty.txt").write_text("dirty")
        subprocess.run(["git", "add", "dirty.txt"], cwd=tmp_git_repo, capture_output=True)
        assert check_clean_tree(tmp_git_repo) is False


class TestCreateTaskBranch:
    def test_creates_branch(self, tmp_git_repo):
        base_sha = create_task_branch(tmp_git_repo, "abc123def456", "main")
        # Verify we're on the new branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "otto/abc123def456"
        assert len(base_sha) == 40  # full SHA

    def test_recreates_stale_branch(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        # Should not raise — deletes and recreates
        base_sha = create_task_branch(tmp_git_repo, "abc123def456", "main")
        assert len(base_sha) == 40


class TestBuildCandidateCommit:
    def test_creates_candidate_with_changes(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        # Simulate agent changes
        (tmp_git_repo / "new_file.py").write_text("print('hello')\n")
        candidate = build_candidate_commit(tmp_git_repo, base_sha, testgen_file=None)
        assert candidate != base_sha
        assert len(candidate) == 40

    def test_includes_testgen_file(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        (tmp_git_repo / "new_file.py").write_text("print('hello')\n")
        # Create a fake testgen file
        testgen_dir = tmp_git_repo / ".git" / "otto" / "testgen" / "abc123def456"
        testgen_dir.mkdir(parents=True)
        testgen_file = testgen_dir / "otto_verify_abc123def456.py"
        testgen_file.write_text("def test_verify(): assert True\n")
        candidate = build_candidate_commit(tmp_git_repo, base_sha, testgen_file=testgen_file)
        # Verify test file is in the candidate
        show = subprocess.run(
            ["git", "show", f"{candidate}:tests/otto_verify_abc123def456.py"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        )
        assert show.returncode == 0


class TestMergeToDefault:
    def test_fast_forward_merge(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        (tmp_git_repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "otto: add feature (#1)"],
            cwd=tmp_git_repo, capture_output=True,
        )
        success = merge_to_default(tmp_git_repo, "abc123def456", "main")
        assert success
        # Should be on main now
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "main"


class TestCleanupBranch:
    def test_deletes_branch(self, tmp_git_repo):
        create_task_branch(tmp_git_repo, "abc123def456", "main")
        subprocess.run(["git", "checkout", "main"], cwd=tmp_git_repo, capture_output=True)
        cleanup_branch(tmp_git_repo, "abc123def456", "main")
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert "otto/abc123def456" not in branches
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement runner.py — git operations**

```python
"""Otto runner — core execution loop with branch management and verification."""

import asyncio
import fcntl
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from otto.config import load_config
from otto.tasks import load_tasks, update_task
from otto.testgen import generate_tests, detect_test_framework, test_file_path
from otto.verify import run_verification

logger = logging.getLogger("otto.runner")


def check_clean_tree(project_dir: Path) -> bool:
    """Check if the working tree is clean (no uncommitted changes)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and not result.stdout.strip()


def create_task_branch(
    project_dir: Path, key: str, default_branch: str,
    task: dict[str, Any] | None = None,
) -> str:
    """Create otto/<key> branch. Returns base SHA.

    If branch exists and was preserved from a diverge failure, raises RuntimeError.
    Otherwise deletes stale branch and recreates.
    """
    branch_name = f"otto/{key}"

    # Check if branch exists
    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=project_dir,
        capture_output=True,
    )
    if check.returncode == 0:
        # Check if this was preserved from a diverge failure (structured error_code)
        if task and task.get("status") == "failed" and task.get("error_code") == "merge_diverged":
            raise RuntimeError(
                f"Branch otto/{key} preserved from diverge failure — "
                f"manually resolve or run 'otto reset' first"
            )
        # Delete stale branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=project_dir,
            capture_output=True,
        )

    # Record base SHA
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Create and checkout new branch
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    return base_sha


def build_candidate_commit(
    project_dir: Path,
    base_sha: str,
    testgen_file: Path | None,
) -> str:
    """Build a candidate commit with agent changes + generated test."""
    # If agent made commits, squash them
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    if head != base_sha:
        # Agent made commits — squash
        subprocess.run(
            ["git", "reset", "--mixed", base_sha],
            cwd=project_dir, capture_output=True, check=True,
        )

    # Stage all agent changes explicitly (never git add -A per spec)
    # Stage modified/deleted tracked files
    subprocess.run(
        ["git", "add", "-u"],
        cwd=project_dir, capture_output=True, check=True,
    )
    # Stage untracked files (excluding ignored via .git/info/exclude)
    # Use -z for null-terminated output to handle filenames with special chars
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=project_dir, capture_output=True, text=True,
    )
    for f in untracked.stdout.split("\0"):
        if f:
            subprocess.run(
                ["git", "add", "--", f],
                cwd=project_dir, capture_output=True,
            )

    # Copy testgen file into project if available
    if testgen_file and testgen_file.exists():
        framework = detect_test_framework(project_dir) or "pytest"
        rel_path = test_file_path(framework, testgen_file.stem.replace("otto_verify_", ""))
        dest = project_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(testgen_file, dest)
        subprocess.run(
            ["git", "add", str(rel_path)],
            cwd=project_dir, capture_output=True, check=True,
        )

    # Create candidate commit
    subprocess.run(
        ["git", "commit", "-m", "otto: candidate commit", "--allow-empty"],
        cwd=project_dir, capture_output=True, check=True,
    )

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()


def merge_to_default(project_dir: Path, key: str, default_branch: str) -> bool:
    """Fast-forward merge task branch to default branch. Returns True on success."""
    branch_name = f"otto/{key}"
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True, check=True,
    )
    result = subprocess.run(
        ["git", "merge", "--ff-only", branch_name],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode == 0:
        # Delete merged branch
        subprocess.run(
            ["git", "branch", "-d", branch_name],
            cwd=project_dir, capture_output=True,
        )
        return True
    # Merge failed (branch diverged) — stay on default branch, preserve task branch
    return False


def cleanup_branch(project_dir: Path, key: str, default_branch: str = "main") -> None:
    """Delete a task branch. Checks out default_branch if on the task branch."""
    branch_name = f"otto/{key}"
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    ).stdout.strip()
    if current == branch_name:
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=project_dir, capture_output=True,
        )
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_dir, capture_output=True,
    )


async def run_task(
    task: dict[str, Any],
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path | None,
) -> bool:
    """Run a single task through the full loop. Returns True if passed."""
    key = task["key"]
    task_id = task["id"]
    prompt = task["prompt"]
    max_retries = task.get("max_retries", config["max_retries"])
    verify_cmd = task.get("verify")
    test_command = config.get("test_command")
    default_branch = config["default_branch"]
    timeout = config["verify_timeout"]

    # Create branch
    base_sha = create_task_branch(project_dir, key, default_branch, task=task)
    if tasks_file:
        update_task(tasks_file, key, status="running", attempts=0)

    # Start testgen concurrently
    testgen_task = asyncio.create_task(
        asyncio.to_thread(generate_tests, prompt, project_dir, key)
    )

    # Setup log directory
    log_dir = project_dir / "otto_logs" / key
    log_dir.mkdir(parents=True, exist_ok=True)

    session_id = None
    last_error = None  # verification failure output for retry feedback
    for attempt in range(max_retries + 1):
        attempt_num = attempt + 1
        logger.info(f"Task #{task_id} ({key}) — attempt {attempt_num}/{max_retries + 1}")

        if tasks_file:
            update_task(tasks_file, key, attempts=attempt_num)

        # Build agent prompt — on retries, use verification failure feedback
        if attempt == 0 or last_error is None:
            agent_prompt = (
                f"{prompt}\n\nYou are working in {project_dir}. Do NOT create git commits."
            )
        else:
            agent_prompt = (
                f"Verification failed. Fix the issue.\n\n"
                f"{last_error}\n\n"
                f"Original task: {prompt}\n\n"
                f"You are working in {project_dir}. Do NOT create git commits."
            )

        try:
            options = ClaudeAgentOptions(
                prompt=agent_prompt,
                options={
                    "dangerously_skip_permissions": True,
                    "cwd": str(project_dir),
                    "model": config["model"],
                },
            )
            if session_id:
                options.options["resume"] = session_id

            result = query(options)

            # Extract session_id for resume
            if hasattr(result, "session_id"):
                session_id = result.session_id
                if tasks_file:
                    update_task(tasks_file, key, session_id=session_id)

        except Exception as e:
            logger.error(f"Agent error: {e}")
            continue

        # Await testgen on first attempt
        testgen_file = None
        if attempt == 0:
            try:
                testgen_file = await asyncio.wait_for(testgen_task, timeout=120)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Testgen failed or timed out: {e}")
        else:
            if testgen_task.done():
                testgen_file = testgen_task.result()

        # Build candidate commit
        candidate_sha = build_candidate_commit(project_dir, base_sha, testgen_file)

        # Run verification in disposable worktree
        verify_result = run_verification(
            project_dir=project_dir,
            candidate_sha=candidate_sha,
            test_command=test_command,
            testgen_file=testgen_file,
            verify_cmd=verify_cmd,
            timeout=timeout,
        )

        # Write verification log
        verify_log = log_dir / f"attempt-{attempt_num}-verify.log"
        verify_log.write_text(
            "\n".join(f"{t.tier}: {'PASS' if t.passed else 'FAIL'}\n{t.output}"
                      for t in verify_result.tiers)
        )

        if verify_result.passed:
            # Amend commit message
            subprocess.run(
                ["git", "commit", "--amend", "-m",
                 f"otto: {prompt[:60]} (#{task_id})"],
                cwd=project_dir, capture_output=True,
            )
            # Merge to default
            if merge_to_default(project_dir, key, default_branch):
                if tasks_file:
                    update_task(tasks_file, key, status="passed")
                logger.info(f"Task #{task_id} PASSED — merged to {default_branch}")
                return True
            else:
                if tasks_file:
                    update_task(
                        tasks_file, key, status="failed",
                        error=f"branch diverged — otto/{key} preserved, manual rebase needed",
                        error_code="merge_diverged",
                    )
                logger.error(f"Task #{task_id} — merge failed (branch diverged)")
                return False
        else:
            # Verification failed — unwind candidate commit for retry
            subprocess.run(
                ["git", "reset", "--mixed", "HEAD~1"],
                cwd=project_dir, capture_output=True,
            )
            last_error = verify_result.failure_output
            logger.warning(
                f"Task #{task_id} attempt {attempt_num} — verification failed"
            )

    # All retries exhausted
    subprocess.run(["git", "reset", "--hard"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "checkout", default_branch],
        cwd=project_dir, capture_output=True,
    )
    cleanup_branch(project_dir, key, default_branch)
    # Clean up testgen artifacts for this task
    testgen_dir = project_dir / ".git" / "otto" / "testgen" / key
    if testgen_dir.exists():
        shutil.rmtree(testgen_dir, ignore_errors=True)
    if tasks_file:
        update_task(
            tasks_file, key, status="failed",
            error="max retries exhausted", error_code="max_retries",
        )
    logger.error(f"Task #{task_id} FAILED — all retries exhausted")
    return False


async def run_all(
    config: dict[str, Any],
    tasks_file: Path,
    project_dir: Path,
) -> int:
    """Run all pending tasks. Returns exit code (0=all passed, 1=any failed)."""
    default_branch = config["default_branch"]

    # Acquire process lock — use canonical repo root to prevent path aliasing
    # Use git-common-dir for lock (shared across linked worktrees)
    git_common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    lock_path = Path(git_common) / "otto.lock"
    lock_path.touch()
    lock_fh = open(lock_path, "r")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another otto process is running")
        return 2

    # Signal handling — set flag, cleanup in main loop (async-safe)
    current_task_key = None
    interrupted = False

    def _signal_handler(signum, frame):
        nonlocal interrupted
        interrupted = True

    old_sigint = signal.signal(signal.SIGINT, _signal_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    try:
        # Baseline check
        test_command = config.get("test_command")
        if test_command:
            logger.info("Running baseline check...")
            result = subprocess.run(
                test_command, shell=True, cwd=project_dir,
                capture_output=True, timeout=config["verify_timeout"],
            )
            if result.returncode != 0:
                logger.error("Baseline tests failing — fix before running otto")
                return 2

        # Process tasks
        tasks = load_tasks(tasks_file)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            logger.info("No pending tasks")
            return 0

        any_failed = False
        for task in pending:
            if interrupted:
                logger.warning("Interrupted — cleaning up")
                break
            current_task_key = task["key"]
            if not check_clean_tree(project_dir):
                logger.error("Working tree is dirty — aborting")
                return 2
            success = await run_task(task, config, project_dir, tasks_file)
            if not success:
                any_failed = True
            current_task_key = None

        # Cleanup on interruption
        if interrupted and current_task_key:
            subprocess.run(["git", "reset", "--hard"], cwd=project_dir, capture_output=True)
            subprocess.run(
                ["git", "checkout", default_branch],
                cwd=project_dir, capture_output=True,
            )
            cleanup_branch(project_dir, current_task_key, default_branch)
            if tasks_file:
                try:
                    update_task(
                        tasks_file, current_task_key,
                        status="failed", error="interrupted",
                        error_code="interrupted",
                    )
                except Exception:
                    pass
            return 1

        return 1 if any_failed else 0

    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: All tests PASS (git operation tests)

- [ ] **Step 5: Commit**

```bash
git add otto/runner.py tests/test_runner.py
git commit -m "feat: add runner module — core loop with branch management and verification"
```

---

## Chunk 4: CLI and Integration

### Task 7: cli.py — Click entrypoint

**Files:**
- Create: `otto/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement cli.py**

```python
"""Otto CLI — entrypoint for all otto commands."""

import asyncio
import logging
import sys
from pathlib import Path

import click

from otto.config import create_config, load_config
from otto.tasks import add_task, load_tasks, save_tasks, update_task


@click.group()
def main():
    """Otto — autonomous Claude Code agent runner."""
    pass


@main.command()
def init():
    """Initialize otto for this project."""
    project_dir = Path.cwd()
    config_path = create_config(project_dir)
    config = load_config(config_path)
    click.echo(f"Created {config_path}")
    click.echo(f"  test_command: {config['test_command'] or '(not detected)'}")
    click.echo(f"  default_branch: {config['default_branch']}")
    click.echo(f"  max_retries: {config['max_retries']}")
    click.echo(f"  model: {config['model']}")
    click.echo("\nCommit otto.yaml to share config with your team.")


@main.command()
@click.argument("prompt", required=False)
@click.option("--verify", default=None, help="Custom verification command")
@click.option("--max-retries", default=None, type=int, help="Max retry attempts")
@click.option("-f", "--file", "import_file", default=None, type=click.Path(exists=True),
              help="Import tasks from a YAML file")
def add(prompt, verify, max_retries, import_file):
    """Add a task to the queue (or import from file with -f)."""
    import yaml as _yaml

    tasks_path = Path.cwd() / "tasks.yaml"

    if import_file:
        data = _yaml.safe_load(Path(import_file).read_text()) or {}
        imported = data.get("tasks", [])
        for t in imported:
            task = add_task(tasks_path, t["prompt"],
                           verify=t.get("verify"), max_retries=t.get("max_retries"))
            click.echo(f"Added task #{task['id']} ({task['key']}): {t['prompt'][:60]}")
        click.echo(f"Imported {len(imported)} tasks")
        return

    if not prompt:
        click.echo("Error: provide a prompt or use -f to import", err=True)
        sys.exit(2)

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries)
    click.echo(f"Added task #{task['id']} ({task['key']}): {prompt}")


@main.command()
@click.argument("prompt", required=False)
@click.option("--dry-run", is_flag=True, help="Show what would run without executing")
def run(prompt, dry_run):
    """Run pending tasks (or a one-off task if prompt given)."""
    from otto.runner import run_all, run_task

    project_dir = Path.cwd()
    config_path = project_dir / "otto.yaml"
    if not config_path.exists():
        click.echo("Error: otto.yaml not found. Run 'otto init' first.", err=True)
        sys.exit(2)
    config = load_config(config_path)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if dry_run:
        tasks_path = project_dir / "tasks.yaml"
        tasks = load_tasks(tasks_path)
        pending = [t for t in tasks if t.get("status") == "pending"]
        click.echo(f"Config: {project_dir / 'otto.yaml'}")
        click.echo(f"  test_command: {config.get('test_command') or '(none)'}")
        click.echo(f"  model: {config['model']}")
        click.echo(f"  max_retries: {config['max_retries']}")
        click.echo(f"\nPending tasks: {len(pending)}")
        for t in pending:
            click.echo(f"  #{t['id']} ({t['key']}): {t['prompt'][:60]}")
        return

    if prompt:
        # One-off mode — adhoc-<timestamp>-<pid> per spec
        # Still acquires process lock to prevent concurrent runs
        import fcntl
        import os
        import time

        git_common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=project_dir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        lock_path = Path(git_common) / "otto.lock"
        lock_path.touch()
        lock_fh = open(lock_path, "r")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            click.echo("Another otto process is running", err=True)
            sys.exit(2)

        try:
            key = f"adhoc-{int(time.time())}-{os.getpid()}"
            task = {
                "id": 0,
                "key": key,
                "prompt": prompt,
                "status": "pending",
            }
            success = asyncio.run(
                run_task(task, config, project_dir, tasks_file=None)
            )
            sys.exit(0 if success else 1)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
    else:
        tasks_path = project_dir / "tasks.yaml"
        exit_code = asyncio.run(run_all(config, tasks_path, project_dir))
        sys.exit(exit_code)


@main.command()
def status():
    """Show task status."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    if not tasks:
        click.echo("No tasks found. Use 'otto add' to create one.")
        return

    # Simple table
    click.echo(f"{'ID':>4}  {'Key':12}  {'Status':10}  {'Att':>3}  Prompt")
    click.echo("-" * 70)
    for t in tasks:
        click.echo(
            f"{t.get('id', '?'):>4}  {t.get('key', '?'):12}  "
            f"{t.get('status', '?'):10}  {t.get('attempts', 0):>3}  "
            f"{t['prompt'][:40]}"
        )


@main.command()
@click.argument("task_id", type=int)
def retry(task_id):
    """Reset a failed task to pending."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            if t.get("status") != "failed":
                click.echo(
                    f"Task #{task_id} is '{t.get('status')}', not 'failed'", err=True
                )
                sys.exit(1)
            update_task(
                tasks_path, t["key"],
                status="pending", attempts=0, session_id=None, error=None,
            )
            click.echo(f"Reset task #{task_id} to pending")
            return
    click.echo(f"Task #{task_id} not found", err=True)
    sys.exit(1)


@main.command()
@click.argument("task_id", type=int)
def logs(task_id):
    """Show logs for a task."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        if t.get("id") == task_id:
            log_dir = Path.cwd() / "otto_logs" / t["key"]
            if not log_dir.exists():
                click.echo(f"No logs for task #{task_id}")
                return
            for log_file in sorted(log_dir.iterdir()):
                click.echo(f"\n=== {log_file.name} ===")
                click.echo(log_file.read_text())
            return
    click.echo(f"Task #{task_id} not found", err=True)
    sys.exit(1)


@main.command()
@click.option("--yes", is_flag=True, help="Skip confirmation")
def reset(yes):
    """Reset all tasks and clean up branches."""
    if not yes:
        click.confirm("Reset all tasks to pending and delete otto/* branches?", abort=True)

    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    for t in tasks:
        t["status"] = "pending"
        t.pop("attempts", None)
        t.pop("session_id", None)
        t.pop("error", None)
    save_tasks(tasks_path, tasks)

    # Delete otto/* branches
    import subprocess
    result = subprocess.run(
        ["git", "branch", "--list", "otto/*"],
        capture_output=True, text=True,
    )
    for branch in result.stdout.strip().split("\n"):
        branch = branch.strip()
        if branch:
            subprocess.run(["git", "branch", "-D", branch], capture_output=True)

    # Clean logs
    import shutil
    log_dir = Path.cwd() / "otto_logs"
    if log_dir.exists():
        shutil.rmtree(log_dir)

    # Clean testgen artifacts
    testgen_dir = Path.cwd() / ".git" / "otto"
    if testgen_dir.exists():
        shutil.rmtree(testgen_dir)

    click.echo(f"Reset {len(tasks)} tasks to pending. Cleaned branches, logs, and testgen.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add otto/cli.py tests/test_cli.py
git commit -m "feat: add CLI module — init, add, run, status, logs, retry, reset"
```

---

### Task 8: Integration test — end-to-end flow

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write an integration test that exercises the full flow**

```python
"""Integration test — end-to-end otto flow with mocked agent."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from otto.config import create_config, load_config
from otto.runner import run_all
from otto.tasks import add_task, load_tasks


class TestEndToEnd:
    @patch("otto.runner.query")
    @patch("otto.runner.generate_tests")
    def test_task_passes_and_merges(
        self, mock_testgen, mock_query, tmp_git_repo
    ):
        """Full flow: add task → run → verify → merge to main."""
        # Setup: create config, add a task
        create_config(tmp_git_repo)
        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = None  # Skip baseline and tier 1
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Create hello.py that prints hello")

        # Mock agent: simulate creating a file
        def fake_agent(options):
            (tmp_git_repo / "hello.py").write_text("print('hello')\n")
            result = MagicMock()
            result.session_id = "test-session-123"
            return result

        mock_query.side_effect = fake_agent
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

    @patch("otto.runner.query")
    @patch("otto.runner.generate_tests")
    def test_task_fails_and_reverts(
        self, mock_testgen, mock_query, tmp_git_repo
    ):
        """Task fails all retries → branch deleted, main untouched."""
        create_config(tmp_git_repo)
        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = "false"  # Always fails
        config["max_retries"] = 0
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Do something that fails verification")

        def fake_agent(options):
            (tmp_git_repo / "bad.py").write_text("broken\n")
            return MagicMock(session_id="s1")

        mock_query.side_effect = fake_agent
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
```

- [ ] **Step 2: Run integration tests**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full test suite**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration test for otto flow"
```

---

### Task 9: Final polish

- [ ] **Step 1: Verify `otto` CLI works as installed command**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/otto --help`
Expected: Shows help with all subcommands (init, add, run, status, logs, retry, reset)

- [ ] **Step 2: Run full test suite one final time**

Run: `/Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 3: Commit any final fixes**

```bash
git add otto/ tests/ pyproject.toml
git commit -m "chore: final polish and cleanup"
```
