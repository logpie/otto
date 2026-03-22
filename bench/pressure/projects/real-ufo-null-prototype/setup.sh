#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/unjs/ufo.git .tmp-clone
cd .tmp-clone
git checkout --quiet da733f6e149a95ede3f92c9cc5060fc18c976725
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
node -e "const fs=require('fs'); const p=JSON.parse(fs.readFileSync('package.json','utf8')); p.scripts.test='npx vitest run'; delete p.scripts.pretest; delete p.scripts.posttest; fs.writeFileSync('package.json', JSON.stringify(p, null, 2) + '\n');"
npm install --ignore-scripts 2>/dev/null
git add -A && git commit -m "init ufo at pre-null-prototype-parsequery"
