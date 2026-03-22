"""Otto test generation — generate integration tests via Agent SDK."""

import ast
import asyncio
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
from otto.display import print_agent_tool


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


def get_relevant_file_contents(project_dir: Path, task_hint: str = "") -> str:
    """Return full contents of files relevant to a task.

    Uses the same AST symbol index + import graph as build_blackbox_context,
    but returns full file contents instead of stubs. For the coding agent
    who needs to read and edit files.
    """
    try:
        tree_result = subprocess.run(
            ["git", "ls-files"], cwd=project_dir,
            capture_output=True, text=True, timeout=10,
        )
        file_tree = tree_result.stdout.strip() if tree_result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""

    # Trust git: if it's tracked, it's probably relevant.
    # Only skip: generated dirs, lock files, test files, and binary/huge files.
    _SKIP_DIRS = {"node_modules", ".next", "dist", "build", "__pycache__",
                  "coverage", ".venv", "venv", "vendor", "target", ".cache",
                  ".turbo", ".vercel", ".output"}
    _SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                   "Cargo.lock", "go.sum"}  # lock files are huge noise
    _MAX_FILE_SIZE = 50_000  # skip files >50KB (likely generated/minified)

    source_files: list[str] = []
    for rel in file_tree.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        p = Path(rel)
        if p.name in _SKIP_NAMES:
            continue
        if any(skip in p.parts for skip in _SKIP_DIRS):
            continue
        # Skip test files — agent writes its own
        if p.name.startswith(("test_", "spec_")):
            continue
        if p.name.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                            ".spec.ts", ".spec.tsx")):
            continue
        if "__tests__" in p.parts:
            continue
        full = project_dir / rel
        if not full.is_file():
            continue
        # Skip binary/huge files by size
        try:
            if full.stat().st_size > _MAX_FILE_SIZE:
                continue
            # Quick binary check: try reading as UTF-8
            full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        source_files.append(rel)

    if not source_files:
        return ""

    if len(source_files) > _MAX_STUB_FILES and task_hint:
        sym, graph = _build_project_index(project_dir, source_files)
        selected = _find_relevant_files(task_hint, source_files, sym, graph)
    else:
        selected = source_files[:_MAX_STUB_FILES]

    parts: list[str] = []
    for rel in selected:
        full = project_dir / rel
        try:
            content = full.read_text()
            parts.append(f"# {rel}\n{content}")
        except (OSError, UnicodeDecodeError):
            continue

    return "\n\n".join(parts)


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
    spec: list[str],
    key: str,
    blackbox_context: str,
    project_dir: Path,
    framework: str = "pytest",
    quiet: bool = False,
    task_spec: str = "",
) -> tuple[Path | None, list[str], float]:
    """Run adversarial testgen agent in an isolated temp directory.

    The agent receives blackbox_context (public stubs, file tree) as a string
    in its prompt. It writes the test file in the temp dir. After generation,
    we copy the test file to the project's tests/ directory.

    This enforces mechanical isolation — the agent literally cannot read
    implementation code.

    Returns (path to the copied test file or None, list of log lines).
    """
    spec_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(spec))
    test_rel = f"tests/test_otto_{key}.py"
    tmp_dir = tempfile.mkdtemp(prefix="otto_testgen_")
    log_lines: list[str] = []

    # Include architect design context if available
    from otto.architect import load_design_context
    design_ctx = load_design_context(project_dir, role="coding")
    design_section = ""
    if design_ctx:
        design_section = f"\n\nTEST CONVENTIONS AND DATA MODEL (follow these):\n{design_ctx}\n"

    prompt = f"""You are an engineer writing acceptance tests that verify a spec is met.

SPEC:
{spec_text}

TASK DESCRIPTION:
{task_spec if task_spec else "(not provided)"}

PROJECT CONTEXT (public interface only — all context you need is here):
{blackbox_context}{design_section}

Your working directory is: {tmp_dir}
Write the test file to: {tmp_dir}/{test_rel}

IMPORTANT: Everything you need is in the SPEC and PROJECT CONTEXT above.
Start writing the test file IMMEDIATELY — do NOT explore broadly.
During self-review, if you need to verify specific details (exact function
signatures, enum values, flag names), you may read the relevant source file.

Requirements:
- Test EXACTLY what the spec says — no more, no less.
- Each spec item should have at least one test that directly verifies it.
- Test the public interface (CLI via subprocess, library via imports).
- Use subprocess.run() for CLI testing, not CliRunner.
- Tests should fail before implementation and pass after correct implementation.
- Be independent and hermetic (use tmp_path, no shared state).
- Do NOT invent requirements beyond the spec.
- NO trivial tests. Use pytest.mark.parametrize where appropriate.

CRITICAL IMPORT RULE:
- For NEW functions/classes that don't exist yet: import INSIDE each test function, not at module level.
  Example: def test_search(): from bookmarks import search_bookmarks
- For EXISTING functions (listed in project context): import at module level is fine.
- This ensures pytest can collect the tests even before the feature is implemented.
- Module-level imports of non-existent names cause collection errors which break the pipeline.

Steps:
1. WRITE the test file immediately (don't explore first)
2. VALIDATE: python -c "import ast; ast.parse(open('<test_file>').read()); print('OK')"
3. If syntax error: fix and re-validate
4. VALIDATE: python -m pytest --collect-only <test_file>
5. If collection fails: read relevant files to debug, fix, re-validate
6. SELF-REVIEW: Could a lazy implementation pass these tests? Strengthen if needed.
7. If improved in step 6, re-validate (steps 2-4)
"""
    try:
        # Create tests/ subdirectory in temp dir
        (Path(tmp_dir) / "tests").mkdir(parents=True, exist_ok=True)

        # Copy source files into temp dir so pytest --collect-only can resolve imports.
        # This doesn't break adversarial isolation — the agent's prompt only has stubs,
        # and we tell it not to read files. The copies just let pytest validate imports.
        import shutil as _shutil
        for src_file in project_dir.glob("*.py"):
            if not src_file.name.startswith("test_"):
                _shutil.copy2(str(src_file), str(Path(tmp_dir) / src_file.name))
        # Also copy tests/__init__.py and conftest.py if they exist
        src_tests = project_dir / "tests"
        if src_tests.is_dir():
            dst_tests = Path(tmp_dir) / "tests"
            for init_file in ["__init__.py", "conftest.py"]:
                src_init = src_tests / init_file
                if src_init.exists():
                    _shutil.copy2(str(src_init), str(dst_tests / init_file))

        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=tmp_dir,
        )

        # Stream agent messages
        testgen_cost = 0.0
        result_msg = None
        num_turns = 0
        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                result_msg = message
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    testgen_cost = float(raw_cost)
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                result_msg = message
                raw_cost = getattr(message, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    testgen_cost = float(raw_cost)
            elif hasattr(message, "content"):
                num_turns += 1
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        if not quiet:
                            print(block.text, flush=True)
                        log_lines.append(block.text)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        log_line = print_agent_tool(block, quiet=quiet)
                        log_lines.append(log_line)

        # Check if agent reported an error
        if result_msg and getattr(result_msg, "is_error", False):
            error_detail = getattr(result_msg, "result", None) or "unknown error"
            print(f"  testgen agent error: {error_detail}", flush=True)
            log_lines.append(f"ERROR: {error_detail}")
            return None, log_lines, testgen_cost

        # Check if agent never started (no result message at all)
        if num_turns == 0 and result_msg is None:
            print("  testgen agent produced no output — agent may have failed to start", flush=True)
            log_lines.append("ERROR: agent produced no output")
            return None, log_lines, testgen_cost

        # Check if test file was written in temp dir
        test_file_in_tmp = Path(tmp_dir) / test_rel
        if not test_file_in_tmp.exists():
            return None, log_lines, testgen_cost

        # Validate syntax before copying
        try:
            ast.parse(test_file_in_tmp.read_text())
        except SyntaxError as e:
            print(f"  testgen: generated test has syntax error: {e}", flush=True)
            return None, log_lines, testgen_cost

        # Copy to project dir
        dest = project_dir / test_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(test_file_in_tmp), str(dest))
        return dest, log_lines, testgen_cost

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _subprocess_env() -> dict:
    """Return env dict with current Python's bin dir on PATH.

    Mirrors otto.verify._subprocess_env — kept separate to avoid circular
    imports (verify.py imports from testgen.py).
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
    """Read existing test files to provide import style context.

    Prioritizes otto-generated tests (test_otto_*.py) so that later testgen
    agents follow the same conventions/helpers as earlier ones.
    """
    test_dirs = [project_dir / "tests", project_dir / "test"]
    otto_samples: list[str] = []
    other_samples: list[str] = []
    for test_dir in test_dirs:
        if not test_dir.is_dir():
            continue
        for f in sorted(test_dir.iterdir()):
            if f.suffix != ".py" or not f.name.startswith("test_"):
                continue
            try:
                content = f.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            # Otto-generated tests — include more (full helpers + first tests)
            if f.name.startswith("test_otto_"):
                lines = content.splitlines()[:80]
                otto_samples.append(f"# {f.relative_to(project_dir)}\n" + "\n".join(lines))
            elif len(other_samples) < 2:
                lines = content.splitlines()[:50]
                other_samples.append(f"# {f.relative_to(project_dir)}\n" + "\n".join(lines))
        if otto_samples or other_samples:
            break
    # Otto tests first (convention source), then regular tests
    return "\n\n".join(otto_samples + other_samples) if (otto_samples or other_samples) else ""


async def run_holistic_testgen(
    tasks: list[dict],
    project_dir: Path,
    blackbox_context: str,
    quiet: bool = False,
) -> dict[str, Path | None]:
    """Generate tests for ALL tasks in a single agent call.

    Produces consistent test files with shared conftest.py. Returns
    {key: test_file_path} for each task. Falls back to per-task testgen
    if holistic fails for a specific task.
    """
    from otto.architect import load_design_context

    task_sections = []
    for i, t in enumerate(tasks, 1):
        spec = t.get("spec", [])
        spec_text = "\n".join(f"   - {item}" for item in spec)
        task_sections.append(
            f"{i}. Task #{t.get('id', '?')} (key: {t['key']}): {t['prompt']}\n"
            f"   Spec:\n{spec_text}"
        )

    design_ctx = load_design_context(project_dir, role="coding")
    design_section = ""
    if design_ctx:
        design_section = f"\n\nTEST CONVENTIONS AND DATA MODEL (follow these):\n{design_ctx}\n"

    tmp_dir = tempfile.mkdtemp(prefix="otto_holistic_testgen_")

    # Build file list for agent
    test_files_list = []
    for t in tasks:
        test_files_list.append(f"- tests/test_otto_{t['key']}.py — tests for task #{t.get('id', '?')}")

    prompt = f"""You are an engineer writing acceptance tests for ALL features at once.

TASKS (each with its spec):
{chr(10).join(task_sections)}

PROJECT CONTEXT (public interface only — all context you need is here):
{blackbox_context}{design_section}

Your working directory is: {tmp_dir}
Write these files in {tmp_dir}:
- tests/conftest.py — shared fixtures for ALL test files
{chr(10).join(test_files_list)}

RULES:
- Test EXACTLY what each spec says — no more, no less.
- Each spec item should have at least one test that directly verifies it.
- Use CONSISTENT conventions across all test files.
- Share fixtures via conftest.py. Each file must be independently runnable.
- Test the public interface (CLI via subprocess, library via imports).
- Use subprocess.run() for CLI testing, not CliRunner.
- Tests should fail before implementation and pass after correct implementation.
- Be independent and hermetic (use tmp_path, no shared state).
- Do NOT invent requirements beyond the spec.
- NO trivial tests. Use pytest.mark.parametrize where appropriate.

CRITICAL IMPORT RULE:
- For NEW functions/classes that don't exist yet: import INSIDE each test function, not at module level.
  Example: def test_search(): from bookmarks import search_bookmarks
- For EXISTING functions (listed in project context): import at module level is fine.
- This ensures pytest can collect the tests even before the feature is implemented.
- Module-level imports of non-existent names cause collection errors which break the pipeline.

Steps:
1. WRITE conftest.py with shared fixtures
2. WRITE each test file
3. VALIDATE each: python -c "import ast; ast.parse(open('<file>').read()); print('OK')"
4. VALIDATE collection: python -m pytest --collect-only <file>
5. If errors: fix and re-validate
6. SELF-REVIEW: Could a lazy implementation pass these tests? Strengthen if needed.
   If unsure about exact API details, read the specific source file to verify.
"""

    results: dict[str, Path | None] = {}
    try:
        (Path(tmp_dir) / "tests").mkdir(parents=True, exist_ok=True)

        # Copy source files so pytest --collect-only can resolve imports
        import shutil as _shutil
        for src_file in project_dir.glob("*.py"):
            if not src_file.name.startswith("test_"):
                _shutil.copy2(str(src_file), str(Path(tmp_dir) / src_file.name))
        src_tests = project_dir / "tests"
        if src_tests.is_dir():
            dst_tests = Path(tmp_dir) / "tests"
            for init_file in ["__init__.py", "conftest.py"]:
                src_init = src_tests / init_file
                if src_init.exists():
                    _shutil.copy2(str(src_init), str(dst_tests / init_file))

        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=tmp_dir,
        )

        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                pass
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                pass
            elif hasattr(message, "content"):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        if not quiet:
                            print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block, quiet=quiet)

        # Collect generated test files
        from otto.config import git_meta_dir
        for t in tasks:
            key = t["key"]
            test_rel = f"tests/test_otto_{key}.py"
            test_in_tmp = Path(tmp_dir) / test_rel

            if not test_in_tmp.exists():
                results[key] = None
                continue

            # Validate syntax
            try:
                ast.parse(test_in_tmp.read_text())
            except SyntaxError:
                results[key] = None
                continue

            # Copy to testgen storage
            testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
            testgen_dir.mkdir(parents=True, exist_ok=True)
            dest = testgen_dir / f"test_otto_{key}.py"
            shutil.copy2(str(test_in_tmp), str(dest))
            results[key] = dest

        # Copy conftest.py if generated and valid
        conftest_tmp = Path(tmp_dir) / "tests" / "conftest.py"
        if conftest_tmp.exists():
            try:
                ast.parse(conftest_tmp.read_text())
                conftest_dest = project_dir / "tests" / "conftest.py"
                conftest_dest.parent.mkdir(parents=True, exist_ok=True)
                if not conftest_dest.exists():
                    shutil.copy2(str(conftest_tmp), str(conftest_dest))
            except SyntaxError:
                pass  # Skip invalid conftest

    except Exception as e:
        if not quiet:
            print(f"  holistic testgen error: {e}", flush=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return results


async def generate_tests(
    task_prompt: str,
    project_dir: Path,
    key: str,
) -> Path | None:
    """Generate integration tests via Agent SDK. Returns path to generated test file or None."""
    framework = detect_test_framework(project_dir) or "pytest"
    existing_tests = _read_existing_tests(project_dir)
    # Pre-implementation: use stubs only (same as adversarial — don't show full source)
    blackbox_ctx = build_blackbox_context(project_dir, task_hint=task_prompt)

    # Write to <git-common-dir>/otto/testgen/<key>/ (handles linked worktrees)
    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / key
    testgen_dir.mkdir(parents=True, exist_ok=True)

    rel_path = test_file_path(framework, key)
    out_file = testgen_dir / rel_path.name

    example_section = ""
    if existing_tests:
        example_section = f"""
EXISTING TESTS (for reference on fixtures/helpers — do NOT copy how they invoke the system under test):
{existing_tests}
"""

    prompt = f"""You are an engineer writing acceptance tests for a coding task.

TASK: {task_prompt}

PROJECT CONTEXT (public interface only):
{blackbox_ctx}
{example_section}
TEST FRAMEWORK: {framework}

Write integration tests that verify the task was completed correctly.
Write the test file to: {out_file}

Rules — "test like a user":
- Test the system the way a real user would use it:
  - CLI apps: use subprocess.run() to invoke the actual command. Check stdout, stderr, exit codes.
    Do NOT use in-process test runners (CliRunner, invoke()) — they skip the real entry point.
  - Libraries/APIs: import and call the public interface as a consumer would.
  - Web apps: make HTTP requests to the actual server endpoint.
- Tests must be hermetic and deterministic — no external network calls
- Mocks/fakes ONLY if the project already provides test fixtures for them
- Do NOT grep source code for strings — test actual behavior
- The tests should be runnable with the standard test command for {framework}

CRITICAL IMPORT RULE:
- For NEW functions/classes that don't exist yet: import INSIDE each test function, not at module level.
  Example: def test_search(): from bookmarks import search_bookmarks
- For EXISTING functions (listed in project context): import at module level is fine.
- This ensures pytest can collect the tests even before the feature is implemented.
- Module-level imports of non-existent names cause collection errors which break the pipeline.

Follow these steps:
1. Write the test file
2. VALIDATE syntax: python -c "import ast; ast.parse(open('<test_file>').read()); print('OK')"
3. If syntax error: fix and re-validate
4. VALIDATE collection: python -m pytest --collect-only <test_file>
5. If collection fails: fix and re-validate
6. SELF-REVIEW: Read your tests back and ask:
   - Are any tests trivial (would pass with a broken implementation)? Strengthen them.
   - Could a lazy implementation (return empty list, hardcoded value) pass? Add tests that catch it.
   - Do assertions verify actual behavior or just check types/existence? Tighten them.
   - Unsure about exact API details? Read the specific source file to verify.
7. If you improved tests in step 6, re-run validation (steps 2-5)
Do NOT finish until validation passes AND self-review is done."""

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
        )

        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                pass
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                pass
            elif hasattr(message, "content"):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block)

        if out_file.exists():
            return out_file
        return None

    except Exception as e:
        print(f"  testgen agent error: {e}", file=sys.stderr, flush=True)
        return None



async def generate_integration_tests(
    tasks: list[dict],
    project_dir: Path,
    ripple_risks: list[tuple[int, str, str]] | None = None,
) -> Path | None:
    """Generate cross-feature integration tests via Agent SDK.

    Takes ALL passed tasks and generates tests that exercise features
    working together — multi-step workflows crossing task boundaries.
    ripple_risks: list of (task_id, changed_file, affected_file) from reconciliation.
    """
    framework = detect_test_framework(project_dir) or "pytest"
    existing_tests = _read_existing_tests(project_dir)

    # Include relevant source files so agent doesn't need to explore
    all_prompts = " ".join(t.get("prompt", "") for t in tasks)
    source_context = get_relevant_file_contents(project_dir, task_hint=all_prompts)

    task_sections = []
    for i, task in enumerate(tasks, 1):
        section = f"FEATURE {i}: {task['prompt']}"
        spec = task.get("spec", [])
        if spec:
            section += "\nSpec:\n" + "\n".join(f"  - {r}" for r in spec)
        task_sections.append(section)

    example_section = ""
    if existing_tests:
        example_section = f"""
EXISTING TESTS (for reference on fixtures/helpers — do NOT copy how they invoke the system under test):
{existing_tests}
"""

    testgen_dir = git_meta_dir(project_dir) / "otto" / "testgen" / "_integration"
    testgen_dir.mkdir(parents=True, exist_ok=True)
    out_file = testgen_dir / "otto_integration.py"

    ripple_section = ""
    if ripple_risks:
        lines = []
        for tid, changed, affected in ripple_risks:
            lines.append(f"  {affected} imports {changed} (changed by task #{tid})")
        ripple_section = (
            "\n\nRIPPLE RISKS — these files import from files changed by tasks "
            "but were not part of any task:\n" + "\n".join(lines) +
            "\nWrite integration tests that specifically exercise these interactions.\n"
        )

    # Include architect design context for integration tests
    from otto.architect import load_design_context
    integ_design_ctx = load_design_context(project_dir, role="integration")
    integ_design_section = ""
    if integ_design_ctx:
        integ_design_section = f"\n\nARCHITECTURE AND TEST CONVENTIONS (follow these):\n{integ_design_ctx}\n"

    prompt = f"""You are an engineer writing cross-feature acceptance tests.

The following features were implemented:

{chr(10).join(task_sections)}

PROJECT DIRECTORY: {project_dir}

RELEVANT SOURCE FILES (already read for you — start writing tests immediately):
{source_context}
{example_section}{ripple_section}{integ_design_section}
TEST FRAMEWORK: {framework}

Write integration tests that exercise these features WORKING TOGETHER.
Write the test file to: {out_file}

Focus on:
- User journey tests: simulate a real session (e.g., add bookmarks -> search -> favorite a result -> list favorites -> verify the searched-and-favorited bookmark appears)
- State consistency: after feature A modifies data, feature B sees the updated state correctly
- Data round-trips: create via one feature, read via another, verify consistency
- Feature independence: using feature A does NOT break feature B's behavior
- Ordering: verify operations work regardless of the order features are used

Do NOT re-test individual features — those are already covered by per-task tests.
Test ONLY cross-feature interactions and multi-step workflows.

Include:
- A smoke test: run --help or a basic command to verify the app works with all features present.
- Full CLI-to-CLI pipelines: if one command produces output another consumes, test the pipeline
  end-to-end via subprocess (e.g., train a model -> classify text -> verify result).
- Data persistence across features: if feature A saves state, verify feature B can read it.
- Real user scenarios: what would a user actually do in a single session? Simulate that.

Rules — "test like a user":
- CLI apps: use subprocess.run() to invoke the actual command. Check stdout, stderr, exit codes.
  Do NOT use in-process test runners (CliRunner, invoke()).
- Libraries/APIs: import and call the public interface as a consumer would.
- Tests must be hermetic and deterministic — no external network calls
- The tests should be runnable with the standard test command for {framework}

CRITICAL IMPORT RULE:
- For NEW functions/classes that were just implemented: import INSIDE each test function, not at module level.
  Example: def test_search(): from bookmarks import search_bookmarks
- For EXISTING functions (already in the codebase before this run): import at module level is fine.
- This ensures pytest can collect the tests even if run before all features are merged.
- Module-level imports of non-existent names cause collection errors which break the pipeline.

Follow these steps:
1. Write the test file
2. VALIDATE syntax: python -c "import ast; ast.parse(open('<test_file>').read()); print('OK')"
3. If syntax error: fix and re-validate
4. VALIDATE collection: python -m pytest --collect-only <test_file>
5. If collection fails: fix and re-validate
6. SELF-REVIEW: Read your tests back and ask:
   - Are any tests trivial (would pass with a broken implementation)? Strengthen them.
   - Could a lazy implementation (return empty list, hardcoded value) pass? Add tests that catch it.
   - Do assertions verify actual behavior or just check types/existence? Tighten them.
   - Unsure about exact API details? Read the specific source file to verify.
7. If you improved tests in step 6, re-run validation (steps 2-5)
Do NOT finish until validation passes AND self-review is done."""

    try:
        agent_opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=str(project_dir),
        )

        async for message in query(prompt=prompt, options=agent_opts):
            if isinstance(message, ResultMessage):
                pass
            elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                pass
            elif hasattr(message, "content"):
                for block in message.content:
                    if TextBlock and isinstance(block, TextBlock) and block.text:
                        print(block.text, flush=True)
                    elif ToolUseBlock and isinstance(block, ToolUseBlock):
                        print_agent_tool(block)

        if out_file.exists():
            return out_file
        return None

    except Exception as e:
        print(f"  integration testgen agent error: {e}", file=sys.stderr, flush=True)
        return None


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
