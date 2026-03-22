#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/sindresorhus/camelcase.git .tmp-clone
cd .tmp-clone
git checkout --quiet a077c7bcf5a37630f3733fd7dcd377991377ac32
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
node -e "const fs=require('fs'); const p=JSON.parse(fs.readFileSync('package.json','utf8')); p.scripts.test='ava'; delete p.scripts.pretest; delete p.scripts.posttest; fs.writeFileSync('package.json', JSON.stringify(p, null, 2) + '\n');"
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init camelcase at pre-preserve-uppercase-feature"
