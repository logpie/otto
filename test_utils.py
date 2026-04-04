from utils import format_name, format_date, format_currency, validate_email

def test_format_name():
    assert format_name("John", "Doe") == "John Doe"

def test_format_currency():
    assert format_currency(10) == "$10.00"

def test_validate_email():
    assert validate_email("test@example.com")
    assert not validate_email("invalid")
