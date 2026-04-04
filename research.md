# Research: Cost Accounting & QA Scope Fixes

## Problem 1: Cost Accounting — Costs Lost on Failure

### Symptom
Otto build reported $1.09 total but real costs were ~$7.71:
- Planner: $0.66
- First coding unit: $3.01 (overwritten by retry's $0.43)
- Batch QA: $6.62 (never added to total — confirmed via qa-profile.json)
- Retry coding unit: $0.43

### Root Causes

**A. Batch QA cost dropped on failure path**
- `_run_batch_qa` returns `cost_usd` correctly (qa-profile.json shows $6.62)
- `batch_qa_costs` dict accumulates per-task shares (orchestrator.py:1821)
- But when batch fails → rollback → tasks marked `merge_conflict` → line 2217 adds `batch_qa_costs` only for tasks that produce results
- Failed tasks skip the cost addition path entirely

**B. Retry overwrites first unit's cost**
- `live-state.json` for the unit gets overwritten on retry
- First run: $3.01, 632s. Retry: $0.43, 118s
- Only the retry's numbers survive in live-state.json
- Same bug class as the QA log overwrite we fixed earlier

**C. Events.jsonl doesn't capture batch QA cost**
- `batch_completed` and `all_done` events show `cost=$0.00`
- No event emitted for batch QA completion with its cost

### Fix Approach
1. Track `total_run_cost` accumulator at orchestrator level — add ALL costs regardless of pass/fail
2. Per-attempt cost tracking for units (never overwrite live-state on retry)
3. Batch QA cost always added to run total, even on failure/rollback
4. `run-history.jsonl` uses the accumulator, not sum of task results

---

## Problem 2: QA Edited Project Files → Broke Merge

### Symptom
Batch QA agent used Edit tool on `src/middleware.ts` — created divergence from task commits — retry merge conflicts on all 5 tasks.

### Root Cause
- QA prompt says "VERIFY" but never said "don't edit project files"
- No tool-level restriction — QA had full Edit/Write access

### Fix (Already Applied)
1. Prompt: "CRITICAL: You are READ-ONLY. NEVER use the Edit tool on project source files."
2. SDK: `disallowed_tools=["Edit", "NotebookEdit"]` in qa_opts

---

## Problem 3: Inner Loop QA Has Browser/Visual Scope (Wrong Layer)

### Symptom
Inner loop QA prompt includes agent-browser instructions and `◈` visual spec support. But visual/UX testing belongs in outer loop (certifier journey agents), not inner loop.

### Decision
- **Inner loop QA**: technical verification only. API-level, code-testable specs. No browser.
- **Outer loop certifier**: user experience verification. Journey agents with agent-browser simulate real users.

### What to Change
- `otto/qa.py`: Remove agent-browser instructions from `_QA_COMMON_INSTRUCTIONS`, remove `◈` handling
- `otto/spec.py`: Remove `◈` marker from spec format — all inner loop specs are code-testable
- `otto/certifier/journey_agent.py`: Ensure agent-browser is available for journey agents

---

## Execution Order
1. Cost accounting (Problem 1) — most impactful, affects all runs
2. Browser scope cleanup (Problem 3) — simplifies inner loop
3. QA read-only already done (Problem 2)
