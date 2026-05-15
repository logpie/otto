# Otto Field Tests

Small live-run scenarios for exercising Otto v5 decomposition behavior after
runner changes. These are not unit tests. They create fresh product repos,
run `otto v5 run` with Claude, then summarize shape, time, spend, verdict, and
boot-smoke results in a single markdown matrix.

The rig intentionally does not add Otto rules or classifiers. It uses the
runner as shipped.

## Running

Dry-run the matrix without live LLM calls:

```bash
uv run python scripts/run_field_tests.py --dry-run
```

Run one scenario:

```bash
OTTO_ALLOW_REAL_COST=1 uv run python scripts/run_field_tests.py --scenario 01-csv-json-cli
```

Run all scenarios serially:

```bash
OTTO_ALLOW_REAL_COST=1 uv run python scripts/run_field_tests.py
```

Run with scenario-level concurrency:

```bash
OTTO_ALLOW_REAL_COST=1 uv run python scripts/run_field_tests.py --parallel 2
```

Reports are written to `bench/field-tests/results-<timestamp>.md` unless
`--report-path` is provided. Fresh product repos are created under
`bench/field-tests/runs/<timestamp>/` and ignored by git.

## Scenario Format

Each scenario directory contains:

- `intent.md`: the product brief passed to Otto.
- `expected_shape.md`: expected decomposition behavior and what would be wrong.
- `success_criteria.md`: human criteria plus driver metadata.

The driver reads metadata from bullet lines in `success_criteria.md`:

```markdown
- kind: web
- budget_seconds: 1200
- max_parallel: 3
- tier: auto
- boot_smoke: true
- smoke_path: /
- smoke_port_var: PORT
```

`kind: cli` skips the boot-smoke test. Web/static scenarios should require a
root `start.sh` that honors `$PORT` for the user-facing HTTP surface. Full-stack
scenarios may also use `$API_PORT`, `$BACKEND_PORT`, `$FRONTEND_PORT`, and the
range variables exported by the driver.

## Port Allocation

Each scenario gets a non-overlapping port range. By default scenario `N` gets:

- `PORT` / `FRONTEND_PORT`: `19000 + N * 100`
- `API_PORT` / `BACKEND_PORT`: `19000 + N * 100 + 1`
- `FIELD_TEST_PORT_START`: range start
- `FIELD_TEST_PORT_END`: range end

Override with `--base-port` and `--port-stride` when needed.

## Interpreting Results

The report matrix is the first-pass signal:

- `Shape`: root inline vs emitted children vs nested decomposition.
- `Nodes` / `Depth`: tree size and hierarchy.
- `Wall`: elapsed driver time for the scenario.
- `Agent`: sum of `duration_s` from v5 lead summaries.
- `Cost`: provider-reported dollars collected after the run.
- `Verdict`: final root verdict from `task_graph.json`.
- `Boot`: `pass`, `fail`, or `skipped`.
- `Bugs / notes`: non-pass verdicts, CLI failures, missing artifacts, boot
  failures, and runner-visible failure reasons.

Use this matrix to decide which decomposition shapes should drive the next Otto
fix batch. A failed field test is useful when it clearly points to runner shape,
integration, branch, or generated-product issues.

## Adding A Scenario

1. Create `bench/field-tests/<number>-<slug>/`.
2. Add the three required markdown files.
3. Keep the product small enough for a 15 to 20 minute budget.
4. Make success criteria concrete enough that a human can run the output.
5. For web products, require `start.sh` and `$PORT`.
6. Run `uv run python scripts/run_field_tests.py --dry-run --scenario <slug>`
   to confirm discovery before spending real LLM budget.
