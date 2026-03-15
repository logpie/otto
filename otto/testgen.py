"""Otto test generation — generate integration tests via claude -p."""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import ResultMessage, TextBlock, ToolUseBlock
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]

from otto.config import git_meta_dir

TESTGEN_TIMEOUT = 180  # seconds


@dataclass
class TestValidationResult:
    """Result of validating generated tests."""

    __test__ = False  # prevent pytest from collecting this as a test class

    status: str  # "tdd_ok", "all_pass", "collection_error", "no_tests"
    passed: int = 0
    failed: int = 0
    error_output: str = ""


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


_MAX_STUB_FILES = 15  # Max source files to extract stubs from


def _build_project_index(project_dir: Path, source_files: list[str]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Build a symbol index and import graph from Python source files.

    Returns:
        symbol_to_file: {"BookmarkStore": "store.py", "search": "store.py", ...}
        import_graph: {"cli.py": {"store.py", "config.py"}, ...}
    """
    symbol_to_file: dict[str, str] = {}
    import_graph: dict[str, set[str]] = {}

    # Map module paths to file paths (e.g., "bookmarks.store" -> "bookmarks/store.py")
    module_to_file: dict[str, str] = {}
    for rel in source_files:
        # "bookmarks/store.py" -> "bookmarks.store"
        mod = rel.replace("/", ".").removesuffix(".py")
        module_to_file[mod] = rel
        # Also map just the last component: "store" -> "bookmarks/store.py"
        parts = mod.split(".")
        if len(parts) > 1:
            module_to_file[parts[-1]] = rel

    for rel in source_files:
        full = project_dir / rel
        try:
            source = full.read_text()
            tree = ast.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        # Extract symbols defined in this file
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol_to_file[node.name] = rel
            elif isinstance(node, ast.ClassDef):
                symbol_to_file[node.name] = rel
                # Also index methods as "ClassName.method"
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbol_to_file[f"{node.name}.{item.name}"] = rel

        # Extract imports to build dependency graph
        deps: set[str] = set()
        for node in ast.walk(tree):
            modules_to_check: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    modules_to_check.append(node.module)
                # Relative imports: "from . import store" or "from .store import X"
                if node.level and node.level > 0:
                    # Resolve relative to current file's package
                    current_parts = Path(rel).parts[:-1]  # directory parts
                    if node.module:
                        modules_to_check.append(".".join(current_parts) + "." + node.module)
            elif isinstance(node, ast.Import):
                # "import pkg.mod"
                for alias in node.names:
                    modules_to_check.append(alias.name)

            for mod in modules_to_check:
                if mod in module_to_file:
                    deps.add(module_to_file[mod])
                last = mod.split(".")[-1]
                if last in module_to_file:
                    deps.add(module_to_file[last])
        if deps:
            import_graph[rel] = deps

    return symbol_to_file, import_graph


def _find_relevant_files(
    task_hint: str,
    source_files: list[str],
    symbol_to_file: dict[str, str],
    import_graph: dict[str, set[str]],
    max_files: int = _MAX_STUB_FILES,
) -> list[str]:
    """Find files relevant to a task using symbol matching + import graph traversal.

    1. Extract words from task_hint that match known symbols
    2. Find files containing those symbols
    3. Follow import graph to find related files
    4. Return up to max_files
    """
    if not task_hint:
        return source_files[:max_files]

    # Extract words from hint that could be symbols (3+ chars)
    hint_words = {w for w in re.split(r'[\s\W]+', task_hint) if len(w) >= 3}
    # Also keep dotted names intact: "BookmarkStore.search" stays as one token
    for match in re.findall(r'\b\w+\.\w+\b', task_hint):
        hint_words.add(match)

    # Find directly referenced files via substring matching
    relevant: set[str] = set()
    for word in hint_words:
        word_lower = word.lower()
        for sym, filepath in symbol_to_file.items():
            sym_lower = sym.lower()
            # Substring match: "bilibili" matches "BilibiliCrawler", "bilibili_config"
            # Also: "crawler" matches "base_crawler", "CrawlerManager"
            if word_lower in sym_lower or sym_lower in word_lower:
                relevant.add(filepath)
    # Also match against file paths directly
    for word in hint_words:
        word_lower = word.lower()
        for filepath in source_files:
            if word_lower in filepath.lower():
                relevant.add(filepath)

    # Follow import graph one level — files that import from relevant files, or that relevant files import
    expanded: set[str] = set(relevant)
    for filepath in relevant:
        # Files that this file imports from
        if filepath in import_graph:
            expanded.update(import_graph[filepath])
        # Files that import this file
        for other, deps in import_graph.items():
            if filepath in deps:
                expanded.add(other)

    # If we found relevant files, use them. Otherwise fall back to all files.
    if expanded:
        result = sorted(expanded)
    else:
        result = source_files

    return result[:max_files]


def build_blackbox_context(project_dir: Path, task_hint: str = "") -> str:
    """Build a sanitized project context for adversarial test generation.

    Returns a string containing:
    1. File tree (via git ls-files)
    2. Public API stubs (signatures + docstrings, no bodies) — for relevant files only
    3. CLI help (best effort)
    4. Existing test samples

    Uses AST-based symbol index + import graph to find relevant files.
    Falls back to all files (capped at _MAX_STUB_FILES) if no matches found.
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

    # Collect candidate source files
    source_files: list[str] = []
    if file_tree:
        for rel_path in file_tree.splitlines():
            rel = rel_path.strip()
            if not rel.endswith(".py"):
                continue
            basename = Path(rel).name
            if basename.startswith("test_") or basename in ("__init__.py", "conftest.py"):
                continue
            if rel.startswith("tests/") or rel.startswith("test/"):
                continue
            full = project_dir / rel
            if full.is_file():
                source_files.append(rel)

    # 2. Public API stubs — use symbol index to pick relevant files
    if source_files:
        if len(source_files) <= _MAX_STUB_FILES:
            # Small project — include everything
            selected = source_files
        else:
            # Large project — use AST index to find relevant files
            symbol_to_file, import_graph = _build_project_index(project_dir, source_files)
            selected = _find_relevant_files(task_hint, source_files, symbol_to_file, import_graph)

        stubs_parts: list[str] = []
        for rel in selected:
            full = project_dir / rel
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


async def run_testgen_agent(
    rubric: list[str],
    key: str,
    blackbox_context: str,
    project_dir: Path,
    framework: str = "pytest",
) -> tuple[Path | None, list[str]]:
    """Run adversarial testgen agent in an isolated temp directory.

    The agent receives blackbox_context (public stubs, file tree) as a string
    in its prompt. It writes the test file in the temp dir. After generation,
    we copy the test file to the project's tests/ directory.

    This enforces mechanical isolation — the agent literally cannot read
    implementation code.

    Returns (path to the copied test file or None, list of log lines).
    """
    rubric_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(rubric))
    test_rel = f"tests/test_otto_{key}.py"
    tmp_dir = tempfile.mkdtemp(prefix="otto_testgen_")
    log_lines: list[str] = []

    prompt = f"""You are a QA engineer writing black-box tests from a specification.
You have NOT seen the implementation — it hasn't been written yet.
Your job is to write tests that will CATCH BUGS, not confirm correctness.

SPEC (acceptance criteria):
{rubric_text}

PROJECT CONTEXT (public interface only):
{blackbox_context}

Your working directory is: {tmp_dir}
Write the test file to: {tmp_dir}/{test_rel}

BEFORE writing any tests:
1. Read the existing test files in the project to understand import patterns, fixtures, and style.
2. Read the public API stubs above carefully — understand what functions/classes exist.
3. Think about what a REAL USER would do with this feature and what could go wrong.
Only THEN write the test file.

Your tests MUST:
- Test the public interface only (CLI via subprocess, library via imports)
- Be designed to FAIL on the current codebase (the feature doesn't exist yet)
- Be independent and hermetic (use tmp_path, no shared state)
- Use subprocess.run() for CLI testing, not CliRunner
- Include negative tests (what should NOT happen)
- Test the FULL user workflow, not just individual functions
- Test data persistence: if the feature saves/loads state, verify the roundtrip works

Think like a devil's advocate — how might a developer implement this INCORRECTLY?
- What corners might they cut? (skip normalization, hardcode values, ignore edge cases)
- What math/logic might they get wrong? (off-by-one, wrong formula, missing terms)
- What would a lazy implementation look like? Write tests that would catch it.
- For each spec item, ask: "could this pass with a trivially wrong implementation?"
  If yes, make the test more specific.

Testing quality guidelines:
- NO trivial tests (assert exists, assert type, assert True). Every test must verify behavior that could break.
- Bundle tests that share expensive setup — don't duplicate identical fixtures across many tests.
- Use pytest.mark.parametrize for the same behavior with different inputs.
- Split tests when a failure would be ambiguous — each test should pinpoint one broken behavior.
- Prefer fewer strong tests over many weak ones.
- Always include a smoke test: if the project has a CLI, verify `python -m <package> --help` exits 0.

Write the test file now. Do NOT explain — just write the file.
"""
    try:
        # Create tests/ subdirectory in temp dir
        (Path(tmp_dir) / "tests").mkdir(parents=True, exist_ok=True)

        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=tmp_dir,
        )

        # Stream agent messages
        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                pass  # Final result
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                pass  # Duck-type ResultMessage
            elif hasattr(message, "content"):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                        log_lines.append(block.text)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        inputs = block.input or {}
                        detail = ""
                        if block.name in ("Read", "Glob", "Grep"):
                            detail = inputs.get("file_path") or inputs.get("path") or inputs.get("pattern") or ""
                        elif block.name in ("Edit", "Write"):
                            detail = inputs.get("file_path") or ""
                        elif block.name == "Bash":
                            cmd = inputs.get("command") or ""
                            detail = cmd[:80]
                        print(f"  → {block.name}  {detail}", flush=True)
                        log_lines.append(f"→ {block.name}  {detail}")

        # Check if test file was written in temp dir
        test_file_in_tmp = Path(tmp_dir) / test_rel
        if not test_file_in_tmp.exists():
            return None, log_lines

        # Copy to project dir
        dest = project_dir / test_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(test_file_in_tmp), str(dest))
        return dest, log_lines

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _subprocess_env() -> dict:
    """Return env dict with current Python's bin dir on PATH.

    Re-implemented here (mirrors otto.verify._subprocess_env) to avoid
    importing from verify which would create coupling.
    """
    venv_bin = str(Path(sys.executable).parent)
    env = os.environ.copy()
    existing = env.get("PATH", "")
    if venv_bin not in existing.split(os.pathsep):
        env["PATH"] = venv_bin + os.pathsep + existing
    return env


def validate_generated_tests(
    test_file: Path,
    framework: str,
    project_dir: Path,
) -> TestValidationResult:
    """Two-phase validation of generated test file.

    Phase 1: ``pytest --collect-only`` — checks syntax, imports, fixture resolution.
    Phase 2: ``pytest <file> -v`` — runs tests, counts passed/failed.

    Returns a TestValidationResult with status:
    - ``"collection_error"`` — syntax/import errors (test is broken)
    - ``"no_tests"`` — no test functions found
    - ``"all_pass"`` — all tests pass (tests may be trivial)
    - ``"tdd_ok"`` — some/all tests fail (TDD invariant holds)
    """
    env = _subprocess_env()

    # Phase 1: collection check
    try:
        collect = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(test_file)],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return TestValidationResult(
            status="collection_error",
            error_output="Collection timed out",
        )

    if collect.returncode != 0:
        return TestValidationResult(
            status="collection_error",
            error_output=(collect.stdout + collect.stderr)[:2000],
        )

    # Check if any tests were collected
    combined = collect.stdout + collect.stderr
    # pytest --collect-only -q outputs "N tests collected" or "no tests ran"
    if "no tests ran" in combined or "0 selected" in combined:
        return TestValidationResult(status="no_tests")
    # Also check for empty collection (e.g. file with no test_ functions)
    # pytest -q --collect-only lists test items, then "N tests collected"
    import re as _re
    collected_match = _re.search(r"(\d+) tests? collected", combined)
    if collected_match and int(collected_match.group(1)) == 0:
        return TestValidationResult(status="no_tests")

    # Phase 2: run tests
    try:
        run = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-v"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return TestValidationResult(
            status="collection_error",
            error_output="Test run timed out",
        )

    output = run.stdout + run.stderr

    # Parse passed/failed counts from pytest output
    # Typical lines: "2 passed", "1 failed, 1 passed", "2 failed"
    passed = 0
    failed = 0
    passed_match = _re.search(r"(\d+) passed", output)
    failed_match = _re.search(r"(\d+) failed", output)
    if passed_match:
        passed = int(passed_match.group(1))
    if failed_match:
        failed = int(failed_match.group(1))

    if passed == 0 and failed == 0:
        return TestValidationResult(status="no_tests", error_output=output[:2000])

    if failed > 0:
        return TestValidationResult(
            status="tdd_ok",
            passed=passed,
            failed=failed,
            error_output=output[:2000],
        )

    return TestValidationResult(
        status="all_pass",
        passed=passed,
        failed=failed,
    )


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
            return Path(f"tests/test_otto_{key}.py")
        case "jest" | "mocha":
            return Path(f"__tests__/test_otto_{key}.test.js")
        case "vitest":
            return Path(f"__tests__/test_otto_{key}.test.ts")
        case "go":
            return Path(f"test_otto_{key}_test.go")
        case "cargo":
            return Path(f"tests/test_otto_{key}.rs")
        case _:
            return Path(f"tests/test_otto_{key}.py")


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



def generate_integration_tests(
    tasks: list[dict],
    project_dir: Path,
) -> Path | None:
    """Generate cross-feature integration tests via claude -p.

    Takes ALL passed tasks and generates tests that exercise features
    working together — multi-step workflows crossing task boundaries.
    """
    framework = detect_test_framework(project_dir) or "pytest"
    # Build task hint from all task prompts for smart file selection
    all_prompts = " ".join(t.get("prompt", "") for t in tasks)
    context = build_blackbox_context(project_dir, task_hint=all_prompts)
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
- User journey tests: simulate a real session (e.g., add bookmarks → search → favorite a result → list favorites → verify the searched-and-favorited bookmark appears)
- State consistency: after feature A modifies data, feature B sees the updated state correctly
- Data round-trips: create via one feature, read via another, verify consistency
- Feature independence: using feature A does NOT break feature B's behavior
- Ordering: verify operations work regardless of the order features are used

Do NOT re-test individual features — those are already covered by per-task tests.
Test ONLY cross-feature interactions and multi-step workflows.

Include:
- A smoke test: run --help or a basic command to verify the app works with all features present.
- Full CLI-to-CLI pipelines: if one command produces output another consumes, test the pipeline
  end-to-end via subprocess (e.g., train a model → classify text → verify result).
- Data persistence across features: if feature A saves state, verify feature B can read it.
- Real user scenarios: what would a user actually do in a single session? Simulate that.

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


def run_mutation_check(
    project_dir: Path,
    test_file: Path,
    test_command: str,
    timeout: int = 60,
) -> tuple[bool, str]:
    """Run a simple mutation check: comment out a random non-trivial line in the
    most recently changed source file, run the test file, check if tests catch it.

    Returns (caught, description) where caught=True means tests detected the mutation.
    Restores the file after the check.
    """
    import random

    # Find the most recently changed source file via git diff
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if diff_result.returncode != 0 or not diff_result.stdout.strip():
        return False, "could not determine changed files"

    # Filter to source files (not tests, not configs)
    changed = []
    for f in diff_result.stdout.strip().splitlines():
        f = f.strip()
        if not f.endswith(".py"):
            continue
        name = Path(f).name
        if name.startswith("test_") or name == "conftest.py" or name == "__init__.py":
            continue
        if f.startswith("tests/") or f.startswith("test/"):
            continue
        if (project_dir / f).is_file():
            changed.append(f)

    if not changed:
        return False, "no source files changed"

    # Pick the first changed source file (most likely the implementation)
    target_file = project_dir / changed[0]
    original_content = target_file.read_text()
    lines = original_content.splitlines()

    # Find non-trivial lines (not blank, not comment, not import, not decorators)
    candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("import ") or stripped.startswith("from "):
            continue
        if stripped.startswith("@"):
            continue
        if stripped in ("pass", "...", '"""', "'''"):
            continue
        # Skip class/def declarations themselves (we want body lines)
        if stripped.startswith("class ") or stripped.startswith("def ") or stripped.startswith("async def "):
            continue
        candidates.append(i)

    if not candidates:
        return False, f"no mutable lines in {changed[0]}"

    # Pick a random non-trivial line
    line_idx = random.choice(candidates)
    mutated_line = lines[line_idx]
    description = f"commented out line {line_idx + 1} in {changed[0]}: {mutated_line.strip()[:60]}"

    # Apply mutation
    lines[line_idx] = "# MUTATION: " + mutated_line
    target_file.write_text("\n".join(lines) + "\n")

    try:
        # Run just the adversarial test file
        env = _subprocess_env()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-x", "-q"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        caught = result.returncode != 0
        return caught, description
    except subprocess.TimeoutExpired:
        return False, f"mutation test timed out ({description})"
    finally:
        # Always restore the file
        target_file.write_text(original_content)
