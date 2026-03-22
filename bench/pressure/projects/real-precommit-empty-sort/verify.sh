#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py test_empty.txt test_nonempty.txt test_sorted.txt; exit $rc' EXIT

cat > verify_check.py <<'PY'
import sys
import os

sys.path.insert(0, '.')
from pre_commit_hooks.file_contents_sorter import sort_file_contents, FAIL, PASS

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_empty_file_unchanged():
    """An empty file should remain empty and return PASS (no modification)."""
    fname = "test_empty.txt"
    with open(fname, "wb") as f:
        f.write(b"")
    with open(fname, "rb+") as f:
        result = sort_file_contents(f, key=None)
    assert result == PASS, f"Expected PASS for empty file, got {result}"
    with open(fname, "rb") as f:
        content = f.read()
    assert content == b"", f"Empty file should stay empty, got {content!r}"


def check_nonempty_file_sorted():
    """A non-empty file should still be sorted properly with trailing newline."""
    fname = "test_nonempty.txt"
    with open(fname, "wb") as f:
        f.write(b"banana\napple\ncherry\n")
    with open(fname, "rb+") as f:
        result = sort_file_contents(f, key=None)
    assert result == FAIL, f"Expected FAIL (file was modified), got {result}"
    with open(fname, "rb") as f:
        content = f.read()
    assert content == b"apple\nbanana\ncherry\n", f"Expected sorted content, got {content!r}"


def check_already_sorted_unchanged():
    """An already sorted file should return PASS."""
    fname = "test_sorted.txt"
    with open(fname, "wb") as f:
        f.write(b"apple\nbanana\ncherry\n")
    with open(fname, "rb+") as f:
        result = sort_file_contents(f, key=None)
    assert result == PASS, f"Expected PASS for sorted file, got {result}"


def check_source_code_fix():
    """The fix should conditionally add the trailing newline."""
    source = open("pre_commit_hooks/file_contents_sorter.py").read()
    # The buggy code has: after_string = b'\\n'.join(after) + b'\\n'
    # The fix should check if after_string is non-empty before adding newline
    assert "if after_string" in source or "if after" in source or "if len" in source, \
        "Expected a conditional check before adding trailing newline"


report("Empty file remains empty", check_empty_file_unchanged)
report("Non-empty file is sorted correctly", check_nonempty_file_sorted)
report("Already sorted file returns PASS", check_already_sorted_unchanged)
report("Source code has conditional newline", check_source_code_fix)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
