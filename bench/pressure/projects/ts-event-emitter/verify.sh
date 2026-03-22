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
  throw new Error('event emitter module not found')
}

function findEmitter(mod) {
  return mod.TypedEmitter || mod.EventEmitter || mod.default || Object.values(mod).find((value) => typeof value === 'function' && value.prototype?.emit)
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

const mod = requireFirst(['dist/index.js', 'dist/emitter.js', 'index.js', 'src/index.js'])
const Emitter = findEmitter(mod)
const Mediator = mod.Mediator || Object.values(mod).find((value) => typeof value === 'function' && value.prototype?.request)

async function checkBasicOnEmit() {
  const emitter = new Emitter()
  let seen = null
  emitter.on('click', (payload) => { seen = payload })
  emitter.emit('click', { x: 1 })
  assert.deepStrictEqual(seen, { x: 1 })
}

async function checkOnceAndOff() {
  const emitter = new Emitter()
  let count = 0
  const handler = () => { count += 1 }
  emitter.once('ping', handler)
  emitter.emit('ping')
  emitter.emit('ping')
  emitter.on('pong', handler)
  emitter.off('pong', handler)
  emitter.emit('pong')
  assert.strictEqual(count, 1)
}

async function checkWildcardAndPrepend() {
  const emitter = new Emitter()
  const order = []
  emitter.on('*', () => order.push('wild'))
  emitter.on('evt', () => order.push('normal'))
  emitter.prepend('evt', () => order.push('first'))
  emitter.emit('evt', {})
  assert.deepStrictEqual(order.slice(0, 2), ['wild', 'first'])
}

async function checkEmitAsync() {
  const emitter = new Emitter()
  const seen = []
  emitter.on('async', async () => {
    await new Promise((resolve) => setTimeout(resolve, 30))
    seen.push('done')
  })
  await emitter.emitAsync('async', {})
  assert.deepStrictEqual(seen, ['done'])
}

async function checkMaxListenersWarning() {
  const emitter = new Emitter({ maxListeners: 1 })
  let warned = false
  const originalWarn = console.warn
  console.warn = () => { warned = true }
  try {
    emitter.on('warn', () => {})
    emitter.on('warn', () => {})
  } finally {
    console.warn = originalWarn
  }
  assert.strictEqual(warned, true)
}

async function checkMediator() {
  assert.ok(Mediator, 'Mediator export missing')
  const mediator = new Mediator()
  mediator.on('sum', async (payload) => payload.a + payload.b)
  const value = await mediator.request('sum', { a: 2, b: 3 })
  assert.strictEqual(value, 5)
}

;(async () => {
  await report('basic on/emit delivers payloads', checkBasicOnEmit)
  await report('once fires once and off removes handlers', checkOnceAndOff)
  await report('wildcard listeners and prepend both affect dispatch order', checkWildcardAndPrepend)
  await report('emitAsync waits for async handlers', checkEmitAsync)
  await report('maxListeners emits a warning instead of failing hard', checkMaxListenersWarning)
  await report('Mediator request/response resolves handler output', checkMediator)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
