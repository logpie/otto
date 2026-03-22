#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/vercel/ms.git .tmp-clone
cd .tmp-clone
git checkout --quiet 0d5ab182ef22686cb4086fe9b67b1276bcb644ef
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
# Override test to just run jest with Node env (skip edge runtime, pnpm wrappers)
node -e "const fs=require('fs'); const p=JSON.parse(fs.readFileSync('package.json','utf8')); p.scripts.test='npx jest --env node --forceExit'; delete p.scripts.pretest; delete p.scripts.posttest; fs.writeFileSync('package.json', JSON.stringify(p, null, 2) + '\n');"
npm install 2>/dev/null
git add -A && git commit -m "init ms at pre-month-support"
