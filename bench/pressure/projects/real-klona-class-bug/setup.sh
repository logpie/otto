#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/lukeed/klona.git .tmp-clone
cd .tmp-clone
git checkout --quiet 28565c3efae17b3c72c5086602437841cb1435b8
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init klona at pre-class-object-assign-fix"
