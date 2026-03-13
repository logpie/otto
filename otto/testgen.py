"""Otto test generation — generate integration tests via claude -p."""

import json
import re
import subprocess
from pathlib import Path

from otto.config import git_meta_dir

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
            start_new_session=True,  # own process group for clean kill on timeout
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

    # Write to <git-common-dir>/otto/testgen/<key>/ (handles linked worktrees)
    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    testgen_dir.mkdir(parents=True, exist_ok=True)

    rel_path = test_file_path(framework, key)
    out_file = testgen_dir / rel_path.name
    out_file.write_text(output)

    return out_file
