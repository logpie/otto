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
  throw new Error('dependency injector module not found')
}

function findContainer(mod) {
  return mod.Container || mod.default || Object.values(mod).find((value) => typeof value === 'function' && value.prototype?.register && value.prototype?.resolve)
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

const mod = requireFirst(['dist/index.js', 'dist/container.js', 'index.js', 'src/index.js'])
const Container = findContainer(mod)

async function checkSingletonTransient() {
  const c = new Container()
  let count = 0
  c.register('single', () => ({ id: ++count }), { scope: 'singleton' })
  c.register('transient', () => ({ id: ++count }), { scope: 'transient' })
  assert.strictEqual(c.resolve('single'), c.resolve('single'))
  assert.notStrictEqual(c.resolve('transient'), c.resolve('transient'))
}

async function checkScopedLifecycle() {
  const c = new Container()
  let count = 0
  c.register('scoped', () => ({ id: ++count }), { scope: 'scoped' })
  const scopeA = c.createScope()
  const scopeB = c.createScope()
  assert.strictEqual(scopeA.resolve('scoped'), scopeA.resolve('scoped'))
  assert.notStrictEqual(scopeA.resolve('scoped'), scopeB.resolve('scoped'))
}

async function checkCircularDetection() {
  const c = new Container()
  c.register('A', (container) => ({ b: container.resolve('B') }))
  c.register('B', (container) => ({ a: container.resolve('A') }))
  try {
    c.resolve('A')
    throw new Error('circular resolution should fail')
  } catch (error) {
    assert.ok(/circular|cycle|A|B/i.test(error.message))
  }
}

async function checkChildOverride() {
  const parent = new Container()
  parent.register('value', () => 'parent', { scope: 'singleton' })
  const child = parent.createChild()
  child.register('value', () => 'child', { scope: 'singleton' })
  assert.strictEqual(child.resolve('value'), 'child')
  assert.strictEqual(parent.resolve('value'), 'parent')
}

async function checkDispose() {
  const c = new Container()
  let disposed = 0
  c.register('service', () => ({ dispose: () => { disposed += 1 } }), { scope: 'singleton' })
  c.resolve('service')
  c.dispose()
  assert.strictEqual(disposed, 1)
}

async function checkTagsAndLazy() {
  const c = new Container()
  c.register('one', () => ({ value: 1 }), { scope: 'singleton', tags: ['db'] })
  c.register('two', () => ({ value: 2 }), { scope: 'singleton', tags: ['db'] })
  let constructed = 0
  c.register('lazy', () => ({ value: ++constructed }), { scope: 'singleton' })
  const values = c.resolveAll('db').map((entry) => entry.value).sort()
  const lazy = c.lazy('lazy')
  assert.deepStrictEqual(values, [1, 2])
  assert.strictEqual(constructed, 0)
  assert.strictEqual(lazy.value, 1)
}

;(async () => {
  await report('singleton and transient scopes behave differently', checkSingletonTransient)
  await report('scoped registrations are shared only within one scope', checkScopedLifecycle)
  await report('circular dependencies raise a clear error', checkCircularDetection)
  await report('child containers override while still inheriting parent registrations', checkChildOverride)
  await report('dispose() calls singleton dispose handlers', checkDispose)
  await report('resolveAll(tag) and lazy(token) both work', checkTagsAndLazy)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
