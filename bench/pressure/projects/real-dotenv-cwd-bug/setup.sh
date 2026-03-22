#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/theskumar/python-dotenv.git .tmp-clone
cd .tmp-clone
git checkout --quiet 5317a56
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init python-dotenv at v0.21.1 (pre-cwd-fix)"
