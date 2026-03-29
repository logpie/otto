# Otto Architecture — Detailed Pipeline Reference

This document is the source of truth for otto's execution pipeline. Use it for debugging, onboarding, and understanding what happens when you run `otto run`.

## Overview

```
otto run
  │
  ├─ 1. Preflight (validate branch/tree, stale recovery, no mutations)
  ├─ 2. Smart Planner (single LLM call, high effort)
  │     ├─ INDEPENDENT → parallel batch
  │     ├─ ADDITIVE (same file) → serialize
  │     ├─ DEPENDENT → serialize (later batch)
  │     ├─ CONTRADICTORY → flag + schedule in separate batches (never drop)
  │     └─ UNCERTAIN → serialize (conservative)
  │     Missing tasks auto-added as serial batches (safety net)
  │
  ├─ 3. PER Loop (Plan-Execute-Replan)
  │     │
  │     ├─ For each batch:
  │     │     │
  │     │     ├─ PARALLEL (max_parallel > 1, batch_size > 1)
  │     │     │    Each task → own git worktree → code + test (no QA)
  │     │     │    Then → serial merge phase
  │     │     │
  │     │     └─ SERIAL (max_parallel = 1 or single task)
  │     │          Each task → otto/{key} branch → code + test (no QA)
  │     │
  │     ├─ Merge conflicts:
  │     │    1. git merge (handles most cases)
  │     │    2. If conflict → coding agent re-applies with full diff
  │     │       (one agent, adapts intelligently, no cherry-pick)
  │     │
  │     ├─ Batch QA (one session, combined specs from all tasks)
  │     │    Verify ALL [must] items on integrated codebase
  │     │    Generate cross-task integration tests
  │     │    If [must] fails → retry failed tasks (up to max_retries rounds)
  │     │    Each round: re-code → re-merge → re-QA
  │     │    If still failing after max_retries → rollback batch, continue run
  │     │    (only infrastructure errors abort the entire run)
  │     │
  │     └─ Smart replan if failures occurred
  │          Uses dependency analysis + failure context
  │          Rolled-back tasks re-scheduled in later batches
  │          Tasks depending on permanently failed tasks flagged
  │
  └─ 4. Summary + exit
```

---

## 1. Preflight

```
run_per() entry point:
  │
  ├─ Acquire otto.lock (prevent concurrent runs)
  ├─ Clean orphaned worktrees from previous crashes
  │
  └─ preflight_checks() — read-only validation first, no mutations
       │
       ├─ Validate git state (fail-fast, never auto-checkout or stash):
       │    ├─ Wrong branch → EXIT 2 ("run: git checkout {branch}")
       │    └─ Dirty tree → EXIT 2 ("commit or stash before running")
       │
       ├─ Recover stale tasks:
       │    running/verified/merge_pending → reset to pending
       │    (merged tasks are NOT reset — code already on main)
       │
       ├─ Update .git/info/exclude (framework + otto ignores, no commits)
       ├─ Load tasks.yaml → filter pending tasks
       │    └─ No pending tasks → EXIT 0
       │
       └─ Return (exit_code, pending_tasks)
```

**Key files:** `runner.py:preflight_checks()`, `git_ops.py:check_clean_tree()`

---

## 2. Planning

```
plan(pending_tasks)
  │
  ├─ Single task? → instant (no LLM needed)
  │
  └─ Multiple tasks? → single LLM call (effort=high)
       │
       ├─ Pairwise relationship analysis:
       │    INDEPENDENT → parallel batch
       │    ADDITIVE (same file) → serialize
       │    DEPENDENT → serialize (later batch)
       │    CONTRADICTORY → separate batches (never dropped)
       │    UNCERTAIN → serialize (conservative)
       │
       ├─ Respects depends_on constraints (topological sort)
       ├─ Missing tasks auto-added as serial batches (safety net)
       ├─ Returns ExecutionPlan { batches, analysis, conflicts }
       │
       └─ Parse fails? → fallback to serial_plan()

Validation: _normalize_plan enforces dependency constraints
  └─ Invalid coverage? → fallback to serial_plan()
```

**Key files:** `planner.py:plan()`, `planner.py:serial_plan()`

---

## 3. PER Loop (Plan-Execute-Replan)

### 3a. Batch Execution Mode

```
For each batch in plan:
  │
  ├─ max_parallel > 1 AND batch has 2+ tasks?
  │    │
  │    ├─ YES → _run_batch_parallel()
  │    │    ├─ Snapshot HEAD as base_sha
  │    │    ├─ For each task (bounded by semaphore):
  │    │    │    ├─ git worktree add .otto-worktrees/otto-task-{key} base_sha --detach
  │    │    │    ├─ Install deps in worktree
  │    │    │    ├─ coding_loop(task_work_dir=worktree)
  │    │    │    └─ Cleanup worktree (finally block)
  │    │    │
  │    │    └─ Return list[TaskResult]
  │    │
  │    └─ Then → merge_parallel_results() (see section 3c)
  │
  └─ NO → Serial execution
       └─ For each task sequentially:
            ├─ Create branch otto/{key}
            ├─ coding_loop(task_work_dir=project_dir)
            └─ Merge to default branch via ff-only
```

### 3b. Per-Task Pipeline (`coding_loop` → `run_task_v45`)

This is the core of otto — what happens for each individual task:

```
run_task_v45(task, config, project_dir, task_work_dir)
  │
  ╔══════════════════════════════════════════════════════╗
  ║  PREPARE                                             ║
  ╠══════════════════════════════════════════════════════╣
  ║  ├─ Create log dir: otto_logs/{key}/                 ║
  ║  ├─ Snapshot pre-existing untracked files             ║
  ║  ├─ Run baseline tests                                ║
  ║  │    └─ Baseline fails? → EXIT (error_code=BASELINE) ║
  ║  ├─ Record baseline test count + flaky test names     ║
  ║  └─ Emit: phase=prepare, status=done                  ║
  ╚══════════════════════════════════════════════════════╝
         │
         │  (in parallel, background thread)
         ├──────────────────────────────────────────┐
         │                                          │
  ╔══════╧═════════════════════╗    ╔═══════════════╧═══════════════╗
  ║  CODING (attempt loop)     ║    ║  SPEC GENERATION (if enabled)  ║
  ╠════════════════════════════╣    ╠════════════════════════════════╣
  ║                            ║    ║  Sonnet agent generates:       ║
  ║  for attempt in max_retries║    ║  ├─ [must] gating criteria     ║
  ║    │                       ║    ║  ├─ [must ◈] visual/subjective ║
  ║    ├─ Build prompt:        ║    ║  ├─ [should] advisory          ║
  ║    │  attempt 0: bare CC   ║    ║  └─ [should ◈] visual advisory ║
  ║    │  attempt 1+: + spec   ║    ║                                ║
  ║    │    + failure excerpt   ║    ║  Runs in background thread     ║
  ║    │    + learnings         ║    ║  Available by attempt 2        ║
  ║    │                       ║    ╚════════════════════════════════╝
  ║    ├─ Run coding agent     ║
  ║    │  (CC, bypassPerms,    ║
  ║    │   no custom sys prompt)║
  ║    │                       ║
  ║    ├─ No changes?          ║
  ║    │  └─ Run QA on existing║
  ║    │     code (see below)  ║
  ║    │                       ║
  ║    ├─ Build candidate:     ║
  ║    │  ├─ git reset --mixed ║
  ║    │  ├─ git add (tracked  ║
  ║    │  │   + new project    ║
  ║    │  │   files only)      ║
  ║    │  ├─ git commit        ║
  ║    │  └─ Anchor as ref:    ║
  ║    │     refs/otto/        ║
  ║    │     candidates/{key}/ ║
  ║    │     attempt-{N}       ║
  ║    │                       ║
  ║    ├─────────────────────► TESTING
  ║    ├─────────────────────► QA
  ║    │                       ║
  ║    ├─ All passed?          ║
  ║    │  └─ Break (success)   ║
  ║    │                       ║
  ║    └─ Failed?              ║
  ║       ├─ Set last_error    ║
  ║       │  (failure excerpt, ║
  ║       │   not raw output)  ║
  ║       └─ Continue loop     ║
  ╚════════════════════════════╝
```

#### Testing Phase Detail

```
TESTING
  │
  ├─ skip_test? → skip, proceed to QA
  │
  ├─ PRE-TEST (working dir sanity check)
  │    ├─ Run tests locally
  │    ├─ Extract failing test names
  │    ├─ Compare against baseline flaky set
  │    │    ├─ Only baseline failures? → proceed (flaky)
  │    │    └─ New failures? → reset workspace, retry
  │    │
  │    └─ Emit: phase=test, status=running
  │
  ├─ FULL TEST (clean disposable worktree)
  │    ├─ Create temp worktree at candidate_sha
  │    ├─ Install deps
  │    ├─ Run test_command (jest/pytest/etc.)
  │    ├─ Run custom verify command (if task.verify set)
  │    ├─ Write attempt-N-verify.log
  │    │
  │    ├─ Tests pass? → proceed to QA
  │    ├─ Tests fail locally but pass in worktree? → proceed (env issue)
  │    └─ Tests fail? → build retry excerpt, retry
  │
  ├─ CLAIM VERIFICATION (audit-only, non-blocking)
  │    └─ Regex audit: agent log vs test evidence
  │       (e.g., agent said "tests pass" but exit code was 1)
  │
  └─ Emit: phase=test, status=done/fail
```

#### QA Phase Detail

```
QA
  │
  ├─ skip_qa? → skip, proceed to merge
  │
  ├─ Await spec (if still generating in background)
  │
  ├─ Determine QA tier:
  │    ├─ Tier 0: SKIP QA
  │    │    All [must] items have tests + first attempt + local change
  │    │
  │    ├─ Tier 1: TARGETED
  │    │    Unmapped [must] items OR 5+ files changed
  │    │
  │    └─ Tier 2: FULL + BROWSER
  │         High-risk domains (auth, crypto, payment)
  │         OR visual specs (◈ items)
  │         OR SPA + retry attempts
  │
  ├─ Run QA agent (CC, bypassPerms, chrome-devtools MCP)
  │    ├─ Test [must] items first (in order)
  │    │    └─ Any [must] fails? → write verdict immediately, stop
  │    ├─ Test [must ◈] visual items (browser required)
  │    │    └─ Start dev server, navigate, take_screenshot(filePath=...)
  │    ├─ Test [should] items (advisory only)
  │    │
  │    ├─ For each verified item, record proof:
  │    │    ├─ Targeted command (jest --testPathPattern=X)
  │    │    ├─ Command output
  │    │    └─ Screenshot path (visual items)
  │    │
  │    └─ Write verdict JSON to output file
  │
  ├─ Parse verdict:
  │    ├─ Structured JSON? → use directly
  │    └─ Fallback: regex for "VERDICT: PASS/FAIL"
  │
  ├─ Infrastructure error? → sleep 5s, retry once
  │
  ├─ Write proof artifacts:
  │    ├─ qa-proofs/proof-report.md (human-readable)
  │    ├─ qa-proofs/must-N.md (per-item)
  │    ├─ qa-proofs/regression-check.sh (re-runnable)
  │    └─ qa-proofs/screenshot-*.png (browser captures)
  │
  ├─ All [must] passed? → proceed to merge
  │
  └─ [must] failed? → build retry error from evidence, retry
       └─ Retry error shows WHICH criteria failed + WHY
          (not generic "QA failed")
```

### 3c. Serial Merge Phase (parallel mode)

After all parallel tasks finish, merge verified candidates onto main one-by-one:

```
merge_parallel_results()
  │
  For each verified task (sorted by key):
    │
    ├─ Find best candidate ref
    │    └─ refs/otto/candidates/{key}/attempt-{highest}
    │
    ├─ merge_candidate(project_dir, candidate_ref, default_branch)
    │    │
    │    ├─ Create temp branch from current HEAD
    │    ├─ git merge --no-edit candidate_sha
    │    │
    │    ├─ Merge succeeds? → proceed to post-merge tests
    │    └─ Merge conflicts? → abort, mark merge_conflict
    │         └─ Queued for re-apply (see 3d below)
    │
    ├─ Post-merge test verification (unless skip_test)
    │    ├─ run_test_suite() in fresh worktree at new_sha
    │    │
    │    ├─ Tests pass? → fast-forward main
    │    └─ Tests fail? → revert, mark post_merge_test_fail
    │         └─ Queued for re-apply (see 3d below)
    │
    ├─ Fast-forward: git merge --ff-only new_sha
    │
    └─ Update task: status=passed, commit_sha, cost
```

### 3d. Auto-Retry (Merge Conflict Resolution) & Replan

When merge fails, the coding agent re-runs on updated main with intelligent
feedback. One agent, one path — trust it to self-regulate.

E2e verified: merge conflict retry takes ~56s (vs ~154s original), costs ~$0.15
(vs ~$0.60 original). Agent reads 3 files, makes targeted edits, adapts to
sibling task's code. 75% cheaper, 64% faster than original coding.

```
After merge phase:
  │
  ├─ merge_conflict?
  │    └─ coding_loop with "MERGE CONFLICT CONTEXT" feedback:
  │         ├─ Full diff from previous implementation
  │         ├─ Files previously changed (diff --stat)
  │         ├─ Strategy: "Read diff → read main → apply with Edit → test"
  │         ├─ Agent self-regulates: simple → fast, complex → explores more
  │         └─ Replace result in batch_results
  │
  ├─ post_merge_test_fail?
  │    └─ coding_loop with test failure feedback:
  │         ├─ Full diff + "tests FAILED on integrated codebase"
  │         ├─ Strategy: "Adapt implementation to work with other task"
  │         └─ Replace result in batch_results
  │
  ├─ Batch QA (up to max_retries rounds)
  │    ├─ Initial QA on integrated codebase
  │    ├─ If [must] fails → re-code failed tasks → re-merge → re-QA
  │    ├─ Repeat up to max_retries rounds
  │    └─ After max_retries: rollback batch, mark failed tasks,
  │         reset rolled-back (innocent) tasks to pending
  │         Continue run (don't abort remaining batches)
  │
  ├─ Remove completed/failed tasks from plan
  │    (rolled-back tasks stay in plan for replan)
  │
  └─ Any failures AND remaining batches?
       │
       └─ Smart replan(context, remaining_plan)
            ├─ Receives: dependency analysis, failed vs rolled-back keys,
            │    task prompts, completed results
            ├─ Tasks depending on failed tasks → late batch with warning
            ├─ Rolled-back tasks → re-scheduled (they passed before)
            ├─ Independent tasks → can parallelize
            └─ Falls back to serial remaining plan if replan fails
```

---

## 4. Task State Machine

```
                           ┌─────────┐
                           │ pending  │◄─────────────────────────┐
                           └────┬─────┘                          │
                                │ run starts                     │
                           ┌────▼─────┐                          │
                           │ running   │                          │
                           └────┬─────┘                          │
                                │                                │
                 ┌──────────────┼──────────────┐                 │
                 │              │               │                 │
          (parallel)      (serial)        (all modes)            │
                 │              │               │                 │
          ┌──────▼──────┐      │         ┌─────▼─────┐          │
          │  verified    │      │         │  failed    │          │
          └──────┬──────┘      │         └───────────┘          │
                 │              │         max_retries             │
          ┌──────▼──────┐      │         exhausted,              │
          │merge_pending │      │         timeout,                │
          └──────┬──────┘      │         baseline fail           │
                 │              │                                 │
          ┌──────┼──────┐      │                                 │
          │             │      │                                 │
   ┌──────▼──────┐  ┌───▼─────▼───┐                             │
   │merge_failed  │  │   passed     │                             │
   └──────┬──────┘  └──────────────┘                             │
          │          merge succeeded,                             │
          │          all tests pass                               │
          │                                                      │
          └──► auto-retry: re-run coding_loop() ─────────────────┘
               on updated main with previous
               diff as feedback (full pipeline:
               coding + tests + QA, no max_turns,
               up to max_retries attempts)
```

---

## 5. Cost Model

| Component | Model | Typical Cost | When |
|-----------|-------|-------------|------|
| **Planner** | CC default | ~$0.02-0.05 | Once per run (effort=high, multi-task only) |
| **Replanner** | CC default | ~$0.02-0.05 | After batch failure (with dependency context) |
| **Spec gen** | Sonnet | ~$0.15-0.30 | Once per task (background thread) |
| **Coding agent** | CC default | ~$0.50-1.50 | Per attempt (resumes session) |
| **QA agent** | CC default | ~$0.30-1.00 | Per attempt (tier-dependent) |
| **Merge re-apply** | CC default | ~$0.15-0.50 | Coding agent with full diff — adapts intelligently (e2e verified: ~$0.15) |
| **Typical task** | | **~$1.00-2.50** | Single attempt, no retries |
| **With retry** | | **~$2.50-4.00** | Coding + QA + retry coding + retry QA |

---

## 6. Skip Flags

| Flag | Skips | Effect | Use Case |
|------|-------|--------|----------|
| `--no-spec` | Spec generation | QA runs "prompt-only" (elevated tier) | Quick iteration |
| `--no-qa` | QA phase entirely | Pass after tests succeed | Trusted changes |
| `--no-test` | Testing phase | Pass after coding | Doc-only, config |

These flags affect the per-task pipeline only. Post-merge verification in the parallel merge phase always runs tests (unless `skip_test` is also set in config).

---

## 7. File Layout

```
your-project/
├── otto.yaml                          # Configuration
├── tasks.yaml                         # Task queue (pending/running/passed/failed)
├── .otto-worktrees/                   # Parallel task worktrees (auto-cleaned)
│   └── otto-task-{key}/               # One per parallel task
├── .otto-scratch/                     # Temp workspace (per-task)
└── otto_logs/
    ├── run-history.jsonl              # One line per run: tasks, cost, time
    ├── v4_events.jsonl                # Telemetry events
    ├── learnings.jsonl                # Cross-run learnings
    ├── planner.log                    # Task analysis, relationships, batch structure
    ├── orchestrator.log               # Batch decisions, merge, parallel lifecycle
    ├── spec-agent.log                 # Spec generation logs
    └── {task_key}/
        ├── live-state.json            # Real-time progress (for otto status)
        ├── task-summary.json          # Per-phase cost + timing breakdown
        ├── attempt-N-agent.log        # Coding agent full log
        ├── attempt-N-verify.log       # Test suite output
        ├── qa-report.md               # QA agent report
        ├── qa-verdict.json            # Structured verdict
        ├── qa-agent.log               # QA agent tool calls + output
        ├── qa-tier.log                # Why QA chose tier 0/1/2
        ├── cost-warning.log           # Parallel $0 cost warnings
        └── qa-proofs/
            ├── proof-report.md        # Human-readable proof per must item
            ├── regression-check.sh    # Re-runnable verification commands
            ├── must-1.md ... must-N.md # Per-item evidence
            └── screenshot-*.png       # Browser captures
```

---

## 8. Configuration Reference

```yaml
# otto.yaml
max_retries: 3              # Attempts per task before giving up
default_branch: main         # Target branch for merges
verify_timeout: 300          # Test suite timeout (seconds)
max_task_time: 3600          # Per-task circuit breaker (seconds)
qa_timeout: 3600             # QA agent timeout (seconds)
max_parallel: 1              # 1=serial (default), 2+=parallel worktrees
install_timeout: 120         # npm ci / pip install timeout in worktrees

# Per-agent setting scopes
coding_agent_settings: "user,project"  # Reads user + project CLAUDE.md
spec_agent_settings: "project"         # Project CLAUDE.md only
qa_agent_settings: "project"           # Project CLAUDE.md only

# Auto-detected (override in otto.yaml if wrong)
test_command: "npx jest"     # or "pytest", "cargo test", etc.
```

---

## 9. Retry Excerpt

When a task fails, the next attempt doesn't get the raw 847K test output. Instead, `build_retry_excerpt()` extracts:

```
Raw test output (847K chars)
  │
  ├─ Extract FAIL blocks (jest: "● Test Suite", pytest: "FAILED")
  ├─ Extract summary lines ("Tests: 3 failed, 109 passed")
  ├─ Drop PASS noise, warnings, coverage tables
  └─ Result: ~2.5K chars of failure-relevant content

Saves ~$2/retry by avoiding prompt cache invalidation.
```

---

## 10. Flaky Test Detection

```
Baseline (run_task_v45 prepare phase, not preflight):
  Auto-detect test command → run tests → record failing test names as "known flaky set"

Pre-verify (after coding):
  Run tests → extract failing test names
  │
  ├─ All failures in flaky set? → proceed (pre-existing)
  └─ New failures? → retry (agent introduced them)
```

---

## 11. Debugging Checklist

When something goes wrong, check in this order:

1. **`otto status`** — current task states, phase timings
2. **`otto_logs/{key}/live-state.json`** — real-time progress
3. **`otto_logs/{key}/task-summary.json`** — per-phase cost + timing breakdown
4. **`otto_logs/{key}/attempt-N-agent.log`** — what the coding agent did
5. **`otto_logs/{key}/attempt-N-verify.log`** — test output
6. **`otto_logs/{key}/qa-agent.log`** — what QA ran (Bash commands + output)
7. **`otto_logs/{key}/qa-report.md`** — QA findings
8. **`otto_logs/{key}/qa-proofs/proof-report.md`** — evidence per spec item
9. **`otto_logs/planner.log`** — task analysis, relationships, batch structure
10. **`otto_logs/orchestrator.log`** — batch decisions, merge, parallel lifecycle
11. **`otto_logs/run-history.jsonl`** — cost/time trends across runs
12. **`otto_logs/v4_events.jsonl`** — detailed telemetry events
13. **`git log --oneline -20`** — what got merged
14. **`git worktree list`** — any orphaned worktrees?
