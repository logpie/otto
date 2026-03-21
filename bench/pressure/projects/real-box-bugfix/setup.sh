#!/usr/bin/env bash
set -euo pipefail
# Clone python-box at v7.3.1 (buggy version — box_dots get() regression, issue #288)
git clone --quiet https://github.com/cdgriffith/Box.git .tmp-clone
cd .tmp-clone
git checkout --quiet 91cc956aa2d480202aebb21cda01e19d351624b5
cd ..
cp -r .tmp-clone/box ./box
cp -r .tmp-clone/test ./test
cp .tmp-clone/pyproject.toml ./pyproject.toml 2>/dev/null || true
cp .tmp-clone/setup.py ./setup.py 2>/dev/null || true
cp .tmp-clone/setup.cfg ./setup.cfg 2>/dev/null || true
rm -rf .tmp-clone
git add -A && git commit -m "init python-box at v7.3.1 (buggy)"
