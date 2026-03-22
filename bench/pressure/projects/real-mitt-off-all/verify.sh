#!/usr/bin/env bash
set -uo pipefail
npm run build >/dev/null 2>&1 || true
trap 'rc=$?; rm -f verify_check.cjs; exit $rc' EXIT
cat > verify_check.cjs <<'JS'
const assert = require('assert');
function loadMitt() {
  try { const m = require('./dist/mitt.js'); return m.default || m; } catch {}
  try { const m = require('./dist/mitt.cjs'); return m.default || m; } catch {}
  throw new Error('Unable to load mitt');
}
const mitt = loadMitt();
let failures = 0;
function report(name, fn) {
  try { fn(); console.log(`PASS ${name}`); }
  catch (error) { failures += 1; console.log(`FAIL ${name}: ${error.message}`); }
}
report('off(type) removes all handlers for that event', () => {
  const emitter = mitt();
  let count = 0;
  emitter.on('foo', () => { count += 1; });
  emitter.on('foo', () => { count += 10; });
  emitter.emit('foo');
  assert.equal(count, 11);
  emitter.off('foo');
  emitter.emit('foo');
  assert.equal(count, 11);
});
report('off(type, handler) still removes only the targeted handler', () => {
  const emitter = mitt();
  let count = 0;
  const first = () => { count += 1; };
  const second = () => { count += 10; };
  emitter.on('foo', first);
  emitter.on('foo', second);
  emitter.off('foo', first);
  emitter.emit('foo');
  assert.equal(count, 10);
});
process.exit(failures ? 1 : 0);
JS
node verify_check.cjs
