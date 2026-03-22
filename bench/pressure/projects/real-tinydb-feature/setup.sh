#!/usr/bin/env bash
set -euo pipefail
# Clone tinydb at commit before "persist empty tables" feature (PR #518)
git clone --quiet https://github.com/msiemens/tinydb.git .tmp-clone
cd .tmp-clone
git checkout --quiet 939441264a9abcd8e3d2e8b36308d18b1742259f
cd ..
# Copy entire repo (minus .git) to preserve all config files
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init tinydb at pre-persist-empty-tables"
