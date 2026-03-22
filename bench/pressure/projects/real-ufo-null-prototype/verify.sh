#!/usr/bin/env bash
set -uo pipefail
trap 'rc=$?; rm -f test/verify_parsequery_nullproto.test.ts; exit $rc' EXIT
cat > test/verify_parsequery_nullproto.test.ts <<'TS'
import { describe, expect, it } from 'vitest'
import { parseQuery } from '../src/index'
describe('parseQuery null-prototype verification', () => {
  it('returned object does not inherit from Object.prototype', () => {
    const q = parseQuery('?foo=bar')
    expect('hasOwnProperty' in q).toBe(false)
    expect('toString' in q).toBe(false)
  })
  it('still parses ordinary query keys', () => {
    const q = parseQuery('?foo=bar&baz=qux')
    expect(q.foo).toBe('bar')
    expect(q.baz).toBe('qux')
  })
  it('retains array behavior for repeated keys', () => {
    const q = parseQuery('?tag=a&tag=b')
    expect(q.tag).toEqual(['a', 'b'])
  })
})
TS
npx vitest run test/verify_parsequery_nullproto.test.ts
