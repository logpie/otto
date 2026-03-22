#!/usr/bin/env bash
set -euo pipefail
# Clone cachetools at commit before cache stampede fix (issue #294)
git clone --quiet https://github.com/tkem/cachetools.git .tmp-clone
cd .tmp-clone
git checkout --quiet 3b3167a8744c388ac5652b5dbda48c5cae1449ab
cd ..
# Copy entire repo (minus .git) to preserve all config files
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init cachetools at pre-stampede-fix"
