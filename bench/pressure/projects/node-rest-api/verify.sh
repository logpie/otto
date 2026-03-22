#!/usr/bin/env bash
set -euo pipefail

trap 'rm -f verify_check.js' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const { spawn } = require('child_process')
const fs = require('fs')
const path = require('path')

let failures = 0
let server

function findServerFile() {
  const pkg = fs.existsSync('package.json') ? JSON.parse(fs.readFileSync('package.json', 'utf8')) : {}
  const candidates = [pkg.main, 'server.js', 'app.js', 'index.js', 'src/server.js', 'src/app.js', 'src/index.js'].filter(Boolean)
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  throw new Error('server entry point not found')
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
  const port = 31000 + Math.floor(Math.random() * 1000)
  server = spawn(process.execPath, [findServerFile()], {
    env: { ...process.env, PORT: String(port) },
    stdio: ['ignore', 'ignore', 'ignore']
  })
  const base = `http://127.0.0.1:${port}`
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    try {
      await fetch(`${base}/bookmarks`)
      return base
    } catch (_) {
      await new Promise((resolve) => setTimeout(resolve, 150))
    }
  }
  throw new Error('server did not become ready')
}

function cleanup() {
  if (server) server.kill('SIGTERM')
}

process.on('exit', cleanup)
process.on('SIGINT', () => process.exit(1))

function toList(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload.items)) return payload.items
  if (Array.isArray(payload.bookmarks)) return payload.bookmarks
  throw new Error('list response is not an array-like payload')
}

;(async () => {
  const base = await startServer()

  async function request(method, url, body) {
    const response = await fetch(`${base}${url}`, {
      method,
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    })
    const payload = await response.json().catch(() => ({}))
    return { response, payload }
  }

  let createdId

  await report('POST /bookmarks creates a bookmark and sets X-Request-Id', async () => {
    const { response, payload } = await request('POST', '/bookmarks', {
      url: 'https://example.com/docs',
      title: 'Docs',
      tags: ['work']
    })
    assert.strictEqual(response.status < 300, true)
    assert.ok(response.headers.get('x-request-id'))
    createdId = payload.id
    assert.ok(createdId)
  })

  await report('validation errors return {error, requestId}', async () => {
    const { response, payload } = await request('POST', '/bookmarks', {
      url: 'notaurl',
      title: 'x'.repeat(250),
      tags: 'bad'
    })
    assert.ok(response.status >= 400)
    assert.ok(payload.error)
    assert.ok(payload.requestId)
  })

  await report('title and tags are sanitized against raw XSS payloads', async () => {
    const create = await request('POST', '/bookmarks', {
      url: 'https://example.com/xss',
      title: '<script>alert(1)</script>',
      tags: ['<img src=x onerror=1>']
    })
    const item = await request('GET', `/bookmarks/${create.payload.id}`)
    const text = JSON.stringify(item.payload)
    assert.ok(!text.includes('<script>'))
    assert.ok(!text.includes('onerror'))
  })

  await report('list filtering, search, pagination, and tag filtering all work together', async () => {
    await request('POST', '/bookmarks', { url: 'https://example.com/alpha', title: 'Alpha docs', tags: ['work'] })
    await request('POST', '/bookmarks', { url: 'https://example.com/beta', title: 'Beta docs', tags: ['personal'] })
    const { payload } = await request('GET', '/bookmarks?tag=work&search=docs&page=1&limit=1')
    const list = toList(payload)
    assert.strictEqual(list.length, 1)
    assert.ok(JSON.stringify(list[0]).toLowerCase().includes('work'))
  })

  await report('PUT mutates an existing bookmark and DELETE removes it', async () => {
    const updated = await request('PUT', `/bookmarks/${createdId}`, {
      url: 'https://example.com/updated',
      title: 'Updated',
      tags: ['updated']
    })
    assert.strictEqual(updated.response.status < 300, true)
    const deleted = await request('DELETE', `/bookmarks/${createdId}`)
    assert.strictEqual(deleted.response.status < 300, true)
    const missing = await request('GET', `/bookmarks/${createdId}`)
    assert.ok(missing.response.status >= 400)
  })

  await report('rate limiting rejects the 101st request from one client', async () => {
    let limited = false
    for (let i = 0; i < 110; i += 1) {
      const { response } = await request('GET', '/bookmarks')
      if (response.status === 429) {
        limited = true
        break
      }
    }
    assert.strictEqual(limited, true)
  })

  process.exit(failures ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
JS

node verify_check.js
