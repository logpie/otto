#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: ts-result-type"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Build if needed
if [ -f tsconfig.json ]; then
  npx tsc 2>/dev/null || true
fi

# Find compiled JS module
MOD=""
for f in dist/index.js dist/result.js index.js src/index.js; do
  if [ -f "$f" ]; then MOD="$f"; break; fi
done
if [ -z "$MOD" ]; then
  MOD=$(find dist -name '*.js' 2>/dev/null | head -1) || true
fi
if [ -z "$MOD" ]; then echo "  FAIL  No compiled module found"; exit 1; fi

check "Ok constructor wraps a value" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Ok = mod.Ok || mod.ok;
const result = Ok(42);
if (!result.isOk || !result.isOk()) process.exit(1);
'"

check "Err constructor wraps an error" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Err = mod.Err || mod.err;
const result = Err(\"oops\");
if (!result.isErr || !result.isErr()) process.exit(1);
'"

check "map chains on Ok, skips on Err" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Ok = mod.Ok || mod.ok;
const Err = mod.Err || mod.err;
const doubled = Ok(5).map(x => x * 2);
if (doubled.unwrap() !== 10) process.exit(1);
const errResult = Err(\"fail\").map(x => x * 2);
if (!errResult.isErr()) process.exit(1);
'"

check "unwrap throws on Err" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Err = mod.Err || mod.err;
try { Err(\"oops\").unwrap(); process.exit(1); } catch(e) { process.exit(0); }
'"

check "unwrapOr returns default on Err" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Err = mod.Err || mod.err;
const val = Err(\"oops\").unwrapOr(99);
if (val !== 99) process.exit(1);
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
