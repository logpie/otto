#!/usr/bin/env bash
set -euo pipefail

npm run build >/dev/null 2>&1 || npx tsc >/dev/null 2>&1 || true
trap 'rm -f verify_check.js' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const { pathToFileURL } = require('url')

let failures = 0

async function loadModule() {
  for (const candidate of ['dist/index.cjs', 'dist/index.js', 'src/index.js']) {
    const full = path.resolve(candidate)
    if (fs.existsSync(full)) return require(full)
  }
  for (const candidate of ['dist/index.mjs']) {
    const full = path.resolve(candidate)
    if (fs.existsSync(full)) return import(pathToFileURL(full).href)
  }
  throw new Error('radash build output not found')
}

async function report(name, fn) {
  try {
    await fn()
    console.log(`PASS ${name}`)
  } catch (error) {
    failures += 1
    console.log(`FAIL ${name}: ${error.message}`)
  }
}

;(async () => {
  const mod = await loadModule()
  const inRange = mod.inRange

  await report('inRange is exported from the main module', async () => {
    assert.strictEqual(typeof inRange, 'function')
  })

  await report('start is inclusive and end is exclusive', async () => {
    assert.strictEqual(inRange(3, 3, 5), true)
    assert.strictEqual(inRange(5, 3, 5), false)
  })

  await report('two-argument form uses [0, end)', async () => {
    assert.strictEqual(inRange(2, 4), true)
    assert.strictEqual(inRange(4, 4), false)
  })

  await report('inverted bounds are handled by swapping', async () => {
    assert.strictEqual(inRange(3, 5, 1), true)
    assert.strictEqual(inRange(6, 5, 1), false)
  })

  await report('non-number inputs return false', async () => {
    assert.strictEqual(inRange(null, 0, 2), false)
    assert.strictEqual(inRange(undefined, 0, 2), false)
    assert.strictEqual(inRange(Number.NaN, 0, 2), false)
  })

  await report('negative and zero edge cases match lodash semantics', async () => {
    assert.strictEqual(inRange(-3, -5, -1), true)
    assert.strictEqual(inRange(0, 0, 1), true)
    assert.strictEqual(inRange(1, 0, 1), false)
  })

  process.exit(failures ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
JS

node verify_check.js
