#!/usr/bin/env bash
set -euo pipefail

trap 'rm -f verify_check.js' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const fs = require('fs')
const path = require('path')

let failures = 0

function requireFirst(candidates) {
  for (const candidate of candidates) {
    const full = path.resolve(candidate)
    if (fs.existsSync(full)) {
      return require(full)
    }
  }
  throw new Error('queue module not found')
}

function findQueueClass(mod) {
  for (const value of Object.values(mod)) {
    if (typeof value === 'function') {
      const proto = value.prototype || {}
      if (proto.enqueue && proto.process && proto.drain) {
        return value
      }
    }
  }
  throw new Error('TaskQueue class not found')
}

function buildInstance(ClassRef, extra = {}) {
  const args = []
  if ((ClassRef.name || '').toLowerCase().includes('rate')) {
    args.push(extra.rate || 2)
  }
  return new ClassRef(...args)
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

const mod = requireFirst(['index.js', 'task_queue.js', 'queue.js', 'src/index.js', 'src/task_queue.js'])
const TaskQueue = mod.TaskQueue || findQueueClass(mod)
const RateLimitedQueue = mod.RateLimitedQueue || Object.values(mod).find((value) => typeof value === 'function' && /rate/i.test(value.name || ''))

async function waitForDrain(queue, processPromise) {
  if (typeof queue.drain === 'function') {
    await Promise.all([processPromise, queue.drain()])
  } else {
    await processPromise
  }
}

async function checkPriorityOrder() {
  const queue = buildInstance(TaskQueue)
  const order = []
  queue.enqueue(async () => order.push('low'), { priority: 'low' })
  queue.enqueue(async () => order.push('high'), { priority: 'high' })
  queue.enqueue(async () => order.push('medium'), { priority: 'medium' })
  const processing = queue.process(1)
  await waitForDrain(queue, processing)
  assert.deepStrictEqual(order, ['high', 'medium', 'low'])
}

async function checkConcurrencyLimit() {
  const queue = buildInstance(TaskQueue)
  let active = 0
  let maxActive = 0
  for (let i = 0; i < 6; i += 1) {
    queue.enqueue(async () => {
      active += 1
      maxActive = Math.max(maxActive, active)
      await new Promise((resolve) => setTimeout(resolve, 60))
      active -= 1
    }, { priority: 'medium' })
  }
  const processing = queue.process(2)
  await waitForDrain(queue, processing)
  assert.ok(maxActive <= 2)
  assert.ok(maxActive >= 2)
}

async function checkRetryBackoff() {
  const queue = buildInstance(TaskQueue)
  const times = []
  let attempts = 0
  queue.enqueue(async () => {
    times.push(Date.now())
    attempts += 1
    if (attempts < 3) throw new Error('retry me')
  }, { priority: 'high', maxRetries: 2 })
  const processing = queue.process(1)
  await waitForDrain(queue, processing)
  assert.strictEqual(attempts, 3)
  assert.ok(times[1] - times[0] >= 90)
  assert.ok(times[2] - times[1] >= 180)
}

async function checkDlqAndEvents() {
  const queue = buildInstance(TaskQueue)
  const seen = []
  if (typeof queue.on === 'function') {
    queue.on('retrying', () => seen.push('retrying'))
    queue.on('dlq', () => seen.push('dlq'))
  }
  queue.enqueue(async () => { throw new Error('dead') }, { priority: 'high', maxRetries: 1 })
  const processing = queue.process(1)
  await waitForDrain(queue, processing)
  assert.ok(typeof queue.getDLQ === 'function')
  assert.strictEqual(queue.getDLQ().length, 1)
  assert.ok(seen.includes('retrying') || seen.includes('dlq'))
}

async function checkPauseResume() {
  const queue = buildInstance(TaskQueue)
  let started = false
  queue.enqueue(async () => { started = true }, { priority: 'high' })
  queue.pause()
  const processing = queue.process(1)
  await new Promise((resolve) => setTimeout(resolve, 80))
  assert.strictEqual(started, false)
  queue.resume()
  await waitForDrain(queue, processing)
  assert.strictEqual(started, true)
}

async function checkRateLimitedSubclass() {
  assert.ok(RateLimitedQueue, 'RateLimitedQueue export missing')
  const queue = buildInstance(RateLimitedQueue, { rate: 2 })
  const times = []
  for (let i = 0; i < 4; i += 1) {
    queue.enqueue(async () => { times.push(Date.now()) }, { priority: 'medium' })
  }
  const processing = queue.process(4)
  await waitForDrain(queue, processing)
  assert.ok(times.length === 4)
  assert.ok(times[2] - times[0] >= 800)
}

;(async () => {
  await report('priority scheduling dequeues high before medium before low', checkPriorityOrder)
  await report('process(concurrency) never exceeds the requested in-flight limit', checkConcurrencyLimit)
  await report('retry logic uses exponential-ish backoff before success', checkRetryBackoff)
  await report('dead-letter queue captures permanently failing tasks', checkDlqAndEvents)
  await report('pause and resume gate task execution', checkPauseResume)
  await report('RateLimitedQueue spreads work across time', checkRateLimitedSubclass)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
