#!/usr/bin/env bash
set -euo pipefail

cat > utils.py << 'PYEOF'
"""Utility module that will be modified by multiple tasks."""

def format_name(first, last):
    """Format a full name."""
    return f"{first} {last}"

def format_date(date_str):
    """Format a date string to human readable."""
    return date_str

def format_currency(amount, currency='USD'):
    """Format a currency amount."""
    return f"${amount:.2f}"

def validate_email(email):
    """Basic email validation."""
    return '@' in email
PYEOF

cat > test_utils.py << 'PYEOF'
from utils import format_name, format_date, format_currency, validate_email

def test_format_name():
    assert format_name("John", "Doe") == "John Doe"

def test_format_currency():
    assert format_currency(10) == "$10.00"

def test_validate_email():
    assert validate_email("test@example.com")
    assert not validate_email("invalid")
PYEOF

git add -A && git commit -m "init utils module"
