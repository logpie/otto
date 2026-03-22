#!/usr/bin/env bash
set -euo pipefail

npm run build >/dev/null 2>&1 || npx tsc >/dev/null 2>&1 || true
trap 'rm -f verify_check.mjs' EXIT

cat > verify_check.mjs <<'JS'
import assert from 'node:assert';
import { defineCommand, runCommand, renderUsage } from './dist/index.mjs';

let failures = 0;

async function report(name, fn) {
  try {
    await fn();
    console.log(`PASS ${name}`);
  } catch (error) {
    failures += 1;
    console.log(`FAIL ${name}: ${error.message}`);
  }
}

function makeCli() {
  return defineCommand({
    meta: { name: 'root' },
    subCommands: {
      install: defineCommand({ meta: { name: 'install', alias: ['i', 'add'] }, run: () => {} }),
      info: defineCommand({ meta: { name: 'info' }, run: () => {} }),
      nested: defineCommand({
        meta: { name: 'nested' },
        subCommands: {
          child: defineCommand({ meta: { name: 'child', alias: ['c'] }, run: () => {} })
        }
      })
    }
  });
}

await report('alias resolves to subcommand without error', async () => {
  // Before the feature, this throws "Unknown command i"
  await runCommand(makeCli(), { rawArgs: ['i'] });
});

await report('array alias also resolves', async () => {
  await runCommand(makeCli(), { rawArgs: ['add'] });
});

await report('direct key match still works', async () => {
  await runCommand(makeCli(), { rawArgs: ['install'] });
});

await report('aliases appear in rendered help output', async () => {
  const usage = await renderUsage(makeCli());
  assert.ok(usage.includes('install'), 'help should mention install');
  assert.ok(usage.includes('i') || usage.includes('add'), 'help should mention at least one alias');
});

await report('nested subcommand aliases resolve', async () => {
  await runCommand(makeCli(), { rawArgs: ['nested', 'c'] });
});

await report('unknown command still errors', async () => {
  try {
    await runCommand(makeCli(), { rawArgs: ['nonexistent'] });
    throw new Error('should have thrown');
  } catch (e) {
    assert.ok(/unknown|not found|invalid/i.test(e.message), `expected unknown error, got: ${e.message}`);
  }
});

process.exit(failures ? 1 : 0);
JS

node verify_check.mjs
