#!/usr/bin/env bash
set -euo pipefail
npm init -y
node -e "let p=require('./package.json'); p.scripts.test='npx jest --detectOpenHandles --forceExit'; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))"
# Create sample files for processing
mkdir -p fixtures
echo '{"name":"Alice","age":30,"email":"alice@example.com"}' > fixtures/valid.jsonl
echo '{"name":"Bob","age":25}' >> fixtures/valid.jsonl
echo 'not json' > fixtures/mixed.jsonl
echo '{"name":"Carol","age":28}' >> fixtures/mixed.jsonl
echo '' >> fixtures/mixed.jsonl
echo '{"name":"Dave","age":-1}' >> fixtures/mixed.jsonl
git add -A && git commit -m "init with fixtures"
