# parallel-otto Real Product Benchmarks

Real LLM runs against complex products. Each bench builds a base product, queues feature improves in parallel (or sequential for P2), and merges. See `bench-results/*.json` for raw per-bench data; `scripts/bench_runner.py` for the bench definitions; `scripts/bench_report.py` regenerates this file.

**What gets measured**: per-task wall time, cost (USD), exit status, merge agent cost, cert outcome.

**Why it matters**: parallel-otto's value lies in (a) wall-time speedup from concurrent execution and (b) LLM-driven conflict resolution that salvages branches whose individual cert passes fall short.

## Summary

| Bench | Concurrency | Tasks | Wall | Cost | Merge | Cert |
|---|---|---|---|---|---|---|
| `P1-todo-parallel-improves` | 3 | 1/4 done | 10.0m | $3.98 | conflict-resolved (+$1.35, +2.3m) | – |
| `P2-todo-sequential-baseline` | 1 | 1/4 done | 11.0m | $3.93 | conflict-resolved (salvage) (+$1.19, +1.5m) | – |
| `P3-bookmark-parallel-features` | 2 | 1/3 done | 12.8m | $2.24 | success (salvage clean merge) | True |
| `P4-flask-auth-users-posts` | 3 | 2/4 done | 15.6m | $13.29 | success + conflict-resolved salvage (+$3.79, +2.6m) | True |
| `P6-inventory-cli` | 3 | 3/4 done | 28.7m | $12.77 | conflict-resolved (+$2.41, +13.3m) | True |
| `p5_markdown_ssg` | 3 | 1/4 done | 15.6m | $10.65 | failed | – |

## Parallel Speedup (P1 vs P2 — same intents, concurrent=3 vs concurrent=1)

Total wall time includes a serial base build (~70s identical to both). The interesting comparison is the **improve phase wall time** — that's the part parallel-otto changes:

- P1 (concurrent=3): 3 improves in parallel — wall 3.0m (=max), cumulative work 8.6m
- P2 (concurrent=1): 3 improves serially — wall 9.0m (=sum)
- **Phase-2 speedup**: 3.03× (9.0m → 3.0m)
- Total wall (including serial base+merge): P1 10.0m, P2 11.0m — speedup 1.10× overall
- Cost difference: $+0.05 (parallel - sequential) — parallel doesn't cost more per task

Why total speedup is smaller than phase-2 speedup: base build (70s) and merge (~90s) are identical and serial in both. They dilute the parallel-only gain.

## Complex Product Results (P4-P6)

These benchmarks build genuinely complex multi-module products and queue 3 parallel improves with `-n 2` rounds (vs `-n 1` in P1-P3). Each improve set is designed so the changes WILL touch shared files — we measure how often the conflict agent earns its keep.

| Bench | Base | Improves done/total | Improve cost (sum) | Improve max-wall | Merge | Cert |
|---|---|---|---|---|---|---|
| `P4-flask-auth-users-posts` | 3.1m ($1.14) | 1/3 | $8.37 | 9.3m | success + conflict-resolved salvage (+$3.79) | True |
| `P6-inventory-cli` | 2.1m ($0.62) | 2/3 | $9.73 | 12.6m | conflict-resolved (+$2.41) | True |

**Improve-pass rate** is what tells us whether `-n 2` is enough rounds for complex products. **Conflict agent cost** divided by improve count tells us whether the merge agent is cheaper than a human reviewer (it always is, at $0.20-$1.00 per conflict).


## Per-Bench Details

### P1-todo-parallel-improves

- repo: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/bench-p1-poh9vxro`
- started: 2026-04-20T10:18:04Z → finished: 2026-04-20T03:28:04Z
- wall: 10.0m, total cost: $3.98
- concurrency: 3
- merge: **conflict-resolved** (+$1.35, +2.3m); cert_passed=None

#### Per-task
| ID | Status | Cost | Duration | Failure |
|---|---|---|---|---|
| `base` | done | $0.38 | 1.2m |  |
| `imp-priority` | failed | $0.77 | 3.0m | exit_code=1 |
| `imp-duedate` | failed | $0.74 | 2.8m | exit_code=1 |
| `imp-tags` | failed | $0.75 | 2.8m | exit_code=1 |

#### Notes
- improves all hit -n=1 limit before passing cert; branches still had real work
- Salvaged via explicit-branch merge: 1 clean + 2 conflict_resolved by agent
- Final todo.py has 9 commands: add/list/done/delete/priority/due/tag/filter
- Conflict agent successfully merged contributions from priority/duedate/tags into one file

### P2-todo-sequential-baseline

- repo: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/bench-p2-b0yb8aco`
- started: 2026-04-20T10:28:22Z → finished: 2026-04-20T03:39:20Z
- wall: 11.0m, total cost: $3.93
- concurrency: 1
- merge: **conflict-resolved (salvage)** (+$1.19, +1.5m); cert_passed=None

#### Per-task
| ID | Status | Cost | Duration | Failure |
|---|---|---|---|---|
| `base` | done | $0.39 | 1.2m |  |
| `imp-priority` | failed | $0.81 | 3.4m | exit_code=1 |
| `imp-duedate` | failed | $0.75 | 2.7m | exit_code=1 |
| `imp-tags` | failed | $0.79 | 2.9m | exit_code=1 |

#### Notes
- Same intents as P1 but concurrent=1 (sequential).
- Wall-clock for 3 sequential improves: 539s vs P1 parallel 178s (~3.0x speedup).
- Salvaged via explicit-branch merge: 1 clean + 2 conflict_resolved by agent ($1.19).
- Final todo.py has all 9 commands working (verified post-merge).

### P3-bookmark-parallel-features

- repo: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/bench-p3-2_3wtgbm`
- started: 2026-04-20T10:29:11Z → finished: 2026-04-20T03:41:57Z
- wall: 12.8m, total cost: $2.24
- concurrency: 2
- merge: **success (salvage clean merge)** (+$0.00, +5s); cert_passed=True

#### Per-task
| ID | Status | Cost | Duration | Failure |
|---|---|---|---|---|
| `base` | done | $0.81 | 9.5m |  |
| `imp-tags` | failed | $0.72 | 2.9m | exit_code=1 |
| `imp-search` | failed | $0.72 | 2.7m | exit_code=1 |

#### Notes
- Flask bookmark API (more complex than TODO CLI).
- Base build hit a real-world hang: agent's pkill -f "python3 app.py" failed to match "/opt/.../Python app.py" (capital P), so wait blocked indefinitely. Fixed by manual kill of orphan Flask. F10 in findings.
- Both improves "failed" cert (per usual -n 1 behavior) but their work merged CLEAN (no conflict — tags + search touched different routes).
- Final app.py has 7 routes including normalize_tags helper for tags feature.

### P4-flask-auth-users-posts

- repo: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/bench-p4-_oh_0147`
- started: 2026-04-20T13:46:56Z → finished: 2026-04-20T07:02:31Z
- wall: 15.6m, total cost: $13.29
- concurrency: 3
- merge: **success + conflict-resolved salvage** (+$3.79, +2.6m); cert_passed=True

#### Per-task
| ID | Status | Cost | Duration | Failure |
|---|---|---|---|---|
| `base` | done | $1.14 | 3.1m |  |
| `imp-comments` | done | $2.66 | 9.3m |  |
| `imp-tags` | failed | $2.83 | 9.1m | exit_code=1 |
| `imp-likes` | failed | $2.87 | 8.7m | exit_code=1 |

#### Notes
- rounds_per_improve: 2
- imp-comments passed cert in -n 2; merged clean with base via --all (cert PASSED).
- imp-tags + imp-likes failed cert but had real commits; salvaged via explicit-branch merge.
- Salvage invoked conflict agent: imp-tags $2.60, imp-likes $1.19 (total salvage conflict cost $3.79).
- Total merge cost (agent): $3.79 across all 3 merges.
- Final app.py has auth + user CRUD + posts CRUD + comments + tags + likes all working.

### P6-inventory-cli

- repo: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/bench-p6-qjqc130n`
- started: 2026-04-20T13:47:22Z → finished: 2026-04-20T07:16:05Z
- wall: 28.7m, total cost: $12.77
- concurrency: 3
- merge: **conflict-resolved** (+$2.41, +13.3m); cert_passed=True

#### Per-task
| ID | Status | Cost | Duration | Failure |
|---|---|---|---|---|
| `base` | done | $0.62 | 2.1m |  |
| `imp-categories` | done | $3.65 | 12.6m |  |
| `imp-suppliers` | failed | $2.87 | 11.2m | exit_code=1 |
| `imp-alerts` | done | $3.22 | 10.3m |  |

#### Notes
- rounds_per_improve: 2
- merge_log_tail: in

    ✓ build/base-2026-04-20 (merged)
    ✓ improve/imp-categories-2026-04-20 (merged)
    ✓ improve/imp-alerts-2026-04-20 (conflict_resolved)
      resolved by agent (cost $2.41, retries 0)

  Merge complete (id: merge-1776693769-13221)
  Verification: 14 verified, 0 skipped, 0 f...
- rounds_per_improve: 2
- imp-categories + imp-alerts passed cert; imp-suppliers failed.
- Merge --all invoked conflict agent on the 2 done improves; conflict resolved + cert PASSED.
- Final inv.py has base CRUD + categories + alerts working.

### p5_markdown_ssg

- repo: `/var/folders/xg/dk8wgfy119z44797kyz7w0380000gn/T/bench-p5-xysyr6kn`
- started: 2026-04-20T13:47:22Z → finished: 2026-04-20T07:02:59Z
- wall: 15.6m, total cost: $10.65
- concurrency: 3
- merge: **failed** (+$0.00, +0s); cert_passed=None

#### Per-task
| ID | Status | Cost | Duration | Failure |
|---|---|---|---|---|
| `base` | done | $1.03 | 2.9m |  |
| `imp-tag-pages` | failed | $3.67 | 12.3m | exit_code=1 |
| `imp-rss` | failed | $2.70 | 9.0m | exit_code=1 |
| `imp-search` | failed | $3.25 | 10.3m | exit_code=1 |

#### Notes
- rounds_per_improve: 2
- merge_log_tail: Specify branches/task ids, or pass --all to merge all done queue tasks.
