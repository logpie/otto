#!/usr/bin/env bash
set -uo pipefail

npm run build >/dev/null 2>&1 || npx tsc >/dev/null 2>&1 || true
trap 'rc=$?; rm -f verify_check.js; exit $rc' EXIT

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
  throw new Error('schema validator module not found')
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

const mod = requireFirst(['dist/index.js', 'dist/schema.js', 'index.js', 'src/index.js'])
const s = mod.s || mod.default || mod

async function checkBaseTypes() {
  assert.strictEqual(s.string().parse('ok'), 'ok')
  assert.strictEqual(s.number().parse(4), 4)
  assert.strictEqual(s.boolean().parse(true), true)
  assert.deepStrictEqual(s.array(s.number()).parse([1, 2]), [1, 2])
}

async function checkModifiers() {
  assert.strictEqual(s.string().optional().parse(undefined), undefined)
  assert.strictEqual(s.string().nullable().parse(null), null)
  assert.strictEqual(s.string().default('x').parse(undefined), 'x')
  assert.strictEqual(s.string().email().parse('a@example.com'), 'a@example.com')
  assert.strictEqual(s.number().int().min(2).max(5).parse(3), 3)
}

async function checkObjectAndPathErrors() {
  const schema = s.object({ address: s.object({ zip: s.string() }) })
  try {
    schema.parse({ address: { zip: 12345 } })
    throw new Error('parse should have failed')
  } catch (error) {
    const text = JSON.stringify(error)
    assert.ok(text.includes('address') || text.includes('zip'))
  }
}

async function checkSafeParse() {
  const result = s.number().safeParse('bad')
  assert.strictEqual(result.success, false)
  assert.ok(result.error)
}

async function checkUnion() {
  const schema = s.union([s.string(), s.number()])
  assert.strictEqual(schema.parse('x'), 'x')
  assert.strictEqual(schema.parse(3), 3)
  const result = schema.safeParse(true)
  assert.strictEqual(result.success, false)
}

async function checkNestedArrays() {
  const schema = s.object({
    users: s.array(s.object({ name: s.string().min(1), age: s.number().int() }))
  })
  const parsed = schema.parse({ users: [{ name: 'A', age: 1 }] })
  assert.deepStrictEqual(parsed.users[0], { name: 'A', age: 1 })
}

;(async () => {
  await report('primitive and array schemas parse valid values', checkBaseTypes)
  await report('optional, nullable, default, email, and numeric modifiers behave correctly', checkModifiers)
  await report('nested object failures include path information', checkObjectAndPathErrors)
  await report('safeParse returns a non-throwing error result', checkSafeParse)
  await report('union schemas accept either branch and reject mismatches', checkUnion)
  await report('nested arrays of objects infer and parse structured values', checkNestedArrays)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
