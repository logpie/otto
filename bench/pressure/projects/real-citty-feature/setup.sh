#!/usr/bin/env bash
set -euo pipefail
# Clone citty at commit before subcommand aliases feature (PR #236)
git clone --quiet https://github.com/unjs/citty.git .tmp-clone
cd .tmp-clone
git checkout --quiet 69252d409b9a93c3fade0a4d207f598d1cdef732
cd ..
cp -r .tmp-clone/src ./src
cp -r .tmp-clone/test ./test 2>/dev/null || true
cp .tmp-clone/package.json ./package.json
cp .tmp-clone/tsconfig.json ./tsconfig.json 2>/dev/null || true
cp .tmp-clone/vitest.config.ts ./vitest.config.ts 2>/dev/null || true
rm -rf .tmp-clone
git add -A && git commit -m "init citty at pre-subcommand-aliases"
