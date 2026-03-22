#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.mjs; exit $rc' EXIT

cat > verify_check.mjs <<'JS'
import stringWidth from './index.js';

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

function assertEqual(actual, expected, msg) {
    if (actual !== expected) {
        throw new Error(msg + ": expected " + expected + ", got " + actual);
    }
}

report("Unqualified keycap '#\\u20E3' has width 2", function() {
    assertEqual(stringWidth('#\u20E3'), 2, "keycap #");
});

report("Unqualified keycap '0\\u20E3' has width 2", function() {
    assertEqual(stringWidth('0\u20E3'), 2, "keycap 0");
});

report("Minimally-qualified heart on fire has width 2", function() {
    // ❤‍🔥 without VS16: U+2764 U+200D U+1F525
    assertEqual(stringWidth('\u2764\u200D\u{1F525}'), 2, "heart on fire MQ");
});

report("Minimally-qualified rainbow flag has width 2", function() {
    // 🏳‍🌈 without VS16: U+1F3F3 U+200D U+1F308
    assertEqual(stringWidth('\u{1F3F3}\u200D\u{1F308}'), 2, "rainbow flag MQ");
});

report("Regular ASCII string has correct width", function() {
    assertEqual(stringWidth('hello'), 5, "ASCII string");
});

process.exit(failures ? 1 : 0);
JS

node verify_check.mjs
