#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: node-task-queue"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Find the module
MOD=""
for f in task-queue.js taskQueue.js task_queue.js queue.js index.js src/index.js src/task-queue.js src/taskQueue.js; do
  if [ -f "$f" ]; then MOD="./$f"; break; fi
done
if [ -z "$MOD" ]; then echo "  FAIL  No task queue module found"; exit 1; fi

check "TaskQueue class exists and can be instantiated" \
  "node -e '
const mod = require(\"$MOD\");
const TQ = mod.TaskQueue || mod.default || mod;
const q = new TQ();
if (typeof q.enqueue !== \"function\") process.exit(1);
'"

check "priority ordering — high priority tasks dequeued first" \
  "node -e '
const mod = require(\"$MOD\");
const TQ = mod.TaskQueue || mod.default || mod;
const q = new TQ();
const order = [];
q.enqueue(() => { order.push(\"low\"); }, { priority: \"low\" });
q.enqueue(() => { order.push(\"high\"); }, { priority: \"high\" });
q.enqueue(() => { order.push(\"medium\"); }, { priority: \"medium\" });
q.process(1).then(() => q.drain ? q.drain() : null).then(() => {
  // high should come before low
  if (order.indexOf(\"high\") >= order.indexOf(\"low\")) process.exit(1);
  process.exit(0);
}).catch(() => process.exit(1));
setTimeout(() => process.exit(1), 5000);
'"

check "concurrency limit is respected" \
  "node -e '
const mod = require(\"$MOD\");
const TQ = mod.TaskQueue || mod.default || mod;
const q = new TQ();
let concurrent = 0;
let maxConcurrent = 0;
const LIMIT = 2;
for (let i = 0; i < 6; i++) {
  q.enqueue(() => new Promise(resolve => {
    concurrent++;
    if (concurrent > maxConcurrent) maxConcurrent = concurrent;
    setTimeout(() => { concurrent--; resolve(); }, 50);
  }), { priority: \"medium\" });
}
q.process(LIMIT).then(() => q.drain ? q.drain() : null).then(() => {
  if (maxConcurrent > LIMIT) {
    console.error(\"max concurrent was \" + maxConcurrent + \" > limit \" + LIMIT);
    process.exit(1);
  }
  process.exit(0);
}).catch(() => process.exit(1));
setTimeout(() => process.exit(1), 10000);
'"

check "failed tasks go to dead letter queue after retries exhausted" \
  "node -e '
const mod = require(\"$MOD\");
const TQ = mod.TaskQueue || mod.default || mod;
const q = new TQ();
let attempts = 0;
q.enqueue(() => { attempts++; throw new Error(\"fail\"); }, { priority: \"high\", maxRetries: 1 });
q.process(1).then(() => q.drain ? q.drain() : null).then(() => {
  const dlq = typeof q.getDLQ === \"function\" ? q.getDLQ() : (q.dlq || q.deadLetterQueue || []);
  if (dlq.length === 0) { console.error(\"DLQ empty\"); process.exit(1); }
  process.exit(0);
}).catch(() => process.exit(1));
setTimeout(() => process.exit(1), 10000);
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
