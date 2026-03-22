#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/pre-commit/pre-commit-hooks.git .tmp-clone
cd .tmp-clone
git checkout --quiet f71fa2c
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init pre-commit-hooks at v4.4.0 (pre-empty-sort-fix)"
