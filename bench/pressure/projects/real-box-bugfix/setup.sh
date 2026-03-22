#!/usr/bin/env bash
set -euo pipefail
# Clone python-box at v7.3.1 (buggy version — box_dots get() regression, issue #288)
git clone --quiet https://github.com/cdgriffith/Box.git .tmp-clone
cd .tmp-clone
git checkout --quiet 91cc956aa2d480202aebb21cda01e19d351624b5
cd ..
# Copy entire repo (minus .git) to preserve all config files
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
git add -A && git commit -m "init python-box at v7.3.1 (buggy)"
