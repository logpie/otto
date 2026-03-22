#!/usr/bin/env bash
set -uo pipefail
trap 'rc=$?; rm -f test/verify_filterquery.test.ts; exit $rc' EXIT
cat > test/verify_filterquery.test.ts <<'TS'
import { describe, expect, it } from 'vitest'
import { filterQuery } from '../src/index'
describe('filterQuery independent verification', () => {
  it('filters a single query parameter', () => {
    expect(filterQuery('/foo?bar=1&baz=2', key => key !== 'bar')).toBe('/foo?baz=2')
  })
  it('preserves repeated keys that match', () => {
    expect(filterQuery('/foo?tag=a&tag=b&skip=1', key => key !== 'skip')).toBe('/foo?tag=a&tag=b')
  })
  it('preserves fragments', () => {
    expect(filterQuery('/foo?bar=1&baz=2#frag', key => key === 'baz')).toBe('/foo?baz=2#frag')
  })
})
TS
npx vitest run test/verify_filterquery.test.ts
