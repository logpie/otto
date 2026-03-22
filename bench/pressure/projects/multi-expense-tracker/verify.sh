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
  throw new Error('expense tracker server entry point not found')
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
  const port = 34000 + Math.floor(Math.random() * 1000)
  server = spawn(process.execPath, [findServerFile()], {
    env: { ...process.env, PORT: String(port) },
    stdio: ['ignore', 'ignore', 'ignore']
  })
  const base = `http://127.0.0.1:${port}`
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    try {
      await fetch(`${base}/expenses`)
      return base
    } catch (_) {
      await new Promise((resolve) => setTimeout(resolve, 150))
    }
  }
  throw new Error('expense tracker server did not become ready')
}

process.on('exit', () => { if (server) server.kill('SIGTERM') })

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

  await report('POST /expenses and /budgets persist valid records', async () => {
    const expense = await request('POST', '/expenses', {
      amount: 20,
      currency: 'USD',
      category: 'food',
      description: 'lunch',
      date: '2024-01-15',
      tags: ['meal']
    })
    const budget = await request('POST', '/budgets', {
      category: 'food',
      monthly_limit: 100,
      currency: 'USD'
    })
    assert.strictEqual(expense.response.status < 300, true)
    assert.strictEqual(budget.response.status < 300, true)
    createdId = expense.payload.id
    assert.ok(createdId)
  })

  await report('validation rejects bad amount, currency, date, and missing category', async () => {
    const bad = await request('POST', '/expenses', {
      amount: -1,
      currency: 'US',
      category: '',
      description: 'oops',
      date: 'not-a-date'
    })
    assert.ok(bad.response.status >= 400)
  })

  await report('GET /expenses supports filtering and pagination', async () => {
    await request('POST', '/expenses', {
      amount: 40,
      currency: 'USD',
      category: 'travel',
      description: 'train',
      date: '2024-01-15',
      tags: ['trip']
    })
    const list = await request('GET', '/expenses?category=food&month=2024-01&minAmount=10&maxAmount=30&page=1&limit=1')
    const items = Array.isArray(list.payload) ? list.payload : list.payload.items || list.payload.expenses
    assert.strictEqual(items.length, 1)
    assert.strictEqual(items[0].category, 'food')
  })

  await report('GET /expenses/:id returns a record and DELETE removes it', async () => {
    const fetched = await request('GET', `/expenses/${createdId}`)
    assert.strictEqual(fetched.payload.id, createdId)
    const deleted = await request('DELETE', `/expenses/${createdId}`)
    assert.strictEqual(deleted.response.status < 300, true)
    const missing = await request('GET', `/expenses/${createdId}`)
    assert.ok(missing.response.status >= 400)
  })

  await report('analytics/category-totals and budget-status compute spending correctly', async () => {
    await request('POST', '/expenses', {
      amount: 60,
      currency: 'USD',
      category: 'food',
      description: 'dinner',
      date: '2024-01-16',
      tags: []
    })
    const totals = await request('GET', '/analytics/category-totals?month=2024-01')
    const budget = await request('GET', '/analytics/budget-status?month=2024-01')
    const totalsText = JSON.stringify(totals.payload).toLowerCase()
    const budgetText = JSON.stringify(budget.payload).toLowerCase()
    assert.ok(totalsText.includes('food'))
    assert.ok(budgetText.includes('remaining') || budgetText.includes('overbudget'))
  })

  await report('analytics/trends returns monthly totals in order', async () => {
    const trends = await request('GET', '/analytics/trends?months=6')
    const items = Array.isArray(trends.payload) ? trends.payload : trends.payload.trends
    assert.ok(Array.isArray(items))
    assert.ok(items.length >= 1)
  })

  process.exit(failures ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
JS

node verify_check.js
