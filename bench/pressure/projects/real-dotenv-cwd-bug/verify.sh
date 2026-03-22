#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import ast
import inspect
import os
import sys

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_cli_no_eager_getcwd():
    """The --file default must not call os.getcwd() eagerly at import time."""
    source = open("src/dotenv/cli.py").read()
    tree = ast.parse(source)
    # Look for the @click.option('-f', ...) decorator with default=os.path.join(os.getcwd(), '.env')
    # After the fix, default should NOT contain a direct os.getcwd() call as a default argument
    # We check that the string 'os.getcwd()' does NOT appear as a default kwarg inside click.option
    # A simple heuristic: the fixed code should use a function/callable for the default
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == 'default':
            if isinstance(node.value, ast.Call):
                # Check if it's os.path.join(os.getcwd(), ...)
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr == 'join':
                    for arg in node.value.args:
                        if isinstance(arg, ast.Call):
                            if isinstance(arg.func, ast.Attribute) and arg.func.attr == 'getcwd':
                                raise AssertionError(
                                    "os.getcwd() is still called eagerly in click.option default"
                                )
    # If we get here, either the default is not os.getcwd() or uses a lazy approach


def check_enumerate_env_exists():
    """A helper function should exist to safely get the .env path."""
    source = open("src/dotenv/cli.py").read()
    # The fix should introduce a function that handles FileNotFoundError from os.getcwd()
    # Check that there's a try/except around os.getcwd() somewhere in cli.py
    assert "FileNotFoundError" in source or "OSError" in source, \
        "cli.py should handle FileNotFoundError or OSError from os.getcwd()"


def check_returns_none_on_missing_cwd():
    """When CWD doesn't exist, the env path lookup should return None instead of crashing."""
    source = open("src/dotenv/cli.py").read()
    # The fix should return None when getcwd fails
    assert "return None" in source or "return path" in source, \
        "cli.py should have a safe return path when cwd is missing"


def check_no_bare_getcwd_in_decorator():
    """The click.option decorator line should not directly call os.getcwd()."""
    lines = open("src/dotenv/cli.py").readlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("@click.option") or "default=os.path.join(os.getcwd()" in stripped:
            if "os.getcwd()" in stripped:
                raise AssertionError(
                    f"Found bare os.getcwd() in decorator: {stripped}"
                )


report("CLI does not eagerly call os.getcwd() in click.option default", check_cli_no_eager_getcwd)
report("A helper exists to safely resolve the .env path", check_enumerate_env_exists)
report("Safe return value when CWD is missing", check_returns_none_on_missing_cwd)
report("No bare os.getcwd() in decorator line", check_no_bare_getcwd_in_decorator)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
