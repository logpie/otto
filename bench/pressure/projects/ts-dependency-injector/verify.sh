#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: ts-dependency-injector"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Build if needed
if [ -f tsconfig.json ]; then
  npx tsc 2>/dev/null || true
fi

# Find compiled JS module
MOD=""
for f in dist/index.js dist/container.js dist/injector.js index.js src/index.js; do
  if [ -f "$f" ]; then MOD="$f"; break; fi
done
if [ -z "$MOD" ]; then
  MOD=$(find dist -name '*.js' 2>/dev/null | head -1) || true
fi
if [ -z "$MOD" ]; then echo "  FAIL  No compiled module found"; exit 1; fi

check "Container class exists with register/resolve" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Container = mod.Container || mod.default || Object.values(mod).find(v => typeof v === \"function\" && v.prototype?.resolve);
const c = new Container();
if (typeof c.register !== \"function\" || typeof c.resolve !== \"function\") process.exit(1);
'"

check "singleton scope returns same instance" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Container = mod.Container || mod.default || Object.values(mod).find(v => typeof v === \"function\" && v.prototype?.resolve);
const c = new Container();
let count = 0;
c.register(\"svc\", () => ({ id: ++count }), { scope: \"singleton\" });
const a = c.resolve(\"svc\");
const b = c.resolve(\"svc\");
if (a !== b || a.id !== b.id) process.exit(1);
'"

check "transient scope returns new instance each time" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Container = mod.Container || mod.default || Object.values(mod).find(v => typeof v === \"function\" && v.prototype?.resolve);
const c = new Container();
let count = 0;
c.register(\"svc\", () => ({ id: ++count }), { scope: \"transient\" });
const a = c.resolve(\"svc\");
const b = c.resolve(\"svc\");
if (a === b || a.id === b.id) process.exit(1);
'"

check "circular dependency throws with clear error" \
  "node -e '
const mod = require(require(\"path\").resolve(\"$MOD\"));
const Container = mod.Container || mod.default || Object.values(mod).find(v => typeof v === \"function\" && v.prototype?.resolve);
const c = new Container();
c.register(\"A\", (container) => ({ b: container.resolve(\"B\") }));
c.register(\"B\", (container) => ({ a: container.resolve(\"A\") }));
try {
  c.resolve(\"A\");
  process.exit(1);  // should have thrown
} catch(e) {
  const msg = e.message.toLowerCase();
  if (msg.includes(\"circular\") || msg.includes(\"cycle\") || msg.includes(\"recursion\")) process.exit(0);
  process.exit(1);
}
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
