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
