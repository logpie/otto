#!/usr/bin/env bash
set -euo pipefail
trap 'rm -f test/verify_parsequery_nullproto.test.ts' EXIT
cat > test/verify_parsequery_nullproto.test.ts <<'TS'
import { describe, expect, it } from 'vitest'
import { parseQuery } from '../src/index'
describe('parseQuery null-prototype verification', () => {
  it('returns an object with null prototype', () => {
    expect(Object.getPrototypeOf(parseQuery('?foo=bar'))).toBe(null)
  })
  it('still parses ordinary query keys', () => {
    const q = parseQuery('?foo=bar&baz=qux')
    expect(q.foo).toBe('bar')
    expect(q.baz).toBe('qux')
  })
  it('handles prototype-looking keys as plain data', () => {
    const q = parseQuery('?__proto__=x&constructor=y')
    expect(Object.getPrototypeOf(q)).toBe(null)
    expect(q['__proto__']).toBe('x')
    expect(q.constructor).toBe('y')
  })
})
TS
npx vitest run test/verify_parsequery_nullproto.test.ts
