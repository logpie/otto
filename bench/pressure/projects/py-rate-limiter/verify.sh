#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: py-rate-limiter"

check "TokenBucket class exists" \
  "python3 -c 'from rate_limiter import TokenBucket'"

check "consume() returns bool" \
  "python3 -c '
from rate_limiter import TokenBucket
tb = TokenBucket(10, 1)
result = tb.consume()
assert type(result) is bool, f\"expected bool, got {type(result)}\"
'"

check "consume respects capacity (burst limit)" \
  "python3 -c '
from rate_limiter import TokenBucket
tb = TokenBucket(capacity=3, rate=0)  # rate=0 means no refill
assert tb.consume() == True
assert tb.consume() == True
assert tb.consume() == True
assert tb.consume() == False, \"should reject when bucket empty\"
'"

check "thread safety — total consumed never exceeds expected" \
  "python3 -c '
import threading
from rate_limiter import TokenBucket
tb = TokenBucket(capacity=100, rate=0)
consumed = [0]
lock = threading.Lock()
def worker():
    local = 0
    for _ in range(20):
        if tb.consume():
            local += 1
    with lock:
        consumed[0] += local
threads = [threading.Thread(target=worker) for _ in range(50)]
for t in threads: t.start()
for t in threads: t.join()
assert consumed[0] <= 100, f\"consumed {consumed[0]} > capacity 100\"
assert consumed[0] > 0, \"nothing was consumed\"
'"

check "SlidingWindowLimiter per-key isolation" \
  "python3 -c '
from rate_limiter import SlidingWindowLimiter
sw = SlidingWindowLimiter(limit=2, window_seconds=10)
# key1 uses up its 2 requests
assert sw.consume(\"key1\") == True
assert sw.consume(\"key1\") == True
assert sw.consume(\"key1\") == False
# key2 should still be allowed (independent)
assert sw.consume(\"key2\") == True, \"key2 should be independent of key1\"
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
