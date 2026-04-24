# Mission Control Production Validation Matrix

Goal: validate Otto Web Mission Control as a task-management product, not just
a status dashboard.

## Project Fixtures

1. `kanban-real`: existing FastAPI mini-kanban project with prior Codex and
   Claude work, merged branches, and real app tests.
2. `otto-copy`: local clone of the Otto repo for a larger existing codebase.
3. `greenfield-api`: new FastAPI service repo created for this pass.
4. `dirty-blocked`: repo with ready work plus local tracked changes.
5. `failure-lab`: repo seeded with failed, interrupted, stale, queued, and
   terminal records.

## Web Workflows

- Intake: queue build/improve/certify, validate missing intent, provider/model
  effort inheritance, dependencies, explicit task ids.
- Watcher: start, stop with confirmation, restart, observe active counts.
- Live/detail: select rows, stream logs, inspect artifacts, provider/model/effort,
  branch/worktree, overlay reasons.
- Failure/recovery: failed row, stale row, retry/requeue, cleanup, disabled
  cancel/resume states.
- Merge: merge one, merge all, already merged, no ready tasks, dirty blocked,
  branch collision warning, clear post-merge state.
- Filtering: type, outcome, active only, search, clear filters.

## CLI Parity

- `otto queue ls/show/rm/resume/cancel/cleanup/run`
- `otto merge --fast --no-certify <task>`
- `otto merge --fast --no-certify --all`
- Web state agrees with CLI state after each operation.

## Provider Coverage

- Codex: at least one real queued LLM build/certify path.
- Claude: at least one real queued LLM build/certify path when credentials allow.
- Deterministic provider-independent merge/recovery scenarios for broad coverage.

## Pass/Fail Rule

Any CRITICAL or IMPORTANT product, correctness, data-loss, cancellation, merge,
or evidence-integrity issue must be fixed and rerun. NOTE/MINOR product gaps are
recorded as deferred only after triage.

## Completion Status

| Fixture | Status | Coverage |
| --- | --- | --- |
| `greenfield-api` | PASS | Real Codex and Claude queue build/certify/merge flows from the web portal. |
| `failure-lab` | PASS | Dirty merge blocker, collision warning, failed row requeue affordance, queued rows, landing/live/detail sync. |
| `otto-copy` | PASS | Existing large repo intake validation, provider/effort queue form, filters, detail artifacts, queued task removal, watcher start/stop, CLI parity. |
| `kanban-real` | PASS from prior production audit | Real FastAPI mini-kanban Codex and Claude web flows, merge, artifact/log inspection, dirty merge block. |
| `dirty-blocked` | PASS through `failure-lab` and prior kanban audit | Tracked local edits disabled merge and produced explicit dirty path messaging. |
