#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/pytest-dev/iniconfig.git .tmp-clone
cd .tmp-clone
git checkout --quiet 57b7ed9
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init iniconfig at pre-parse-classmethod"
