#!/usr/bin/env bash
set -euo pipefail
npm run build >/dev/null 2>&1 || true
trap 'rm -f verify_check.mjs' EXIT
cat > verify_check.mjs <<'JS'
import assert from 'node:assert/strict';
const mod = await import('./dist/index.js');
const ms = mod.ms || mod.default || mod;
const MONTH = 2629800000;
let failures = 0;
function report(name, fn) {
  try { fn(); console.log(`PASS ${name}`); }
  catch (error) { failures += 1; console.log(`FAIL ${name}: ${error.message}`); }
}
report('short month unit parses correctly', () => { assert.equal(ms('1mo'), MONTH); });
report('long plural month unit parses correctly', () => { assert.equal(ms('2 months'), MONTH * 2); });
report('month-sized values format back to short unit', () => { assert.equal(ms(MONTH), '1mo'); });
report('long formatting uses pluralized month label', () => { assert.equal(ms(MONTH * 2, { long: true }), '2 months'); });
process.exit(failures ? 1 : 0);
JS
node verify_check.mjs
