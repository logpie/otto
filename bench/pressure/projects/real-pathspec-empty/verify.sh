#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import importlib
import os
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
    # Try to actually instantiate with empty patterns instead of inspecting source
    try:
        mod = importlib.import_module("pathspec._backends.re2.pathspec")
    except ImportError:
        print("  (re2 backend not importable — checking source as fallback)")
        source = open("pathspec/_backends/re2/pathspec.py").read()
        if 'raise ValueError' in source:
            lines = source.split('\n')
            for i, line in enumerate(lines):
                if 'not patterns' in line and i + 1 < len(lines) and 'raise ValueError' in lines[i + 1]:
                    raise AssertionError(
                        "re2 backend still raises ValueError for empty patterns"
                    )
        return

    # Find the PathSpec-like class and try instantiating with empty patterns
    for attr_name in dir(mod):
        cls = getattr(mod, attr_name)
        if isinstance(cls, type) and hasattr(cls, '__init__'):
            try:
                cls([])  # empty patterns should not raise
            except ValueError as e:
                if 'empty' in str(e).lower() or 'pattern' in str(e).lower():
                    raise AssertionError(f"re2 backend raises ValueError on empty patterns: {e}")
            except (TypeError, Exception):
                pass  # other errors are fine — we only care about ValueError on empty


def check_hyperscan_no_raise_on_empty():
    """The hyperscan backend should not raise ValueError for empty patterns."""
    try:
        mod = importlib.import_module("pathspec._backends.hyperscan.pathspec")
    except ImportError:
        print("  (hyperscan backend not importable — checking source as fallback)")
        source = open("pathspec/_backends/hyperscan/pathspec.py").read()
        if 'raise ValueError' in source:
            lines = source.split('\n')
            for i, line in enumerate(lines):
                if 'not patterns' in line and i + 1 < len(lines) and 'raise ValueError' in lines[i + 1]:
                    raise AssertionError(
                        "hyperscan backend still raises ValueError for empty patterns"
                    )
        return

    for attr_name in dir(mod):
        cls = getattr(mod, attr_name)
        if isinstance(cls, type) and hasattr(cls, '__init__'):
            try:
                cls([])
            except ValueError as e:
                if 'empty' in str(e).lower() or 'pattern' in str(e).lower():
                    raise AssertionError(f"hyperscan backend raises ValueError on empty patterns: {e}")
            except (TypeError, Exception):
                pass


def check_default_backend_empty_still_works():
    """The default regex backend should still handle empty patterns."""
    from pathspec import PathSpec
    spec = PathSpec.from_lines('gitwildmatch', [])
    results = list(spec.match_files(['foo', 'bar']))
    assert results == [], f"Expected no matches for empty patterns, got {results}"


report("re2 backend does not raise ValueError on empty patterns", check_re2_no_raise_on_empty)
report("hyperscan backend does not raise ValueError on empty patterns", check_hyperscan_no_raise_on_empty)
report("Default backend still handles empty patterns", check_default_backend_empty_still_works)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
