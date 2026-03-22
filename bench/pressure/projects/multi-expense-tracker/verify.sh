#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: multi-expense-tracker (3-layer integration)"

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
  if curl -s "http://localhost:$PORT/expenses" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

check "POST /expenses creates an expense" \
  "curl -sf -X POST http://localhost:$PORT/expenses \
    -H 'Content-Type: application/json' \
    -d '{\"amount\":42.50,\"currency\":\"USD\",\"category\":\"food\",\"description\":\"lunch\",\"date\":\"2024-01-15\",\"tags\":[\"work\"]}' \
    | node -e 'const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); if(!d.id && !d.data?.id) process.exit(1);'"

check "POST /expenses validates — rejects negative amount" \
  "RESP=\$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:$PORT/expenses \
    -H 'Content-Type: application/json' \
    -d '{\"amount\":-10,\"currency\":\"USD\",\"category\":\"food\",\"description\":\"bad\",\"date\":\"2024-01-15\",\"tags\":[]}'); \
   [ \"\$RESP\" = '400' ] || [ \"\$RESP\" = '422' ]"

# Add more expenses for analytics
check "POST second expense for analytics" \
  "curl -sf -X POST http://localhost:$PORT/expenses \
    -H 'Content-Type: application/json' \
    -d '{\"amount\":100.00,\"currency\":\"USD\",\"category\":\"transport\",\"description\":\"taxi\",\"date\":\"2024-01-15\",\"tags\":[]}' >/dev/null"

check "GET /analytics/category-totals returns totals" \
  "curl -sf 'http://localhost:$PORT/analytics/category-totals?month=2024-01' \
    | node -e '
      const d = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
      const data = d.data || d;
      // Should have food and transport categories
      const hasData = (data.food || data.Food || (Array.isArray(data) && data.length > 0));
      if (!hasData && Object.keys(data).length === 0) process.exit(1);
    '"

check "GET /expenses lists created expenses" \
  "curl -sf http://localhost:$PORT/expenses \
    | node -e '
      const d = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
      const arr = Array.isArray(d) ? d : (d.expenses || d.data || []);
      if (arr.length < 2) process.exit(1);
    '"

echo ""
echo "$PASS passed, $FAIL failed"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
trap - EXIT
[ $FAIL -eq 0 ]
