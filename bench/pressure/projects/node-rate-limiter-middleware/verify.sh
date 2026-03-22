#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.js; exit $rc' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const express = require('express')
const fs = require('fs')
const http = require('http')
const path = require('path')

let failures = 0

function requireFirst(candidates) {
  for (const candidate of candidates) {
    const full = path.resolve(candidate)
    if (fs.existsSync(full)) return require(full)
  }
  throw new Error('middleware module not found')
}

function findFactory(mod) {
  if (typeof mod.createRateLimiter === 'function') return mod.createRateLimiter
  for (const value of Object.values(mod)) {
    if (typeof value === 'function') return value
  }
  throw new Error('createRateLimiter export not found')
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

async function startServer(middleware) {
  const app = express()
  app.get('/test', middleware, (req, res) => res.json({ ok: true }))
  const server = http.createServer(app)
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  return {
    server,
    port,
    async request(key = 'a') {
      const response = await fetch(`http://127.0.0.1:${port}/test`, { headers: { 'x-key': key } })
      return { response, body: await response.json().catch(() => ({})) }
    }
  }
}

const mod = requireFirst(['index.js', 'rate_limiter.js', 'middleware.js', 'src/index.js'])
const createRateLimiter = findFactory(mod)

async function withAlgorithm(algorithm, fn) {
  const reached = []
  const middleware = createRateLimiter({
    algorithm,
    limit: 2,
    windowMs: 400,
    keyGenerator: (req) => req.headers['x-key'] || req.ip,
    skipList: ['vip'],
    onLimitReached: () => reached.push('hit')
  })
  const ctx = await startServer(middleware)
  try {
    await fn(ctx, reached)
  } finally {
    ctx.server.close()
  }
}

async function algorithmWorks(name) {
  await withAlgorithm(name, async ({ request }, reached) => {
    assert.strictEqual((await request()).response.status, 200)
    assert.strictEqual((await request()).response.status, 200)
    const limited = await request()
    assert.strictEqual(limited.response.status, 429)
    assert.ok(limited.response.headers.get('retry-after'))
    assert.ok(limited.body.error)
    assert.ok('retryAfter' in limited.body)
    assert.ok(reached.length >= 1)
  })
}

async function checkHeadersAndIsolation() {
  await withAlgorithm('fixed-window', async ({ request }) => {
    const one = await request('alpha')
    assert.strictEqual(one.response.headers.get('x-ratelimit-limit'), '2')
    assert.ok(one.response.headers.get('x-ratelimit-remaining') !== null)
    assert.ok(one.response.headers.get('x-ratelimit-reset') !== null)
    await request('alpha')
    const limited = await request('alpha')
    assert.strictEqual(limited.response.status, 429)
    const other = await request('beta')
    assert.strictEqual(other.response.status, 200)
  })
}

async function checkSkipListAndReset() {
  await withAlgorithm('token-bucket', async ({ request }) => {
    assert.strictEqual((await request('vip')).response.status, 200)
    assert.strictEqual((await request('vip')).response.status, 200)
    assert.strictEqual((await request('vip')).response.status, 200)
    await request('alpha')
    await request('alpha')
    assert.strictEqual((await request('alpha')).response.status, 429)
    await new Promise((resolve) => setTimeout(resolve, 500))
    assert.strictEqual((await request('alpha')).response.status, 200)
  })
}

;(async () => {
  await report('fixed-window limiting rejects the N+1 request with retry metadata', () => algorithmWorks('fixed-window'))
  await report('sliding-window-log limiting also enforces the configured limit', () => algorithmWorks('sliding-window-log'))
  await report('token-bucket limiting also enforces the configured limit', () => algorithmWorks('token-bucket'))
  await report('normal responses include rate-limit headers and keys are isolated', checkHeadersAndIsolation)
  await report('skipList bypasses limiting and windows reset over time', checkSkipListAndReset)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
