#!/usr/bin/env bash
set -euo pipefail
npm init -y
# Fix default test script that exits 1
node -e "let p=require('./package.json'); p.scripts.test='npx jest --detectOpenHandles --forceExit'; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))"
git add -A && git commit -m "init"
