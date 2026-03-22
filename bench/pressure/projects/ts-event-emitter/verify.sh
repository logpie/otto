#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: ts-event-emitter"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Build if needed
if [ -f tsconfig.json ]; then
  npx tsc 2>/dev/null || true
fi

# Find compiled JS module
MOD=""
for f in dist/index.js dist/emitter.js dist/event-emitter.js index.js src/index.js; do
  if [ -f "$f" ]; then MOD="$f"; break; fi
done
if [ -z "$MOD" ]; then
  # Try finding any .js in dist
  MOD=$(find dist -name '*.js' 2>/dev/null | head -1) || true
fi
if [ -z "$MOD" ]; then echo "  FAIL  No compiled module found"; exit 1; fi

check "on/emit work — handler receives emitted payload" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const EE = mod.TypedEmitter || mod.EventEmitter || mod.Emitter || mod.default || Object.values(mod).find(v => typeof v === \"function\");
const emitter = new EE();
let received = null;
emitter.on(\"click\", (data) => { received = data; });
emitter.emit(\"click\", { x: 10 });
if (!received || received.x !== 10) process.exit(1);
'"

check "off removes a handler" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const EE = mod.TypedEmitter || mod.EventEmitter || mod.Emitter || mod.default || Object.values(mod).find(v => typeof v === \"function\");
const emitter = new EE();
let count = 0;
const handler = () => count++;
emitter.on(\"test\", handler);
emitter.emit(\"test\");
emitter.off(\"test\", handler);
emitter.emit(\"test\");
if (count !== 1) process.exit(1);
'"

check "once fires only once" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const EE = mod.TypedEmitter || mod.EventEmitter || mod.Emitter || mod.default || Object.values(mod).find(v => typeof v === \"function\");
const emitter = new EE();
let count = 0;
emitter.once(\"ping\", () => count++);
emitter.emit(\"ping\");
emitter.emit(\"ping\");
if (count !== 1) process.exit(1);
'"

check "wildcard listener receives all events" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const EE = mod.TypedEmitter || mod.EventEmitter || mod.Emitter || mod.default || Object.values(mod).find(v => typeof v === \"function\");
const emitter = new EE();
const events = [];
emitter.on(\"*\", (evt) => events.push(typeof evt === \"string\" ? evt : \"got\"));
emitter.emit(\"click\", {});
emitter.emit(\"hover\", {});
if (events.length < 2) process.exit(1);
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
