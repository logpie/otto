#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/sindresorhus/string-width.git .tmp-clone
cd .tmp-clone
git checkout --quiet d3bd086
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
npm install --ignore-scripts 2>/dev/null || true
git add -A && git commit -m "init string-width at v8.1.1 (pre-emoji-fix)"
