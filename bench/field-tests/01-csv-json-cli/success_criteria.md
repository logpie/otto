# Success Criteria

Scenario metadata:

- kind: cli
- budget_seconds: 900
- max_parallel: 2
- tier: solo
- boot_smoke: false

The product is successful when:

- `python csv_to_json.py sample.csv sample.json` writes valid JSON.
- Blank cells become `null`.
- `--pretty` indents the JSON array.
- `--ndjson` writes one JSON object per line.
- `python tests/run_acceptance.py` passes from a clean checkout.
- User errors return nonzero exit codes without raw Python tracebacks.
