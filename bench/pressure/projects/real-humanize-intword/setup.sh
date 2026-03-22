#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/python-humanize/humanize.git .tmp-clone
cd .tmp-clone
git checkout --quiet fedf432
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init humanize at pre-intword-plural-fix"
