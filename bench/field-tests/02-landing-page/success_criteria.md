# Success Criteria

Scenario metadata:

- kind: web
- budget_seconds: 900
- max_parallel: 2
- tier: auto
- boot_smoke: true
- smoke_path: /
- smoke_port_var: PORT

The product is successful when:

- `./start.sh` serves the landing page on the supplied `$PORT`.
- `GET /` returns HTML containing "Loaf & Light".
- The note form validates empty fields and shows a confirmation after valid
  input.
- The page is responsive without horizontal overflow at 390 px and 1280 px.
- `python tests/run_acceptance.py` passes from a clean checkout.
