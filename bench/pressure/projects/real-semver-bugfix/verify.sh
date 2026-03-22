#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.js; exit $rc' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const semver = require('./index.js')

let failures = 0

async function report(name, fn) {
  try {
    await fn()
    console.log(`PASS ${name}`)
  } catch (error) {
    failures += 1
    console.log(`FAIL ${name}: ${error.message}`)
  }
}

async function checkMixedPrerelease() {
  const parsed = semver.parse('1.0.0-alpha.1ab')
  assert.deepStrictEqual(parsed.prerelease, ['alpha', '1ab'])
}

async function checkLongMixedPrerelease() {
  const parsed = semver.parse('1.0.0-beta.12ab34')
  assert.deepStrictEqual(parsed.prerelease, ['beta', '12ab34'])
}

async function checkNumericPrereleaseUnchanged() {
  const parsed = semver.parse('1.0.0-alpha.1')
  assert.deepStrictEqual(parsed.prerelease, ['alpha', 1])
}

async function checkCoerceKeepsMixedIdentifiers() {
  const coerced = semver.coerce('1.0.0-alpha.1ab', { includePrerelease: true })
  assert.strictEqual(coerced.version, '1.0.0-alpha.1ab')
}

async function checkRoundTripStringification() {
  const version = semver.parse('1.0.0-alpha.1ab')
  assert.strictEqual(String(version), '1.0.0-alpha.1ab')
}

async function checkUnrelatedVersionsStillParse() {
  assert.strictEqual(semver.parse('2.3.4').version, '2.3.4')
  assert.strictEqual(semver.valid('3.0.0-rc.1'), '3.0.0-rc.1')
}

;(async () => {
  await report('parse preserves mixed alphanumeric prerelease identifiers', checkMixedPrerelease)
  await report('longer mixed prerelease identifiers are preserved intact', checkLongMixedPrerelease)
  await report('pure numeric prerelease identifiers still behave normally', checkNumericPrereleaseUnchanged)
  await report('coerce(includePrerelease) keeps mixed identifiers intact', checkCoerceKeepsMixedIdentifiers)
  await report('version objects stringify back to the full prerelease', checkRoundTripStringification)
  await report('other semver parsing behavior remains intact', checkUnrelatedVersionsStillParse)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
