# Parallel QA — Experiment Results & Future Work

## What shipped (2026-03-31)

`parallel_qa: true` in otto.yaml enables per-task QA sessions via `asyncio.gather`.
Instead of one batch QA session verifying all tasks, each task gets its own concurrent session.

### Code-only QA results (validated)

| Metric | Flat (default) | Parallel | Delta |
|--------|---------------|----------|-------|
| QA wall clock | 193s avg | 105s avg | **-46%** |
| QA cost | $0.91 avg | $1.31 avg | +44% |
| QA turns | 92 avg | 34 avg (per task ~29) | -63% |
| Quality | 20/20 must items | 20/20 must items | identical |
| Data points | 4 runs | 2 runs | |

Project: edge-conflicting-tasks (3 tasks modifying same utils.py, 20 must specs).
A/B tested with frozen specs, interleaved runs, same machine.

### How it works

```
orchestrator.py:_run_batch_qa()
  │
  ├─ parallel_qa: false (default)
  │    └─ run_qa(tasks=[all], ...) — one batch session
  │
  └─ parallel_qa: true
       ├─ asyncio.gather(
       │    run_qa(tasks=[task1], session_id=0),
       │    run_qa(tasks=[task2], session_id=1),
       │    run_qa(tasks=[task3], session_id=2),
       │  )
       ├─ Merge verdicts in Python (must_items + extras + infrastructure_error)
       ├─ Cross-task integration gated by post-batch test on HEAD
       └─ Focused retries only re-verify failed tasks
```

### Safety features

- **Exception-safe**: `_run_single_task_qa` catches exceptions, returns structured failure
- **Infrastructure error propagation**: if any session hits API error, merged result reflects it
- **Chrome isolation**: each session gets `--userDataDir chrome-profile-{N}` (unique browser profile)
- **Chrome cleanup**: profiles removed in `finally` block
- **Proof preservation**: per-task proofs kept intact, batch summary written separately
- **Focused retries**: only failed task keys re-verified on retry rounds

## Browser QA — current status and future work

### What works

- Single-session browser QA: fully functional (navigate, screenshot, DOM eval, click)
- Per-session chrome user data dirs: isolated, no contention
- Browser proof artifacts: screenshots saved, referenced in proof report

### What's unproven

Browser parallel QA speed improvement is **inconclusive**. We have only 1 valid data point each:
- Flat: 358s initial QA
- Parallel: 570s initial QA (dominated by 331s LLM text generation outlier in 1 session)

The 570s was NOT a browser issue — it was LLM variance (the same problem that causes 158s vs 266s in code-only runs).

### Known issues to fix before browser parallelism is reliable

1. **`--port` flag unsupported by chrome-devtools-mcp** (removed from code, but means all sessions use the same debug port — isolation is only via userDataDir)

2. **Server port conflicts**: Parallel QA sessions all run against the same project directory. If each task's QA starts an Express server on port 3000, they conflict. The agent sometimes picks a unique port, sometimes doesn't.
   - **Fix**: Pass a unique `QA_SERVER_PORT` env var per session, or instruct the QA prompt to use a random port.

3. **LLM variance dominates**: The slowest parallel session determines wall clock. One session with a 331s text generation gap negates the parallelism benefit.
   - **Fix**: The verdict early-capture mechanism is in place but can't force-stop the session. Would need SDK-level session abort.

4. **Cost**: 3 parallel sessions × $2 each = $6 vs 1 flat session × $3. The cost doubles because each session independently loads context and runs the test suite.
   - **Partial fix**: Stagger session starts by 1-2s to benefit from prompt caching (shared prefix). Not tested.

### Approaches tested and rejected

| Approach | Result | Why |
|----------|--------|-----|
| Prompt-driven subagents | 2x slower, 2x cost | Agent dispatches serially, re-verifies |
| Single-task spec-group splitting | 0-33% faster, 3x cost | Each group redundantly loads context + runs test suite |

### Recommended next steps

1. **More browser A/B data**: Run edge-conflicting-tasks flat vs parallel 3x each on a quiet machine. The current data is too noisy.

2. **Server port isolation**: Add `QA_SERVER_PORT` env var per parallel session. Pass unique ports (3000+session_id) via the SDK env.

3. **Prompt caching experiment**: Stagger parallel session starts by 2s. Check if sessions 2+3 are cheaper (cached prefix).

4. **Verdict MCP tool**: Replace the Write-based verdict with a custom MCP tool that accepts JSON, validates, and returns immediately. Eliminates the 40-70s verdict write/rewrite cycle that affects all QA modes.

### Key files

| File | What |
|------|------|
| `otto/orchestrator.py:_run_batch_qa()` | Parallel dispatch, verdict merge, cleanup |
| `otto/qa.py:_build_qa_mcp_servers()` | Per-session chrome isolation (session_id → userDataDir) |
| `otto/qa.py:run_qa()` | session_id parameter threading |
| `bench/ab-qa-test.sh` | A/B/C test runner for validating changes |
| `docs/parallel-qa-findings.md` | This document |
