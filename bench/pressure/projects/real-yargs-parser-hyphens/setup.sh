#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/yargs/yargs-parser.git .tmp-clone
cd .tmp-clone
git checkout --quiet a030551
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
npm install --ignore-scripts 2>/dev/null || true
# Build the TypeScript sources to JavaScript (rollup CJS bundle may fail on newer Node)
npx tsc -p tsconfig.test.json 2>/dev/null || true
git add -A && git commit -m "init yargs-parser at pre-hyphens-fix"
