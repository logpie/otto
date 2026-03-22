#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import datetime as dt
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


def check_no_60_seconds():
    """59.9 seconds with %0.0f format should carry over to '1 minute', not show '60 seconds'."""
    result = humanize.precisedelta(
        dt.timedelta(seconds=59.9),
        minimum_unit="seconds",
        format="%0.0f",
    )
    assert result == "1 minute", f"Expected '1 minute', got '{result}'"


def check_carry_across_all_units():
    """23h 59m 59.9s rounded should carry all the way up to '2 days'."""
    result = humanize.precisedelta(
        dt.timedelta(days=1, hours=23, minutes=59, seconds=59.9),
        minimum_unit="seconds",
        format="%0.0f",
    )
    assert result == "2 days", f"Expected '2 days', got '{result}'"


def check_singular_second():
    """0.999 seconds with %0.0f should be '1 second' (singular), not '1 seconds'."""
    result = humanize.precisedelta(
        dt.timedelta(seconds=0.999),
        minimum_unit="seconds",
        format="%0.0f",
    )
    assert result == "1 second", f"Expected '1 second', got '{result}'"


def check_hour_carry():
    """59m 59.9s with %0.0f should carry to '1 hour'."""
    result = humanize.precisedelta(
        dt.timedelta(seconds=3599.9),
        minimum_unit="seconds",
        format="%0.0f",
    )
    assert result == "1 hour", f"Expected '1 hour', got '{result}'"


report("59.9s rounds to 1 minute (no '60 seconds')", check_no_60_seconds)
report("Carry propagates across all units", check_carry_across_all_units)
report("Singular form for 1 second", check_singular_second)
report("59m59.9s carries to 1 hour", check_hour_carry)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
