#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: ts-schema-validator"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Build TS if needed
if [ -f tsconfig.json ]; then
  npx tsc --noEmit 2>/dev/null || npx tsc 2>/dev/null || true
fi

# Find compiled output or use ts-node
EXEC="node"
if command -v npx >/dev/null 2>&1 && npx ts-node --version >/dev/null 2>&1; then
  EXEC="npx ts-node --transpileOnly"
elif [ -d dist ]; then
  true  # will use node with dist files
fi

# Find the module — try compiled JS first, then TS via ts-node
MOD=""
for f in dist/index.js dist/schema.js dist/validator.js index.js src/index.js src/schema.js; do
  if [ -f "$f" ]; then MOD="$f"; break; fi
done
if [ -z "$MOD" ]; then
  for f in src/index.ts index.ts src/schema.ts; do
    if [ -f "$f" ]; then MOD="$f"; EXEC="npx ts-node --transpileOnly"; break; fi
  done
fi
if [ -z "$MOD" ]; then echo "  FAIL  No module found"; exit 1; fi

check "string schema parses valid string" \
  "$EXEC -e '
const mod = require(require(\"path\").resolve(\"${MOD%.ts}.js\" !== \"$MOD\" ? \"$MOD\" : \"$MOD\".replace(\".ts\",\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const str = (s.string || s.String || (() => { throw new Error(\"no string\"); }))();
const result = str.parse(\"hello\");
if (result !== \"hello\") process.exit(1);
' 2>/dev/null || $EXEC -e '
const mod = require(require(\"path\").resolve(\"$MOD\".replace(/\.ts$/,\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const str = s.string();
str.parse(\"hello\");
'"

check "number schema rejects string input" \
  "$EXEC -e '
const mod = require(require(\"path\").resolve(\"$MOD\".replace(/\.ts$/,\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const num = s.number();
try { num.parse(\"not a number\"); process.exit(1); } catch(e) { process.exit(0); }
' 2>/dev/null || $EXEC -e '
const mod = require(require(\"path\").resolve(\"$MOD\".replace(/\.ts$/,\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const num = s.number();
const r = num.safeParse(\"not a number\");
if (r.success !== false) process.exit(1);
'"

check "object schema validates nested fields" \
  "$EXEC -e '
const mod = require(require(\"path\").resolve(\"$MOD\".replace(/\.ts$/,\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const schema = s.object({ name: s.string(), age: s.number() });
const result = schema.parse({ name: \"Alice\", age: 30 });
if (result.name !== \"Alice\" || result.age !== 30) process.exit(1);
'"

check "validation errors include field path" \
  "$EXEC -e '
const mod = require(require(\"path\").resolve(\"$MOD\".replace(/\.ts$/,\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const schema = s.object({ address: s.object({ zip: s.string() }) });
try {
  schema.parse({ address: { zip: 12345 } });
  process.exit(1);
} catch(e) {
  const msg = e.message || JSON.stringify(e.errors || e.issues || e);
  // Error should reference the path \"address.zip\" or similar
  if (msg.includes(\"zip\") || msg.includes(\"address\")) process.exit(0);
  process.exit(1);
}
'"

check "safeParse returns success:false on invalid input" \
  "$EXEC -e '
const mod = require(require(\"path\").resolve(\"$MOD\".replace(/\.ts$/,\"\")));
const s = mod.s || mod.schema || mod.Schema || mod.default || mod;
const num = s.number();
const r = num.safeParse(\"bad\");
if (r.success === false) process.exit(0);
process.exit(1);
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
