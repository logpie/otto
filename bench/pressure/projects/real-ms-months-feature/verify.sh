#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f src/verify_months.test.ts; exit $rc' EXIT

cat > src/verify_months.test.ts <<'TS'
import { describe, expect, it } from '@jest/globals';
import { ms } from './index';

describe('month support verification', () => {
  it('parses short month unit (1mo)', () => {
    const result = ms('1mo' as any);
    expect(typeof result).toBe('number');
    expect(result).toBeGreaterThan(2500000000);
    expect(result).toBeLessThan(2700000000);
  });

  it('parses long month unit (2 months)', () => {
    const result = ms('2 months' as any);
    expect(typeof result).toBe('number');
    expect(result).toBeGreaterThan(5000000000);
    expect(result).toBeLessThan(5400000000);
  });

  it('formats month-sized values back to month unit', () => {
    const monthMs = ms('1mo' as any);
    const formatted = ms(monthMs);
    expect(typeof formatted).toBe('string');
    expect(formatted).toMatch(/mo|month/i);
  });

  it('existing units still work', () => {
    expect(ms('1d')).toBe(86400000);
    expect(ms('1h')).toBe(3600000);
  });
});
TS

npx jest src/verify_months.test.ts --env node --forceExit --no-coverage 2>&1
exit $?
