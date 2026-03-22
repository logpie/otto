#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: edge-greenfield-complex (JsonDB)"

# Find the module
MOD=""
for m in jsondb json_db db database; do
  if python3 -c "import $m" 2>/dev/null; then MOD=$m; break; fi
done
if [ -z "$MOD" ]; then echo "  FAIL  No jsondb module found"; exit 1; fi

TMPDIR=$(mktemp -d /tmp/jsondb_verify_XXXXXX)
trap "rm -rf $TMPDIR" EXIT

check "insert and find documents" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from $MOD import JsonDB
db = JsonDB(\"$TMPDIR\")
col = db.collection(\"users\")
col.insert({\"name\": \"Alice\", \"age\": 30})
col.insert({\"name\": \"Bob\", \"age\": 25})
results = col.find({\"name\": \"Alice\"})
assert len(results) == 1, f\"expected 1 result, got {len(results)}\"
assert results[0][\"name\"] == \"Alice\"
'"

check "update with \$set operator" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from $MOD import JsonDB
db = JsonDB(\"$TMPDIR/up\")
col = db.collection(\"users\")
col.insert({\"name\": \"Alice\", \"age\": 30})
col.update({\"name\": \"Alice\"}, {\"\$set\": {\"age\": 31}})
results = col.find({\"name\": \"Alice\"})
assert results[0][\"age\"] == 31, f\"age not updated: {results[0]}\"
'"

check "delete removes matching documents" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from $MOD import JsonDB
db = JsonDB(\"$TMPDIR/del\")
col = db.collection(\"items\")
col.insert({\"name\": \"A\"})
col.insert({\"name\": \"B\"})
col.delete({\"name\": \"A\"})
results = col.find({})
names = [r[\"name\"] for r in results]
assert \"A\" not in names, f\"A should be deleted, got {names}\"
assert \"B\" in names, f\"B should remain, got {names}\"
'"

check "\$gt query operator works" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from $MOD import JsonDB
db = JsonDB(\"$TMPDIR/gt\")
col = db.collection(\"nums\")
col.insert({\"val\": 10})
col.insert({\"val\": 20})
col.insert({\"val\": 30})
results = col.find({\"val\": {\"\$gt\": 15}})
assert len(results) == 2, f\"expected 2 results with val>15, got {len(results)}\"
vals = sorted([r[\"val\"] for r in results])
assert vals == [20, 30], f\"got {vals}\"
'"

check "find_one returns single document" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from $MOD import JsonDB
db = JsonDB(\"$TMPDIR/one\")
col = db.collection(\"users\")
col.insert({\"name\": \"Alice\"})
col.insert({\"name\": \"Bob\"})
result = col.find_one({\"name\": \"Bob\"})
assert result is not None, \"find_one returned None\"
assert result[\"name\"] == \"Bob\"
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
