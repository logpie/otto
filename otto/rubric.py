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
