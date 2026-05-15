# Success Criteria

Scenario metadata:

- kind: cli
- budget_seconds: 2400
- max_parallel: 4
- tier: modular
- boot_smoke: false
- smoke_port_var: PORT

The product is successful when:

- `./start.sh` runs the acceptance suite and prints a PASS summary.
- `python tests/run_acceptance.py` passes from a clean checkout:
  - `let x = 3; let y = x * (2 + 4); y / 3` evaluates to `6`.
  - `calc tokens` / `calc ast` emit the documented token/AST shapes.
  - a lexical-error program yields the lexical diagnostic code + location.
  - a use-before-`let` program yields the semantic diagnostic + location.
  - a malformed-syntax program yields the syntax diagnostic + location.
  - crash count across all error programs is zero (diagnostics, not throws).
- `CHARTER.md` documents the four-stage ownership and nested subtree.

Graph-shape acceptance (the point of this scenario):

- Max task-graph depth >= 3 (root -> analysis-engine -> stage leaves).
- The analysis-engine task has `decomposition == emit` with grandchildren.
- Every grandchild branch reaches engine integration then `main`; no
  `merge_blocked` at root, no lost branches, no `unverified` leaves.
