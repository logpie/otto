# Local Data Platform

Build a small local data platform: ingest records from multiple sources,
run them through a transformation pipeline, and serve the results.

This is a FORCED-RECURSION field-test scenario with TWO independently
multi-subsystem children, so recursion must happen on more than one branch
in parallel. This is the hardest concurrency case: two separate nested
subtrees integrating at the same time under the post-hardening code.

## Top-level shape (must be honored)

- A shared-contracts/scaffold child: the canonical record schema, the
  pipeline stage interface, storage choice, port conventions, `CHARTER.md`.
- An **ingestion subsystem** child that is itself multi-source and MUST emit
  a nested subtree — one leaf per source connector below.
- A **transformation subsystem** child that is itself multi-stage and MUST
  emit a nested subtree — one leaf per transform stage below.
- A serving child: an HTTP API + a minimal dashboard page that reads the
  final processed store.

## Ingestion subsystem — three independent connectors (each a nested leaf)

1. **CSV connector**: read a seeded `data/*.csv` into canonical records.
2. **JSONL connector**: read a seeded `data/*.jsonl` into canonical records.
3. **Synthetic connector**: deterministically generate N canonical records
   from a seed (for load/idempotency tests).

Each connector normalizes to the shared record schema and is independently
testable. One inline leaf must not own all three.

## Transformation subsystem — three independent stages (each a nested leaf)

1. **Validation stage**: drop/flag records failing the schema; emit a
   rejected-records report.
2. **Enrichment stage**: derive fields (e.g. normalized timestamps, a
   derived category) per documented rules.
3. **Aggregation stage**: produce per-category counts and totals into the
   final processed store the serving layer reads.

Stages are ordered (validate -> enrich -> aggregate) via the shared stage
interface; no stage imports another stage's internals.

## Required deliverables

- `start.sh` at repo root serving the dashboard on `$PORT`; API on
  `$API_PORT`/`$BACKEND_PORT` if separate. It must run ingestion+transform
  once on the seed data before serving so the dashboard is non-empty.
- Persist in SQLite or local JSON. No external services.
- `tests/run_acceptance.py` that, from a clean checkout: ingests all three
  sources, runs the full pipeline, asserts validation rejects the seeded
  bad rows, enrichment derives the documented fields, aggregation totals
  are correct, the API serves the aggregates, and a second full run is
  idempotent (same processed store).
- `CHARTER.md` documenting both nested subtrees and the shared contracts.

Keep each connector/stage deliberately small but real.
