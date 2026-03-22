#!/usr/bin/env bash
set -uo pipefail
trap 'rc=$?; rm -f test/verify_parsequery_nullproto.test.ts; exit $rc' EXIT
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
  it('prototype-polluting keys do not affect the prototype chain', () => {
    const q = parseQuery('?constructor=y&toString=z')
    expect(Object.getPrototypeOf(q)).toBe(null)
    // On a null-prototype object, constructor is a regular data property
    expect(q.constructor).toBe('y')
  })
})
TS
npx vitest run test/verify_parsequery_nullproto.test.ts
