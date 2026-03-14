"""Otto test generation — generate integration tests via claude -p."""

import ast
import json
import re
import subprocess
from pathlib import Path

from otto.config import git_meta_dir

TESTGEN_TIMEOUT = 180  # seconds


def _extract_public_stubs(source_code: str) -> str:
    """Extract public API stubs from Python source code using AST.

    Returns function/method signatures with docstrings, class definitions,
    and module-level constants. Does NOT include function bodies or
    implementation logic. Python-only for MVP.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return ""

    lines: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Top-level function: signature + docstring only
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            # Decorators
            for dec in node.decorator_list:
                lines.append(f"@{ast.unparse(dec)}")
            sig = f"{prefix} {node.name}({ast.unparse(ast.arguments(**{f: getattr(node.args, f) for f in node.args._fields}))}):"
            # Add return annotation if present
            if node.returns:
                sig = f"{prefix} {node.name}({ast.unparse(node.args)}) -> {ast.unparse(node.returns)}:"
            lines.append(sig)
            docstring = ast.get_docstring(node)
            if docstring:
                lines.append(f'    """{docstring}"""')
            lines.append("")

        elif isinstance(node, ast.ClassDef):
            # Class: definition + docstring + method stubs
            for dec in node.decorator_list:
                lines.append(f"@{ast.unparse(dec)}")
            lines.append(f"class {node.name}:")
            docstring = ast.get_docstring(node)
            if docstring:
                lines.append(f'    """{docstring}"""')
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    for dec in item.decorator_list:
                        lines.append(f"    @{ast.unparse(dec)}")
                    if item.returns:
                        sig = f"    {prefix} {item.name}({ast.unparse(item.args)}) -> {ast.unparse(item.returns)}:"
                    else:
                        sig = f"    {prefix} {item.name}({ast.unparse(item.args)}):"
                    lines.append(sig)
                    method_doc = ast.get_docstring(item)
                    if method_doc:
                        lines.append(f'        """{method_doc}"""')
                elif isinstance(item, ast.Assign):
                    lines.append(f"    {ast.unparse(item)}")
            lines.append("")

        elif isinstance(node, ast.Assign):
            # Module-level constant/assignment
            lines.append(ast.unparse(node))

        elif isinstance(node, ast.AnnAssign):
            # Module-level annotated assignment (e.g. x: int = 5)
            lines.append(ast.unparse(node))

    return "\n".join(lines).strip()


def build_blackbox_context(project_dir: Path) -> str:
    """Build a sanitized project context for adversarial test generation.

    Returns a string containing:
    1. File tree (via git ls-files)
    2. Public API stubs (signatures + docstrings, no bodies) for each .py source file
    3. CLI help (best effort)
    4. Existing test samples

    Skips tests/, __init__.py, and conftest.py when extracting stubs.
    Python-only for MVP.
    """
    sections: list[str] = []

    # 1. File tree
    try:
        tree_result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        file_tree = tree_result.stdout.strip() if tree_result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        file_tree = ""
    if file_tree:
        sections.append(f"FILE TREE:\n{file_tree}")

    # 2. Public API stubs
    stubs_parts: list[str] = []
    if file_tree:
        for rel_path in file_tree.splitlines():
            rel = rel_path.strip()
            if not rel.endswith(".py"):
                continue
            # Skip test files, __init__.py, conftest.py
            basename = Path(rel).name
            if basename.startswith("test_") or basename in ("__init__.py", "conftest.py"):
                continue
            if rel.startswith("tests/") or rel.startswith("test/"):
                continue
            full = project_dir / rel
            if not full.is_file():
                continue
            try:
                source = full.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            stubs = _extract_public_stubs(source)
            if stubs:
                stubs_parts.append(f"# {rel}\n{stubs}")
    if stubs_parts:
        sections.append("PUBLIC API STUBS:\n" + "\n\n".join(stubs_parts))

    # 3. CLI help (best effort — try python -m <package> --help)
    # Detect top-level package name from __main__.py or setup files
    cli_help = ""
    for rel_path in (file_tree or "").splitlines():
        rel = rel_path.strip()
        if rel.endswith("__main__.py"):
            pkg = str(Path(rel).parent).replace("/", ".")
            if pkg and pkg != ".":
                try:
                    result = subprocess.run(
                        ["python", "-m", pkg, "--help"],
                        cwd=project_dir,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        cli_help = result.stdout.strip()
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                    pass
                break
    if cli_help:
        sections.append(f"CLI HELP:\n{cli_help}")

    # 4. Existing test samples
    test_samples = _read_existing_tests(project_dir)
    if test_samples:
        sections.append(f"EXISTING TEST SAMPLES:\n{test_samples}")

    return "\n\n".join(sections)


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

    return f"""You are a code generator. Your ONLY job is to output valid {framework} test code.
Do NOT explain what you're doing. Do NOT ask for permissions. Do NOT describe the tests.
Do NOT use markdown fences. Just output the raw test code starting with import statements.

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


def _call_and_validate(prompt: str, framework: str) -> tuple[str | None, str | None]:
    """Call claude -p, strip markdown fences, validate output.

    Returns (code, None) on success, or (None, bad_output) on failure.
    """
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
        return None, None
    except FileNotFoundError:
        print("  testgen: claude CLI not found", file=sys.stderr, flush=True)
        return None, None

    if result.returncode != 0:
        print(f"  testgen: claude exited {result.returncode}: {result.stderr[:200]}", file=sys.stderr, flush=True)
        return None, None

    if not result.stdout.strip():
        print("  testgen: empty output from claude", file=sys.stderr, flush=True)
        return None, None

    output = result.stdout.strip()
    fence_match = re.search(r"```(?:\w*)\n(.*?)```", output, re.DOTALL)
    if fence_match:
        output = fence_match.group(1).strip()

    if _validate_test_output(output, framework):
        return output, None

    print(f"  testgen: validation failed — first 100 chars: {output[:100]}", file=sys.stderr, flush=True)
    return None, output


def _call_with_retry(prompt: str, framework: str, max_retries: int = 2) -> str | None:
    """Call claude -p with retries on validation failure.

    Each retry includes the previous bad output so the model knows what went wrong.
    """
    last_bad_output = None

    for attempt in range(max_retries + 1):
        if attempt == 0:
            current_prompt = prompt
        else:
            current_prompt = (
                f"I asked you to generate {framework} test code but your output was not valid code.\n\n"
                f"Your bad output started with:\n{last_bad_output[:200] if last_bad_output else '(empty)'}\n\n"
                f"This is wrong. Output ONLY executable {framework} test code. "
                f"The very first line must be an import statement. No prose, no explanations, "
                f"no file paths, no asking for permissions.\n\n"
                f"Original request:\n{prompt}"
            )

        code, bad_output = _call_and_validate(current_prompt, framework)
        if code is not None:
            return code
        last_bad_output = bad_output

    return None


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

    llm_prompt = f"""You are a code generator. Your ONLY job is to output valid {framework} test code.
Do NOT explain what you're doing. Do NOT ask for permissions. Do NOT describe the tests.
Do NOT use markdown fences. Just output the raw test code starting with import statements.

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

    llm_prompt = f"""You are a code generator. Your ONLY job is to output valid {framework} test code.
Do NOT explain what you're doing. Do NOT ask for permissions. Do NOT describe the tests.
Do NOT use markdown fences. Just output the raw test code starting with import statements.

Write cross-feature INTEGRATION tests. The following features were implemented:

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
