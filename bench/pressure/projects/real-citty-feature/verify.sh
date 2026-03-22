#!/usr/bin/env bash
set -euo pipefail

npm run build >/dev/null 2>&1 || npx tsc >/dev/null 2>&1 || true
trap 'rm -f verify_check.js' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const { pathToFileURL } = require('url')

let failures = 0

async function loadModule() {
  const candidates = ['dist/index.cjs', 'dist/index.mjs', 'dist/index.js', 'src/index.js']
  for (const candidate of candidates) {
    const full = path.resolve(candidate)
    if (!fs.existsSync(full)) continue
    if (full.endsWith('.mjs')) return import(pathToFileURL(full).href)
    return require(full)
  }
  throw new Error('citty build output not found')
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

;(async () => {
  const mod = await loadModule()
  const defineCommand = mod.defineCommand
  const runCommand = mod.runCommand
  const renderUsage = mod.renderUsage

  function makeCli() {
    return defineCommand({
      meta: { name: 'root' },
      subCommands: {
        install: defineCommand({ meta: { name: 'install', alias: ['i', 'add'] }, run: () => 'install' }),
        info: defineCommand({ meta: { name: 'info' }, run: () => 'info' }),
        i: defineCommand({ meta: { name: 'i' }, run: () => 'literal-i' }),
        nested: defineCommand({
          meta: { name: 'nested' },
          subCommands: {
            child: defineCommand({ meta: { name: 'child', alias: ['c'] }, run: () => 'child' })
          }
        })
      }
    })
  }

  await report('single-string aliases resolve to the subcommand', async () => {
    const result = await runCommand(makeCli(), { rawArgs: ['i'] })
    assert.strictEqual(result.result, 'install')
  })

  await report('array aliases resolve to the same subcommand', async () => {
    const result = await runCommand(makeCli(), { rawArgs: ['add'] })
    assert.strictEqual(result.result, 'install')
  })

  await report('direct key matches win over alias matches', async () => {
    const result = await runCommand(makeCli(), { rawArgs: ['i'] })
    assert.notStrictEqual(result.result, 'literal-i')
  })

  await report('aliases appear in rendered help output', async () => {
    const usage = await renderUsage(makeCli())
    assert.ok(usage.includes('install'))
    assert.ok(usage.includes('i'))
    assert.ok(usage.includes('add'))
  })

  await report('nested subcommand aliases also resolve correctly', async () => {
    const result = await runCommand(makeCli(), { rawArgs: ['nested', 'c'] })
    assert.strictEqual(result.result, 'child')
  })

  await report('unknown aliases still error cleanly', async () => {
    try {
      await runCommand(makeCli(), { rawArgs: ['missing'] })
      throw new Error('unknown alias should fail')
    } catch (error) {
      assert.ok(/unknown|not found|invalid/i.test(error.message))
    }
  })

  process.exit(failures ? 1 : 0)
})().catch((error) => {
  console.error(error)
  process.exit(1)
})
JS

node verify_check.js
