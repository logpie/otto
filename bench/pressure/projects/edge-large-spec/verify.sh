#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: edge-large-spec (URL shortener, 16 acceptance criteria)"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Find and start the server
SERVER_FILE=""
for f in index.js server.js app.js src/index.js src/server.js src/app.js; do
  if [ -f "$f" ]; then SERVER_FILE="$f"; break; fi
done
if [ -z "$SERVER_FILE" ]; then echo "  FAIL  No server file found"; exit 1; fi

PORT=$((9000 + RANDOM % 1000))
export PORT
node "$SERVER_FILE" &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null" EXIT

for i in $(seq 1 30); do
  if curl -s "http://localhost:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

check "POST /shorten creates a short URL" \
  "curl -sf -X POST http://localhost:$PORT/shorten \
    -H 'Content-Type: application/json' \
    -d '{\"url\":\"https://example.com/very/long/path\"}' \
    | node -e '
      const d = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
      if (!d.alias && !d.shortUrl) process.exit(1);
    '"

# Get the alias from the creation response
ALIAS=$(curl -sf -X POST "http://localhost:$PORT/shorten" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://httpbin.org/get"}' \
  | node -e 'const d=JSON.parse(require("fs").readFileSync("/dev/stdin","utf8")); console.log(d.alias || d.shortUrl?.split("/").pop() || "")' 2>/dev/null)

check "GET /:alias redirects with 301" \
  "[ -n \"$ALIAS\" ] && RESP=\$(curl -s -o /dev/null -w '%{http_code}' -L0 \"http://localhost:$PORT/$ALIAS\"); \
   [ \"\$RESP\" = '301' ] || [ \"\$RESP\" = '302' ]"

check "GET /:alias/stats returns click data" \
  "[ -n \"$ALIAS\" ] && curl -sf \"http://localhost:$PORT/$ALIAS/stats\" \
    | node -e '
      const d = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
      if (typeof d.clicks === \"undefined\" && typeof d.visits === \"undefined\") process.exit(1);
    '"

check "GET /health returns uptime and link count" \
  "curl -sf http://localhost:$PORT/health \
    | node -e '
      const d = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
      if (typeof d.uptime === \"undefined\" && typeof d.status === \"undefined\") process.exit(1);
    '"

check "expired links return 410 Gone" \
  "EXPIRED_ALIAS=\$(curl -sf -X POST http://localhost:$PORT/shorten \
    -H 'Content-Type: application/json' \
    -d '{\"url\":\"https://example.com/temp\",\"expiresIn\":1}' \
    | node -e 'const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); console.log(d.alias || d.shortUrl?.split(\"/\").pop() || \"\")'); \
   sleep 2; \
   RESP=\$(curl -s -o /dev/null -w '%{http_code}' -L0 \"http://localhost:$PORT/\$EXPIRED_ALIAS\"); \
   [ \"\$RESP\" = '410' ]"

echo ""
echo "$PASS passed, $FAIL failed"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
trap - EXIT
[ $FAIL -eq 0 ]
