# i2p Known Issues & Future Work

**Date**: 2026-03-30
**Context**: Issues identified during i2p v1 implementation and first e2e run.

---

## Build Tracking & Task Identity

### No build concept in tasks.yaml
Tasks have no `build` field. Multiple `otto build` runs in the same project append to the same flat task list. No way to group tasks by build, see build history, or know which build a task belongs to.

**Impact**: `otto retry 3` works (IDs are unique) but the user can't easily see which build created task #3. `otto status` shows a flat list without build grouping.

**Future fix**: Add `build_id` field to tasks (e.g., `build: "ecommerce-1743300000"`). Store build metadata (intent, plan cost, product-spec path) in `otto_logs/builds/`. Group tasks by build in `otto status`.

### Product-spec.md is shared
If user runs `otto build` twice, the second build overwrites `product-spec.md`. Product QA for the first build would use the wrong spec.

**Future fix**: Store product artifacts per build (e.g., `otto_logs/builds/{build_id}/product-spec.md`). Or simpler: prevent concurrent builds (guard against pending tasks from prior build).

### No build history display
`otto status` shows task-level state. No way to see: "build 1 completed (5/5 passed), build 2 in progress (2/3 done)."

**Future fix**: `otto status` groups by build when build tracking exists. Shows build-level summary + per-task detail.

---

## Planner Quality

### Over-serialized dependencies
The product planner creates linear dependency chains when parallel execution is possible. E-commerce test: 5 serial batches when 3 would suffice (tasks 2, 3, 5 could parallel after task 1).

**Root cause**: Planner confuses conceptual ordering ("admin comes after checkout") with data dependencies ("admin needs the Order model from scaffold"). Prompt improved with explicit guidance and examples, but needs validation on more runs.

**Impact**: ~25 min wasted wall time on the e-commerce test (5 serial batches instead of 3).

### Task 1 disproportionately expensive
Scaffold/setup tasks are always the most expensive (e-commerce: $4.76 for task 1 vs $0.13-$0.89 for tasks 2-5). The agent fights tooling issues (Prisma seed runner, npm config) that later tasks don't encounter.

**Not fixable by i2p** — this is a coding agent efficiency issue. The agent should give up on failing tools faster.

---

## Context Updater

### Content quality depends on diff quality
The context updater receives `git diff --stat` (file names + line counts), not actual code changes. It produces better context when given real diff hunks.

**Future fix**: Pass truncated diff hunks (first 3000 chars) instead of just --stat. Balance cost vs quality.

### Prompt still needs tuning
First run produced meta-commentary ("Updated context.md with entries covering...") instead of actual content. Fixed with `max_turns=1` and stricter system prompt, but needs validation on more runs.

---

## Product QA (untested)

### Not yet validated end-to-end
Product QA (`product_qa.py`) and the outer loop (`outer_loop.py`) are built but haven't been tested with a real product QA run. The e-commerce test used `--no-qa`.

### No replan path
The outer loop creates fix tasks for failed journeys but has no replan path for structural failures (wrong architecture, missing decomposition). All failures are treated as implementation bugs.

**Future fix**: Add failure classification (implementation bug vs planning failure) in the outer loop. On planning failure, re-invoke product planner with failure context.

---

## Observability Gaps

### Batch QA failure reason not in orchestrator.log
When batch QA fails a task, the orchestrator log says "retry round 1/3: 1 failed task(s)" without the reason. The reason IS in `otto_logs/batch-qa-{timestamp}/qa-agent.log` but you have to know to look there.

**Future fix**: Include the top-line failure reason in orchestrator.log.

### Product planner cost not in run-history.jsonl
Planning cost ($0.37 for ecommerce) is logged in product-planner.log but not in the run-level cost accounting.

### Context updater cost not tracked
Context update LLM calls (~$0.01/task) are not included in task cost or run cost.

---

## Edit-then-execute gap
When the user chooses `[e] edit` during plan review, they edit `product-spec.md` but the task prompts are NOT regenerated. The tasks may not match the edited spec.

**Future fix**: After editing, re-run the planner with the edited spec as input (just task decomposition, not full planning). Or warn that edits only affect product QA, not task prompts.
