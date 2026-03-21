#!/usr/bin/env bash
set -euo pipefail
# Clone node-semver at commit before prerelease parsing fix (issue #775)
git clone --quiet https://github.com/npm/node-semver.git .tmp-clone
cd .tmp-clone
git checkout --quiet 2677f2a88334b0e728dbfe9ad9f5f57458437c87
cd ..
cp -r .tmp-clone/internal ./internal
cp -r .tmp-clone/classes ./classes
cp -r .tmp-clone/functions ./functions
cp -r .tmp-clone/ranges ./ranges
cp -r .tmp-clone/test ./test
cp .tmp-clone/index.js ./index.js 2>/dev/null || true
cp .tmp-clone/preload.js ./preload.js 2>/dev/null || true
cp .tmp-clone/range.bnf ./range.bnf 2>/dev/null || true
cp .tmp-clone/package.json ./package.json 2>/dev/null || true
rm -rf .tmp-clone
# Fix test script if needed
node -e "let p=require('./package.json'); if(!p.scripts.test) p.scripts.test='tap test/'; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))" 2>/dev/null || true
git add -A && git commit -m "init node-semver at pre-prerelease-fix"
