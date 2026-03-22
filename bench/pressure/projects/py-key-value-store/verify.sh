#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: py-key-value-store"

TMPDIR=$(mktemp -d /tmp/kvstore_verify_XXXXXX)
trap "rm -rf $TMPDIR" EXIT

check "KVStore get/set/delete work" \
  "python3 -c '
import sys, os
os.chdir(\"$TMPDIR\")
from kv_store import KVStore
store = KVStore(\"$TMPDIR/test.db\")
store.set(\"key1\", \"value1\")
assert store.get(\"key1\") == \"value1\", f\"got {store.get(\\\"key1\\\")}\"
store.delete(\"key1\")
assert store.get(\"key1\") is None, \"key1 should be deleted\"
'"

check "get returns default for missing key" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from kv_store import KVStore
store = KVStore(\"$TMPDIR/test2.db\")
assert store.get(\"nonexistent\") is None
assert store.get(\"nonexistent\", \"default\") == \"default\"
'"

check "TTL expiry — expired key not returned" \
  "python3 -c '
import time, os; os.chdir(\"$TMPDIR\")
from kv_store import KVStore
store = KVStore(\"$TMPDIR/test3.db\")
store.set(\"ephemeral\", \"gone_soon\", ttl_seconds=1)
assert store.get(\"ephemeral\") == \"gone_soon\"
time.sleep(1.5)
result = store.get(\"ephemeral\")
assert result is None, f\"expired key returned {result}\"
'"

check "glob pattern matching on keys" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from kv_store import KVStore
store = KVStore(\"$TMPDIR/test4.db\")
store.set(\"user:1\", \"alice\")
store.set(\"user:2\", \"bob\")
store.set(\"config:theme\", \"dark\")
matches = store.keys(\"user:*\")
assert len(matches) == 2, f\"expected 2 matches, got {len(matches)}: {matches}\"
assert \"config:theme\" not in matches
'"

check "exists() returns correct booleans" \
  "python3 -c '
import os; os.chdir(\"$TMPDIR\")
from kv_store import KVStore
store = KVStore(\"$TMPDIR/test5.db\")
store.set(\"present\", 42)
assert store.exists(\"present\") == True
assert store.exists(\"absent\") == False
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
