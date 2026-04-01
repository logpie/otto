# Claude-Impact Findings

Living document for bugs and behavioral findings discovered while hardening Otto for other providers, but which also affect or would affect Claude-based Otto runs.

Last updated: 2026-03-31

## Purpose

This is not a Codex-specific bug list.

It records:
- provider-agnostic Otto bugs found during Codex validation
- observability gaps that also matter for Claude runs
- cleanup items where Claude remains the default/primary path but the abstraction work exposed weak assumptions

Use this as a handoff/reference doc when asking Claude to review or continue work.

## Confirmed Claude-Impact Bugs

### 1. Per-task `live-state.json` was stale after merge

Status: fixed on `feat/otto-codex-support`

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

Status: fixed on `feat/otto-codex-support`

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

Status: fixed on `feat/otto-codex-support`

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

Status: improved on `feat/otto-codex-support`

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

Status: fixed on `feat/otto-codex-support`

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

Status: fixed on `feat/otto-codex-support`

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

## Recommended Next Claude Review Questions

If continuing from this doc, ask Claude to check:

1. Do any remaining merge/batch finalization paths still bypass live-state/task-summary refresh?
2. Are there any other telemetry events visible in display/legacy logs but still absent from `v4_events.jsonl`?
3. Should `v4_events.jsonl` be renamed now, or only after all readers/tests are migrated?
4. Are there remaining Claude-only assumptions in prompts/config that should be split from generic provider behavior?
