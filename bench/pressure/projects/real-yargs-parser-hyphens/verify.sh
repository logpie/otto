#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.mjs; exit $rc' EXIT

cat > verify_check.mjs <<'JS'
import fs from 'fs';

// Try loading the parser from various locations
let parser;
try {
    const m = await import('./build/lib/index.js');
    parser = m.default || m;
} catch (e1) {
    try {
        const m = await import('./build/index.cjs');
        parser = m.default || m;
    } catch (e2) {
        // Try compiling first
        console.log("FAIL Could not load yargs-parser: " + e1.message);
        process.exit(1);
    }
}

let failures = 0;

function report(name, fn) {
    try {
        fn();
        console.log("PASS " + name);
    } catch (e) {
        failures++;
        console.log("FAIL " + name + ": " + e.message);
    }
}

report("option ending with triple hyphens is parsed as option", function() {
    const parsed = parser(['--foo---']);
    // After fix: --foo--- should be parsed as an option (not a positional)
    if (parsed._.includes('--foo---')) {
        throw new Error("--foo--- was treated as positional instead of being parsed as option");
    }
    // It should appear as some key in the parsed result
    const hasKey = parsed['foo---'] !== undefined || parsed['foo'] !== undefined;
    if (!hasKey) {
        throw new Error("--foo--- was not parsed as any option key");
    }
});

report("triple hyphens alone (---) still treated as positional", function() {
    const parsed = parser(['---']);
    if (!parsed._.includes('---')) {
        throw new Error("--- should be treated as positional, got: " + JSON.stringify(parsed));
    }
});

report("quadruple hyphens alone (----) still treated as positional", function() {
    const parsed = parser(['----']);
    if (!parsed._.includes('----')) {
        throw new Error("---- should be treated as positional, got: " + JSON.stringify(parsed));
    }
});

report("normal double-dash options still work", function() {
    const parsed = parser(['--foo', 'bar']);
    if (parsed.foo !== 'bar') {
        throw new Error("Expected --foo=bar, got: " + JSON.stringify(parsed));
    }
});

report("source code has anchored regex", function() {
    const source = fs.readFileSync('lib/yargs-parser.ts', 'utf8');
    // The fix: regex should be /^---+(=|$)/ (anchored) not /---+(=|$)/ (unanchored)
    if (source.includes('/---+(=|$)/') && !source.includes('/^---+(=|$)/')) {
        throw new Error("Regex /---+(=|$)/ is not anchored with ^");
    }
});

process.exit(failures ? 1 : 0);
JS

# Build TypeScript if build is stale or missing
if [ ! -f build/lib/index.js ]; then
    npx tsc -p tsconfig.test.json 2>/dev/null || true
fi

node verify_check.mjs
