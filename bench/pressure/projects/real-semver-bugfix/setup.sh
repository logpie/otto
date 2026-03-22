#!/usr/bin/env bash
set -euo pipefail
# Clone node-semver at commit before prerelease parsing fix (issue #775)
git clone --quiet https://github.com/npm/node-semver.git .tmp-clone
cd .tmp-clone
git checkout --quiet 2677f2a88334b0e728dbfe9ad9f5f57458437c87
cd ..
# Copy entire repo (minus .git) to preserve all config files
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
# Override test script: original runs lint+template checks that need CI setup.
# We only need the actual test runner (tap). Also remove pre/posttest hooks.
node -e "let p=require('./package.json'); p.scripts.test='tap'; delete p.scripts.pretest; delete p.scripts.posttest; delete p.scripts.prepublishOnly; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))"
# Install dependencies (tap test runner)
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init node-semver at pre-prerelease-fix"
