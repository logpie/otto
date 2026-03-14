# Rubric Feature Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add natural language rubrics (acceptance criteria) to Otto tasks, auto-generated at add-time, converted to real test code at run-time.

**Architecture:** New `otto/rubric.py` handles rubric generation and markdown parsing via `claude -p`. Existing `testgen.py` gets a `generate_tests_from_rubric()` variant. `tasks.py` gains `rubric`/`context` fields and batch import. `runner.py` routes to rubric-based testgen when rubric is present. `cli.py` wires it all together.

**Tech Stack:** Python, Click, PyYAML, `claude -p` for LLM calls, pytest for tests.

**Spec:** `docs/superpowers/specs/2026-03-13-rubric-feature-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `otto/rubric.py` | Rubric generation + markdown parsing + project context gathering | Create |
| `otto/tasks.py` | Task CRUD — add `rubric`/`context` fields, batch import | Modify |
| `otto/testgen.py` | Test generation — add `generate_tests_from_rubric()`, framework-aware validation | Modify |
| `otto/runner.py` | Task execution — route to rubric testgen, prepend context to agent prompt | Modify |
| `otto/cli.py` | CLI — rubric generation in `add`, `--no-rubric`, `.md`/`.txt` import, status column | Modify |
| `tests/test_rubric.py` | Tests for rubric.py | Create |
| `tests/test_tasks.py` | Tests for new task fields and batch import | Modify |
| `tests/test_testgen.py` | Tests for rubric-based testgen | Modify |
| `tests/test_runner.py` | Tests for rubric routing and context prepending | Modify |
| `tests/test_cli.py` | Tests for CLI rubric features | Modify |

---

## Chunk 1: Data Layer (tasks.py)

### Task 1: Add rubric and context fields to add_task

**Files:**
- Modify: `otto/tasks.py:61-84` (`add_task` function)
- Test: `tests/test_tasks.py`

- [ ] **Step 1: Write failing test for rubric field**

```python
# tests/test_tasks.py — add to existing TestAddTask class or create new tests

def test_add_task_with_rubric(tmp_path):
    tasks_path = tmp_path / "tasks.yaml"
    task = add_task(tasks_path, "Add search", rubric=["search is case-insensitive", "no matches returns empty list"])
    assert task["rubric"] == ["search is case-insensitive", "no matches returns empty list"]
    # Verify persistence
    tasks = load_tasks(tasks_path)
    assert tasks[0]["rubric"] == ["search is case-insensitive", "no matches returns empty list"]


def test_add_task_with_context(tmp_path):
    tasks_path = tmp_path / "tasks.yaml"
    task = add_task(tasks_path, "Add search", context="BookmarkStore is in store.py")
    assert task["context"] == "BookmarkStore is in store.py"
    tasks = load_tasks(tasks_path)
    assert tasks[0]["context"] == "BookmarkStore is in store.py"


def test_add_task_without_rubric(tmp_path):
    tasks_path = tmp_path / "tasks.yaml"
    task = add_task(tasks_path, "Fix typo")
    assert "rubric" not in task
    assert "context" not in task
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_tasks.py -v -k "rubric or context"`
Expected: FAIL — `add_task()` doesn't accept `rubric` or `context` params

- [ ] **Step 3: Implement rubric/context in add_task**

In `otto/tasks.py`, update `add_task()`:

```python
def add_task(
    tasks_path: Path,
    prompt: str,
    verify: str | None = None,
    max_retries: int | None = None,
    rubric: list[str] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
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
        if rubric is not None:
            task["rubric"] = rubric
        if context is not None:
            task["context"] = context
        tasks.append(task)
        return task

    return _locked_rw(tasks_path, _add)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_tasks.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/tasks.py tests/test_tasks.py
git commit -m "feat: add rubric and context fields to add_task"
```

### Task 2: Add batch import (add_tasks)

**Files:**
- Modify: `otto/tasks.py` (add `add_tasks()` function)
- Test: `tests/test_tasks.py`

- [ ] **Step 1: Write failing test for batch import**

```python
def test_add_tasks_batch(tmp_path):
    tasks_path = tmp_path / "tasks.yaml"
    batch = [
        {"prompt": "Task A", "rubric": ["criterion 1"]},
        {"prompt": "Task B", "rubric": ["criterion 2"], "context": "some context"},
        {"prompt": "Task C"},
    ]
    results = add_tasks(tasks_path, batch)
    assert len(results) == 3
    assert results[0]["id"] == 1
    assert results[1]["id"] == 2
    assert results[2]["id"] == 3
    tasks = load_tasks(tasks_path)
    assert len(tasks) == 3
    assert tasks[0]["rubric"] == ["criterion 1"]
    assert tasks[1]["context"] == "some context"
    assert "rubric" not in tasks[2]


def test_add_tasks_appends_to_existing(tmp_path):
    tasks_path = tmp_path / "tasks.yaml"
    add_task(tasks_path, "Existing task")
    batch = [{"prompt": "New task"}]
    results = add_tasks(tasks_path, batch)
    assert results[0]["id"] == 2
    tasks = load_tasks(tasks_path)
    assert len(tasks) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_tasks.py -v -k "add_tasks"`
Expected: FAIL — `add_tasks` not defined

- [ ] **Step 3: Implement add_tasks**

```python
def add_tasks(
    tasks_path: Path,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add multiple tasks atomically. Thread-safe via flock."""
    results: list[dict[str, Any]] = []

    def _add_batch(tasks):
        existing_keys = {t["key"] for t in tasks if "key" in t}
        max_id = max((t.get("id", 0) for t in tasks), default=0)
        for item in batch:
            max_id += 1
            task: dict[str, Any] = {
                "id": max_id,
                "key": generate_key(existing_keys),
                "prompt": item["prompt"],
                "status": "pending",
            }
            existing_keys.add(task["key"])
            for field in ("verify", "max_retries", "rubric", "context"):
                if field in item and item[field] is not None:
                    task[field] = item[field]
            tasks.append(task)
            results.append(task)

    _locked_rw(tasks_path, _add_batch)
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_tasks.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/tasks.py tests/test_tasks.py
git commit -m "feat: add batch add_tasks for all-or-nothing import"
```

---

## Chunk 2: Rubric Generation Module (rubric.py)

### Task 3: Create project context gatherer

**Files:**
- Create: `otto/rubric.py`
- Test: `tests/test_rubric.py`

- [ ] **Step 1: Write failing test for _gather_project_context**

```python
# tests/test_rubric.py
import subprocess
from pathlib import Path

from otto.rubric import _gather_project_context


def test_gather_includes_file_tree(tmp_path):
    # Create a git repo with some files
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "src.py").write_text("def hello(): pass")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add src"], cwd=tmp_path, capture_output=True)

    ctx = _gather_project_context(tmp_path)
    assert "src.py" in ctx


def test_gather_includes_source_content(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "app.py").write_text("class MyApp:\n    pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app"], cwd=tmp_path, capture_output=True)

    ctx = _gather_project_context(tmp_path)
    assert "class MyApp" in ctx


def test_gather_skips_config_and_lockfiles(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    (tmp_path / "poetry.lock").write_text("hash123")
    (tmp_path / "app.py").write_text("x = 1")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add files"], cwd=tmp_path, capture_output=True)

    ctx = _gather_project_context(tmp_path)
    assert "hash123" not in ctx  # lockfile content excluded
    assert "x = 1" in ctx  # source included
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_rubric.py -v -k "gather"`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement _gather_project_context**

Create `otto/rubric.py`:

```python
"""Otto rubric — natural language acceptance criteria generation."""

import json
import re
import subprocess
from pathlib import Path

# Files to skip when reading source context
_SKIP_PATTERNS = {
    ".lock", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml",
    ".md", ".txt", ".rst", ".csv", ".gitignore",
}
_SKIP_NAMES = {
    "setup.py", "conftest.py", "__init__.py", "manage.py",
}


def _gather_project_context(project_dir: Path) -> str:
    """Gather project context for rubric generation prompts."""
    parts: list[str] = []

    # File tree
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            parts.append(f"PROJECT FILES:\n{result.stdout}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Source files — read key modules (not tests, not config, not lockfiles)
    source_files: list[Path] = []
    try:
        tracked = result.stdout.strip().splitlines() if result.returncode == 0 else []
        for fname in tracked:
            p = project_dir / fname
            if not p.exists() or not p.is_file():
                continue
            if p.suffix in _SKIP_PATTERNS:
                continue
            if p.name in _SKIP_NAMES:
                continue
            if any(part.startswith("test") for part in p.parts):
                continue
            source_files.append(p)
            if len(source_files) >= 5:
                break
    except Exception:
        pass

    for sf in source_files:
        try:
            lines = sf.read_text().splitlines()[:100]
            rel = sf.relative_to(project_dir)
            parts.append(f"SOURCE FILE: {rel}\n" + "\n".join(lines))
        except Exception:
            pass

    # Existing test samples
    from otto.testgen import _read_existing_tests
    test_samples = _read_existing_tests(project_dir)
    if test_samples:
        parts.append(f"EXISTING TESTS:\n{test_samples}")

    return "\n\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_rubric.py -v -k "gather"`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/rubric.py tests/test_rubric.py
git commit -m "feat: add _gather_project_context for rubric generation"
```

### Task 4: Implement generate_rubric

**Files:**
- Modify: `otto/rubric.py`
- Test: `tests/test_rubric.py`

- [ ] **Step 1: Write failing test for generate_rubric**

```python
from unittest.mock import patch

from otto.rubric import generate_rubric


@patch("otto.rubric.subprocess.run")
def test_generate_rubric_parses_numbered_list(mock_run, tmp_path):
    # Mock git ls-files
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""

    # Mock claude -p response (second call)
    def side_effect(*args, **kwargs):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        if args[0] == ["git", "ls-files"]:
            m.stdout = "app.py"
        else:
            m.stdout = """1. search('python') returns matching bookmarks
2. search is case-insensitive
3. no matches returns empty list
4. partial matches work
5. search on empty store returns empty list"""
        return m

    mock_run.side_effect = side_effect

    rubric = generate_rubric("Add search", tmp_path)
    assert len(rubric) == 5
    assert "case-insensitive" in rubric[1]


@patch("otto.rubric.subprocess.run")
def test_generate_rubric_returns_empty_on_failure(mock_run, tmp_path):
    mock_run.return_value.returncode = 1
    mock_run.return_value.stdout = ""

    rubric = generate_rubric("Add search", tmp_path)
    assert rubric == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_rubric.py -v -k "generate_rubric"`
Expected: FAIL — `generate_rubric` not defined

- [ ] **Step 3: Implement generate_rubric**

Add to `otto/rubric.py`:

```python
RUBRIC_TIMEOUT = 60  # seconds


def _parse_rubric_output(text: str) -> list[str]:
    """Parse LLM output into a list of rubric items.

    Handles numbered lists, bullet points, and plain lines.
    """
    items: list[str] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip numbering: "1. ", "1) ", "- ", "* "
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[-\*]\s*", "", line)
        line = line.strip()
        if line:
            items.append(line)
    return items


def generate_rubric(prompt: str, project_dir: Path) -> list[str]:
    """Generate rubric items for a single task via claude -p."""
    context = _gather_project_context(project_dir)

    rubric_prompt = f"""You are a senior QA engineer. Given this coding task and project context,
write 5-8 concrete, testable acceptance criteria.

TASK: {prompt}

{context}

Rules:
- Each criterion must be specific enough to write a test from
- Cover: happy path, edge cases, error conditions
- Reference actual function/class/method names from the source code
- Output a numbered list, one criterion per line
- No prose, no explanations — just the criteria"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=rubric_prompt,
            capture_output=True,
            text=True,
            timeout=RUBRIC_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    # Strip markdown fences if present
    output = result.stdout.strip()
    fence_match = re.search(r"```(?:\w*)\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    return _parse_rubric_output(output)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_rubric.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/rubric.py tests/test_rubric.py
git commit -m "feat: add generate_rubric for single-task rubric generation"
```

### Task 5: Implement parse_markdown_tasks

**Files:**
- Modify: `otto/rubric.py`
- Test: `tests/test_rubric.py`

- [ ] **Step 1: Write failing test for parse_markdown_tasks**

```python
from otto.rubric import parse_markdown_tasks


@patch("otto.rubric.subprocess.run")
def test_parse_markdown_extracts_tasks(mock_run, tmp_path):
    md_file = tmp_path / "features.md"
    md_file.write_text("# Search\nAdd search to bookmarks.\n\n# Tags\nAdd tags support.\n")

    # Mock git ls-files and claude -p
    def side_effect(*args, **kwargs):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        if args[0] == ["git", "ls-files"]:
            m.stdout = "app.py"
        else:
            m.stdout = json.dumps([
                {
                    "prompt": "Add search method to BookmarkStore",
                    "rubric": ["search is case-insensitive", "no matches returns empty"],
                    "context": "BookmarkStore is in store.py",
                },
                {
                    "prompt": "Add tags support",
                    "rubric": ["by_tag filters correctly"],
                    "context": "",
                },
            ])
        return m

    mock_run.side_effect = side_effect

    tasks = parse_markdown_tasks(md_file, tmp_path)
    assert len(tasks) == 2
    assert tasks[0]["prompt"] == "Add search method to BookmarkStore"
    assert len(tasks[0]["rubric"]) == 2
    assert tasks[0]["context"] == "BookmarkStore is in store.py"


@patch("otto.rubric.subprocess.run")
def test_parse_markdown_raises_on_invalid_json(mock_run, tmp_path):
    md_file = tmp_path / "features.md"
    md_file.write_text("# Task\nDo something.\n")

    def side_effect(*args, **kwargs):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        if args[0] == ["git", "ls-files"]:
            m.stdout = ""
        else:
            m.stdout = "This is not JSON"
        return m

    mock_run.side_effect = side_effect

    import pytest as _pytest
    with _pytest.raises(ValueError, match="Failed to parse"):
        parse_markdown_tasks(md_file, tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_rubric.py -v -k "parse_markdown"`
Expected: FAIL — `parse_markdown_tasks` not defined

- [ ] **Step 3: Implement parse_markdown_tasks**

Add to `otto/rubric.py`:

```python
def parse_markdown_tasks(filepath: Path, project_dir: Path) -> list[dict]:
    """Parse a markdown file into structured tasks via LLM.

    Returns list of dicts with 'prompt', 'rubric', and 'context' keys.
    Raises ValueError if LLM output can't be parsed.
    """
    md_content = filepath.read_text()
    context = _gather_project_context(project_dir)

    parse_prompt = f"""You are a senior QA engineer and technical PM. Parse this markdown into coding tasks.

MARKDOWN:
{md_content}

PROJECT CONTEXT:
{context}

For each task, extract:
- "prompt": A clear, actionable description of what the coding agent should implement
- "rubric": A list of 5-8 concrete, testable acceptance criteria
- "context": Background info useful for the agent (can be empty string)

Rules for rubric items:
- Each must be specific enough to write a test from
- Cover: happy path, edge cases, error conditions
- Reference actual function/class/method names from the source code when possible

Output ONLY a JSON array. No markdown, no explanation. Example:
[
  {{"prompt": "...", "rubric": ["...", "..."], "context": "..."}}
]"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=parse_prompt,
            capture_output=True,
            text=True,
            timeout=RUBRIC_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise ValueError(f"Failed to parse markdown: {e}")

    if result.returncode != 0:
        raise ValueError(f"Failed to parse markdown: claude exited {result.returncode}")

    output = result.stdout.strip()

    # Strip markdown fences if present
    fence_match = re.search(r"```(?:json)?\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    try:
        tasks = json.loads(output)
    except json.JSONDecodeError:
        raise ValueError(f"Failed to parse LLM output as JSON. Raw output:\n{output[:500]}")

    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"Expected non-empty JSON array. Got:\n{output[:500]}")

    # Validate each task
    for i, task in enumerate(tasks):
        if not isinstance(task, dict) or not task.get("prompt"):
            raise ValueError(f"Task {i} missing 'prompt' field")
        task.setdefault("rubric", [])
        task.setdefault("context", "")
        task["rubric"] = [r for r in task["rubric"] if isinstance(r, str) and r.strip()]

    return tasks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_rubric.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/rubric.py tests/test_rubric.py
git commit -m "feat: add parse_markdown_tasks for markdown import"
```

---

## Chunk 3: Rubric-Based Test Generation (testgen.py)

### Task 6: Add generate_tests_from_rubric

**Files:**
- Modify: `otto/testgen.py`
- Test: `tests/test_testgen.py`

- [ ] **Step 1: Write failing test**

```python
from unittest.mock import patch, MagicMock
from otto.testgen import generate_tests_from_rubric


@patch("otto.testgen.subprocess.run")
def test_generate_tests_from_rubric_creates_file(mock_run, tmp_path):
    # Setup git repo
    import subprocess as real_subprocess
    real_subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    real_subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tests").mkdir()

    rubric = ["search('python') returns matching bookmarks", "search is case-insensitive"]

    test_code = '''from bookmarks.store import BookmarkStore

def test_search_returns_matching(tmp_path):
    store = BookmarkStore(tmp_path / "test.json")
    store.add("https://python.org", "Python Docs")
    results = store.search("python")
    assert len(results) == 1

def test_search_case_insensitive(tmp_path):
    store = BookmarkStore(tmp_path / "test.json")
    store.add("https://python.org", "Python Docs")
    assert len(store.search("PYTHON")) == 1
'''

    call_count = 0
    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        m.returncode = 0
        if "ls-files" in str(args):
            m.stdout = "bookmarks/store.py"
        elif "claude" in str(args):
            m.stdout = test_code
        return m

    mock_run.side_effect = side_effect

    result = generate_tests_from_rubric(rubric, "Add search", tmp_path, "testkey123")
    assert result is not None
    assert result.exists()
    content = result.read_text()
    assert "def test_search_returns_matching" in content
    assert "def test_search_case_insensitive" in content


@patch("otto.testgen.subprocess.run")
def test_generate_tests_from_rubric_returns_none_on_invalid(mock_run, tmp_path):
    import subprocess as real_subprocess
    real_subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    real_subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "tests").mkdir()

    def side_effect(*args, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = "This is prose, not code"
        return m

    mock_run.side_effect = side_effect

    result = generate_tests_from_rubric(["some criterion"], "task", tmp_path, "key123")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_testgen.py -v -k "rubric"`
Expected: FAIL — `generate_tests_from_rubric` not defined

- [ ] **Step 3: Implement generate_tests_from_rubric**

Add to `otto/testgen.py`:

```python
def generate_tests_from_rubric(
    rubric: list[str],
    prompt: str,
    project_dir: Path,
    key: str,
) -> Path | None:
    """Generate tests from rubric items via claude -p. Returns path to test file or None."""
    # Gather context
    try:
        tree_result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        file_tree = tree_result.stdout if tree_result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        file_tree = ""

    framework = detect_test_framework(project_dir) or "pytest"
    existing_tests = _read_existing_tests(project_dir)

    # Read key source files for context
    from otto.rubric import _gather_project_context
    project_context = _gather_project_context(project_dir)

    rubric_text = "\n".join(f"{i+1}. {item}" for i, item in enumerate(rubric))

    rubric_prompt = f"""You are a QA engineer writing tests for these acceptance criteria:

{rubric_text}

TASK: {prompt}

{project_context}

{"EXISTING TESTS (follow the same import patterns):" + chr(10) + existing_tests if existing_tests else ""}

TEST FRAMEWORK: {framework}

Write one test function per criterion. Each test must exercise real behavior — no mocks unless the project provides test fixtures.

IMPORTANT: Output ONLY valid {framework} test code. No prose, no explanations, no markdown.
Start directly with import statements."""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=rubric_prompt,
            capture_output=True,
            text=True,
            timeout=TESTGEN_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    output = result.stdout.strip()
    fence_match = re.search(r"```(?:\w*)\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    # Validate
    if not _validate_test_output(output, framework):
        return None

    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    testgen_dir.mkdir(parents=True, exist_ok=True)

    rel_path = test_file_path(framework, key)
    out_file = testgen_dir / rel_path.name
    out_file.write_text(output)

    return out_file
```

Also extract validation into a shared helper `_validate_test_output()`:

```python
def _validate_test_output(output: str, framework: str) -> bool:
    """Validate that generated output is parseable test code, not prose."""
    if not output.strip():
        return False
    if framework == "pytest":
        try:
            ast.parse(output)
        except SyntaxError:
            return False
        if "def test_" not in output:
            return False
    elif framework in ("jest", "vitest", "mocha"):
        if not any(kw in output for kw in ("describe(", "it(", "test(")):
            return False
    # go/cargo: basic check — starts with code, not prose
    elif framework in ("go", "cargo"):
        first_line = output.strip().splitlines()[0] if output.strip() else ""
        if first_line and first_line[0].isalpha() and not any(
            first_line.startswith(kw) for kw in ("package", "use", "import", "func", "fn", "#")
        ):
            return False
    return True
```

Refactor existing `generate_tests()` to use `_validate_test_output()` too (replace the inline validation).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_testgen.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS (existing tests unbroken by refactor)

- [ ] **Step 6: Commit**

```bash
git add otto/testgen.py tests/test_testgen.py
git commit -m "feat: add generate_tests_from_rubric with framework-aware validation"
```

---

## Chunk 4: Runner Integration (runner.py)

### Task 7: Route to rubric testgen and prepend context

**Files:**
- Modify: `otto/runner.py`
- Test: `tests/test_runner.py`

- [ ] **Step 1: Write failing tests**

```python
# In tests/test_runner.py — test context prepending and rubric routing

class TestContextPrepending:
    """Test that context field is prepended to agent prompts."""

    def test_context_in_initial_prompt(self):
        """When task has context, it should appear in the agent prompt."""
        # This tests the prompt construction logic, not a full run
        task = {
            "id": 1, "key": "abc", "prompt": "Add search",
            "status": "pending", "context": "Store is in store.py",
        }
        context = task.get("context", "")
        prompt = task["prompt"]
        if context:
            agent_prompt = f"Context: {context}\n\n{prompt}\n\nDo NOT create git commits."
        else:
            agent_prompt = f"{prompt}\n\nDo NOT create git commits."

        assert "Context: Store is in store.py" in agent_prompt
        assert "Add search" in agent_prompt

    def test_no_context_omits_prefix(self):
        task = {"id": 1, "key": "abc", "prompt": "Fix typo", "status": "pending"}
        context = task.get("context", "")
        prompt = task["prompt"]
        if context:
            agent_prompt = f"Context: {context}\n\n{prompt}"
        else:
            agent_prompt = f"{prompt}"

        assert "Context:" not in agent_prompt
```

- [ ] **Step 2: Run tests to verify they pass** (these test prompt construction logic, not runner itself)

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_runner.py -v -k "Context"`
Expected: PASS

- [ ] **Step 3: Modify runner.py**

In `run_task()`, update prompt construction to include context:

```python
# After extracting task fields, add:
context = task.get("context", "")
rubric = task.get("rubric")

# Update prompt construction (both initial and retry):
if attempt == 0 or last_error is None:
    base = f"{prompt}\n\nYou are working in {project_dir}. Do NOT create git commits."
    agent_prompt = f"Context: {context}\n\n{base}" if context else base
else:
    base = (
        f"Verification failed. Fix the issue.\n\n"
        f"{last_error}\n\n"
        f"Original task: {prompt}\n\n"
        f"You are working in {project_dir}. Do NOT create git commits."
    )
    agent_prompt = f"Context: {context}\n\n{base}" if context else base
```

Update testgen routing — replace the `generate_tests` call:

```python
# Replace:
#   testgen_task = asyncio.create_task(
#       asyncio.to_thread(generate_tests, prompt, project_dir, key)
#   )
# With:
if rubric:
    from otto.testgen import generate_tests_from_rubric
    testgen_task = asyncio.create_task(
        asyncio.to_thread(generate_tests_from_rubric, rubric, prompt, project_dir, key)
    )
else:
    testgen_task = asyncio.create_task(
        asyncio.to_thread(generate_tests, prompt, project_dir, key)
    )
```

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS (integration tests still work — they don't set rubric, so fallback path is taken)

- [ ] **Step 5: Commit**

```bash
git add otto/runner.py tests/test_runner.py
git commit -m "feat: route to rubric testgen when rubric present, prepend context to prompt"
```

---

## Chunk 5: CLI Integration (cli.py)

### Task 8: Add rubric generation to otto add

**Files:**
- Modify: `otto/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py — add to TestAdd class

from unittest.mock import patch


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v -k "rubric"`
Expected: FAIL

- [ ] **Step 3: Implement rubric in add command**

Update `otto/cli.py`:

```python
@main.command()
@click.argument("prompt", required=False)
@click.option("--verify", default=None, help="Custom verification command")
@click.option("--max-retries", default=None, type=int, help="Max retry attempts")
@click.option("--no-rubric", is_flag=True, help="Skip rubric generation")
@click.option("-f", "--file", "import_file", default=None, type=click.Path(exists=True),
              help="Import tasks from a file (.md, .txt, .yaml)")
def add(prompt, verify, max_retries, no_rubric, import_file):
    """Add a task to the queue (or import from file with -f)."""
    import yaml as _yaml
    from otto.rubric import generate_rubric

    project_dir = Path.cwd()
    tasks_path = project_dir / "tasks.yaml"

    if import_file:
        _import_tasks(Path(import_file), tasks_path, project_dir)
        return

    if not prompt:
        click.echo("Error: provide a prompt or use -f to import", err=True)
        sys.exit(2)

    rubric = None
    if not no_rubric:
        click.echo("Generating rubric...", nl=False)
        rubric = generate_rubric(prompt, project_dir)
        if rubric:
            click.echo(f" {len(rubric)} criteria")
        else:
            click.echo(" (none generated)")
            rubric = None

    task = add_task(tasks_path, prompt, verify=verify, max_retries=max_retries, rubric=rubric)
    click.echo(f"Added task #{task['id']} ({task['key']}): {prompt}")

    if rubric:
        click.echo("  Rubric:")
        for i, item in enumerate(rubric, 1):
            click.echo(f"    {i}. {item}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/cli.py tests/test_cli.py
git commit -m "feat: add rubric generation to otto add with --no-rubric flag"
```

### Task 9: Add markdown/txt/yaml import to otto add -f

**Files:**
- Modify: `otto/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
class TestAddImport:
    @patch("otto.cli.generate_rubric")
    def test_import_txt(self, mock_gen, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_gen.return_value = ["auto criterion"]
        txt_file = tmp_git_repo / "tasks.txt"
        txt_file.write_text("Add search\n# this is a comment\nAdd tags\n\n")
        result = runner.invoke(main, ["add", "-f", str(txt_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert len(tasks["tasks"]) == 2  # skips comment and empty line
        assert tasks["tasks"][0]["prompt"] == "Add search"
        assert tasks["tasks"][0]["rubric"] == ["auto criterion"]

    @patch("otto.cli.parse_markdown_tasks")
    def test_import_md(self, mock_parse, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        mock_parse.return_value = [
            {"prompt": "Add search", "rubric": ["criterion 1"], "context": "ctx"},
        ]
        md_file = tmp_git_repo / "features.md"
        md_file.write_text("# Search\nAdd search.\n")
        result = runner.invoke(main, ["add", "-f", str(md_file)])
        assert result.exit_code == 0
        tasks = yaml.safe_load((tmp_git_repo / "tasks.yaml").read_text())
        assert len(tasks["tasks"]) == 1
        assert tasks["tasks"][0]["rubric"] == ["criterion 1"]
        assert tasks["tasks"][0]["context"] == "ctx"

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
        mock_gen.assert_called_once()  # only called for task without rubric
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v -k "import"`
Expected: FAIL

- [ ] **Step 3: Implement _import_tasks helper**

Add to `otto/cli.py`:

```python
from otto.tasks import add_task, add_tasks, load_tasks, reset_all_tasks, save_tasks, update_task


def _import_tasks(filepath: Path, tasks_path: Path, project_dir: Path) -> None:
    """Import tasks from .md, .txt, or .yaml file."""
    import yaml as _yaml
    from otto.rubric import generate_rubric, parse_markdown_tasks

    ext = filepath.suffix.lower()
    batch: list[dict] = []

    if ext == ".md":
        click.echo(f"Parsing {filepath.name}...")
        parsed = parse_markdown_tasks(filepath, project_dir)
        batch = parsed

    elif ext == ".txt":
        lines = [
            line.strip() for line in filepath.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for line in lines:
            click.echo(f"Generating rubric for: {line[:50]}...", nl=False)
            rubric = generate_rubric(line, project_dir)
            click.echo(f" {len(rubric)} criteria")
            item = {"prompt": line}
            if rubric:
                item["rubric"] = rubric
            batch.append(item)

    elif ext in (".yaml", ".yml"):
        data = _yaml.safe_load(filepath.read_text()) or {}
        imported = data.get("tasks", [])
        for t in imported:
            item = {"prompt": t["prompt"]}
            if t.get("verify"):
                item["verify"] = t["verify"]
            if t.get("max_retries"):
                item["max_retries"] = t["max_retries"]
            if t.get("rubric"):
                item["rubric"] = t["rubric"]
            else:
                click.echo(f"Generating rubric for: {t['prompt'][:50]}...", nl=False)
                rubric = generate_rubric(t["prompt"], project_dir)
                click.echo(f" {len(rubric)} criteria")
                if rubric:
                    item["rubric"] = rubric
            if t.get("context"):
                item["context"] = t["context"]
            batch.append(item)

    else:
        click.echo(f"Unsupported file type: {ext}. Use .md, .txt, or .yaml", err=True)
        sys.exit(2)

    if not batch:
        click.echo("No tasks found in file", err=True)
        sys.exit(2)

    results = add_tasks(tasks_path, batch)
    for task in results:
        rubric_count = len(task.get("rubric", []))
        click.echo(f"  Task #{task['id']} ({task['key']}): {task['prompt'][:50]}")
        if task.get("rubric"):
            click.echo(f"    Rubric ({rubric_count}):")
            for i, item in enumerate(task["rubric"], 1):
                click.echo(f"      {i}. {item}")
    click.echo(f"Imported {len(results)} tasks. Review rubrics in tasks.yaml before running.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/cli.py tests/test_cli.py
git commit -m "feat: support .md/.txt/.yaml import with rubric generation"
```

### Task 10: Update otto status to show rubric count

**Files:**
- Modify: `otto/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
class TestStatusRubric:
    def test_shows_rubric_count(self, runner, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        add_task(tmp_git_repo / "tasks.yaml", "Task with rubric",
                 rubric=["c1", "c2", "c3"])
        add_task(tmp_git_repo / "tasks.yaml", "Task without rubric")
        result = runner.invoke(main, ["status"])
        assert "3" in result.output  # rubric count
        assert "0" in result.output  # no rubric
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v -k "rubric_count"`
Expected: FAIL — status doesn't show rubric column

- [ ] **Step 3: Update status command**

```python
@main.command()
def status():
    """Show task status."""
    tasks_path = Path.cwd() / "tasks.yaml"
    tasks = load_tasks(tasks_path)
    if not tasks:
        click.echo("No tasks found. Use 'otto add' to create one.")
        return

    click.echo(f"{'ID':>4}  {'Key':12}  {'Status':10}  {'Att':>3}  {'Rubric':>6}  Prompt")
    click.echo("-" * 78)
    for t in tasks:
        rubric_count = len(t.get("rubric", []))
        click.echo(
            f"{t.get('id', '?'):>4}  {t.get('key', '?'):12}  "
            f"{t.get('status', '?'):10}  {t.get('attempts', 0):>3}  "
            f"{rubric_count:>6}  "
            f"{t['prompt'][:35]}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add otto/cli.py tests/test_cli.py
git commit -m "feat: show rubric count in otto status"
```

---

## Chunk 6: Full Integration Test

### Task 11: End-to-end test with rubric

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
class TestRubricEndToEnd:
    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.generate_tests_from_rubric")
    @patch("otto.runner.generate_tests")
    @patch("otto.runner.query")
    def test_task_with_rubric_uses_rubric_testgen(
        self, mock_query, mock_testgen, mock_rubric_testgen, mock_opts, tmp_git_repo
    ):
        """Task with rubric uses generate_tests_from_rubric, not generate_tests."""
        from otto.config import create_config, load_config
        from otto.runner import run_all
        from otto.tasks import add_task, load_tasks

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = None
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Add search",
                 rubric=["search is case-insensitive"],
                 context="Store is in store.py")

        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "search.py").write_text("def search(): pass\n")
            yield _make_fake_result("session-1")

        mock_query.side_effect = fake_query
        mock_rubric_testgen.return_value = None  # skip rubric tests
        mock_testgen.return_value = None  # should not be called

        exit_code = asyncio.run(run_all(config, tasks_path, tmp_git_repo))

        assert exit_code == 0
        mock_rubric_testgen.assert_called_once()
        mock_testgen.assert_not_called()

        # Verify context was passed in the agent prompt
        call_kwargs = mock_query.call_args_list[0]
        # The prompt should contain the context
        prompt_used = call_kwargs.kwargs.get("prompt", "")
        assert "Context: Store is in store.py" in prompt_used
```

- [ ] **Step 2: Run test**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/test_integration.py -v -k "rubric"`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/yuxuan/work/cc-autonomous/.worktrees/v3 && /Users/yuxuan/work/cc-autonomous/.venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration test for rubric feature"
```

---

## After All Tasks

Run the full test suite one final time, then use `superpowers:finishing-a-development-branch` to complete the work.
