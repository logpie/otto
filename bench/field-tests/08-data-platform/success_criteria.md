# Success Criteria

Scenario metadata:

- kind: web
- budget_seconds: 2700
- max_parallel: 4
- tier: modular
- boot_smoke: true
- smoke_path: /
- smoke_port_var: PORT

The product is successful when:

- `./start.sh` runs ingestion+transform on the seed data, then serves a
  non-empty dashboard on `$PORT`.
- `python tests/run_acceptance.py` passes from a clean checkout:
  - all three connectors ingest their seeded sources;
  - validation rejects the seeded bad rows (rejected report present);
  - enrichment derives the documented fields;
  - aggregation per-category totals are correct;
  - the API serves the aggregates;
  - a second full run is idempotent (identical processed store).
- `CHARTER.md` documents both nested subtrees and shared contracts.

Graph-shape acceptance (the point of this scenario):

- Max task-graph depth >= 3.
- BOTH the ingestion task and the transformation task have
  `decomposition == emit`, each with their own grandchildren.
- Every grandchild branch reaches the correct parent integration (no
  cross-subtree leakage) and then `main`; no `merge_blocked` at root,
  no lost branches, no `unverified` leaves.
