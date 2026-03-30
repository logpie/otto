# Pressure Test Findings — 2026-03-28

## Summary

Ran 10 multi-task projects + 10 golden single-task projects against otto v5.

- **Golden set (single-task): 8/10 PASS** — no regressions. 2 pre-existing infra failures.
- **Multi-task: 5/10 PASS, 5 false positives (50%)** — but 1 was a verify.sh bug, not otto.
- **After QA fixes + sibling context: 0/5 improved** — failures are coding agent capability gaps.
- **Planner regression: silently drops CONTRADICTORY tasks** instead of resolving them.

## Batch QA Bugs Found (3 bugs, all fixed by impl CC)

1. **QA1: Placeholder diff** — `orchestrator.py:388` — QA agent got a string instead of real `git diff`. Fixed.
2. **QA2: Per-batch scope** — `orchestrator.py:870` — QA only saw current batch tasks, not cumulative. Fixed.
3. **QA3: Prompt contradiction** — `qa.py:36` vs `qa.py:1066` — "stop at first failure" vs "don't stop." Fixed.

Fixes were structurally correct but didn't improve outcomes — the 5 failures are coding agent capability issues, not information gaps.

## NEW BUG: Planner Drops Tasks

greenfield-expense-api: Tasks 2+3 marked `status: conflict, error_code: planner_conflict` and never executed. Previously they ran fine.

humanize (10 tasks): 2 tasks silently dropped from batches. Same bug.

The planner identifies CONTRADICTORY relationships but **drops tasks instead of resolving them**. It should either: resolve and schedule, or flag to user and continue with remaining tasks.

**Where:** Planner prompt line 707: "CONTRADICTORY: incompatible edits to the same thing, flag and exclude from execution." The "exclude from execution" instruction is too aggressive.

**Root cause: two-phase planner is fragile.** The planner runs Haiku (low effort) to shortlist "suspicious" pairs, then Sonnet (medium effort) to classify + batch. Problems:
- Haiku misses pairs → Sonnet never analyzes them → tasks silently absent from batches
- humanize `ordinal()` task was never flagged by Haiku, so Sonnet's batcher forgot it
- Sonnet at medium effort loses track of tasks when generating complex batch JSON

**Suggested fix:** Single strong model call (high effort) does both triage + batching. The cost saving from two phases is ~$0.01-0.02, not worth the reliability loss. Post-plan validation should also verify all input task keys appear in output batches.

## Planner Batching — Real Data

Tested planner-only (no full runs) on 3 real-world repos with 10-15 tasks each:

| Project | Tasks | max_parallel | Batches | Largest Batch | Effective Parallelism |
|---------|-------|-------------|---------|---------------|----------------------|
| radash (JS utils) | 15 | 5 | 6 | **6** | 67% — separate files per module |
| TinyDB (Python DB) | 12 | 4 | 7 | **3** | 42% — shared core table.py |
| humanize (Python utils) | 10 | 4 | 7 | **2** | 20% — most funcs in same files |

Also tested on our synthetic projects:

| Project | Tasks | Batches | Batch Sizes | Why |
|---------|-------|---------|-------------|-----|
| parallel-4-tasks-batched | 4 | 1 | [4] | All INDEPENDENT — different files |
| brownfield-flask-auth | 3 | 3 | [1,1,1] | Chain: auth→isolation→rate-limit |
| greenfield-kanban-api | 4 | 4 | [1,1,1,1] | Chain: data→API→filters→activity |
| brownfield-refactor-monolith | 4 | 4 | [1,1,1,1] | Chain: models→db→reporting→features |

### Bottleneck: ADDITIVE = always serialize

The planner classifies "same file, different functions" as ADDITIVE → serialize. This is correct for merge conflict avoidance but kills parallelism on monolithic codebases.

radash example: chunk(), flatten(), uniqBy(), groupBy() all go in `src/array.ts` — 4 independent functions in the same file, all serialized.

humanize: filesize(), percentage(), scientific() all go in separate logical areas but same module files — serialized.

**The tradeoff:** ADDITIVE parallel + re-apply on conflict would be faster (`max(task1, task2) + re-apply` vs `task1 + task2`). Re-apply works (proven by parallel-4-tasks-batched). But same-file conflicts are more likely than cross-file conflicts, so re-apply would trigger more often.

## Coding Agent Capability Gaps (not fixable by planner/QA)

The 5 false positives persist after fixes because the coding agent doesn't:

| Bug Pattern | Example | Agent Has Context? |
|-------------|---------|-------------------|
| Add ON DELETE CASCADE for FK | parallel-web-features | Yes (sibling context says posts have DELETE) |
| Filter reads by owner_id | brownfield-flask-auth | **Verify bug** — Alice was admin, otto was correct |
| Wire new code into existing handlers | greenfield-kanban-api (activity log) | Yes (sibling context mentions card endpoints) |
| Match types across tasks | greenfield-expense-api (str vs int) | Tasks dropped by planner (regression) |
| Slim down original file after extract | brownfield-refactor-monolith | Spec/verify threshold mismatch |

The agent gets sibling context but doesn't reason about: FK cascading, hooking new code into existing functions, or cross-task type contracts.

## Verify Script Audit

Audited all 72 verify.sh scripts. Found 12 issues across 8 projects:

- **brownfield-flask-auth**: verify was wrong (first user = admin, verify didn't account for it). **Fixed.**
- **brownfield-express-features**: stock calc wrong (74+10=84, asserts 85). Vacuous min_rating check.
- **parallel-competing-migrations**: admin check silently passes when no restriction exists.
- 5 more minor issues (overconstrained assertions, env assumptions).

**Golden set (real-* repos): clean** — 2 minor overconstrained source checks out of 27 projects.

Full audit: `.worktrees/pressure-test/bench/pressure/reports/2026-03-28-verify-audit.md`

## Files

- Full v5 report: `.worktrees/pressure-test/bench/pressure/reports/2026-03-27-v5-pressure-test.md`
- Verify audit: `.worktrees/pressure-test/bench/pressure/reports/2026-03-28-verify-audit.md`
- Test projects: `.worktrees/pressure-test/bench/pressure/projects/`
- Results: `.worktrees/pressure-test/bench/pressure/results/`
- Planner test workdirs: `/tmp/planner-test-{humanize-10,tinydb-12,radash-15}/`
