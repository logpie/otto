#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/cpburnz/python-pathspec.git .tmp-clone
cd .tmp-clone
git checkout --quiet 3a94667
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init python-pathspec at v1.0.0 (pre-empty-patterns-fix)"
