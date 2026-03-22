#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
from iniconfig import IniConfig

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_parse_classmethod_exists():
    """IniConfig should have a parse() classmethod."""
    assert hasattr(IniConfig, 'parse'), "IniConfig has no 'parse' method"
    # It should be callable as a classmethod
    assert callable(getattr(IniConfig, 'parse')), "IniConfig.parse is not callable"


def check_strip_inline_comments():
    """parse() with strip_inline_comments=True should remove inline comments."""
    data = "[section]\nkey = value # this is a comment\n"
    config = IniConfig.parse("test.ini", data, strip_inline_comments=True)
    val = config.get("section", "key")
    assert val == "value", f"Expected 'value', got '{val!r}'"


def check_no_strip_by_default():
    """parse() should preserve inline comments when strip_inline_comments=False."""
    data = "[section]\nkey = value # not a comment\n"
    config = IniConfig.parse("test.ini", data, strip_inline_comments=False)
    val = config.get("section", "key")
    assert "# not a comment" in val, f"Expected inline comment preserved, got '{val!r}'"


def check_strip_section_whitespace():
    """parse() with strip_section_whitespace=True should strip whitespace from section names."""
    data = "[  section  ]\nkey = value\n"
    config = IniConfig.parse("test.ini", data, strip_inline_comments=False, strip_section_whitespace=True)
    val = config.get("section", "key")
    assert val == "value", f"Expected 'value', got '{val!r}'"


def check_basic_parse_works():
    """parse() should correctly parse a basic INI file."""
    data = "[defaults]\nname = test\ncount = 5\n"
    config = IniConfig.parse("test.ini", data, strip_inline_comments=False)
    assert config.get("defaults", "name") == "test"
    assert config.get("defaults", "count") == "5"


report("parse() classmethod exists", check_parse_classmethod_exists)
report("Inline comments are stripped", check_strip_inline_comments)
report("Inline comments preserved by default", check_no_strip_by_default)
report("Section whitespace stripping", check_strip_section_whitespace)
report("Basic parse works", check_basic_parse_works)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
