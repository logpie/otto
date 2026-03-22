#!/usr/bin/env bash
set -euo pipefail

cat > scheduler.js << 'JSEOF'
/**
 * Job scheduler with priority queue and cron-like scheduling.
 */
class PriorityQueue {
  constructor() {
    this.heap = [];
  }

  enqueue(item, priority) {
    this.heap.push({ item, priority });
    this._bubbleUp(this.heap.length - 1);
  }

  dequeue() {
    if (this.heap.length === 0) return null;
    const top = this.heap[0];
    const last = this.heap.pop();
    if (this.heap.length > 0) {
      this.heap[0] = last;
      this._sinkDown(0);
    }
    return top.item;
  }

  _bubbleUp(idx) {
    while (idx > 0) {
      const parentIdx = Math.floor(idx / 2);
      if (this.heap[parentIdx].priority <= this.heap[idx].priority) break;
      [this.heap[parentIdx], this.heap[idx]] = [this.heap[idx], this.heap[parentIdx]];
      idx = parentIdx;
    }
  }

  _sinkDown(idx) {
    const length = this.heap.length;
    while (true) {
      let smallest = idx;
      const left = 2 * idx + 1;
      const right = 2 * idx + 2;
      if (left < length && this.heap[left].priority > this.heap[smallest].priority) {
        smallest = left;
      }
      if (right < length && this.heap[right].priority > this.heap[smallest].priority) {
        smallest = right;
      }
      if (smallest === idx) break;
      [this.heap[smallest], this.heap[idx]] = [this.heap[idx], this.heap[smallest]];
      idx = smallest;
    }
  }

  get size() {
    return this.heap.length;
  }
}

class Scheduler {
  constructor() {
    this.queue = new PriorityQueue();
    this.running = false;
    this.results = [];
    this.timers = [];
  }

  addJob(name, fn, { priority = 5, delay = 0, interval = null } = {}) {
    const job = { name, fn, priority, delay, interval, addedAt: Date.now() };
    if (delay > 0) {
      const timer = setTimeout(function() {
        this.queue.enqueue(job, priority);
      }, delay);
      this.timers.push(timer);
    } else {
      this.queue.enqueue(job, priority);
    }
    return job;
  }

  async run(concurrency = 1) {
    this.running = true;
    const workers = [];
    for (let i = 0; i < concurrency; i++) {
      workers.push(this._worker(i));
    }
    await Promise.all(workers);
    return this.results;
  }

  async _worker(id) {
    while (this.running) {
      const job = this.queue.dequeue();
      if (!job) {
        break;
      }
      try {
        const result = await job.fn();
        this.results.push({ name: job.name, status: 'ok', result });
      } catch (err) {
        this.results.push({ name: job.name, status: 'error', error: err });
      }
      // Re-enqueue if interval job
      if (job.interval) {
        this.queue.enqueue(job, job.priority);
      }
    }
  }

  stop() {
    this.running = false;
    this.timers.forEach(t => clearTimeout(t));
  }
}

module.exports = { PriorityQueue, Scheduler };
JSEOF

cat > scheduler.test.js << 'JSEOF'
const { PriorityQueue, Scheduler } = require('./scheduler');

test('priority queue basic operations', () => {
  const pq = new PriorityQueue();
  pq.enqueue('low', 10);
  pq.enqueue('high', 1);
  pq.enqueue('medium', 5);
  expect(pq.dequeue()).toBe('high');
  expect(pq.dequeue()).toBe('medium');
  expect(pq.dequeue()).toBe('low');
});

test('scheduler runs jobs', async () => {
  const s = new Scheduler();
  const order = [];
  s.addJob('a', () => order.push('a'), { priority: 2 });
  s.addJob('b', () => order.push('b'), { priority: 1 });
  await s.run();
  expect(order).toContain('a');
  expect(order).toContain('b');
});
JSEOF

npm init -y
node -e "let p=require('./package.json'); p.scripts.test='npx jest --detectOpenHandles --forceExit'; require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))"
git add -A && git commit -m "init scheduler"
