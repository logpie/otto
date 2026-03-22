#!/usr/bin/env bash
set -euo pipefail
# Clone citty at commit before subcommand aliases feature (PR #236)
git clone --quiet https://github.com/unjs/citty.git .tmp-clone
cd .tmp-clone
git checkout --quiet 69252d409b9a93c3fade0a4d207f598d1cdef732
cd ..
# Copy entire repo (minus .git) to preserve all config files
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
# Fix test script: original uses pnpm + lint + type-check. We only need vitest.
node -e "let p=require('./package.json'); p.scripts.test='npx vitest run'; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))"
# Install dependencies (vitest, typescript needed for tests)
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init citty at pre-subcommand-aliases"
