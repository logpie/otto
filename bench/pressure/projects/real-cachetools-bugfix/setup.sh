#!/usr/bin/env bash
set -euo pipefail
# Clone cachetools at commit before cache stampede fix (issue #294)
git clone --quiet https://github.com/tkem/cachetools.git .tmp-clone
cd .tmp-clone
git checkout --quiet 3b3167a8744c388ac5652b5dbda48c5cae1449ab
cd ..
cp -r .tmp-clone/src ./src
cp -r .tmp-clone/tests ./tests
cp .tmp-clone/pyproject.toml ./pyproject.toml 2>/dev/null || true
cp .tmp-clone/setup.py ./setup.py 2>/dev/null || true
cp .tmp-clone/setup.cfg ./setup.cfg 2>/dev/null || true
rm -rf .tmp-clone
git add -A && git commit -m "init cachetools at pre-stampede-fix"
