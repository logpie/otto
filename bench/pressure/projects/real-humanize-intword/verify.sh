#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import humanize

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_singular_million():
    """intword(1234567, '%.0f') should say '1 million' not '1 millions'."""
    result = humanize.intword(1234567, "%.0f")
    assert result == "1 million", f"Expected '1 million', got '{result}'"


def check_plural_million():
    """intword(1234567, '%.1f') should say '1.2 million' (singular, since 1.2 is not exactly 1)."""
    result = humanize.intword(1234567, "%.1f")
    # 1.234567 rounded to 1 decimal -> 1.2, which uses plural in English ("1.2 million")
    assert result == "1.2 million", f"Expected '1.2 million', got '{result}'"


def check_boundary_rounding_up():
    """intword(999500, '%.0f') should round up to '1 million'."""
    result = humanize.intword(999500, "%.0f")
    assert result == "1 million", f"Expected '1 million', got '{result}'"


def check_boundary_stays_lower():
    """intword(999499, '%.0f') should stay as '999 thousand'."""
    result = humanize.intword(999499, "%.0f")
    assert result == "999 thousand", f"Expected '999 thousand', got '{result}'"


def check_googol():
    """10**101 should return '10.0 googol' not a raw number string."""
    result = humanize.intword(10**101)
    assert "googol" in result, f"Expected googol in result, got '{result}'"


report("Singular form for '1 million'", check_singular_million)
report("Plural form for '1.2 million'", check_plural_million)
report("Boundary rounding up to next unit", check_boundary_rounding_up)
report("Boundary stays at lower unit", check_boundary_stays_lower)
report("Large numbers use googol", check_googol)

raise SystemExit(1 if failures else 0)
PY

# Install into a temp venv so import works after worktree cleanup
_venv=$(mktemp -d)/venv
python3 -m venv "$_venv" 2>/dev/null
"$_venv/bin/pip" install -q -e . 2>/dev/null || true
"$_venv/bin/python" verify_check.py
_rc=$?
rm -rf "$(dirname "$_venv")"
exit $_rc
