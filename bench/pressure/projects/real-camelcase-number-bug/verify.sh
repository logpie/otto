#!/usr/bin/env bash
set -uo pipefail
trap 'rc=$?; rm -f verify_check.mjs; exit $rc' EXIT
cat > verify_check.mjs <<'JS'
import assert from 'node:assert/strict';
import camelCase from './index.js';
let failures = 0;
function report(name, fn) {
  try { fn(); console.log(`PASS ${name}`); }
  catch (error) { failures += 1; console.log(`FAIL ${name}: ${error.message}`); }
}
report('basic dash-separated words still camel-case normally', () => {
  assert.equal(camelCase('foo-bar'), 'fooBar');
});
report('underscore case after digits preserves lowercase until separator boundary', () => {
  assert.equal(camelCase('b2b_registration_request'), 'b2bRegistrationRequest');
});
report('dash case after digits also preserves the intended casing', () => {
  assert.equal(camelCase('b2b-registration-request'), 'b2bRegistrationRequest');
});
report('mixed prefixes with digits continue to camel-case correctly', () => {
  assert.equal(camelCase('api_v2_client'), 'apiV2Client');
});
process.exit(failures ? 1 : 0);
JS
node verify_check.mjs
