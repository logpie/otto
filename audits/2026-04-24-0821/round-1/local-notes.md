# Local Notes

## Commands

- `find otto/mission_control otto/web tests -type f | wc -l` -> `197`
- `rg` sweep for TODO/FIXME/NotImplemented/sloppy test assertions over Mission Control, web, and touched tests.
- `npm run web:typecheck` -> passed
- `npm run web:build` -> passed
- `uv run pytest -x -q` -> `922 passed, 18 deselected in 102.74s`
- `uv run pytest -q --maxfail=10` -> `922 passed, 18 deselected in 103.19s`
- `git diff --check` -> passed

## Manual Product Checks

- `agent-browser` was used against live web servers on existing repo, greenfield API, and failure-lab fixtures.
- Direct CLI parity checks confirmed queue state after web removal.
- Temporary web servers and browser sessions were stopped after E2E checks.
