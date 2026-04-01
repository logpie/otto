# Claude-Impact Findings

Living document for bugs and behavioral findings discovered while hardening Otto for other providers, but which also affect or would affect Claude-based Otto runs.

Last updated: 2026-03-31

Current fixing commit on `feat/otto-codex-support`:
- `4360b24` — Add Codex provider support and harden batch observability

## Purpose

This is not a Codex-specific bug list.

It records:
- provider-agnostic Otto bugs found during Codex validation
- observability gaps that also matter for Claude runs
- cleanup items where Claude remains the default/primary path but the abstraction work exposed weak assumptions

Use this as a handoff/reference doc when asking Claude to review or continue work.

## Confirmed Claude-Impact Bugs

### 1. Per-task `live-state.json` was stale after merge

Status: fixed in commit `4360b24`

What was wrong:
- Task runner wrote final `live-state.json` at `verified`
- Orchestrator merge phase updated task status in `tasks.yaml`
- But `otto_logs/<task>/live-state.json` stayed at `verified` with `merge: pending`

Why this affects Claude:
- This is independent of provider
- Any Claude run that reaches merge through the same orchestrator path would leave stale live task state
- `otto status -w` and any tooling reading per-task live-state could show misleading status

Fix:
- Merge/failure paths now refresh per-task `live-state.json`
- Merge phase also updates `merge.status`

### 2. Per-task `task-summary.json` was stale after merge

Status: fixed in commit `4360b24`

What was wrong:
- `task-summary.json` was written by task execution at `verified`
- Batch/per-task merge finalization could move the task to `passed`/`merged`
- But the summary file was not updated accordingly

Why this affects Claude:
- Same orchestrator path
- Same stale summary problem regardless of provider

Fix:
- Merge/failure finalization now refreshes `task-summary.json` status alongside live-state

### 3. `v4_events.jsonl` was missing `agent_tool` events

Status: fixed in commit `4360b24`

What was wrong:
- Live display and legacy `pilot_results.jsonl` received tool events
- `v4_events.jsonl` did not
- The newer telemetry stream therefore lacked exactly the tool-level trace needed for debugging

Why this affects Claude:
- Claude emits richer tool events than Codex
- Missing them from `v4_events.jsonl` was arguably worse for Claude because that path had more structured signal available

Fix:
- `agent_tool` events are now emitted into `v4_events.jsonl` as `AgentToolCall`

### 4. Greenfield auto-ignore heuristics were too weak

Status: improved in commit `4360b24`

What was wrong:
- `.git/info/exclude` only added framework ignores after detecting manifests like `pyproject.toml`
- Very small greenfield repos could finish runs with untracked `__pycache__/`, `.pytest_cache/`, etc.

Why this affects Claude:
- Same preflight path
- Same dirty temp repo artifacts in provider-agnostic runs

Fix:
- Added simple language heuristics for Python/JS repos before manifests exist
- Python repos now auto-ignore `__pycache__/`, `.pytest_cache/`, `.venv/`

### 5. Batch QA could leave stale or missing per-task QA artifacts

Status: fixed in commit `4360b24`

What was wrong:
- In batch mode, shared QA artifacts lived under `otto_logs/batch-qa-*`
- When a batch contained only one merged task, or when batch-QA retry re-verified
  only one failed task, `run_qa()` fell through its single-task artifact path
  and did not sync the final QA report/proof back into that task's own log dir
- Result: task status could end `passed` while the task-local proof report was
  stale, failed, or missing

Why this affects Claude:
- Entirely provider-agnostic
- This is a shared batch-QA / retry bookkeeping bug
- Claude runs using multi-batch execution or batch-QA retry would hit the same
  stale audit trail problem

Fix:
- Added batch-context artifact syncing even for single-task batch/retry runs
- Per-task `qa-report.md`, `qa-verdict.json`, and `qa-proofs/proof-report.md`
  are now refreshed from the final batch QA result

### 6. Proof reports assumed cost was always available

Status: fixed in commit `4360b24`

What was wrong:
- Proof report headers always rendered `QA $X.XX`
- That was misleading for providers without exact cost data

Why this affects Claude:
- Claude itself still exposes real cost today, so this was not normally visible
  on the Claude path
- But the proof artifact format was provider-assumption-heavy rather than
  provider-agnostic, which is still a generic bug in the reporting layer

Fix:
- Proof reports now render `QA cost unavailable` when cost is not measurable

## Important Notes, Not Strictly Bugs

### 5. Telemetry file is still named `v4_events.jsonl`

Status: still true

Notes:
- The code still uses `otto_logs/v4_events.jsonl`
- Tests still assert that filename
- If the project intended to rename this to a provider-neutral or version-neutral filename, that rename has not actually landed

Why this matters for Claude:
- Mostly naming/debt, not runtime correctness
- But it creates confusion when discussing “current” Otto behavior with Claude reviewers

### 6. Claude-oriented vocabulary still exists in docs/comments

Status: partially cleaned up, not finished

Notes:
- Some misleading runtime/UI wording was updated during Codex hardening
- But many comments/docs still refer to Claude defaults, `CLAUDE.md`, or Claude-specific assumptions

Why this matters for Claude:
- Some of this wording is still correct because Claude remains a real provider path
- But mixed generic/provider-specific language makes maintenance harder and invites wrong abstractions

### 7. Task decomposition can trade away useful whole-system context

Status: open product/design finding

What we observed:
- In the serious `multi-blog-engine` comparison, Otto executed the benchmark as
  3 pre-defined tasks (data layer → service layer → CLI), while bare Codex got
  one monolithic prompt containing all 3 layers together.
- The task-3 CLI layer initially missed an empty-file persistence edge case that
  bare Codex handled in its one-shot implementation.

Why this matters for Claude too:
- This is not a Codex-only phenomenon. It is a harness/decomposition effect.
- Any provider can lose valuable global context when later-layer tasks are split
  away from the earlier product shape.
- For layered product-building tasks, decomposition can improve control while
  simultaneously making the model think too locally.

Practical implication:
- Otto should not assume that "more decomposition" is always better.
- For some prompt shapes, especially strongly layered work with one coherent
  product surface, a single integrated coding pass may outperform staged task
  execution on first-try quality.

Suggested direction:
- Add a planner mode that can classify some task sets as "keep integrated"
  instead of always splitting execution into narrowly scoped later-layer tasks.
- Alternatively, pass a stronger holistic context packet from earlier tasks into
  later tasks so the model retains whole-system intent, not just local diffs.

Working vocabulary:
- `task` = the user-visible unit of intent
- `batch` = the set of work Otto advances together before the next global decision point
- recommended batch definition:
  "the set of work that Otto chooses to advance together before the next global decision point"

Current design recommendation:
- keep `batch` as the orchestrator unit
- do not introduce a new top-level "execution unit" concept
- if integrated execution is added later, prefer a small batch attribute such as:
  - `execution_style: separate | integrated`
  rather than inventing more nouns

Reason:
- `batch` already matches Otto's merge / batch-QA / rollback / replan behavior
- the confusion comes from overloading batch semantics, not from the `batch` concept itself

## Real-World Validation That Matters For Claude Too

These scenarios were exercised with Codex, but they validate provider-agnostic Otto paths that Claude also uses:

### Multi-batch dependency execution

Scenario:
- `bench/pressure/projects/multi-blog-engine`

Observed:
- Planned into 3 sequential batches
- Each batch merged, then ran batch QA
- Final batch hit batch-QA failure, retried task 3, re-merged, and passed

Claude relevance:
- Validates planner → batch execution → merge → batch QA → batch-QA retry path

### Parallel same-file conflict handling

Scenario:
- `bench/pressure/projects/edge-conflicting-tasks`

Observed:
- Planned into 1 parallel batch with 3 tasks
- Merged 1 task cleanly
- 2 tasks hit merge conflicts and were queued for re-apply on updated `main`
- Re-applied tasks ran successfully
- Batch QA later failed one task, triggered targeted batch-QA retry, reran that task, and passed

Claude relevance:
- Validates parallel worktrees, merge conflict handling, re-apply, post-batch integration gate, and batch-QA retry path

## Things That Were Codex-Specific And Should Not Be Regressed Back Into Claude

These are here so future reviewers do not misclassify them:

- Codex CLI retry/resume incompatibility
- Codex token usage capture
- Codex dollar cost unavailable semantics
- Codex event normalization from CLI JSON into Otto blocks

These are not Claude bugs, though parts of the surrounding observability work touched shared code.

## Open Watchlist

These are not confirmed Claude bugs today, but Claude should sanity-check them when continuing work:

1. `v4_events.jsonl` naming: decide whether to keep it or rename it everywhere.
2. Provider-neutral artifact semantics: verify which comments/docs should remain Claude-specific versus generic.
3. Legacy `pilot_results.jsonl` path: keep watching for duplicate event writes whenever telemetry bridging changes.
4. Greenfield repo cleanliness: confirm there are no remaining nested artifact directories escaping `.git/info/exclude`.
5. Batch QA audit trail: confirm per-task proof/report artifacts stay correct after any future retry/refactor.
6. Decomposition policy: identify which prompt/task shapes should remain integrated rather than being split into narrow sequential layers.

## Planned Direction

Current next-step design work:
- semantic grouping inside batches
- planner emits batches made of execution units
- singleton unit = current per-task execution
- multi-task unit = one integrated coding pass

Design note:
- see [`docs/plans/2026-04-01-semantic-grouping.md`](plans/2026-04-01-semantic-grouping.md)

Rationale:
- preserve `batch` as the top-level orchestrator concept
- avoid inventing more public-facing workflow concepts than necessary
- allow tightly layered tasks to keep whole-system context
- keep conflict-heavy tasks on the current separated path

## Recommended Next Claude Review Questions

If continuing from this doc, ask Claude to check:

1. Do any remaining merge/batch finalization paths still bypass live-state/task-summary refresh?
2. Are there any other telemetry events visible in display/legacy logs but still absent from `v4_events.jsonl`?
3. Should `v4_events.jsonl` be renamed now, or only after all readers/tests are migrated?
4. Are there remaining Claude-only assumptions in prompts/config that should be split from generic provider behavior?
