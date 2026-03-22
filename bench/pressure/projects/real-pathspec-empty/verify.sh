#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import sys
sys.path.insert(0, '.')

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_re2_no_raise_on_empty():
    """The re2 backend should not raise ValueError for empty patterns."""
    source = open("pathspec/_backends/re2/pathspec.py").read()
    # Before fix: 'if not patterns:\n\t\traise ValueError'
    # After fix: the ValueError raise for empty patterns is removed
    if 'raise ValueError' in source:
        # Check if it's guarded by "if not patterns"
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if 'not patterns' in line and i + 1 < len(lines) and 'raise ValueError' in lines[i + 1]:
                raise AssertionError(
                    "re2 backend still raises ValueError for empty patterns"
                )


def check_hyperscan_no_raise_on_empty():
    """The hyperscan backend should not raise ValueError for empty patterns."""
    source = open("pathspec/_backends/hyperscan/pathspec.py").read()
    if 'raise ValueError' in source:
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if 'not patterns' in line and i + 1 < len(lines) and 'raise ValueError' in lines[i + 1]:
                raise AssertionError(
                    "hyperscan backend still raises ValueError for empty patterns"
                )


def check_re2_handles_empty_gracefully():
    """The re2 backend code should handle the case of no patterns being passed."""
    source = open("pathspec/_backends/re2/pathspec.py").read()
    # After fix, there should be handling for when patterns is empty
    # The fix changes 'if not patterns: raise' to 'if patterns and not isinstance...'
    assert 'patterns and not isinstance' in source or \
           'if not patterns' not in source or \
           'if patterns:' in source, \
        "re2 backend should handle empty patterns without raising"


def check_default_backend_empty_still_works():
    """The default regex backend should still handle empty patterns."""
    from pathspec import PathSpec
    spec = PathSpec.from_lines('gitwildmatch', [])
    results = list(spec.match_files(['foo', 'bar']))
    assert results == [], f"Expected no matches for empty patterns, got {results}"


report("re2 backend does not raise ValueError on empty patterns", check_re2_no_raise_on_empty)
report("hyperscan backend does not raise ValueError on empty patterns", check_hyperscan_no_raise_on_empty)
report("re2 backend handles empty gracefully", check_re2_handles_empty_gracefully)
report("Default backend still handles empty patterns", check_default_backend_empty_still_works)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
