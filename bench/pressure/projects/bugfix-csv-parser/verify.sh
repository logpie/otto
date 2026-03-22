#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import csv_parser

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_crlf():
    rows = csv_parser.parse_csv("a,b\r\n1,2\r\n")
    assert rows == [["a", "b"], ["1", "2"]]
    assert all("\r" not in field for row in rows for field in row)


def check_no_trailing_newline():
    rows = csv_parser.parse_csv("a,b\n1,2\n3,4")
    assert rows[-1] == ["3", "4"]


def check_quoted_fields():
    rows = csv_parser.parse_csv('name,notes\n"alice","x,y"\n"bob","line1\nline2"\n"eve","he said ""hi"""')
    assert rows[1][1] == "x,y"
    assert rows[2][1] == "line1\nline2"
    assert rows[3][1] == 'he said "hi"'


def check_ragged_headers():
    records = csv_parser.parse_csv_with_headers("name,age,city\nAlice,30,Paris\nBob,25\n")
    assert records[1]["name"] == "Bob"
    assert records[1]["city"] == ""


def check_serialization():
    text = csv_parser.to_csv([["name", "note"], ["alice", 'a,"b"\nnext']])
    assert text.endswith("\n")
    round_tripped = csv_parser.parse_csv(text)
    assert round_tripped[1][1] == 'a,"b"\nnext'


def check_filter_preserves_header():
    rows = [["name", "age"], ["Alice", "30"], ["Bob", "20"]]
    filtered = csv_parser.filter_rows(rows, 1, lambda value: value == "30")
    assert filtered[0] == ["name", "age"]
    assert filtered[1:] == [["Alice", "30"]]


def check_round_trip():
    rows = [["a", "b"], ["1", "2"], ["3", "4"]]
    assert csv_parser.parse_csv(csv_parser.to_csv(rows)) == rows


report("parse_csv handles CRLF without stray carriage returns", check_crlf)
report("parse_csv preserves the last row without a trailing newline", check_no_trailing_newline)
report("parse_csv handles delimiters, newlines, and escaped quotes inside fields", check_quoted_fields)
report("parse_csv_with_headers pads ragged rows with empty strings", check_ragged_headers)
report("to_csv quotes special characters and keeps a trailing newline", check_serialization)
report("filter_rows preserves the header row and filters only data rows", check_filter_preserves_header)
report("parse and serialize round-trip consistently", check_round_trip)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
