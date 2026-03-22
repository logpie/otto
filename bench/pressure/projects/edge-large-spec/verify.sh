#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.js; exit $rc' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const { spawn } = require('child_process')
const fs = require('fs')

let failures = 0
let server

function findServerFile() {
  for (const candidate of ['server.js', 'app.js', 'index.js', 'src/server.js', 'src/app.js', 'src/index.js']) {
    if (fs.existsSync(candidate)) return candidate
  }
  throw new Error('URL shortener server entry point not found')
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

async function startServer() {
  const port = 33000 + Math.floor(Math.random() * 1000)
  server = spawn(process.execPath, [findServerFile()], {
    env: { ...process.env, PORT: String(port) },
    stdio: ['ignore', 'ignore', 'ignore']
  })
  const base = `http://127.0.0.1:${port}`
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    try {
      await fetch(`${base}/health`)
      return base
    } catch (_) {
      await new Promise((resolve) => setTimeout(resolve, 150))
    }
  }
  throw new Error('URL shortener server did not become ready')
}

process.on('exit', () => { if (server) server.kill('SIGTERM') })

;(async () => {
  const base = await startServer()

  async function request(method, url, body, headers = {}) {
    const response = await fetch(`${base}${url}`, {
      method,
      redirect: 'manual',
      headers: body ? { 'content-type': 'application/json', ...headers } : headers,
      body: body ? JSON.stringify(body) : undefined
    })
    const payload = await response.json().catch(() => ({}))
    return { response, payload }
  }

  let alias

  await report('health endpoint responds with uptime/count and CORS headers', async () => {
    const { response, payload } = await request('GET', '/health')
    assert.ok(payload.uptime !== undefined)
    assert.ok(payload.totalLinks !== undefined || payload.total !== undefined)
    assert.ok(response.headers.get('access-control-allow-origin') !== null)
  })

  await report('POST /shorten returns a base62 alias and short URL', async () => {
    const { response, payload } = await request('POST', '/shorten', { url: 'https://example.com/one' })
    assert.strictEqual(response.status < 300, true)
    assert.ok(payload.shortUrl)
    assert.ok(/^[A-Za-z0-9]{7}$/.test(payload.alias))
    alias = payload.alias
  })

  await report('custom alias validation accepts valid aliases and rejects invalid ones', async () => {
    const good = await request('POST', '/shorten', { url: 'https://example.com/two', customAlias: 'Alpha12' })
    assert.strictEqual(good.response.status < 300, true)
    const bad = await request('POST', '/shorten', { url: 'https://example.com/three', customAlias: '!!' })
    assert.ok(bad.response.status >= 400)
    assert.ok(bad.payload.error && bad.payload.code)
  })

  await report('duplicate URLs reuse the same alias', async () => {
    const first = await request('POST', '/shorten', { url: 'https://example.com/dupe' })
    const second = await request('POST', '/shorten', { url: 'https://example.com/dupe' })
    assert.strictEqual(first.payload.alias, second.payload.alias)
  })

  await report('redirects increment stats and record click metadata', async () => {
    const click = await request('GET', `/${alias}`, null, { referer: 'https://ref.example', 'user-agent': 'verify-agent' })
    assert.strictEqual(click.response.status, 301)
    const stats = await request('GET', `/${alias}/stats`)
    const text = JSON.stringify(stats.payload).toLowerCase()
    assert.ok(text.includes('click'))
    assert.ok(text.includes('ref'))
  })

  await report('expired links return 410 Gone', async () => {
    const created = await request('POST', '/shorten', { url: 'https://example.com/short', expiresIn: 1 })
    await new Promise((resolve) => setTimeout(resolve, 1200))
    const expired = await request('GET', `/${created.payload.alias}`)
    assert.strictEqual(expired.response.status, 410)
  })

  await report('recent/top endpoints rank links and shorten rate limiting is enforced', async () => {
    for (let i = 0; i < 9; i += 1) {
      await request('POST', '/shorten', { url: `https://example.com/rate-${i}` })
    }
    const limited = await request('POST', '/shorten', { url: 'https://example.com/rate-over' })
    assert.strictEqual(limited.response.status, 429)
    const top = await request('GET', '/api/top')
    const recent = await request('GET', '/api/recent')
    assert.ok(Array.isArray(top.payload) || Array.isArray(top.payload.links))
    assert.ok(Array.isArray(recent.payload) || Array.isArray(recent.payload.links))
  })

  process.exit(failures ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
JS

node verify_check.js
