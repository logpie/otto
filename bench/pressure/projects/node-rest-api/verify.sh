#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: node-rest-api"

# Install deps if needed
[ -d node_modules ] || npm install --silent 2>/dev/null

# Find and start the server
SERVER_FILE=""
for f in index.js server.js app.js src/index.js src/server.js src/app.js; do
  if [ -f "$f" ]; then SERVER_FILE="$f"; break; fi
done
if [ -z "$SERVER_FILE" ]; then echo "  FAIL  No server file found"; exit 1; fi

# Start server on a random port
PORT=$((9000 + RANDOM % 1000))
export PORT
node "$SERVER_FILE" &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null" EXIT

# Wait for server to be ready
for i in $(seq 1 30); do
  if curl -s "http://localhost:$PORT/bookmarks" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

check "POST /bookmarks creates a bookmark" \
  "curl -sf -X POST http://localhost:$PORT/bookmarks \
    -H 'Content-Type: application/json' \
    -d '{\"url\":\"https://example.com\",\"title\":\"Test Bookmark\",\"tags\":[\"test\"]}' \
    | node -e 'const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); process.exit(d.url || d.data?.url ? 0 : 1)'"

check "GET /bookmarks returns a list" \
  "curl -sf http://localhost:$PORT/bookmarks \
    | node -e 'const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const arr=Array.isArray(d)?d:(d.bookmarks||d.data||[]); process.exit(arr.length>0?0:1)'"

check "GET /bookmarks/:id returns single bookmark" \
  "ID=\$(curl -sf http://localhost:$PORT/bookmarks | node -e 'const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const arr=Array.isArray(d)?d:(d.bookmarks||d.data||[]); console.log(arr[0]?.id||1)'); \
   curl -sf http://localhost:$PORT/bookmarks/\$ID | node -e 'JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"))'"

check "responses include X-Request-Id header" \
  "curl -sI http://localhost:$PORT/bookmarks 2>&1 | grep -i 'x-request-id'"

check "rate limit headers present (X-RateLimit-Limit)" \
  "curl -sI http://localhost:$PORT/bookmarks 2>&1 | grep -i 'x-ratelimit'"

echo ""
echo "$PASS passed, $FAIL failed"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
trap - EXIT
[ $FAIL -eq 0 ]
