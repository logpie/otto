#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: bugfix-csv-parser (6 bugs)"

# Bug 1: \r\n line ending handling
check "Bug1: parse_csv handles CRLF line endings" \
  "python3 -c '
from csv_parser import parse_csv
text = \"a,b,c\r\n1,2,3\r\n\"
rows = parse_csv(text)
assert len(rows) == 2, f\"expected 2 rows, got {len(rows)}: {rows}\"
assert rows[0] == [\"a\", \"b\", \"c\"], f\"row0: {rows[0]}\"
assert rows[1] == [\"1\", \"2\", \"3\"], f\"row1: {rows[1]}\"
# Check no stray \\r in fields
for row in rows:
    for field in row:
        assert \"\\r\" not in field, f\"stray \\\\r in field: {repr(field)}\"
'"

# Bug 2: Last row dropped when file lacks trailing newline
check "Bug2: last row preserved without trailing newline" \
  "python3 -c '
from csv_parser import parse_csv
text = \"a,b\n1,2\n3,4\"  # no trailing newline
rows = parse_csv(text)
assert len(rows) == 3, f\"expected 3 rows, got {len(rows)}: {rows}\"
assert rows[2] == [\"3\", \"4\"], f\"last row: {rows[2]}\"
'"

# Bug 3: IndexError on ragged rows (fewer columns than headers)
check "Bug3: ragged CSV (fewer columns) handled gracefully" \
  "python3 -c '
from csv_parser import parse_csv_with_headers
text = \"name,age,city\nAlice,30,NYC\nBob,25\n\"
records = parse_csv_with_headers(text)
assert len(records) == 2, f\"expected 2 records, got {len(records)}\"
# Bob's row should have city as empty/None, not crash
bob = records[1]
assert bob[\"name\"] == \"Bob\", f\"got {bob}\"
city = bob.get(\"city\", None)
assert city is None or city == \"\", f\"expected empty city, got {repr(city)}\"
'"

# Bug 4: to_csv should quote fields containing newlines or quote chars
check "Bug4: to_csv quotes fields with newlines and quotes" \
  "python3 -c '
from csv_parser import to_csv
rows = [[\"hello\", \"line1\nline2\", \"has \\\"quotes\\\"\"]]
result = to_csv(rows)
# Fields with newlines or quotes should be wrapped in quotes
assert \"\\\"line1\" in result or \"\\\"line1\\nline2\\\"\" in result, f\"newline field not quoted: {repr(result)}\"
'"

# Bug 5 + general: round-trip parse/serialize
check "Bug5: round-trip parse then serialize preserves data" \
  "python3 -c '
from csv_parser import parse_csv, to_csv
original = \"name,value\nAlice,100\nBob,200\n\"
rows = parse_csv(original)
serialized = to_csv(rows)
rows2 = parse_csv(serialized)
assert rows == rows2, f\"round-trip mismatch:\\noriginal rows: {rows}\\nafter round-trip: {rows2}\"
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
