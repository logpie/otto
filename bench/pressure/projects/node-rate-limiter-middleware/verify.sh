#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: node-rate-limiter-middleware"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Create a tiny test server inline
PORT=$((9000 + RANDOM % 1000))

cat > /tmp/verify_rl_server_$PORT.js << JSEOF
const express = require('express');
const app = express();

// Find the rate limiter module
let createRateLimiter;
const paths = [
  './index.js', './rate-limiter.js', './rateLimiter.js', './middleware.js',
  './src/index.js', './src/rate-limiter.js', './src/rateLimiter.js', './src/middleware.js'
];
for (const p of paths) {
  try {
    const mod = require(require('path').resolve(p));
    createRateLimiter = mod.createRateLimiter || mod.rateLimiter || mod.default || mod;
    if (typeof createRateLimiter === 'function') break;
  } catch(e) {}
}
if (typeof createRateLimiter !== 'function') {
  console.error('No createRateLimiter found');
  process.exit(1);
}

const limiter = createRateLimiter({
  algorithm: 'fixed-window',
  limit: 3,
  windowMs: 60000
});

app.use(limiter);
app.get('/test', (req, res) => res.json({ ok: true }));
app.listen($PORT, () => console.log('ready'));
JSEOF

node /tmp/verify_rl_server_$PORT.js &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; rm -f /tmp/verify_rl_server_$PORT.js" EXIT

for i in $(seq 1 30); do
  if curl -s "http://localhost:$PORT/test" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

check "first request succeeds with 200" \
  "curl -sf http://localhost:$PORT/test"

check "rate limit headers present on response" \
  "curl -sI http://localhost:$PORT/test 2>&1 | grep -i 'x-ratelimit-limit'"

check "returns 429 after limit exceeded" \
  "curl -sf http://localhost:$PORT/test >/dev/null; \
   RESP=\$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$PORT/test); \
   [ \"\$RESP\" = '429' ]"

check "429 response includes Retry-After header" \
  "curl -sI http://localhost:$PORT/test 2>&1 | grep -i 'retry-after'"

check "429 response body is JSON with error field" \
  "curl -s http://localhost:$PORT/test | node -e '
    const d = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
    if (!d.error) process.exit(1);
  '"

echo ""
echo "$PASS passed, $FAIL failed"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
trap - EXIT
rm -f /tmp/verify_rl_server_$PORT.js
[ $FAIL -eq 0 ]
