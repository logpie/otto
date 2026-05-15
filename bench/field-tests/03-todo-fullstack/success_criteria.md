# Success Criteria

Scenario metadata:

- kind: web
- budget_seconds: 1200
- max_parallel: 3
- tier: lead
- boot_smoke: true
- smoke_path: /
- smoke_port_var: PORT

The product is successful when:

- `./start.sh` serves the user-facing UI on `$PORT`.
- `GET /` returns the TODO app shell.
- API CRUD works and persists to a local JSON file.
- Completing, filtering, and deleting todos are reachable from the UI.
- `python tests/run_acceptance.py` passes from a clean checkout.
