#!/usr/bin/env bash
set -euo pipefail
trap 'rm -f verify_check.mjs' EXIT
cat > verify_check.mjs <<'JS'
import assert from 'node:assert/strict';
import camelCase from './index.js';
let failures = 0;
function report(name, fn) {
  try { fn(); console.log(`PASS ${name}`); }
  catch (error) { failures += 1; console.log(`FAIL ${name}: ${error.message}`); }
}
report('option preserves uppercase runs', () => {
  assert.equal(camelCase('foo-BAR', { preserveConsecutiveUppercase: true }), 'fooBAR');
});
report('default behavior unchanged without option', () => {
  assert.equal(camelCase('foo-BAR'), 'fooBar');
});
report('composes with pascalCase', () => {
  assert.equal(camelCase('foo-BAR', { preserveConsecutiveUppercase: true, pascalCase: true }), 'FooBAR');
});
process.exit(failures ? 1 : 0);
JS
node verify_check.mjs
