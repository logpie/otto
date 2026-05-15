# Field-Test Rig Plan

Timestamp: 2026-05-15

## Objective And Owned Files

Build a reusable field-test rig for small Otto v5 scenarios.

Owned files:
- `bench/field-tests/**`
- `scripts/run_field_tests.py`
- `tests/test_run_field_tests.py`
- `research-field-tests.md`
- `plan-field-tests.md`
- `review.md` for implementation-gate notes

Current worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`,
branch `cc-i2p-2`.

## Plan Gate

1. Add scenario directories for five shapes:
   - CSV to JSON CLI: expected inline/no decomp.
   - HTML/JS landing page: expected inline or tiny decomp.
   - TODO FastAPI + React: expected architect plus flat parallel leaves.
   - Mini CRM: expected architect plus vertical slices.
   - Blog generator: expected split ownership around parser/render/index-feed
     contracts.

   Verify: `find bench/field-tests -maxdepth 2 -type f` shows `intent.md`,
   `expected_shape.md`, and `success_criteria.md` for each scenario.

2. Add `scripts/run_field_tests.py` with:
   - Scenario discovery and metadata parsing from `success_criteria.md`.
   - Fresh git project creation per scenario.
   - `otto v5 run` invocation with `--provider claude` and scenario time budget.
   - Real-cost guard unless `--dry-run`.
   - Per-scenario stdout log, project metadata, task graph parsing, summary
     parsing, bug extraction, boot-smoke cleanup, and markdown report writer.
   - `--scenario NAME`, `--parallel N`, `--dry-run`, `--report-path`, and
     port-range options.

   Verify: focused unit tests cover metadata parsing, report rendering, dry-run
   behavior, and task graph summary aggregation without launching Otto.

3. Add `bench/field-tests/README.md` documenting:
   - How to add a scenario.
   - How to run one/all scenarios.
   - Real-cost opt-in.
   - Port allocation and `start.sh` contract.
   - How to interpret the report matrix.

   Verify: README includes `OTTO_ALLOW_REAL_COST=1`, `--scenario`,
   `--parallel`, `--dry-run`, and report path examples.

4. Run deterministic validation only:
   - `uv run pytest -q tests/test_run_field_tests.py`
   - `uv run ruff check scripts/run_field_tests.py tests/test_run_field_tests.py`
   - `uv run python scripts/run_field_tests.py --dry-run --report-path /tmp/otto-field-test-dry-run.md`

   Verify: commands pass and `/tmp/otto-field-test-dry-run.md` contains the
   scenario matrix with no live Otto runs.

## Risks And Verification Angles

- Risk: accidental live LLM spend. Mitigation: use `scripts.real_cost_guard` and
  make `--dry-run` the only mode that bypasses it.
- Risk: parallel boot tests collide. Mitigation: deterministic port stride and
  runtime intent note requiring `$PORT`.
- Risk: boot-smoke leaks processes. Mitigation: launch `start.sh` in its own
  process group and kill the process group in `finally`.
- Risk: report hides failures. Mitigation: surface non-pass verdicts, nonzero
  CLI exit, missing task graph, boot failures, and summary failure reasons in
  the `Bugs / notes` column.

## Review Trail

Plan Gate status: APPROVED locally via `/codex-gate` checklist. External Codex
MCP review is unavailable in this session; the available `codex-gate` skill is
a local checklist and explicitly says not to invoke unavailable reviewer tools.
