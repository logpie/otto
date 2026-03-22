#!/usr/bin/env bash
set -euo pipefail

cat > csv_parser.py << 'PYEOF'
"""CSV parser that handles quoted fields, escapes, and various delimiters."""

def parse_csv(text, delimiter=',', quote_char='"'):
    """Parse CSV text into list of rows (list of strings)."""
    rows = []
    current_row = []
    current_field = ''
    in_quotes = False
    i = 0

    while i < len(text):
        char = text[i]

        if char == quote_char:
            if in_quotes:
                # Check for escaped quote (doubled)
                if i + 1 < len(text) and text[i + 1] == quote_char:
                    current_field += quote_char
                    i += 2
                    continue
                else:
                    in_quotes = False
            else:
                in_quotes = True
            i += 1
            continue

        if char == delimiter and not in_quotes:
            current_row.append(current_field)
            current_field = ''
            i += 1
            continue

        if char == '\n' and not in_quotes:
            current_row.append(current_field)
            rows.append(current_row)
            current_row = []
            current_field = ''
            i += 1
            continue

        current_field += char
        i += 1

    if current_field or current_row:
        current_row.append(current_field)

    return rows


def parse_csv_with_headers(text, delimiter=',', quote_char='"'):
    """Parse CSV with first row as headers. Returns list of dicts."""
    rows = parse_csv(text, delimiter, quote_char)
    if not rows:
        return []
    headers = rows[0]
    result = []
    for row in rows[1:]:
        record = {}
        for i, header in enumerate(headers):
            record[header] = row[i]
        result.append(record)
    return result


def to_csv(rows, delimiter=',', quote_char='"'):
    """Convert list of rows back to CSV string."""
    lines = []
    for row in rows:
        fields = []
        for field in row:
            field_str = str(field)
            if delimiter in field_str:
                field_str = f'{quote_char}{field_str}{quote_char}'
            fields.append(field_str)
        lines.append(delimiter.join(fields))
    return '\n'.join(lines)


def filter_rows(rows, column_idx, predicate):
    """Filter rows where predicate(value) is True for given column."""
    return [row for row in rows if predicate(row[column_idx])]
PYEOF

cat > test_csv_parser.py << 'PYEOF'
from csv_parser import parse_csv, parse_csv_with_headers, to_csv

def test_simple_parse():
    text = "a,b,c\n1,2,3\n"
    rows = parse_csv(text)
    assert rows == [['a', 'b', 'c'], ['1', '2', '3']]

def test_quoted_fields():
    text = '"hello, world",simple,"with ""quotes"""\n'
    rows = parse_csv(text)
    assert rows[0][0] == 'hello, world'
    assert rows[0][2] == 'with "quotes"'

def test_with_headers():
    text = "name,age\nAlice,30\nBob,25\n"
    records = parse_csv_with_headers(text)
    assert records[0]['name'] == 'Alice'
    assert records[1]['age'] == '25'
PYEOF

git add -A && git commit -m "init csv parser"
