# CSV to JSON CLI

Build a small Python command-line tool that converts CSV files to JSON.

Required behavior:

- Provide an executable CLI entry point: `python csv_to_json.py input.csv output.json`.
- Treat the first CSV row as headers.
- Write a JSON array of objects, preserving column names exactly.
- Support `--pretty` for indented JSON.
- Support `--ndjson` to write one JSON object per line instead of an array.
- Convert blank CSV cells to `null`.
- Keep all nonblank values as strings. Do not guess numbers or dates.
- Print a concise success message with row count and destination path.
- Return a nonzero exit code with a friendly error for missing input, malformed
  CSV, or unwritable output.
- Include a small `tests/run_acceptance.py` that creates sample CSV files and
  verifies array JSON, pretty JSON, NDJSON, blank cells, and error handling.

This is a CLI product. Do not start a web server and do not require external
services.
