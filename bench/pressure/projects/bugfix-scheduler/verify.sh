#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: bugfix-scheduler (6 bugs)"

[ -d node_modules ] || npm install --silent 2>/dev/null

# Bug 1+2: Heap ordering — parent index and sinkDown comparison
check "Bug1+2: priority queue orders 10+ items correctly" \
  "node -e '
const { PriorityQueue } = require(\"./scheduler\");
const pq = new PriorityQueue();
const priorities = [7, 3, 9, 1, 5, 8, 2, 6, 4, 10, 0];
priorities.forEach((p, i) => pq.enqueue(\"item\" + p, p));
const result = [];
while (pq.size > 0) result.push(pq.dequeue());
// Should come out in ascending priority order
for (let i = 1; i < result.length; i++) {
  const prev = parseInt(result[i-1].replace(\"item\",\"\"));
  const curr = parseInt(result[i].replace(\"item\",\"\"));
  if (prev > curr) { console.error(\"out of order:\", result); process.exit(1); }
}
'"

# Bug 3: setTimeout callback loses 'this' context
check "Bug3: delayed job actually executes (this-binding fixed)" \
  "node -e '
const { Scheduler } = require(\"./scheduler\");
const s = new Scheduler();
let ran = false;
s.addJob(\"delayed\", () => { ran = true; }, { delay: 50 });
// Wait for the delay, then run
setTimeout(async () => {
  await s.run();
  s.stop();
  process.exit(ran ? 0 : 1);
}, 200);
setTimeout(() => process.exit(1), 5000);
'"

# Bug 5: Error serialization — should store err.message not raw Error
check "Bug5: error results have readable message, not empty object" \
  "node -e '
const { Scheduler } = require(\"./scheduler\");
const s = new Scheduler();
s.addJob(\"failing\", () => { throw new Error(\"test error msg\"); }, { priority: 1 });
s.run().then(() => {
  const errResult = s.results.find(r => r.status === \"error\");
  if (!errResult) process.exit(1);
  // The error field should be a string message, not an Error object that serializes to {}
  const serialized = JSON.stringify(errResult);
  if (serialized.includes(\"test error msg\")) process.exit(0);
  // If error is stored as Error object, JSON.stringify makes it {}
  if (serialized.includes(\"{}\")) { console.error(\"Error serialized as {}\"); process.exit(1); }
  process.exit(0);
}).catch(() => process.exit(1));
setTimeout(() => process.exit(1), 5000);
'"

check "scheduler runs jobs in priority order" \
  "node -e '
const { Scheduler } = require(\"./scheduler\");
const s = new Scheduler();
const order = [];
s.addJob(\"low\", () => order.push(\"low\"), { priority: 10 });
s.addJob(\"high\", () => order.push(\"high\"), { priority: 1 });
s.addJob(\"mid\", () => order.push(\"mid\"), { priority: 5 });
s.run(1).then(() => {
  if (order[0] !== \"high\") { console.error(\"expected high first, got\", order); process.exit(1); }
  if (order[2] !== \"low\") { console.error(\"expected low last, got\", order); process.exit(1); }
  process.exit(0);
}).catch(() => process.exit(1));
setTimeout(() => process.exit(1), 5000);
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
