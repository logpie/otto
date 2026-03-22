#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.js; exit $rc' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const { spawn } = require('child_process')
const fs = require('fs')
const path = require('path')
const WebSocket = require('ws')

let failures = 0
let server

function findServerFile() {
  for (const candidate of ['server.js', 'app.js', 'index.js', 'src/server.js', 'src/index.js']) {
    if (fs.existsSync(candidate)) return candidate
  }
  throw new Error('chat server entry point not found')
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
  const port = 32000 + Math.floor(Math.random() * 1000)
  server = spawn(process.execPath, [findServerFile()], {
    env: { ...process.env, PORT: String(port) },
    stdio: ['ignore', 'ignore', 'ignore']
  })
  const url = `ws://127.0.0.1:${port}`
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    try {
      await connect(url)
      return url
    } catch (_) {
      await new Promise((resolve) => setTimeout(resolve, 150))
    }
  }
  throw new Error('chat server did not become ready')
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url)
    ws.once('open', () => resolve(ws))
    ws.once('error', reject)
  })
}

function send(ws, type, payload) {
  ws.send(JSON.stringify({ type, payload }))
}

function waitFor(ws, predicate, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off('message', onMessage)
      reject(new Error('timed out waiting for websocket message'))
    }, timeout)
    function onMessage(raw) {
      const text = raw.toString()
      let payload = text
      try {
        payload = JSON.parse(text)
      } catch (_) {}
      if (predicate(payload, text)) {
        clearTimeout(timer)
        ws.off('message', onMessage)
        resolve(payload)
      }
    }
    ws.on('message', onMessage)
  })
}

function expectNoMessage(ws, timeout = 500) {
  return new Promise((resolve, reject) => {
    const onMessage = (raw) => {
      clearTimeout(timer)
      ws.off('message', onMessage)
      reject(new Error(`unexpected message: ${raw}`))
    }
    const timer = setTimeout(() => {
      ws.off('message', onMessage)
      resolve()
    }, timeout)
    ws.on('message', onMessage)
  })
}

process.on('exit', () => { if (server) server.kill('SIGTERM') })

;(async () => {
  const url = await startServer()

  await report('room members receive messages while other rooms stay isolated', async () => {
    const alice = await connect(url)
    const bob = await connect(url)
    const cara = await connect(url)
    send(alice, 'join', { room: 'alpha', username: 'alice' })
    send(bob, 'join', { room: 'alpha', username: 'bob' })
    send(cara, 'join', { room: 'beta', username: 'cara' })
    send(alice, 'message', { text: 'hello-room' })
    await waitFor(bob, (_, text) => text.includes('hello-room'))
    await expectNoMessage(cara)
    alice.close(); bob.close(); cara.close()
  })

  await report('duplicate usernames in one room are rejected', async () => {
    const one = await connect(url)
    const two = await connect(url)
    send(one, 'join', { room: 'dupe', username: 'sam' })
    send(two, 'join', { room: 'dupe', username: 'sam' })
    const message = await waitFor(two, (_, text) => text.toLowerCase().includes('error') || text.toLowerCase().includes('duplicate'))
    assert.ok(message)
    one.close(); two.close()
  })

  await report('list_rooms and list_users reflect active state', async () => {
    const a = await connect(url)
    const b = await connect(url)
    send(a, 'join', { room: 'state', username: 'ann' })
    send(b, 'join', { room: 'state', username: 'ben' })
    send(a, 'list_rooms', {})
    const rooms = await waitFor(a, (_, text) => text.includes('state'))
    send(a, 'list_users', {})
    const users = await waitFor(a, (_, text) => text.includes('ann') && text.includes('ben'))
    assert.ok(rooms && users)
    a.close(); b.close()
  })

  await report('message history is replayed to new room members', async () => {
    const a = await connect(url)
    send(a, 'join', { room: 'history', username: 'hist-a' })
    send(a, 'message', { text: 'first-history' })
    send(a, 'message', { text: 'second-history' })
    const b = await connect(url)
    send(b, 'join', { room: 'history', username: 'hist-b' })
    await waitFor(b, (_, text) => text.includes('first-history') || text.includes('second-history'))
    a.close(); b.close()
  })

  await report('disconnect cleanup removes empty rooms from later room listings', async () => {
    const a = await connect(url)
    send(a, 'join', { room: 'cleanup', username: 'solo' })
    a.close()
    await new Promise((resolve) => setTimeout(resolve, 200))
    const b = await connect(url)
    send(b, 'join', { room: 'other', username: 'observer' })
    send(b, 'list_rooms', {})
    await waitFor(b, (_, text) => !text.includes('cleanup'))
    b.close()
  })

  process.exit(failures ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
JS

node verify_check.js
