#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
from datetime import datetime, timedelta, timezone
import inspect
import utils

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def call_format_date(value, **extras):
    sig = inspect.signature(utils.format_date)
    kwargs = {}
    for key, val in extras.items():
        if key in sig.parameters:
            kwargs[key] = val
        elif key == "format" and "format_str" in sig.parameters:
            kwargs["format_str"] = val
        elif key == "format" and "fmt" in sig.parameters:
            kwargs["fmt"] = val
    return utils.format_date(value, **kwargs)


def check_format_name_variants():
    full = utils.format_name("John", "Doe", middle="Michael", title="Dr.", suffix="Jr.", format="full")
    formal = utils.format_name("John", "Doe", title="Dr.", format="formal")
    informal = utils.format_name("John", "Doe", format="informal")
    assert full == "Dr. John Michael Doe Jr."
    assert formal == "Dr. Doe, John"
    assert informal == "John Doe"


def check_format_name_omits_missing_parts():
    rendered = utils.format_name("Jane", "Roe", format="full")
    assert "  " not in rendered
    assert ", ," not in rendered
    assert rendered == "Jane Roe"


def check_relative_date():
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    rendered = call_format_date(recent)
    lowered = rendered.lower()
    assert "ago" in lowered or "hour" in lowered


def check_absolute_and_custom_date():
    old = "2024-01-15T10:30:00+00:00"
    absolute = call_format_date(old)
    custom = call_format_date(old, format="%Y/%m/%d")
    assert any(token in absolute for token in ("January", "Jan", "2024"))
    assert "2024/01/15" == custom


def check_email_validation():
    assert utils.validate_email("user@example.com") is True
    assert utils.validate_email(("a" * 65) + "@example.com") is False
    assert utils.validate_email("user..name@example.com") is False
    assert utils.validate_email("user@example") is False
    assert utils.validate_email("bad space@example.com") is False


def check_url_validation():
    assert utils.validate_url("https://example.com/path?q=1") is True
    assert utils.validate_url("http://example.com") is True
    assert utils.validate_url("ftp://example.com") is False
    assert utils.validate_url("not a url") is False


def check_phone_validation():
    valid = utils.validate_phone("+14155552671", "US")
    invalid = utils.validate_phone("12", "US")
    assert valid is True
    assert invalid is False


report("format_name supports full, formal, and informal variants", check_format_name_variants)
report("format_name omits missing optional parts cleanly", check_format_name_omits_missing_parts)
report("format_date renders recent timestamps relatively", check_relative_date)
report("format_date renders older dates absolutely and honors custom formats", check_absolute_and_custom_date)
report("validate_email enforces RFC-like edge cases", check_email_validation)
report("validate_url accepts only valid http(s) URLs", check_url_validation)
report("validate_phone distinguishes valid and invalid numbers", check_phone_validation)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
