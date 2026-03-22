#!/usr/bin/env bash
set -euo pipefail

# Radash is TypeScript — use the project's jest+ts-jest to verify.
# This avoids needing a build step and tests the source directly.
trap 'rm -f src/tests/verify_inrange.test.ts' EXIT

cat > src/tests/verify_inrange.test.ts <<'TS'
// Independent verification of inRange feature
// Tests behavior, not implementation details

let inRange: any;

beforeAll(() => {
    // Try multiple import paths — otto may organize differently
    try { inRange = require('../number').inRange; } catch {}
    if (!inRange) try { inRange = require('../index').inRange; } catch {}
    if (!inRange) try { inRange = require('../../src/number').inRange; } catch {}
});

describe('inRange independent verification', () => {
    test('function exists and is exported', () => {
        expect(typeof inRange).toBe('function');
    });

    test('basic range check: 3 is in [1, 5)', () => {
        expect(inRange(3, 1, 5)).toBe(true);
    });

    test('outside range: 6 is not in [1, 5)', () => {
        expect(inRange(6, 1, 5)).toBe(false);
    });

    test('start is inclusive: 1 is in [1, 5)', () => {
        expect(inRange(1, 1, 5)).toBe(true);
    });

    test('end is exclusive: 5 is not in [1, 5)', () => {
        expect(inRange(5, 1, 5)).toBe(false);
    });

    test('two-arg form: inRange(3, 5) means [0, 5)', () => {
        expect(inRange(3, 5)).toBe(true);
        expect(inRange(5, 5)).toBe(false);
        expect(inRange(-1, 5)).toBe(false);
    });

    test('non-number inputs return false', () => {
        expect(inRange(null as any, 1, 5)).toBe(false);
        expect(inRange(undefined as any, 1, 5)).toBe(false);
        expect(inRange(NaN, 1, 5)).toBe(false);
    });

    test('negative ranges work', () => {
        expect(inRange(-3, -5, -1)).toBe(true);
        expect(inRange(0, -5, -1)).toBe(false);
    });

    test('zero edge cases', () => {
        expect(inRange(0, 0, 1)).toBe(true);
        expect(inRange(0, 1)).toBe(true);  // [0, 1)
    });
});
TS

# Run with project's jest (has ts-jest configured)
npx jest src/tests/verify_inrange.test.ts --forceExit --no-coverage 2>&1
exit_code=$?

# Print summary
if [ $exit_code -eq 0 ]; then
    echo "PASS all inRange checks verified"
else
    echo "FAIL inRange verification failed"
fi
exit $exit_code
