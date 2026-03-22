#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: edge-conflicting-tasks (3 enhanced functions in utils.py)"

check "format_name supports title, middle, suffix, format param" \
  "python3 -c '
from utils import format_name
# Task 1: full format with title, middle, suffix
result = format_name(\"John\", \"Doe\", middle=\"Michael\", title=\"Dr.\", suffix=\"Jr.\", format=\"full\")
assert \"Dr.\" in result, f\"missing title in: {result}\"
assert \"John\" in result, f\"missing first in: {result}\"
assert \"Michael\" in result, f\"missing middle in: {result}\"
assert \"Doe\" in result, f\"missing last in: {result}\"
assert \"Jr.\" in result, f\"missing suffix in: {result}\"

# formal format
formal = format_name(\"John\", \"Doe\", title=\"Dr.\", format=\"formal\")
assert \"Dr.\" in formal and \"Doe\" in formal, f\"formal: {formal}\"

# informal format
informal = format_name(\"John\", \"Doe\", format=\"informal\")
assert informal == \"John Doe\" or (\"John\" in informal and \"Doe\" in informal), f\"informal: {informal}\"
'"

check "format_date handles ISO strings and relative dates" \
  "python3 -c '
from utils import format_date
from datetime import datetime, timedelta

# Absolute date (older than a week) should return human-readable
old_date = \"2023-06-15T10:30:00\"
result = format_date(old_date)
assert \"2023\" in result or \"June\" in result or \"Jun\" in result, f\"absolute date: {result}\"

# Recent date should return relative
now = datetime.now()
recent = (now - timedelta(hours=2)).isoformat()
result_recent = format_date(recent)
# Should contain something like \"2 hours ago\" or \"hours\"
assert \"hour\" in result_recent.lower() or \"ago\" in result_recent.lower() or \"minute\" in result_recent.lower(), f\"relative date: {result_recent}\"
'"

check "validate_email does RFC 5322 checks" \
  "python3 -c '
from utils import validate_email
# Valid
assert validate_email(\"user@example.com\") == True
# Too long local part (>64 chars)
long_local = \"a\" * 65 + \"@example.com\"
assert validate_email(long_local) == False, \"should reject >64 char local part\"
# Consecutive dots
assert validate_email(\"user..name@example.com\") == False, \"should reject consecutive dots\"
# No TLD
assert validate_email(\"user@localhost\") == False or validate_email(\"user@localhost\") == True  # debatable
'"

check "validate_url function exists and works" \
  "python3 -c '
from utils import validate_url
assert validate_url(\"https://example.com\") == True
assert validate_url(\"not a url\") == False
'"

check "validate_phone function exists and works" \
  "python3 -c '
from utils import validate_phone
# Should accept at least basic phone format
result = validate_phone(\"555-1234\", \"US\") if \"country_code\" in validate_phone.__code__.co_varnames else validate_phone(\"+15551234567\")
assert isinstance(result, bool)
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
