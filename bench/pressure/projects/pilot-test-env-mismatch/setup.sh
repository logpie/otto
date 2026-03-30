#!/usr/bin/env bash
set -euo pipefail

# Node.js project that uses vitest (NOT jest)
cat > package.json << 'EOF'
{
  "name": "math-utils",
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "test": "vitest run"
  },
  "devDependencies": {
    "vitest": "^1.6.0"
  }
}
EOF

cat > vitest.config.js << 'EOF'
import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: { globals: true }
});
EOF

cat > math.js << 'EOF'
/**
 * Math utility functions.
 */
export function add(a, b) {
  return a + b;
}

export function subtract(a, b) {
  return a - b;
}
EOF

cat > math.test.js << 'EOF'
import { describe, it, expect } from 'vitest';
import { add, subtract } from './math.js';

describe('math', () => {
  it('adds numbers', () => {
    expect(add(2, 3)).toBe(5);
  });
  it('subtracts numbers', () => {
    expect(subtract(5, 3)).toBe(2);
  });
});
EOF

npm install --silent 2>/dev/null
git add -A && git commit -m "init math-utils with vitest"
