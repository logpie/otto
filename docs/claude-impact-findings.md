# Claude-Impact Findings

Living document for bugs and behavioral findings discovered while hardening Otto for other providers, but which also affect or would affect Claude-based Otto runs.

Last updated: 2026-04-03

Current fixing commit on `feat/otto-codex-support`:
- `4360b24` — Add Codex provider support and harden batch observability
- `d9200d5` — Add planner batch unit schema groundwork
- `424d614` — Make orchestrator unit-aware for batch execution
- `0f908f6` — Add integrated unit execution and retry plumbing
- `b0f0331` — Implement semantic grouping with planner units
- `d7e7272` — Harden multi-blog-engine verifier assumptions
- `d2f4859` — Bias planner toward integrated layered units
- `fb8cbac` — Cap integrated units to small layered groups
- `a742e93` — Fix planner, QA, and benchmark observability
- `HEAD (working tree)` — QA replay robustness for bare-output certification
- `HEAD (working tree)` — Decouple proof-of-work from merge gating, add fixed-plan benchmarking, and tighten QA evidence reuse/profiling

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

### 7. Post-plan integrated-unit capping could rewrite planner intent

Status: fixed in commit `a742e93`

What was wrong:
- planner output was post-processed by a hard size cap that split integrated
  units after planning
- that could erase dependency staging the planner had just reasoned about
- on the real `multi-expense-tracker` grouped run, all task pairs were logged
  as `DEPENDENT`, but the final structure still ran `{task1, task2}` beside
  `{task3}` in the same batch because the post-pass cap rewrote the unit shape

Why this affects Claude:
- entirely provider-agnostic planner/orchestrator behavior
- Claude runs using the same semantic-grouping path would inherit the same
  bad execution structure and the same wasted retry/conflict work

Fix:
- removed hard post-plan integrated-unit splitting
- planner now owns unit boundaries again
- post-processing may still serialize units into later batches for
  dependency/uncertainty reasons, but it does not rewrite unit composition

### 8. Planner default reasoning effort was still too low for batching decisions

Status: fixed in commit `a742e93`

What was wrong:
- normal runs were still using `planner_effort: medium` by default
- with semantic grouping now affecting unit formation and checkpoint placement,
  that default was too weak and increased plan variance on the same inputs

Why this affects Claude:
- the batching decision is a shared provider-agnostic orchestration decision
- a low-effort planner can make unstable decomposition choices for Claude too

Fix:
- default planner effort is now `high`

### 9. QA could crash after doing the real work because of a logging shadow bug

Status: fixed in commit `a742e93`

What was wrong:
- `otto/qa.py` imported `append_text_log` at module scope
- `_run_qa_prompt()` also re-imported it inside the function
- that made `append_text_log` a local variable in Python, so earlier uses in
  the same function could fail with:
  `cannot access local variable 'append_text_log' where it is not associated with a value`
- real effect: the task implementation and verification could be correct, but
  Otto would still fail the task on an internal QA crash

Why this affects Claude:
- entirely provider-agnostic QA path
- any Claude task reaching that path could fail even after correct coding/test work

Fix:
- removed the local re-import and used the module-level binding consistently

### 10. Integrated-unit/member-task observability was too sparse

Status: fixed in commit `a742e93`

What was wrong:
- synthetic integrated units wrote a rich `task-summary.json` and `live-state.json`
- but the expanded member tasks only got sparse merge-status stubs
- benchmark copies also dropped `otto_logs/`, so postmortem debugging often
  lacked the evidence needed to explain a run from artifacts alone

Why this affects Claude:
- same orchestrator path
- same task-level observability gap

Fix:
- member task live-state/summary now carry duration, cost availability,
  phase timings, and attempts when expanded from an integrated unit
- benchmark harness now preserves `otto_logs/` and other run artifacts

### 11. Coding-agent exceptions could lose the partial attempt transcript

Status: fixed in commit `a742e93`

What was wrong:
- if the coding-agent/query path threw mid-stream, Otto could fail with only a
  terse phase error and no persisted partial `attempt-N-agent.log`
- this made provider/runtime failures hard to debug from copied artifacts

Why this affects Claude:
- same coding-agent wrapper path
- same missing transcript problem on Claude failures

Fix:
- partial coding-agent transcript is now persisted even when the stream/query throws

### 12. `proof_of_work` was coupled to merge-gating QA policy

Status: fixed in working tree

What was wrong:
- `proof_of_work` was intended as audit/reporting metadata
- but the flag changed the actual QA prompt and therefore changed merge-gating behavior
- that meant the same task could take different retry paths or batch-QA shapes depending on a reporting flag

Why this affects Claude:
- completely provider-agnostic
- any Claude-backed Otto run using the same QA path would inherit the same hidden control-flow coupling

Fix:
- `proof_of_work` is now metadata only
- merge-gating QA prompt/contract is identical for `true` and `false`
- the flag remains in `qa-profile.json` and logs for audit/profiling only

### 13. Benchmark comparisons needed a fixed-plan mode

Status: fixed in working tree

What was wrong:
- planner variance was large enough that A/B timing comparisons could flip between:
  - integrated `2-batch` shapes
  - serialized `3-batch` shapes
- this made provider/QA-policy comparisons noisy and easy to misread

Why this affects Claude:
- same planner/orchestrator path
- Claude comparisons are just as vulnerable to false conclusions if plan shape drifts between runs

Fix:
- added `fixed_plan` config support
- supports deterministic batch/unit layouts by `task_ids` or `task_keys`
- benchmark runs can now compare two settings under the exact same execution plan

### 14. Batch-QA cost was dominated by executable certification, not by the proof-of-work flag

Status: characterized in working tree

What we found:
- after decoupling `proof_of_work`, fixed-plan blog runs showed near-identical runtime with the flag on/off
- the real cost center remained post-merge batch QA itself:
  - grouped executable probes
  - integration probes
  - verdict synthesis overhead

Why this affects Claude:
- provider-agnostic finding about Otto’s QA contract
- Claude runs would pay the same kind of certification cost under the same batch-QA design

Current mitigation:
- QA prompt now biases toward reusing existing repo tests first
- prompts explicitly prefer existing full-stack/shared-boundary tests before inventing new integration probes
- prompts also push for compact verdict payloads to reduce verdict-synthesis overhead

Current best-known prompt shape:
- the “compact/reuse” variant improved fixed-plan blog runtime materially
- a later “small probe” prompt variant regressed by causing more bespoke probe churn and a batch-QA retry, so that change was intentionally rolled back

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

### 12. Task decomposition can trade away useful whole-system context

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

Follow-up evidence from real runs:
- grouping an entire 3-task layered chain into one integrated unit was too coarse on `multi-blog-engine`
- grouping the first 2 tightly layered tasks and leaving the 3rd as its own unit performed much better
- the same pair-grouping policy also looked reasonable on `multi-expense-tracker`

Current practical recommendation:
- do not hard-cap unit size mechanically after planning
- let the planner choose one batch / one unit when holistic execution is truly the better shape
- still teach the planner to think about retry coarseness and checkpoint value before grouping 3+ tasks together

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

### Clean single-task Otto vs bare Codex comparison

Scenario:
- `bench/pressure/projects/real-citty-feature`

Observed:
- bare Codex benchmark run: external verify PASS in about `151s`
- Otto using Codex provider: external verify PASS in about `377s`
- this is a clean pair where both runners passed the same external verifier

Claude relevance:
- reminds reviewers that single-task clean repos can still favor the bare model
  on speed even when Otto's harness is functioning correctly
- the right question is not just "did Otto pass", but whether its extra control
  bought anything over the direct model path on that task shape

### Direct replay of Otto's spec/QA bar on bare-Codex outputs

Scenario:
- fresh bare-Codex benchmark outputs were preserved via benchmark snapshots
- Otto's own generated task specs were then replayed against those exact outputs
  without rerunning implementation

Observed:
- `real-citty-feature`
  - bare Codex external verify: PASS
  - Otto spec/QA replay on bare output: PASS (`must_passed=true`) on Claude path
- `real-semver-bugfix`
  - bare Codex external verify: PASS
  - Otto spec/QA replay on bare output: PASS (`must_passed=true`) on Claude path

Interpretation:
- these fresh replays do not support the theory that Otto's spec bar is
  inherently stricter than bare Codex's output quality on these clean
  single-task real-repo cases
- at least for these tasks, bare Codex appears able to satisfy Otto's own
  acceptance criteria when evaluated against the same output artifact
- the bigger issue was Otto QA robustness, not spec strictness

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
7. Planner variance: measure whether repeated planner calls on the same task set
   still flip between serial and grouped shapes even at `planner_effort=high`.
8. Direct replay question: on fresh bare-Codex outputs, run Otto's own
   spec/QA layer against the produced code and compare that verdict to
   external `verify.sh`.

## Planned Direction

Current next-step design work:
- semantic grouping inside batches
- planner emits batches made of execution units
- singleton unit = current per-task execution
- multi-task unit = one integrated coding pass

Design note:
- see [`docs/plans/2026-04-01-semantic-grouping.md`](plans/2026-04-01-semantic-grouping.md)
- comparison note:
  - [`docs/codex-vs-otto-comparison-2026-04-01.md`](codex-vs-otto-comparison-2026-04-01.md)

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
5. Do copied benchmark artifacts now contain enough evidence to debug a failure
   from logs alone without needing the original `/tmp` workdir?
