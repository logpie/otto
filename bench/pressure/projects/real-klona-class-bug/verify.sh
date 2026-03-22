#!/usr/bin/env bash
set -uo pipefail
npm run build >/dev/null 2>&1 || true
trap 'rc=$?; rm -f verify_check.cjs; exit $rc' EXIT
cat > verify_check.cjs <<'JS'
const assert = require('assert');
function loadKlona() {
  for (const c of ['./dist/index.js', './src/index.js']) {
    try { const m = require(c); return m.klona || m.default || m; } catch {}
  }
  throw new Error('Unable to load klona');
}
const klona = loadKlona();
let failures = 0;
function report(name, fn) {
  try { fn(); console.log(`PASS ${name}`); }
  catch (error) { failures += 1; console.log(`FAIL ${name}: ${error.message}`); }
}
class Widget {
  constructor() {
    Object.assign(this, { name: 'default-name', settings: { enabled: false, level: 0 } });
  }
  describe() { return `${this.name}:${this.settings.level}`; }
}
report('cloned class instance keeps source field values', () => {
  const original = new Widget();
  original.name = 'custom-name';
  original.settings.level = 7;
  const copy = klona(original);
  assert.equal(copy.name, 'custom-name');
  assert.equal(copy.describe(), 'custom-name:7');
});
report('cloned class instance preserves prototype', () => {
  const copy = klona(new Widget());
  assert.ok(copy instanceof Widget);
});
report('nested objects are deeply cloned', () => {
  const original = new Widget();
  original.settings.enabled = true;
  const copy = klona(original);
  assert.deepStrictEqual(copy.settings, { enabled: true, level: 0 });
  assert.notStrictEqual(copy.settings, original.settings);
});
process.exit(failures ? 1 : 0);
JS
node verify_check.cjs
