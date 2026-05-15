# Success Criteria

Scenario metadata:

- kind: web
- budget_seconds: 1200
- max_parallel: 3
- tier: auto
- boot_smoke: true
- smoke_path: /
- smoke_port_var: PORT

The product is successful when:

- `./start.sh` serves the CRM UI on `$PORT`.
- Users can create companies, contacts, and deals.
- Contacts and deals link to companies.
- Deal stage filtering works.
- Dashboard totals update from persisted deal data.
- `python tests/run_acceptance.py` passes from a clean checkout.
