# Batch QA Pipeline Redesign

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move QA from per-task to batch-level (one QA session on integrated codebase) and add scoped merge conflict resolution to eliminate wasteful full re-coding.

**Architecture:** Per-task pipeline stops at "verified" (code + tests pass). All tasks merge, conflicts resolved by scoped agent (not full re-code). One batch QA session verifies all specs on the integrated codebase. Failed items trigger targeted retry + re-QA.

**Tech Stack:** Python 3.11, Claude Agent SDK, asyncio, git

---

## Problem

QA runs per-task in isolation, then tasks merge, then conflicts get resolved by re-running the ENTIRE coding pipeline ($2-3, ~5min per task). This is slow, wasteful, and doesn't catch cross-task interaction bugs.

Real example: 4 simple features took 30+ minutes because merge conflicts triggered full re-implementations with full QA per re-apply.

## Design

### Pipeline: Before vs After

**Before (current):**
```
Per-task:     code → test → QA(spec) → verify         ~5min each
Merge:        git merge → conflict? → FULL RE-CODE → test → QA → verify  ~5min
Post-run:     integration test (test suite only)
```

**After:**
```
Per-task:     code → test → verify                     ~2min each (no QA, no spec)
Merge:        git merge → conflict? → scoped re-apply → test   ~30-60s
Batch QA:     ONE session, combined specs, integrated code      ~5min total
              └─ if [must] fails → retry task → re-QA (task's specs + cross-task)
Post-run:     test suite (deterministic, always runs)
```

### Compatibility Matrix

| Scenario | Per-task | Merge | Batch QA | Post-run tests |
|----------|----------|-------|----------|----------------|
| **Single task, serial** | code → test → QA → merge | N/A | Skipped (per-task QA) | N/A |
| **Multi-task, serial** | code → test → verify | Sequential on main | Combined specs | Full suite |
| **Multi-task, parallel** | code → test → verify | Parallel → serial merge | Combined specs | Full suite |
| **--no-qa (any)** | code → test → verify/merge | As above | **Skipped** | Full suite (kept!) |
| **No-change task** | QA on existing code (kept) | N/A | Included in batch | Full suite |

**Key rules:**
- Batch QA activates when 2+ tasks in the run. Single task keeps per-task QA.
- `--no-qa` skips QA but **keeps** the deterministic post-run test suite.
- No-change tasks (agent made 0 edits) still get QA validation — per-task in single-task mode, batch QA in multi-task mode.

### QA Mode: Orchestrator Decides

The orchestrator passes an explicit `qa_mode` to each task runner — the task runner does NOT infer this from task count.

```python
class QAMode(Enum):
    PER_TASK = "per_task"    # Single-task run: full QA inside run_task_v45
    BATCH = "batch"          # Multi-task run: skip per-task QA, batch later
    SKIP = "skip"            # --no-qa: skip all QA
```

Decision logic in orchestrator:
```python
if config.get("skip_qa"):
    qa_mode = QAMode.SKIP
elif len(pending) == 1:
    qa_mode = QAMode.PER_TASK
else:
    qa_mode = QAMode.BATCH
```

### Task States

New state for multi-task runs: tasks are `merged` after merge phase but before batch QA approves them.

```
pending → running → verified → merged → passed  (batch QA approves)
                              → merged → failed  (batch QA rejects + retry exhausted)
                  → failed (coding/test failure)
                  → merge_failed → scoped re-apply → merged
```

Tasks show `merged` in `otto status` between merge and batch QA. Only `passed` after batch QA.

### Scoped Merge Conflict Resolution

When `git merge` fails:

```
1. Try mechanical first: git cherry-pick --no-commit candidate onto main
   (cherry-pick may succeed where merge fails for some conflict types)

2. If mechanical fails: get FULL PATCH from candidate ref
   - git diff base_sha..candidate_sha  (full patch, not diff --stat)
   - Persisted at refs/otto/candidates/{key}/attempt-N (already exists)

3. Give scoped agent the patch + conflicted files:
   "Apply this patch to the current codebase. Here are the conflicted files.
    Resolve conflicts by reading both versions and producing the correct merge.
    Do NOT re-explore the codebase. Do NOT re-implement the feature.
    Just apply these specific changes."

   - max_turns=30 (very scoped)
   - system_prompt: "You are a merge conflict resolver, not a feature implementer."
   - effort="low"

4. Run test suite on result
   - Pass → merge succeeds
   - Fail → fall back to full coding_loop with QA failure feedback
```

Data availability: candidate refs are already anchored at `refs/otto/candidates/{key}/attempt-N`. Full patch extracted via `git diff base_sha..candidate_sha`.

### Batch QA

Runs **per completed batch** (not only at end of run) so later batches get replan signals.

**When:** After all tasks in a batch are merged (including scoped re-applies).

**Combined spec format:**
```
You are testing the integrated result of N tasks merged onto main.
Verify ALL [must] items — do NOT stop at the first failure.
For each item, include the task_key so failures can be attributed.

## Task #1: Add lightning risk calculator (task_key: 2d829eba)
[must] {task_key: 2d829eba, spec_id: 1} accepts weather data and returns risk level
[must] {task_key: 2d829eba, spec_id: 2} handles missing fields gracefully

## Task #2: Add radar mini map (task_key: cc3ad79a93f5)
[must] {task_key: cc3ad79a93f5, spec_id: 1} renders map centered on location
...

## Cross-Task Integration
Identify interactions between tasks that share files or dependencies.
Generate and run targeted integration tests.
Report findings separately from per-task specs.
```

**Key difference from per-task QA:** The batch QA prompt explicitly instructs:
- Verify ALL items exhaustively (not stop at first failure)
- Include `task_key` in each verdict item for attribution
- Test cross-task interactions
- Run full test suite as regression check

**Verdict structure:**
```json
{
  "must_passed": true,
  "must_items": [
    {"task_key": "2d829eba", "spec_id": 1, "criterion": "...", "status": "pass", "proof": [...]}
  ],
  "integration_findings": [
    {"description": "...", "status": "pass/fail", "test": "...", "tasks_involved": ["key1", "key2"]}
  ],
  "regressions": [],
  "test_suite_passed": true
}
```

### Batch QA Timing

Batch QA runs **per batch**, not once at end of run:

```
Batch 1: [task A, task B] → code → test → merge → BATCH QA
  └─ QA results feed into replan for batch 2
Batch 2: [task C] → code → test → merge → BATCH QA (or per-task if single task)
```

For single-task batches within a multi-task run: run per-task QA inside coding_loop (simpler than batch QA for 1 task).

### Retry on Batch QA Failure

If batch QA fails [must] items:

```
1. Identify which task(s) failed (from task_key attribution)
2. For each failed task (sequential, by task ID):
   a. Re-run coding_loop on current main
      - Prompt: original task prompt + QA failure evidence
      - Agent sees all other tasks' code on main
      - Full retry budget (max_retries)
   b. Merge new code, run test suite
3. Re-QA: re-check ALL [must] items for retried task(s)
   + cross-task checks involving files touched by retried task(s)
   + full test suite
   (broader than just "failed items" — retry may fix one thing and break another)
4. Max 2 batch QA rounds total. If still failing → mark task failed, exit 1.
```

### Spec Generation Timing

In batch mode, spec generation is **deferred to batch QA time** (not run during per-task coding):
- Per-task coding runs bare CC (attempt 0 never uses spec anyway)
- Specs generated in bulk before batch QA session (parallel, all tasks at once)
- Saves cost: no wasted spec gen for tasks that fail coding/testing

In single-task mode: spec gen runs in parallel with coding as today (unchanged).

### `--no-qa` Behavior

`--no-qa` skips all QA (per-task and batch) but **keeps** the deterministic post-run test suite. This is the safety net for multi-task runs even without QA.

```python
# In orchestrator, after all batches:
if len(all_passed) >= 2 and not config.get("skip_test"):
    run_test_suite(project_dir, "HEAD", test_command, timeout)
```

### No-Change Task Handling

If a task's coding agent produces no diff:
- **Single-task mode:** Per-task QA validates existing code against spec (unchanged)
- **Multi-task mode:** Task is included in batch QA with its specs. Batch QA verifies the spec is satisfied by existing code. No special handling needed — batch QA tests the integrated codebase regardless of which task made which change.

## Implementation Phases

### Phase 1: Scoped Merge Conflict Resolution

**File: `otto/merge_resolve.py`** (new — focused conflict resolution)

```python
async def scoped_reapply(
    task_key: str,
    candidate_ref: str,
    base_sha: str,
    config: dict,
    project_dir: Path,
    tasks_file: Path,
) -> tuple[bool, str]:
    """Apply a task's changes to updated main via scoped agent.

    Returns (success, new_head_sha).
    """
```

Steps:
1. Extract full patch: `git diff base_sha..candidate_sha`
2. Try mechanical: `git cherry-pick --no-commit candidate_sha` on temp branch
3. If cherry-pick succeeds → commit, run tests, return
4. If cherry-pick fails → abort, call scoped agent with patch + conflicted files
5. Agent applies patch, tests pass → commit, return
6. Agent fails → return (False, "")

**File: `otto/orchestrator.py`**

Replace the `coding_loop()` re-apply (~lines 290-350) with:
1. Try `scoped_reapply()` first
2. If fails → fall back to `coding_loop()` (full re-code, current behavior)

**Verify:**
- `uv run pytest tests/ -x` passes
- E2e: two tasks touching same file → scoped re-apply completes in <60s

### Phase 2: Move QA to Batch Level

**File: `otto/orchestrator.py`**

Add `QAMode` enum and pass to task runner. Add `_run_batch_qa()`:

```python
async def _run_batch_qa(
    merged_tasks: list[dict],      # tasks with specs
    config: dict,
    project_dir: Path,
    tasks_file: Path,
    telemetry: Any,
    context: Any,
) -> dict:
    """Run combined QA on integrated codebase. Returns verdict."""
```

**File: `otto/runner.py`**

- Accept `qa_mode: str` parameter in `run_task_v45()`
- When `qa_mode == "batch"`: skip QA, skip spec gen, return at "verified"
- When `qa_mode == "per_task"`: existing behavior (unchanged)
- When `qa_mode == "skip"`: existing skip_qa behavior

**File: `otto/qa.py`**

Add `format_batch_spec()` — combines specs from multiple tasks with task headers and task_key attribution.

Update batch QA system prompt:
- Verify ALL items (no early stop)
- Include task_key in each verdict item
- Generate cross-task integration tests
- Run full test suite

**File: `otto/tasks.py`**

Add `merged` status to valid task states.

**Verify:**
- Single task: per-task QA as before
- Multi-task: batch QA after merge, one session
- Specs generated at batch QA time, not during per-task coding

### Phase 3: Batch QA Retry Loop

**File: `otto/orchestrator.py`**

After `_run_batch_qa()`:
1. Extract failed tasks from verdict (by task_key)
2. For each failed task: `coding_loop()` on main with QA failure feedback
3. Merge new code, test
4. Re-QA: all [must] for retried tasks + cross-task checks + test suite

**File: `otto/qa.py`**

Add `run_targeted_batch_qa()`:
- Re-checks all [must] items for specified task(s) (not just failed items)
- Includes cross-task checks for files touched by retried task(s)
- Runs full test suite

**Verify:**
- Batch QA fails → retry → re-QA passes
- Max 2 rounds, then mark failed

### Phase 4: Cleanup and Integration

- Remove post-run integration test (absorbed into batch QA test suite run)
- But **keep** post-run test suite for `--no-qa` mode
- Update `otto status` display for `merged` state
- Update proof report paths: batch QA writes to `otto_logs/batch-qa/`
- Update `otto show` to display batch QA results
- Update architecture docs

**Verify:**
- All existing tests pass
- E2e: 4 tasks, 2 conflicts → scoped re-apply → batch QA → all pass
- `--no-qa`: post-run test suite still runs
- Single task: identical to current behavior

## Files to Modify

| File | Changes |
|------|---------|
| `otto/merge_resolve.py` | New — scoped reapply (mechanical + agent) |
| `otto/orchestrator.py` | QAMode, batch QA, scoped reapply integration, retry loop |
| `otto/runner.py` | Accept qa_mode param, skip QA/spec in batch mode |
| `otto/qa.py` | `format_batch_spec()`, batch QA prompt, `run_targeted_batch_qa()` |
| `otto/tasks.py` | Add `merged` status |
| `otto/context.py` | (optional) batch_qa_verdict field |
| `otto/display.py` | `merged` state display, batch QA progress |
| `tests/test_orchestrator.py` | Scoped reapply, batch QA, retry, compatibility |
| `tests/test_qa.py` | Batch spec formatting, targeted QA |

## Verification Criteria

1. **Single task:** QA runs per-task, identical to today
2. **Two tasks, no conflict:** Batch QA runs once with combined specs
3. **Two tasks, same file:** Scoped re-apply resolves conflict in <60s
4. **Batch QA failure:** Failed task retried, re-QA passes
5. **Cross-task bug:** Batch QA generates integration test that catches it
6. **--no-qa flag:** Skips QA, keeps post-run test suite
7. **max_parallel=1:** Batch QA still runs after serial tasks merge
8. **No-change task:** Included in batch QA, not falsely passed
9. **Multi-task with deps (2 batches):** Batch QA runs per batch, feeds replan
10. **Batch QA reports all failures:** Does not stop at first failed [must]
11. **Retry doesn't regress siblings:** Re-QA checks retried task's ALL specs + cross-task
12. **Scoped re-apply handles renames/deletes:** Not just line edits
13. **Task state `merged`:** Visible in `otto status` between merge and batch QA
14. **Proof reports:** Batch QA writes proofs per task to correct paths
15. **Existing tests pass:** `uv run pytest tests/ -x`

## Additional Design Details (from Codex Round 2)

### `merged` State: Full Integration

`merged` is a first-class transient state, like `running` or `verified`:
- **Startup recovery:** Tasks stuck in `merged` on crash → reset to `verified` (code is on main, re-run batch QA)
- **CLI behavior:** `drop` allowed (removes from queue, code stays on main). `revert` allowed (git revert the commit).
- **Display:** Blue icon with "merged (QA pending)" label
- **Summary counts:** Counted as "in progress" (neither passed nor failed)

### Cross-Batch Regression

Later batches can break earlier batches' code. Each batch QA runs against the **cumulative** integrated codebase (not just the current batch's tasks):

```
Batch 1: [A, B] → merge → batch QA (specs A+B) → PASS → A,B = passed
Batch 2: [C] → merge → batch QA (specs A+B+C) → checks ALL specs
  └─ If batch 2's QA fails spec from A → A gets re-opened
     (A's status: passed → merged, needs re-approval)
```

This means batch QA always checks ALL specs from ALL completed tasks in the run, not just the current batch. Cost is bounded: QA checks more items but only runs once per batch.

### Batch QA Failures Feed Replan

Batch QA failures are folded into `context.results` and batch pass/fail accounting BEFORE the replan decision:

```python
# After batch QA:
for task_key, verdict in qa_failures.items():
    context.results[task_key] = TaskResult(success=False, error_code="qa_failed", ...)
    batch_failed += 1
    batch_passed -= 1

# Now replan sees the failures:
if batch_failed > 0 and not execution_plan.is_empty:
    execution_plan = await replan(context, remaining_plan, ...)
```

### Telemetry for Batch QA

New telemetry events:
- `BatchQAStarted(batch_index, task_count, spec_count)` — batch QA begins
- `BatchQACompleted(batch_index, must_passed, must_failed, integration_findings)` — batch QA ends
- `BatchQAItemResult(task_key, spec_id, status, evidence)` — per-item result

Per-task projection: `otto show <task>` extracts that task's items from the batch QA verdict and displays them as if they were per-task QA results. Proof artifacts written per-task to `otto_logs/{key}/qa-proofs/` (extracted from batch QA).

## Plan Review

### Round 1 — Codex (9 issues)
- [HIGH] Scoped re-apply lacks real patch data — fixed: extract full patch from candidate ref, try cherry-pick first
- [HIGH] Batch QA stops at first failure — fixed: exhaustive evaluation, task_key attribution in verdict
- [HIGH] Serial multi-task state machine underspecified — fixed: added `merged` state, delay `passed` until batch QA
- [HIGH] run_task_v45 can't infer batch mode — fixed: orchestrator passes explicit `qa_mode` enum
- [MEDIUM] Speedup overstated without suppressing spec gen — fixed: defer spec gen to batch QA time in multi-task mode
- [HIGH] --no-qa loses integration protection — fixed: keep deterministic post-run test suite for --no-qa
- [HIGH] Targeted re-QA too narrow — fixed: re-check ALL specs for retried task + cross-task, not just failed items
- [MEDIUM] No-change tasks regress — fixed: included in batch QA with their specs
- [MEDIUM] Batch QA only at end is too late for PER — fixed: run per completed batch, not only end of run

### Round 2 — Codex (4 issues)
- [HIGH] `merged` state breaks crash recovery + CLI — fixed: defined as first-class transient state with startup reset, CLI guards, display icons
- [HIGH] Per-batch QA doesn't revalidate earlier tasks — fixed: each batch QA checks ALL specs from ALL completed tasks (cumulative)
- [HIGH] Batch QA failures don't trigger replan — fixed: fold failures into context.results before replan decision
- [MEDIUM] Telemetry can't represent batch QA — fixed: new BatchQA* events + per-task projection for otto show

### Round 3 — Codex (4 issues)
- [HIGH] `merged → verified` on crash causes duplicate merge — fixed: keep distinct `merged` state on recovery, resume at batch QA only (not re-merge)
- [HIGH] `drop` on `merged` strands unapproved code — fixed: `drop` on `merged` requires `revert` first (or `drop --revert`). Cannot remove task record while unapproved code is on main.
- [HIGH] Reopened task has multiple commits, `revert` only finds first — fixed: track commit SHA per task in tasks.yaml (already done for `passed`). `revert` uses stored SHA, not grep. Multi-revision tasks: revert all associated SHAs.
- [MEDIUM] Cumulative batch QA re-runs expensive checks — fixed: only re-check [must] items from earlier batches that share files/deps with the new batch. Visual/browser (◈) items from untouched earlier tasks are cached (result carried forward, not re-run).

### Round 4 — Codex (3 issues)
- [HIGH] Multi-SHA revert unsafe with interleaved commits — fixed: store ordered list of merge SHAs per task revision. Revert newest-to-oldest. Warn when later tasks depend on files touched by the reverted task. Block if revert would leave main in broken state (tests must pass after revert).
- [HIGH] File-only invalidation misses semantic regressions — fixed: always re-check ALL [must] items when batch touches global-risk files (package.json, lockfiles, routing, DB schema, shared types, build config). File-only narrowing only applies when changes are isolated to task-owned files. Full test suite always runs as hard backstop.
- [MEDIUM] Cached visual results stale after shared UI changes — fixed: cache invalidation includes shared UI primitives (layout, theme, global CSS). Any change to these → invalidate all cached visual (◈) results. Implementation: track "visual dependency files" per project (auto-detected or configured).
