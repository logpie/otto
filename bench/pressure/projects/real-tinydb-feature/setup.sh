#!/usr/bin/env bash
set -euo pipefail
# Clone tinydb at commit before "persist empty tables" feature (PR #518)
git clone --quiet https://github.com/msiemens/tinydb.git .tmp-clone
cd .tmp-clone
git checkout --quiet 939441264a9abcd8e3d2e8b36308d18b1742259f
cd ..
# Copy source into working repo (not the .git — we want our own repo)
cp -r .tmp-clone/tinydb ./tinydb
cp -r .tmp-clone/tests ./tests
cp .tmp-clone/pyproject.toml ./pyproject.toml 2>/dev/null || true
cp .tmp-clone/setup.py ./setup.py 2>/dev/null || true
cp .tmp-clone/setup.cfg ./setup.cfg 2>/dev/null || true
rm -rf .tmp-clone
git add -A && git commit -m "init tinydb at pre-persist-empty-tables"
