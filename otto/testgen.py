"""Otto test generation — generate integration tests via claude -p."""

import ast
import json
import re
import subprocess
from pathlib import Path

from otto.config import git_meta_dir

TESTGEN_TIMEOUT = 180  # seconds


def detect_test_framework(project_dir: Path) -> str | None:
    """Detect which test framework the project uses."""
    if (project_dir / "tests").is_dir() or (project_dir / "test").is_dir():
        return "pytest"
    if (project_dir / "package.json").exists():
        try:
            pkg = json.loads((project_dir / "package.json").read_text())
            deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
            if "vitest" in deps:
                return "vitest"
            if "jest" in deps:
                return "jest"
            if "mocha" in deps:
                return "mocha"
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
        case "jest" | "mocha":
            return Path(f"__tests__/otto_verify_{key}.test.js")
        case "vitest":
            return Path(f"__tests__/otto_verify_{key}.test.ts")
        case "go":
            return Path(f"otto_verify_{key}_test.go")
        case "cargo":
            return Path(f"tests/otto_verify_{key}.rs")
        case _:
            return Path(f"tests/otto_verify_{key}.py")


def _read_existing_tests(project_dir: Path) -> str:
    """Read existing test files to provide import style context."""
    test_dirs = [project_dir / "tests", project_dir / "test"]
    samples = []
    for test_dir in test_dirs:
        if not test_dir.is_dir():
            continue
        for f in sorted(test_dir.iterdir()):
            if f.suffix == ".py" and f.name.startswith("test_"):
                content = f.read_text()
                # Take first 50 lines to show import patterns
                lines = content.splitlines()[:50]
                samples.append(f"# {f.relative_to(project_dir)}\n" + "\n".join(lines))
                if len(samples) >= 2:
                    break
        if samples:
            break
    return "\n\n".join(samples) if samples else ""


def build_testgen_prompt(task_prompt: str, file_tree: str, framework: str,
                         existing_tests: str = "") -> str:
    """Build the prompt for test generation."""
    example_section = ""
    if existing_tests:
        example_section = f"""
EXISTING TESTS (for reference on fixtures/helpers — do NOT copy how they invoke the system under test):
{existing_tests}
"""

    return f"""You are a QA engineer writing integration tests for a coding task.

TASK: {task_prompt}

PROJECT FILES:
{file_tree}
{example_section}
TEST FRAMEWORK: {framework}

Write integration tests that verify the task was completed correctly.

Rules — "test like a user":
- Test the system the way a real user would use it:
  - CLI apps: use subprocess.run() to invoke the actual command. Check stdout, stderr, exit codes.
    Do NOT use in-process test runners (CliRunner, invoke()) — they skip the real entry point
    and miss bugs like missing __main__.py or broken package setup.
  - Libraries/APIs: import and call the public interface as a consumer would.
  - Web apps: make HTTP requests to the actual server endpoint.
- Tests must be hermetic and deterministic — no external network calls
- Mocks/fakes ONLY if the project already provides test fixtures for them
- Do NOT grep source code for strings — test actual behavior
- The tests should be runnable with the standard test command for {framework}

IMPORTANT: Output ONLY valid {framework} test code. No prose, no explanations, no markdown.
Start directly with import statements.
"""


def _validate_test_output(output: str, framework: str) -> bool:
    """Validate that LLM output is actual test code, not prose.

    Checks are framework-aware:
    - pytest: ast.parse + must contain 'def test_'
    - jest/vitest/mocha: must contain describe(, it(, or test(
    - go/cargo: first non-empty line must start with a code keyword
    - Empty string: always False
    """
    if not output or not output.strip():
        return False

    output = output.strip()

    if framework == "pytest":
        try:
            ast.parse(output)
        except SyntaxError:
            return False
        if "def test_" not in output:
            return False
        return True

    if framework in ("jest", "vitest", "mocha"):
        # Must contain at least one test construct
        if any(kw in output for kw in ("describe(", "it(", "test(")):
            return True
        return False

    if framework in ("go", "cargo"):
        # First non-empty line must start with a code keyword
        first_line = ""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break
        go_keywords = ("package", "import", "func", "type", "var", "const")
        cargo_keywords = ("use", "mod", "fn", "#[", "pub", "extern")
        keywords = go_keywords if framework == "go" else cargo_keywords
        return any(first_line.startswith(kw) for kw in keywords)

    return False


def _call_and_validate(prompt: str, framework: str) -> str | None:
    """Call claude -p, strip markdown fences, validate output. Returns code or None."""
    import sys

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TESTGEN_TIMEOUT,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        print(f"  testgen: timed out after {TESTGEN_TIMEOUT}s", file=sys.stderr, flush=True)
        return None
    except FileNotFoundError:
        print("  testgen: claude CLI not found", file=sys.stderr, flush=True)
        return None

    if result.returncode != 0:
        print(f"  testgen: claude exited {result.returncode}: {result.stderr[:200]}", file=sys.stderr, flush=True)
        return None

    if not result.stdout.strip():
        print("  testgen: empty output from claude", file=sys.stderr, flush=True)
        return None

    output = result.stdout.strip()
    fence_match = re.search(r"```(?:\w*)\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    if _validate_test_output(output, framework):
        return output

    print(f"  testgen: validation failed — first 100 chars: {output[:100]}", file=sys.stderr, flush=True)
    return None


def _call_with_retry(prompt: str, framework: str) -> str | None:
    """Call claude -p with one retry on validation failure."""
    output = _call_and_validate(prompt, framework)
    if output is not None:
        return output

    # Retry once with error feedback
    retry_prompt = (
        f"Your previous output was not valid {framework} test code. "
        f"Output ONLY executable test code. No markdown fences, no prose, no explanations. "
        f"Start directly with import statements.\n\n"
        f"Original request:\n{prompt}"
    )
    return _call_and_validate(retry_prompt, framework)


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
    existing_tests = _read_existing_tests(project_dir)
    prompt = build_testgen_prompt(task_prompt, file_tree, framework, existing_tests)

    output = _call_with_retry(prompt, framework)
    if output is None:
        return None

    # Write to <git-common-dir>/otto/testgen/<key>/ (handles linked worktrees)
    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    testgen_dir.mkdir(parents=True, exist_ok=True)

    rel_path = test_file_path(framework, key)
    out_file = testgen_dir / rel_path.name
    out_file.write_text(output)

    return out_file


def generate_tests_from_rubric(
    rubric: list[str],
    prompt: str,
    project_dir: Path,
    key: str,
) -> Path | None:
    """Generate integration tests from explicit rubric items via claude -p.

    Uses _gather_project_context from otto.rubric for richer project context
    (source files + existing tests), unlike generate_tests which only uses file tree.

    Returns path to generated test file, or None on failure/invalid output.
    """
    # Import here to avoid circular import (rubric imports from testgen)
    from otto.rubric import _gather_project_context

    context = _gather_project_context(project_dir)
    framework = detect_test_framework(project_dir) or "pytest"
    existing_tests = _read_existing_tests(project_dir)

    # Build numbered rubric list
    rubric_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(rubric))

    example_section = ""
    if existing_tests:
        example_section = f"""
EXISTING TESTS (for reference on fixtures/helpers — do NOT copy how they invoke the system under test):
{existing_tests}
"""

    llm_prompt = f"""You are a QA engineer writing integration tests for a coding task.

TASK: {prompt}

RUBRIC (each criterion MUST have a corresponding test):
{rubric_text}

PROJECT CONTEXT:
{context}
{example_section}
TEST FRAMEWORK: {framework}

Write integration tests that verify EACH rubric criterion was met.

Rules — "test like a user":
- One or more test functions per rubric item
- Test the system the way a real user would use it:
  - CLI apps: use subprocess.run() to invoke the actual command. Check stdout, stderr, exit codes.
    Do NOT use in-process test runners (CliRunner, invoke()) — they skip the real entry point
    and miss bugs like missing __main__.py or broken package setup.
  - Libraries/APIs: import and call the public interface as a consumer would.
  - Web apps: make HTTP requests to the actual server endpoint.
- Tests must be hermetic and deterministic — no external network calls
- Mocks/fakes ONLY if the project already provides test fixtures for them
- Do NOT grep source code for strings — test actual behavior
- The tests should be runnable with the standard test command for {framework}

IMPORTANT: Output ONLY valid {framework} test code. No prose, no explanations, no markdown.
Start directly with import statements.
"""

    output = _call_with_retry(llm_prompt, framework)
    if output is None:
        return None

    # Write to <git-common-dir>/otto/testgen/<key>/
    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    testgen_dir.mkdir(parents=True, exist_ok=True)

    rel_path = test_file_path(framework, key)
    out_file = testgen_dir / rel_path.name
    out_file.write_text(output)

    return out_file


def generate_integration_tests(
    tasks: list[dict],
    project_dir: Path,
) -> Path | None:
    """Generate cross-feature integration tests via claude -p.

    Takes ALL passed tasks and generates tests that exercise features
    working together — multi-step workflows crossing task boundaries.
    """
    from otto.rubric import _gather_project_context

    framework = detect_test_framework(project_dir) or "pytest"
    context = _gather_project_context(project_dir)
    existing_tests = _read_existing_tests(project_dir)

    task_sections = []
    for i, task in enumerate(tasks, 1):
        section = f"FEATURE {i}: {task['prompt']}"
        rubric = task.get("rubric", [])
        if rubric:
            section += "\nRubric:\n" + "\n".join(f"  - {r}" for r in rubric)
        task_sections.append(section)

    example_section = ""
    if existing_tests:
        example_section = f"""
EXISTING TESTS (for reference on fixtures/helpers — do NOT copy how they invoke the system under test):
{existing_tests}
"""

    llm_prompt = f"""You are a QA engineer writing cross-feature INTEGRATION tests.

The following features were implemented:

{chr(10).join(task_sections)}

PROJECT CONTEXT:
{context}
{example_section}
TEST FRAMEWORK: {framework}

Write integration tests that exercise these features WORKING TOGETHER.

Focus on:
- Multi-step workflows crossing feature boundaries (create via feature A, verify via feature B)
- State interactions (one feature's changes visible to another)
- Data round-trips (import then search then export — verify consistency)
- Features not interfering with each other

Do NOT re-test individual features — those are already covered.
Test ONLY cross-feature interactions and multi-step workflows.

Rules — "test like a user":
- CLI apps: use subprocess.run() to invoke the actual command. Check stdout, stderr, exit codes.
  Do NOT use in-process test runners (CliRunner, invoke()).
- Libraries/APIs: import and call the public interface as a consumer would.
- Tests must be hermetic and deterministic — no external network calls
- The tests should be runnable with the standard test command for {framework}

IMPORTANT: Output ONLY valid {framework} test code. No prose, no explanations, no markdown.
Start directly with import statements.
"""

    output = _call_with_retry(llm_prompt, framework)
    if output is None:
        return None

    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / "_integration"
    testgen_dir.mkdir(parents=True, exist_ok=True)
    out_file = testgen_dir / "otto_integration.py"
    out_file.write_text(output)

    return out_file
