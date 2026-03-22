#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py test_bpdb.py test_pdb.py test_clean.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import sys
import tempfile
import os

sys.path.insert(0, '.')
from pre_commit_hooks.debug_statement_hook import main as debug_main, DEBUG_STATEMENTS

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_bpdb_in_debug_statements():
    """bpdb should be in the DEBUG_STATEMENTS set."""
    assert "bpdb" in DEBUG_STATEMENTS, \
        f"'bpdb' not found in DEBUG_STATEMENTS: {DEBUG_STATEMENTS}"


def check_bpdb_import_detected():
    """'import bpdb' should be detected as a debug statement."""
    fname = "test_bpdb.py"
    with open(fname, "w") as f:
        f.write("import bpdb\n\ndef foo():\n    pass\n")
    ret = debug_main([fname])
    assert ret != 0, f"Expected non-zero return for 'import bpdb', got {ret}"


def check_pdb_still_detected():
    """'import pdb' should still be detected (regression check)."""
    fname = "test_pdb.py"
    with open(fname, "w") as f:
        f.write("import pdb\n\ndef bar():\n    pass\n")
    ret = debug_main([fname])
    assert ret != 0, f"Expected non-zero return for 'import pdb', got {ret}"


def check_clean_file_passes():
    """A file without debug imports should pass."""
    fname = "test_clean.py"
    with open(fname, "w") as f:
        f.write("import os\nimport sys\n\ndef baz():\n    return 42\n")
    ret = debug_main([fname])
    assert ret == 0, f"Expected zero return for clean file, got {ret}"


report("bpdb is in DEBUG_STATEMENTS", check_bpdb_in_debug_statements)
report("'import bpdb' is detected", check_bpdb_import_detected)
report("'import pdb' still detected", check_pdb_still_detected)
report("Clean file passes", check_clean_file_passes)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
