#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.js; exit $rc' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const { PriorityQueue, Scheduler } = require('./scheduler')

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

async function checkPriorityQueue() {
  const pq = new PriorityQueue()
  const priorities = [7, 3, 9, 1, 5, 8, 2, 6, 4, 10, 0]
  priorities.forEach((priority) => pq.enqueue(priority, priority))
  const out = []
  while (pq.size > 0) out.push(pq.dequeue())
  assert.deepStrictEqual(out, [...priorities].sort((a, b) => a - b))
}

async function checkDelayedJobs() {
  const scheduler = new Scheduler()
  const events = []
  scheduler.addJob('delayed', () => { events.push(Date.now()) }, { delay: 120 })
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.strictEqual(events.length, 0)
  await new Promise((resolve) => setTimeout(resolve, 100))
  await scheduler.run()
  scheduler.stop()
  assert.strictEqual(events.length, 1)
}

async function checkIntervalSpacing() {
  const scheduler = new Scheduler()
  const times = []
  scheduler.addJob('interval', () => {
    times.push(Date.now())
    if (times.length === 3) scheduler.stop()
  }, { interval: 120 })
  await scheduler.run()
  assert.ok(times.length >= 3)
  assert.ok(times[1] - times[0] >= 100)
  assert.ok(times[2] - times[1] >= 100)
}

async function checkConcurrency() {
  const scheduler = new Scheduler()
  let active = 0
  let maxActive = 0
  for (let i = 0; i < 6; i += 1) {
    scheduler.addJob(`job-${i}`, async () => {
      active += 1
      maxActive = Math.max(maxActive, active)
      await new Promise((resolve) => setTimeout(resolve, 80))
      active -= 1
    }, { priority: i })
  }
  await scheduler.run(3)
  scheduler.stop()
  assert.ok(maxActive >= 2)
  assert.ok(maxActive <= 3)
}

async function checkSerializableErrors() {
  const scheduler = new Scheduler()
  scheduler.addJob('bad', () => { throw new Error('boom') })
  await scheduler.run()
  scheduler.stop()
  const serialized = JSON.stringify(scheduler.results)
  assert.ok(serialized.includes('boom'))
  assert.ok(!serialized.includes('"error":{}'))
}

async function checkMixedResults() {
  const scheduler = new Scheduler()
  scheduler.addJob('ok', () => 'done', { priority: 1 })
  scheduler.addJob('bad', () => { throw new Error('broken') }, { priority: 2 })
  const results = await scheduler.run(2)
  scheduler.stop()
  assert.ok(results.some((entry) => entry.status === 'ok'))
  assert.ok(results.some((entry) => entry.status === 'error'))
}

;(async () => {
  await report('priority queue is a real min-heap for 10+ items', checkPriorityQueue)
  await report('delayed jobs do not run before their delay elapses', checkDelayedJobs)
  await report('interval jobs wait between executions', checkIntervalSpacing)
  await report('multiple workers process jobs concurrently', checkConcurrency)
  await report('error results serialize with a readable message', checkSerializableErrors)
  await report('mixed success and failure jobs complete without crashing', checkMixedResults)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
