#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: node-websocket-chat"

# Install deps if needed
[ -d node_modules ] || npm install --silent 2>/dev/null

check "ws module is installed" \
  "node -e 'require(\"ws\")'"

# Find server file
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
sleep 1

check "server accepts WebSocket connections" \
  "node -e '
const WebSocket = require(\"ws\");
const ws = new WebSocket(\"ws://localhost:$PORT\");
ws.on(\"open\", () => { ws.close(); process.exit(0); });
ws.on(\"error\", () => process.exit(1));
setTimeout(() => process.exit(1), 3000);
'"

check "join message is accepted" \
  "node -e '
const WebSocket = require(\"ws\");
const ws = new WebSocket(\"ws://localhost:$PORT\");
ws.on(\"open\", () => {
  ws.send(JSON.stringify({type:\"join\",payload:{room:\"test\",username:\"alice\"}}));
  setTimeout(() => { ws.close(); process.exit(0); }, 500);
});
ws.on(\"error\", () => process.exit(1));
setTimeout(() => process.exit(1), 3000);
'"

check "message broadcast reaches room members" \
  "node -e '
const WebSocket = require(\"ws\");
const ws1 = new WebSocket(\"ws://localhost:$PORT\");
const ws2 = new WebSocket(\"ws://localhost:$PORT\");
let received = false;
ws1.on(\"open\", () => {
  ws1.send(JSON.stringify({type:\"join\",payload:{room:\"r1\",username:\"alice\"}}));
});
ws2.on(\"open\", () => {
  ws2.send(JSON.stringify({type:\"join\",payload:{room:\"r1\",username:\"bob\"}}));
  setTimeout(() => {
    ws1.send(JSON.stringify({type:\"message\",payload:{text:\"hello\"}}));
  }, 300);
});
ws2.on(\"message\", (data) => {
  const msg = JSON.parse(data);
  if (msg.type === \"message\" || msg.payload?.text === \"hello\" || msg.text === \"hello\") {
    received = true;
  }
});
setTimeout(() => { ws1.close(); ws2.close(); process.exit(received ? 0 : 1); }, 2000);
'"

echo ""
echo "$PASS passed, $FAIL failed"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
trap - EXIT
[ $FAIL -eq 0 ]
