# Parallel Batch Execution — Pressure Test Guide

Run these tests against real projects to validate parallel task execution. Each test has expected behavior — verify against actual results.

## Setup

```bash
# Use the parallel-batch branch
OTTO="uv run --project /path/to/cc-autonomous otto"

# Target project must have:
# 1. A git repo with clean working tree
# 2. otto.yaml with max_parallel: 2 (or higher)
# 3. A test suite (npm test, pytest, etc.)
```

## Test Categories

### A. Serial Regression (max_parallel: 1)

Verify parallel code doesn't break serial execution.

**A1. Single task, serial**
```bash
$OTTO drop --all --yes
$OTTO add "Add a utility function that formats dates as relative time"
$OTTO run
```
Expected:
- No `.otto-worktrees/` created
- Task passes normally: pending → running → passed
- Proof report at `otto_logs/*/qa-proofs/proof-report.md`
- `otto status` shows ✓ with timing and cost

**A2. Two tasks, serial (max_parallel: 1)**
```bash
sed -i '' 's/max_parallel: 2/max_parallel: 1/' otto.yaml
$OTTO drop --all --yes
$OTTO add "Add a string truncation utility"
$OTTO add "Add a number formatting utility"
$OTTO run
sed -i '' 's/max_parallel: 1/max_parallel: 2/' otto.yaml
```
Expected:
- "Batch 1  2 tasks (sequential)" in output
- Tasks run one at a time
- Both pass
- No worktrees

### B. Parallel Execution

**B1. Two independent tasks (different files)**
```bash
$OTTO drop --all --yes
$OTTO add "Add a wind chill calculation function to src/lib/weather-utils.ts"
$OTTO add "Add a heat index calculation function to src/lib/temperature.ts"
$OTTO run
```
Expected:
- "Batch 1  2 tasks (parallel)"
- Both tasks show ● running simultaneously
- Both verify, both merge sequentially
- Total wall time < 2x single task time
- `.otto-worktrees/` cleaned up after run
- Both have proof reports

**B2. Three parallel tasks**
```bash
# Set max_parallel: 3 in otto.yaml
$OTTO drop --all --yes
$OTTO add "Add a dew point calculation"
$OTTO add "Add a feels-like temperature badge"
$OTTO add "Add a UV index color coding"
$OTTO run
```
Expected:
- All 3 run in parallel
- Serial merge: 3 candidates merged sequentially
- All 3 pass (or some auto-retry on merge conflict)

**B3. More tasks than max_parallel**
```bash
# max_parallel: 2, but 4 tasks
$OTTO drop --all --yes
$OTTO add "Task A: add utility 1"
$OTTO add "Task B: add utility 2"
$OTTO add "Task C: add utility 3"
$OTTO add "Task D: add utility 4"
$OTTO run
```
Expected:
- Semaphore limits to 2 concurrent
- Tasks A+B start first, C+D wait
- All 4 eventually pass

### C. Merge Conflict Handling

**C1. Two tasks editing the same file**
```bash
$OTTO drop --all --yes
$OTTO add "Add a precipitation badge to the current weather display component"
$OTTO add "Add a cloud coverage indicator to the current weather display component"
$OTTO run
```
Expected:
- Both run in parallel
- During merge: one merges first, other may conflict
- If conflict: git merge auto-resolves OR task is auto-retried on updated main
- Both should eventually pass (exit 0)
- Check: `otto status` shows both ✓ passed

**C2. Deliberately conflicting tasks (hard conflict)**
```bash
$OTTO drop --all --yes
$OTTO add "Rewrite the main App component header to show weather alerts"
$OTTO add "Rewrite the main App component header to show location search"
$OTTO run
```
Expected:
- Hard conflict (both rewrite same section)
- One merges, other auto-retries
- Auto-retry should succeed (agent sees first task's code)
- Both pass

### D. Skip Flags

**D1. --no-qa with parallel**
```bash
$OTTO drop --all --yes
$OTTO add "Add a helper function"
$OTTO add "Add another helper function"
$OTTO run --no-qa
```
Expected:
- No QA phase for either task
- Both pass after coding + test only
- Faster than with QA

**D2. --no-spec with parallel**
```bash
$OTTO drop --all --yes
$OTTO add "Add a date formatting utility"
$OTTO run --no-spec
```
Expected:
- No spec generation
- QA runs against original prompt only

**D3. --no-test with parallel**
```bash
$OTTO drop --all --yes
$OTTO add "Add a comment to README"
$OTTO run --no-test --no-qa
```
Expected:
- Coding only, no test, no QA
- Very fast

### E. Robustness

**E1. Interrupt during parallel run (Ctrl+C)**
```bash
$OTTO drop --all --yes
$OTTO add "Add feature A"
$OTTO add "Add feature B"
$OTTO run
# Press Ctrl+C after ~30 seconds
```
Expected:
- Worktrees cleaned up
- Tasks marked as interrupted or reset to pending
- No orphaned `.otto-worktrees/` directories
- `otto status` shows clean state

**E2. Restart after interrupt**
```bash
# After E1, just run again
$OTTO run
```
Expected:
- Stale tasks reset to pending
- Orphaned worktrees cleaned on startup
- Tasks re-run successfully

**E3. Mixed pass/fail in parallel batch**
```bash
$OTTO drop --all --yes
$OTTO add "Add a simple utility function"  # should pass
$OTTO add "Implement quantum teleportation in JavaScript"  # should fail
$OTTO run
```
Expected:
- One passes, one fails
- Exit code 1
- Passed task is merged to main
- Failed task reported with error
- `otto status` shows ✓ #1, ✗ #2

### F. State Transitions

**F1. Verify `otto status` during parallel run**
```bash
# In terminal 1:
$OTTO run

# In terminal 2 (while running):
$OTTO status
$OTTO status -w  # watch mode
```
Expected:
- Shows multiple running tasks simultaneously
- Each task shows current phase (coding/test/qa)
- `verified` state visible during merge phase
- No state corruption

**F2. Drop/revert with new states**
```bash
# After a parallel run with merge_failed:
$OTTO retry 2           # retry a merge_failed task
$OTTO drop 1            # drop a passed task (safe)
$OTTO revert 1          # revert a passed task's commit
```
Expected:
- retry on merge_failed works without --force
- drop refuses running/merge_pending tasks
- revert undoes the specific commit

### G. Display & UX

**G1. Parallel progress display**
Watch the terminal output during a parallel run. Verify:
- Both tasks show progress simultaneously
- Phase completions are readable (not interleaved)
- "Merging N verified tasks" header appears
- Each merge shows ✓ or ✗
- Final summary is correct

**G2. Proof reports for parallel tasks**
After a parallel run:
```bash
cat otto_logs/*/qa-proofs/proof-report.md
ls otto_logs/*/qa-proofs/
```
Verify:
- Each task has its own proof report
- Reports are complete (not truncated or mixed)
- Screenshots in correct task directory

### H. Cost & Performance

**H1. Parallel speedup measurement**
Run the same 2 tasks serial vs parallel:
```bash
# Serial (max_parallel: 1)
time $OTTO run

# Parallel (max_parallel: 2)
time $OTTO run
```
Expected: parallel wall time ≈ max(task1, task2), not task1 + task2

**H2. Cost comparison**
Check total cost serial vs parallel (should be similar — parallel doesn't add cost, just concurrency).

## Verification Checklist (for every run)

After each test, verify:
- [ ] `otto status` shows correct states
- [ ] Exit code matches (0 = all pass, 1 = some failed)
- [ ] `git log --oneline -5` shows otto commits
- [ ] `ls .otto-worktrees/` is empty (cleaned up)
- [ ] `otto_logs/*/live-state.json` cleaned up
- [ ] Proof reports exist for passed tasks
- [ ] No error messages in terminal output (except expected failures)
- [ ] `git status` is clean (no dirty files from otto)

## Reporting

For each scenario, record:
```
Scenario: B1 — Two independent parallel tasks
Result: PASS / FAIL
Tasks: 2 passed, 0 failed
Time: 7m13s (vs ~14m serial)
Cost: $2.83
Issues: none
```

## Known Limitations

- `max_parallel` defaults to 1 (serial) — must opt in
- Merge conflicts auto-retry adds cost (re-runs coding agent)
- API rate limits may throttle parallel tasks
- Large projects with slow `npm ci` add per-worktree overhead
