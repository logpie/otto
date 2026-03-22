#!/usr/bin/env bash
set -euo pipefail

npm run build >/dev/null 2>&1 || npx tsc >/dev/null 2>&1 || true
trap 'rm -f verify_check.js' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const fs = require('fs')
const path = require('path')

let failures = 0

function requireFirst(candidates) {
  for (const candidate of candidates) {
    const full = path.resolve(candidate)
    if (fs.existsSync(full)) return require(full)
  }
  throw new Error('result module not found')
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

const mod = requireFirst(['dist/index.js', 'dist/result.js', 'index.js', 'src/index.js'])
const Ok = mod.Ok || mod.ok
const Err = mod.Err || mod.err
const Some = mod.Some || mod.some
const None = mod.None || mod.none
const ResultAsync = mod.ResultAsync || mod.default?.ResultAsync
const fromPromise = mod.fromPromise || mod.ResultAsync?.fromPromise

async function checkResultMethods() {
  assert.strictEqual(Ok(2).map((x) => x + 1).unwrap(), 3)
  assert.strictEqual(Ok(2).flatMap((x) => Ok(x * 3)).unwrap(), 6)
  assert.strictEqual(Err('x').mapErr((e) => `${e}!`).unwrapOr('fallback'), 'fallback')
  assert.strictEqual(Ok(1).match({ ok: (v) => v + 1, err: () => 0 }), 2)
  assert.strictEqual(Err('bad').isErr(), true)
}

async function checkOptionMethods() {
  const none = typeof None === 'function' ? None() : None
  assert.strictEqual(Some(2).map((x) => x + 1).unwrap(), 3)
  assert.strictEqual(Some(2).flatMap((x) => Some(x * 2)).unwrap(), 4)
  assert.strictEqual(none.unwrapOr(9), 9)
  assert.strictEqual(Some('x').match({ some: (v) => v, none: () => 'n' }), 'x')
}

async function checkConversions() {
  const maybe = Ok(4).ok()
  assert.strictEqual(maybe.unwrap(), 4)
  const result = Some(4).okOr('missing')
  assert.strictEqual(result.unwrap(), 4)
}

async function checkAllAny() {
  const all = mod.Result.all([Ok(1), Ok(2)])
  const any = mod.Result.any([Err('x'), Ok(7), Ok(8)])
  assert.deepStrictEqual(all.unwrap(), [1, 2])
  assert.strictEqual(any.unwrap(), 7)
}

async function checkAsyncResult() {
  assert.ok(ResultAsync || fromPromise, 'async helpers missing')
  const wrapped = fromPromise(Promise.resolve(Ok(3)), (error) => String(error))
  const mapped = await wrapped.map((result) => result.unwrap() + 1)
  assert.strictEqual(mapped.unwrap(), 4)
  const rejected = await fromPromise(Promise.reject(new Error('boom')), (error) => error.message)
  assert.strictEqual(rejected.isErr(), true)
}

;(async () => {
  await report('Result supports map, mapErr, flatMap, unwrapOr, match, and predicates', checkResultMethods)
  await report('Option supports map, flatMap, unwrapOr, match, and none handling', checkOptionMethods)
  await report('Result and Option convert into each other correctly', checkConversions)
  await report('Result.all and Result.any aggregate collections correctly', checkAllAny)
  await report('ResultAsync/fromPromise preserve async success and failure paths', checkAsyncResult)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
