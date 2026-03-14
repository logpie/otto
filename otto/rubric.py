"""Otto rubric generation — generate evaluation rubrics via claude -p."""

import json
import re
import subprocess
from pathlib import Path

from otto.testgen import _read_existing_tests

# Extensions and filenames to skip when selecting source files for context
_SKIP_EXTENSIONS = {
    ".lock", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml",
    ".md", ".txt", ".rst", ".csv",
}
_SKIP_NAMES = {
    "setup.py", "conftest.py", "__init__.py", "manage.py", ".gitignore",
}

_MAX_SOURCE_FILES = 5
_MAX_SOURCE_LINES = 100


def _gather_project_context(project_dir: Path) -> str:
    """Gather project context for rubric/task generation.

    Returns a string containing:
    - File tree from git ls-files
    - Contents of up to 5 key source files (100 lines each)
    - Existing test samples (reused from testgen)
    """
    sections = []

    # 1. File tree
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        file_tree = result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        file_tree = ""

    if file_tree:
        sections.append(f"FILE TREE:\n{file_tree}")

    # 2. Key source file contents
    source_files = []
    if file_tree:
        for line in file_tree.splitlines():
            path = Path(line.strip())
            if not path.name:
                continue
            # Skip by extension
            if path.suffix in _SKIP_EXTENSIONS:
                continue
            # Skip by name
            if path.name in _SKIP_NAMES:
                continue
            # Skip test directories
            if any(part.startswith("test") for part in path.parts[:-1]):
                continue
            source_files.append(path)
            if len(source_files) >= _MAX_SOURCE_FILES:
                break

    file_contents = []
    for rel_path in source_files:
        full_path = project_dir / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text()
            lines = content.splitlines()[:_MAX_SOURCE_LINES]
            file_contents.append(f"# {rel_path}\n" + "\n".join(lines))
        except (OSError, UnicodeDecodeError):
            continue

    if file_contents:
        sections.append("SOURCE FILES:\n" + "\n\n".join(file_contents))

    # 3. Existing test samples
    existing_tests = _read_existing_tests(project_dir)
    if existing_tests:
        sections.append(f"EXISTING TESTS:\n{existing_tests}")

    return "\n\n".join(sections)


# Regex to strip numbered ("1. ", "1) ") or bullet ("- ", "* ") prefixes
_LIST_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*]\s+)")

RUBRIC_TIMEOUT = 120  # seconds


def _parse_rubric_output(text: str) -> list[str]:
    """Parse LLM output into a list of rubric criteria strings.

    Strips numbering (1. , 1) ) and bullets (- , * ) prefixes.
    Skips empty lines.
    """
    criteria = []
    for line in text.splitlines():
        stripped = _LIST_PREFIX_RE.sub("", line).strip()
        if stripped:
            criteria.append(stripped)
    return criteria


def generate_rubric(prompt: str, project_dir: Path) -> list[str]:
    """Generate a rubric for a single task via claude -p.

    Returns a list of rubric criterion strings, or empty list on failure.
    """
    context = _gather_project_context(project_dir)

    system_prompt = f"""You are a senior QA engineer. Given a coding task and project context,
generate a rubric: a numbered list of specific, testable acceptance criteria
that verify the task was completed correctly.

PROJECT CONTEXT:
{context}

TASK: {prompt}

Include ALL of these categories:
- Happy path: the feature works as described
- Error handling: invalid inputs, missing data, edge cases produce clear errors (not crashes or silent failures)
- Negative/anti-pattern: things that must NOT happen (e.g., "delete does NOT affect other records",
  "search does NOT return unrelated results", "invalid input does NOT silently succeed")
- Edge cases: empty inputs, boundary values, special characters, zero/null cases

Output ONLY a numbered list of criteria. No prose, no explanations.
Each criterion should be a single clear sentence. Use "does NOT" or "must NOT" for negative criteria."""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--allowed-tools", ""],
            input=system_prompt,
            capture_output=True,
            text=True,
            timeout=RUBRIC_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return _parse_rubric_output(result.stdout)


MARKDOWN_PARSE_TIMEOUT = 120  # seconds


def parse_markdown_tasks(md_file: Path, project_dir: Path) -> list[dict]:
    """Parse a markdown file into structured tasks via claude -p.

    Sends the markdown content + project context to Claude, expects a JSON array
    where each element has: prompt (str), rubric (list[str]), context (str).

    Returns list of task dicts.
    Raises ValueError on parse failure or invalid task structure.
    """
    md_content = md_file.read_text()
    context = _gather_project_context(project_dir)

    system_prompt = f"""You are a senior QA engineer and technical PM breaking down a feature document into coding tasks.

PROJECT CONTEXT:
{context}

DOCUMENT:
{md_content}

Break this document into individual coding tasks. For each task, provide:
- "prompt": a clear, actionable description of what to implement
- "rubric": 5-8 concrete, testable acceptance criteria

IMPORTANT RULES:
- Each task should be a complete, self-contained unit of work.
- Do NOT create separate tasks for writing tests — test expectations belong in the rubric.
- One heading/section in the document = one task (unless a section is too large, then split by feature, not by "implement" vs "test").
- Rubric items must be specific enough to write a test from. Cover: happy path, edge cases,
  error conditions, and anti-patterns (things that must NOT happen — e.g., "delete does NOT
  affect unrelated records", "invalid input does NOT silently succeed").
- Reference actual function/class names from the project source when possible.

Output ONLY a valid JSON array. No prose, no markdown fences, no explanations.
Example format:
[{{"prompt": "Add search method to BookmarkStore", "rubric": ["search('python') returns matching bookmarks", "search is case-insensitive", "no matches returns empty list"]}}]"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--allowed-tools", ""],
            input=system_prompt,
            capture_output=True,
            text=True,
            timeout=MARKDOWN_PARSE_TIMEOUT,
            start_new_session=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise ValueError(f"Failed to parse markdown tasks: {exc}") from exc

    if result.returncode != 0:
        raise ValueError(f"Failed to parse markdown tasks: claude returned {result.returncode}")

    output = result.stdout.strip()

    # Extract JSON from markdown fences if present
    fence_match = re.search(r"```(?:json)?\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    try:
        tasks = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse tasks JSON: {exc}") from exc

    if not isinstance(tasks, list):
        raise ValueError("Failed to parse tasks JSON: expected a JSON array")

    # Validate structure
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Task {i} is not a dict")
        if not task.get("prompt"):
            raise ValueError(f"Task {i} missing 'prompt'")

    return tasks
