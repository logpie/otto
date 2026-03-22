#!/usr/bin/env bash
set -euo pipefail

trap 'rm -f verify_check.js' EXIT

cat > verify_check.js <<'JS'
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const os = require('os')

let failures = 0

function requireFirst(candidates) {
  for (const candidate of candidates) {
    const full = path.resolve(candidate)
    if (fs.existsSync(full)) return require(full)
  }
  throw new Error('file processor module not found')
}

function findProcessor(mod) {
  for (const value of Object.values(mod)) {
    if (typeof value === 'function') {
      const proto = value.prototype || {}
      if (proto.process || proto.processFile) return value
    }
  }
  if (typeof mod.processFile === 'function' || typeof mod.process === 'function') return mod
  throw new Error('processor export not found')
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

const mod = requireFirst(['index.js', 'file_processor.js', 'processor.js', 'src/index.js', 'src/file_processor.js'])
const Processor = findProcessor(mod)

async function runPipeline(inputFile, outputFile, options) {
  if (typeof Processor === 'function' && Processor.prototype) {
    const instance = new Processor(options)
    if (typeof instance.processFile === 'function') {
      return instance.processFile(inputFile, outputFile, options)
    }
    if (typeof instance.process === 'function') {
      return instance.process(inputFile, outputFile, options)
    }
  }
  if (typeof Processor.processFile === 'function') {
    return Processor.processFile(inputFile, outputFile, options)
  }
  if (typeof Processor.process === 'function') {
    return Processor.process(inputFile, outputFile, options)
  }
  throw new Error('no runnable process method')
}

function readJsonl(file) {
  return fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line))
}

async function checkValidProcessing() {
  const out = path.join(os.tmpdir(), `processor-valid-${Date.now()}.jsonl`)
  const summary = await runPipeline('fixtures/valid.jsonl', out, {
    schema: {
      name: { type: 'string', required: true },
      age: { type: 'number', required: true },
      email: { type: 'email', required: false }
    },
    transform: (record) => ({ ...record, upper: record.name.toUpperCase() })
  })
  const rows = readJsonl(out)
  assert.strictEqual(rows.length, 2)
  assert.strictEqual(rows[0].upper, 'ALICE')
  assert.ok(summary)
}

async function checkMixedFileErrorsContinue() {
  const out = path.join(os.tmpdir(), `processor-mixed-${Date.now()}.jsonl`)
  const summary = await runPipeline('fixtures/mixed.jsonl', out, {
    schema: {
      name: { type: 'string', required: true },
      age: { type: 'number', required: true }
    },
    transform: (record) => record
  })
  const rows = fs.existsSync(out) ? readJsonl(out) : []
  const text = JSON.stringify(summary)
  assert.ok(rows.some((row) => row.name === 'Carol'))
  assert.ok(text.includes('line'))
  assert.ok(text.toLowerCase().includes('error'))
}

async function checkSchemaValidation() {
  const out = path.join(os.tmpdir(), `processor-schema-${Date.now()}.jsonl`)
  const summary = await runPipeline('fixtures/valid.jsonl', out, {
    schema: {
      name: { type: 'string', required: true },
      email: { type: 'email', required: true }
    },
    transform: (record) => record
  })
  const text = JSON.stringify(summary).toLowerCase()
  assert.ok(text.includes('invalid') || text.includes('error'))
}

async function checkBlankLinesSkipped() {
  const out = path.join(os.tmpdir(), `processor-blank-${Date.now()}.jsonl`)
  const summary = await runPipeline('fixtures/mixed.jsonl', out, {
    schema: { name: { type: 'string', required: true } },
    transform: (record) => record
  })
  const text = JSON.stringify(summary).toLowerCase()
  assert.ok(!text.includes('line 3') || text.includes('parse'))
}

async function checkSummaryShape() {
  const out = path.join(os.tmpdir(), `processor-summary-${Date.now()}.jsonl`)
  const summary = await runPipeline('fixtures/mixed.jsonl', out, {
    schema: { name: { type: 'string', required: true } },
    transform: (record) => record
  })
  const text = JSON.stringify(summary).toLowerCase()
  for (const token of ['total', 'valid', 'invalid']) {
    assert.ok(text.includes(token))
  }
}

;(async () => {
  await report('valid JSONL files stream through parse/validate/transform/write', checkValidProcessing)
  await report('parse errors include line references and do not stop later records', checkMixedFileErrorsContinue)
  await report('schema validation rejects records that do not meet requirements', checkSchemaValidation)
  await report('empty lines are skipped rather than treated as hard failures', checkBlankLinesSkipped)
  await report('processing returns an accurate summary report shape', checkSummaryShape)
  process.exit(failures ? 1 : 0)
})()
JS

node verify_check.js
