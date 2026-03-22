#!/usr/bin/env bash
set -euo pipefail
trap 'rm -f test/verify_parsefilename.test.ts' EXIT
cat > test/verify_parsefilename.test.ts <<'TS'
import { describe, expect, it } from 'vitest'
import { parseFilename } from '../src/index'
describe('parseFilename independent verification', () => {
  it('returns the filename when called without options', () => {
    expect(parseFilename('https://example.com/path/to/file.txt')).toBe('file.txt')
  })
  it('returns the filename for a plain path without options', () => {
    expect(parseFilename('/nested/path/report.pdf')).toBe('report.pdf')
  })
  it('preserves strict mode behavior for extensionless names', () => {
    expect(parseFilename('/nested/path/readme', { strict: true })).toBeUndefined()
  })
})
TS
npx vitest run test/verify_parsefilename.test.ts
