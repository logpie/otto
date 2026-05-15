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

- `python -m blog build` or `python build_site.py` generates all required
  files under `output/`.
- Posts are ordered newest-first.
- Tag pages list the correct posts.
- RSS parses as XML and includes all seeded posts.
- `search.json` contains all posts with the required fields.
- `./start.sh` serves the generated site on `$PORT`.
- `python tests/run_acceptance.py` passes from a clean checkout.
