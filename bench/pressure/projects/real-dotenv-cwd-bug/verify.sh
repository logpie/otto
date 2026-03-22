#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import ast
import os
import shutil
import sys
import tempfile

sys.path.insert(0, 'src')

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
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == 'default':
            if isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr == 'join':
                    for arg in node.value.args:
                        if isinstance(arg, ast.Call):
                            if isinstance(arg.func, ast.Attribute) and arg.func.attr == 'getcwd':
                                raise AssertionError(
                                    "os.getcwd() is still called eagerly in click.option default"
                                )


def check_cli_import_no_crash_when_cwd_missing():
    """Importing cli module should not crash when CWD is deleted."""
    # Create a temp dir, chdir into it, delete it, then try importing cli
    tmpdir = tempfile.mkdtemp()
    original_cwd = os.getcwd()
    try:
        os.chdir(tmpdir)
        shutil.rmtree(tmpdir)
        # Force reimport of cli module
        if 'dotenv.cli' in sys.modules:
            del sys.modules['dotenv.cli']
        try:
            import dotenv.cli  # noqa: F401
        except FileNotFoundError:
            raise AssertionError(
                "Importing dotenv.cli crashes with FileNotFoundError when CWD is missing"
            )
        except Exception:
            # Other errors (e.g. missing click) are OK — the point is no FileNotFoundError
            pass
    finally:
        os.chdir(original_cwd)


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
report("CLI import does not crash when CWD is missing", check_cli_import_no_crash_when_cwd_missing)
report("No bare os.getcwd() in decorator line", check_no_bare_getcwd_in_decorator)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
