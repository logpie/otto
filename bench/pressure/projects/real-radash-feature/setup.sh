#!/usr/bin/env bash
set -euo pipefail
# Clone radash at commit before inRange feature (PR #255)
git clone --quiet https://github.com/sodiray/radash.git .tmp-clone
cd .tmp-clone
git checkout --quiet 32a3de441af5c1511f6e5cf3c9e6cd56b671c4ec
cd ..
# Copy entire repo (minus .git) to preserve all config files
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
# Override test script: exclude async tests (env-dependent timer failures)
node -e "let p=require('./package.json'); p.scripts.test='jest --forceExit --testPathIgnorePatterns=async'; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))"
# Install dependencies
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init radash at pre-inRange"
