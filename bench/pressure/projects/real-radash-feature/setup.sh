#!/usr/bin/env bash
set -euo pipefail
# Clone radash at commit before inRange feature (PR #255)
git clone --quiet https://github.com/sodiray/radash.git .tmp-clone
cd .tmp-clone
git checkout --quiet 32a3de441af5c1511f6e5cf3c9e6cd56b671c4ec
cd ..
cp -r .tmp-clone/src ./src
cp -r .tmp-clone/tests ./tests 2>/dev/null || true
cp .tmp-clone/package.json ./package.json
cp .tmp-clone/tsconfig.json ./tsconfig.json 2>/dev/null || true
cp .tmp-clone/jest.config.ts ./jest.config.ts 2>/dev/null || true
rm -rf .tmp-clone
git add -A && git commit -m "init radash at pre-inRange"
