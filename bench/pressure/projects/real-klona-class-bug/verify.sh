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

// EXACT bug from issue #30: constructor uses Object.assign(this, data)
// Old klona re-runs constructor with no args → clone gets undefined properties
report('Object.assign constructor data is preserved in clone', () => {
  class Foobar {
    constructor(data) { Object.assign(this, data); }
  }
  const input = new Foobar({ test: 123, nested: { x: 1 } });
  const output = klona(input);
  assert.equal(output.test, 123, 'clone should have test=123, not undefined');
  assert.deepStrictEqual(output.nested, { x: 1 });
});

report('cloned instance preserves constructor identity', () => {
  class Foobar {
    constructor(data) { Object.assign(this, data); }
  }
  const input = new Foobar({ test: 1 });
  const output = klona(input);
  assert.equal(input.constructor, output.constructor);
  assert.equal(output.constructor.name, 'Foobar');
});

report('nested objects in Object.assign constructor are deep cloned', () => {
  class Foobar {
    constructor(data) { Object.assign(this, data); }
  }
  const input = new Foobar({ items: [1, 2, 3] });
  const output = klona(input);
  assert.deepStrictEqual(output.items, [1, 2, 3]);
  assert.notStrictEqual(output.items, input.items, 'nested array should be a new reference');
});

process.exit(failures ? 1 : 0);
JS
node verify_check.cjs
