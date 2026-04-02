# Otto Working Memory

## Product Direction

- Planner should own batching intelligence.
- Do not hard-cap integrated unit size mechanically after planning.
- Single-batch execution is a valid first-class planner outcome.
- `unit` is the execution granularity.
- `batch` is still the checkpoint / merge / QA / rollback boundary.

## Key Lessons

- Bare Codex can outperform Otto on tightly layered single-feature work because it preserves one holistic context.
- Otto adds value most clearly on conflict-heavy or recovery-heavy work.
- Otto's internal spec/QA bar can be stricter than external `verify.sh`.
- `verify.sh` is useful but not perfect; treat it as one signal, not absolute truth.

## Workflow Bugs Found And Fixed

- `a742e93` — planner, QA, runner, orchestrator, and benchmark observability fixes
- Removed post-plan integrated-unit splitting that broke dependency staging.
- Raised default planner effort to `high`.
- Fixed QA crash caused by local `append_text_log` shadowing in `otto/qa.py`.
- Improved integrated-unit/member-task live-state propagation.
- Persist partial coding-agent logs when the coding query path throws.

## Benchmark Harness Bugs Found And Fixed

- Added `bare-codex` runner to `bench/pressure/bench.sh`.
- Fixed benchmark workdir collisions by namespacing `/tmp` workdirs by label.
- Fixed malformed Otto benchmark cost parsing.
- Fixed brittle status counting in benchmark result parsing.
- Added setup retry for transient setup/network failures.
- Preserve `otto_logs/`, `otto.yaml`, diffs, and a worktree snapshot in benchmark results.
- Added `run_exit` to per-project `result.json`.
- Added `run-meta.json` for benchmark metadata.

## Clean Benchmark Signals So Far

- `real-citty-feature`
  - bare Codex: verify PASS, 151s
  - Otto on Codex: verify PASS, 377s
- `real-semver-bugfix`
  - bare Codex: verify PASS, 89s
  - Otto on Codex: FAIL, but due to an Otto/Codex-streaming-path failure (`Separator is not found, and chunk exceed the limit`), not a clean feature miss
- `real-radash-feature`
  - bare Codex: verify PASS, 106s
- `real-semver-bugfix`
  - bare Codex: verify PASS, 89s

## Open Problems

- Planner variance is still real, even at `planner_effort=high`.
- Otto can still fail on some runs due to infrastructure/streaming issues rather than coding quality.
- Observability bar: should be able to debug a failed run from copied benchmark artifacts alone.

## Benchmark Rules

- Keep Otto in Otto's normal workflow for Otto benchmarks.
- Fix clear verifier bugs if found, but do not bias `verify.sh` toward the current implementation.
- Prefer comparing multiple signals:
  - external verifier
  - Otto QA/spec verdict
  - human judgment when needed
