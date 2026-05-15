# Field-Test Rig Research

Timestamp: 2026-05-15

## Objective

Build a reusable field-test rig that runs small real projects through `otto v5 run`
with Claude Sonnet, captures decomposition shape, wall time, agent time, cost,
verdict, and boot-smoke status, and writes one reviewable markdown matrix per
run. The rig must not change Otto's runner rules or classifiers.

## Existing Surfaces

- `otto v5 run` lives in `otto/cli_v5.py`. It accepts an intent string plus
  `--provider`, `--budget`, `--max-parallel`, `--tier`, and related options.
- `otto v5 run` resolves the current git worktree root and expects to run from
  a git repo. Fresh field-test projects therefore need `git init`, an initial
  commit, and at least `intent.md`/`otto.yaml` before launch.
- v5 run artifacts are written under `<project>/otto_logs/`.
  - Decomposition graph: `otto_logs/cross-sessions/task_graph.json`
  - Lead summaries: `otto_logs/sessions/**/summary.json`
  - Per-node verdicts: `otto_logs/sessions/**/verdict.json`
- `otto/queue/task_graph.py` stores parent-child relationships, task
  decomposition values, final verdicts, owned paths, and per-task cost.
- `otto/lead.py` summary files include `task_id`, `verdict`, `cost_usd`,
  `duration_s`, `decomposition`, `emitted_subtask_ids`, `verify_result`, and
  `failure_reason`. Summing `duration_s` across summaries is the best local
  proxy for agent-seconds.
- Existing benchmark scripts under `scripts/bench_*.py` write artifacts to
  `bench-results/`, use explicit real-cost opt-in via `scripts/real_cost_guard.py`,
  and preserve stdout logs. The field-test rig should reuse that safety pattern
  while producing its own matrix under `bench/field-tests/`.
- `otto.v5_clean_verify` already parses declared ports in `CHARTER.md` and
  `start.sh`; the field-test rig should still allocate a unique port range per
  scenario so generated apps do not collide during parallel runs.

## Constraints And Decisions

- No actual Otto field tests are run during this implementation.
- Real-cost execution should require `OTTO_ALLOW_REAL_COST=1`, matching existing
  benchmark guardrails.
- The driver should control only wall-clock budget. It should collect cost after
  the run, but it should not expose a new USD budget knob.
- Parallel scenario execution is safe only if each scenario gets a deterministic
  non-overlapping port range. The driver should export `PORT`,
  `FRONTEND_PORT`, `API_PORT`, `BACKEND_PORT`, and range metadata into both the
  Otto run environment and boot-smoke environment.
- Web scenarios should ask generated products to provide `start.sh` and honor
  `$PORT` for the user-facing HTTP surface. CLI scenarios skip boot-smoke.
- Generated project dirs can be large nested git repos. Keep them under
  `bench/field-tests/runs/` and ignore the contents with a local `.gitignore`.

## Open Questions

- Exact cost estimates for the new v6.6 v5 path require live runs. Use older
  small I2P/benchmark memory as a rough guide only, and label estimates as such.
- v5 currently still has a built-in `--tree-budget-usd` default in the CLI. The
  field-test driver will not set or expose it, but the underlying guard still
  exists in Otto.
