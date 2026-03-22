#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/developit/mitt.git .tmp-clone
cd .tmp-clone
git checkout --quiet f0f36b09df3eaac16792b6840bdb594301ec3d8f
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init mitt at pre-off-all-handlers-feature"
