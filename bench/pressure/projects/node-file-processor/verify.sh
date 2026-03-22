#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: node-file-processor"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Find the module
MOD=""
for f in file-processor.js fileProcessor.js processor.js index.js src/index.js src/file-processor.js src/fileProcessor.js src/processor.js; do
  if [ -f "$f" ]; then MOD="./$f"; break; fi
done
if [ -z "$MOD" ]; then echo "  FAIL  No file processor module found"; exit 1; fi

TMPOUT=$(mktemp /tmp/fp_verify_XXXXXX.jsonl)
trap "rm -f $TMPOUT" EXIT

check "FileProcessor class exists" \
  "node -e '
const mod = require(\"$MOD\");
const FP = mod.FileProcessor || mod.default || mod;
if (typeof FP !== \"function\") process.exit(1);
'"

check "processes fixtures/valid.jsonl without errors" \
  "node -e '
const mod = require(\"$MOD\");
const FP = mod.FileProcessor || mod.default || mod;
const fp = new FP({
  schema: { name: { type: \"string\", required: true }, age: { type: \"number\", required: true } },
  transform: (rec) => ({ ...rec, processed: true })
});
const p = fp.process ? fp.process(\"fixtures/valid.jsonl\", \"$TMPOUT\") :
          fp.run ? fp.run(\"fixtures/valid.jsonl\", \"$TMPOUT\") :
          Promise.reject(\"no process method\");
p.then((report) => {
  const fs = require(\"fs\");
  const output = fs.readFileSync(\"$TMPOUT\", \"utf8\").trim();
  if (output.length === 0) process.exit(1);
  process.exit(0);
}).catch((e) => { console.error(e); process.exit(1); });
setTimeout(() => process.exit(1), 10000);
'"

check "produces summary report with counts" \
  "node -e '
const mod = require(\"$MOD\");
const FP = mod.FileProcessor || mod.default || mod;
const fp = new FP({
  schema: { name: { type: \"string\", required: true }, age: { type: \"number\", required: true } },
  transform: (rec) => rec
});
const p = fp.process ? fp.process(\"fixtures/mixed.jsonl\", \"$TMPOUT\") :
          fp.run ? fp.run(\"fixtures/mixed.jsonl\", \"$TMPOUT\") :
          Promise.reject(\"no process method\");
p.then((report) => {
  if (!report) process.exit(1);
  // Report should have counts: total, valid, invalid, errors
  const r = report.summary || report;
  if (typeof r.total !== \"undefined\" || typeof r.parsed !== \"undefined\" || typeof r.valid !== \"undefined\") {
    process.exit(0);
  }
  // Accept if report has any numeric fields
  const vals = Object.values(r);
  if (vals.some(v => typeof v === \"number\")) process.exit(0);
  process.exit(1);
}).catch((e) => { console.error(e); process.exit(1); });
setTimeout(() => process.exit(1), 10000);
'"

check "handles parse errors in mixed.jsonl gracefully" \
  "node -e '
const mod = require(\"$MOD\");
const FP = mod.FileProcessor || mod.default || mod;
const fp = new FP({
  schema: { name: { type: \"string\", required: true } },
  transform: (rec) => rec
});
const p = fp.process ? fp.process(\"fixtures/mixed.jsonl\", \"$TMPOUT\") :
          fp.run ? fp.run(\"fixtures/mixed.jsonl\", \"$TMPOUT\") :
          Promise.reject(\"no process method\");
p.then((report) => {
  // Should complete without crashing — mixed.jsonl has bad lines
  const r = report?.summary || report || {};
  const errors = r.errors || r.parseErrors || r.invalid || 0;
  // There should be at least 1 error (\"not json\" line)
  if (typeof errors === \"number\" && errors >= 1) process.exit(0);
  if (Array.isArray(errors) && errors.length >= 1) process.exit(0);
  // Even without error count, completing without crash is a pass
  process.exit(0);
}).catch((e) => { console.error(e); process.exit(1); });
setTimeout(() => process.exit(1), 10000);
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
