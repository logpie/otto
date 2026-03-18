"""Deterministic test quality validation — static AST checks on generated tests.

Runs after testgen produces a test file, before handing it to the coding agent.
All checks are pure AST analysis — no LLM calls, no execution, deterministic.

Catches:
- Root Cause #1: Test logic errors (tautological/unreachable assertions)
- Root Cause #3: Wrong API assumptions (nonexistent imports, CLI commands, fixtures)
- Root Cause #4: Anti-patterns (unconditional skip, xfail, monkeypatch SUT)
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestWarning:
    __test__ = False  # prevent pytest from collecting this as a test class
    """A validation finding with severity and actionable message."""
    severity: str  # "error", "warning", "info"
    check: str     # which check produced this
    message: str   # human-readable, actionable for testgen agent
    line: int = 0  # line number in test file (0 = file-level)

    def __str__(self):
        loc = f":{self.line}" if self.line else ""
        return f"[{self.severity.upper()}] {self.check}{loc}: {self.message}"


def validate_test_quality(
    test_file: Path,
    project_dir: Path,
    framework: str = "pytest",
) -> list[TestWarning]:
    """Run static quality checks on a generated test file.

    Returns list of warnings with severity and actionable message.
    Only checks pytest/Python tests for v1.
    """
    if framework != "pytest":
        return []

    try:
        source = test_file.read_text()
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return [TestWarning("error", "parse", "Test file has syntax errors")]

    warnings: list[TestWarning] = []

    # Run all checks
    warnings.extend(_check_assertions(tree))
    warnings.extend(_check_anti_patterns(tree))
    warnings.extend(_check_imports(tree, project_dir))
    warnings.extend(_check_cli_commands(tree, project_dir))
    warnings.extend(_check_discovery(test_file, project_dir))

    return warnings


# ---------------------------------------------------------------------------
# Check 1: CLI command validation
# ---------------------------------------------------------------------------

def _check_cli_commands(tree: ast.AST, project_dir: Path) -> list[TestWarning]:
    """Verify subprocess CLI calls reference valid project commands."""
    warnings: list[TestWarning] = []

    # Detect CLI surface — look for Click decorators in project source
    cli_commands = _detect_click_commands(project_dir)
    if cli_commands is None:
        return []  # No Click CLI detected, skip check

    # Find the project package name
    pkg_name = _detect_package_name(project_dir)

    # Walk all subprocess.run calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match subprocess.run([...]) or subprocess.run(["python", "-m", ...])
        if not (_is_subprocess_run(func)):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.List):
            continue

        # Extract command parts from the list literal
        parts = []
        for elt in arg.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                parts.append(elt.value)
            else:
                parts.append("?")  # non-literal

        if len(parts) < 3:
            continue

        # Check "python -m <package> <subcommand>"
        if parts[1] == "-m" and pkg_name:
            module = parts[2]
            if module != pkg_name and not module.startswith(f"{pkg_name}."):
                warnings.append(TestWarning(
                    "error", "cli_command",
                    f"subprocess calls 'python -m {module}' but project package is '{pkg_name}'",
                    line=node.lineno,
                ))
            if len(parts) > 3 and parts[3] != "?" and cli_commands:
                subcmd = parts[3]
                if not subcmd.startswith("-") and subcmd not in cli_commands:
                    warnings.append(TestWarning(
                        "warning", "cli_command",
                        f"subcommand '{subcmd}' not found in CLI (known: {', '.join(sorted(cli_commands))})",
                        line=node.lineno,
                    ))

    return warnings


def _is_subprocess_run(func) -> bool:
    """Check if a call target is subprocess.run."""
    if isinstance(func, ast.Attribute) and func.attr == "run":
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            return True
    return False


def _detect_click_commands(project_dir: Path) -> set[str] | None:
    """Detect Click CLI commands from project source. Returns None if no Click CLI found."""
    commands: set[str] = set()
    found_click = False

    for py_file in project_dir.rglob("*.py"):
        # Skip tests, venvs, hidden dirs
        rel = str(py_file.relative_to(project_dir))
        if any(part.startswith(".") or part in ("__pycache__", "tests", "test", "venv", ".venv")
               for part in py_file.parts):
            continue

        try:
            source = py_file.read_text()
            if "click" not in source and "@" not in source:
                continue
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    # @main.command() or @main.command(name="list")
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                        if dec.func.attr == "command":
                            found_click = True
                            # Check for name= keyword arg
                            cmd_name = node.name
                            for kw in dec.keywords:
                                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                    cmd_name = kw.value.value
                            commands.add(cmd_name)
                    # @click.command()
                    elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                        if isinstance(dec.func.value, ast.Name) and dec.func.value.id == "click":
                            if dec.func.attr in ("command", "group"):
                                found_click = True

    return commands if found_click else None


def _detect_package_name(project_dir: Path) -> str | None:
    """Detect the project's Python package name."""
    # Check for __main__.py in subdirectories (skip tests/test dirs)
    for d in project_dir.iterdir():
        if (d.is_dir() and not d.name.startswith((".", "_"))
                and d.name not in ("tests", "test", "venv", ".venv")
                and (d / "__init__.py").exists()):
            return d.name
    # Check pyproject.toml
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            match = re.search(r'name\s*=\s*"([^"]+)"', content)
            if match:
                return match.group(1).replace("-", "_")
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# Check 2: Assertion analysis
# ---------------------------------------------------------------------------

def _walk_no_nested_funcs(node: ast.AST):
    """Walk AST children but don't descend into nested function/class defs."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _walk_no_nested_funcs(child)


def _check_assertions(tree: ast.AST) -> list[TestWarning]:
    """Check for tautological, unreachable, or suspicious assertions."""
    warnings: list[TestWarning] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue

        has_sut_call = False
        has_assert = False

        for child in _walk_no_nested_funcs(node):
            # Track SUT calls
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == "run":
                    has_sut_call = True
                elif isinstance(func, ast.Name) and child != node:
                    # Any function call could be a helper that calls SUT
                    has_sut_call = True
                elif isinstance(func, ast.Attribute):
                    has_sut_call = True

            # Track assertions
            if isinstance(child, ast.Assert):
                has_assert = True

                # Check for tautological assertions
                test = child.test
                if isinstance(test, ast.Constant) and test.value is True:
                    warnings.append(TestWarning(
                        "error", "assertion",
                        "Tautological assertion: 'assert True' always passes",
                        line=child.lineno,
                    ))
                elif isinstance(test, ast.Compare):
                    if (len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                            and len(test.comparators) == 1):
                        left = ast.dump(test.left)
                        right = ast.dump(test.comparators[0])
                        if left == right:
                            # Covers both "x == x" and "1 == 1"
                            if (isinstance(test.left, ast.Constant)
                                    and isinstance(test.comparators[0], ast.Constant)):
                                warnings.append(TestWarning(
                                    "error", "assertion",
                                    f"Tautological assertion: 'assert {test.left.value} == {test.comparators[0].value}'",
                                    line=child.lineno,
                                ))
                            else:
                                warnings.append(TestWarning(
                                    "error", "assertion",
                                    "Tautological assertion: comparing value to itself",
                                    line=child.lineno,
                                ))

        # Check for unreachable assertions
        for i, stmt in enumerate(node.body):
            if isinstance(stmt, ast.Return) and i < len(node.body) - 1:
                # Check if anything after return has an assert
                remaining = node.body[i+1:]
                for rest in remaining:
                    for child in ast.walk(rest):
                        if isinstance(child, ast.Assert):
                            warnings.append(TestWarning(
                                "error", "assertion",
                                "Unreachable assertion after unconditional return",
                                line=child.lineno,
                            ))

        # No SUT call warning — only if also no function calls at all
        if has_assert and not has_sut_call:
            warnings.append(TestWarning(
                "warning", "assertion",
                f"Test '{node.name}' has assertions but no apparent SUT call (may be in fixture/helper)",
                line=node.lineno,
            ))

    return warnings


# ---------------------------------------------------------------------------
# Check 3: Import & fixture validation
# ---------------------------------------------------------------------------

def _check_imports(tree: ast.AST, project_dir: Path) -> list[TestWarning]:
    """Check that test imports reference modules that exist."""
    warnings: list[TestWarning] = []
    pkg_name = _detect_package_name(project_dir)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module

            # Check "from tests.conftest import X" — verify conftest exports X
            if module == "tests.conftest" or module == "conftest":
                conftest_path = project_dir / "tests" / "conftest.py"
                if conftest_path.exists():
                    try:
                        conftest_tree = ast.parse(conftest_path.read_text())
                        conftest_names = _get_defined_names(conftest_tree)
                        for alias in (node.names or []):
                            name = alias.name
                            if name not in conftest_names:
                                warnings.append(TestWarning(
                                    "error", "import",
                                    f"'from {module} import {name}' but conftest.py does not define '{name}'",
                                    line=node.lineno,
                                ))
                    except (OSError, SyntaxError):
                        pass
                else:
                    warnings.append(TestWarning(
                        "error", "import",
                        f"'from {module} import ...' but tests/conftest.py does not exist",
                        line=node.lineno,
                    ))

            # Check project module imports
            elif pkg_name and module.startswith(pkg_name):
                # Convert module path to file path
                parts = module.split(".")
                mod_path = project_dir / "/".join(parts)
                mod_file = project_dir / ("/".join(parts) + ".py")
                pkg_init = mod_path / "__init__.py"
                if not mod_file.exists() and not pkg_init.exists() and not mod_path.with_suffix(".py").exists():
                    # Could be a module that will be created by the implementation
                    # Only error for clearly impossible (parent package doesn't exist)
                    parent_parts = parts[:-1]
                    if parent_parts:
                        parent_path = project_dir / "/".join(parent_parts)
                        if parent_path.exists() or (parent_path.with_suffix(".py")).exists():
                            # Parent exists, child module might be created — warning
                            pass  # OK, feature may add this module
                        else:
                            warnings.append(TestWarning(
                                "error", "import",
                                f"'import {module}' but parent package does not exist",
                                line=node.lineno,
                            ))

    return warnings


def _get_defined_names(tree: ast.AST) -> set[str]:
    """Get all top-level names defined in a module (functions, classes, variables)."""
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name
                names.add(name)
    return names


# ---------------------------------------------------------------------------
# Check 4: Anti-patterns
# ---------------------------------------------------------------------------

def _check_anti_patterns(tree: ast.AST) -> list[TestWarning]:
    """Check for patterns that neuter or weaken the test suite."""
    warnings: list[TestWarning] = []

    for node in ast.walk(tree):
        # Check decorators on test functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_str = ast.dump(dec)
                # @pytest.mark.skip (unconditional)
                if "pytest" in dec_str and "skip" in dec_str:
                    # Check if it's conditional (skipif) or unconditional (skip)
                    if "skipif" not in dec_str:
                        warnings.append(TestWarning(
                            "error", "anti_pattern",
                            f"Unconditional @pytest.mark.skip on '{node.name}' — test will never run",
                            line=node.lineno,
                        ))
                # @pytest.mark.xfail
                if "pytest" in dec_str and "xfail" in dec_str:
                    warnings.append(TestWarning(
                        "error", "anti_pattern",
                        f"@pytest.mark.xfail on '{node.name}' — defeats TDD invariant",
                        line=node.lineno,
                    ))

        # Check for pytest.skip() calls (unconditional, at module or function level)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "pytest"
                        and node.func.attr == "skip"):
                    warnings.append(TestWarning(
                        "error", "anti_pattern",
                        "Unconditional pytest.skip() call — test will never run",
                        line=node.lineno,
                    ))

        # Check for monkeypatching subprocess.run in CLI tests
        if isinstance(node, ast.Call):
            func = node.func
            # Match: patch("subprocess.run"), mock_patch("subprocess.run"),
            # monkeypatch.setattr("subprocess", "run", ...)
            is_patch = False
            if isinstance(func, ast.Name):
                # Direct call or aliased import (patch, mock_patch, etc.)
                # Check if the name was imported from unittest.mock
                if "patch" in func.id.lower():
                    is_patch = True
            elif isinstance(func, ast.Attribute) and func.attr in ("setattr", "patch"):
                is_patch = True
            if is_patch:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if "subprocess" in arg.value and "run" in arg.value:
                            warnings.append(TestWarning(
                                "error", "anti_pattern",
                                "Monkeypatching subprocess.run defeats CLI testing",
                                line=node.lineno,
                            ))

        # Non-hermetic patterns (warnings)
        if isinstance(node, ast.Call):
            func = node.func
            func_str = ""
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name):
                    func_str = f"{func.value.id}.{func.attr}"
                func_str = func.attr if not func_str else func_str
            elif isinstance(func, ast.Name):
                func_str = func.id

            if func_str in ("time.sleep", "sleep"):
                warnings.append(TestWarning(
                    "warning", "anti_pattern",
                    "time.sleep() in tests — may cause flakiness",
                    line=node.lineno,
                ))
            if func_str in ("random.choice", "random.randint", "random.random"):
                warnings.append(TestWarning(
                    "warning", "anti_pattern",
                    "random in tests — may cause non-deterministic results",
                    line=node.lineno,
                ))

        # Broad exception swallowing
        if isinstance(node, ast.ExceptHandler):
            if node.type is None or (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                # Check if body has assert
                for child in ast.walk(node):
                    if isinstance(child, ast.Assert):
                        warnings.append(TestWarning(
                            "warning", "anti_pattern",
                            "Assertion inside broad 'except Exception' — may hide real failures",
                            line=node.lineno,
                        ))
                        break

    return warnings


# ---------------------------------------------------------------------------
# Check 5: Test discovery
# ---------------------------------------------------------------------------

def _check_discovery(test_file: Path, project_dir: Path) -> list[TestWarning]:
    """Best-effort check that the test file follows pytest discovery conventions."""
    warnings: list[TestWarning] = []

    name = test_file.name
    if not name.startswith("test_") and not name.endswith("_test.py"):
        warnings.append(TestWarning(
            "info", "discovery",
            f"File '{name}' doesn't follow pytest naming convention (test_*.py or *_test.py)",
        ))

    # Check if file is in a tests/ directory
    try:
        rel = test_file.relative_to(project_dir)
        parts = rel.parts
        if "tests" not in parts and "test" not in parts:
            warnings.append(TestWarning(
                "info", "discovery",
                f"File not in tests/ or test/ directory — may not be discovered by pytest",
            ))
    except ValueError:
        pass  # file not under project_dir

    return warnings
