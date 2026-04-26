# Test Tier Hunter Notes

Findings integrated from the read-only test audit:

- Default pytest already excludes browser tests correctly with `-m "not browser" -p no:playwright`.
- No explicit fast/smoke/slow/integration markers existed, so day-to-day verification meant running the full 3+ minute non-browser suite.
- Browser README had stale commands that omitted `-m browser -p playwright`.
- Largest non-browser hot spots are `test_hardening.py`, `test_queue_runner.py`, `test_web_mission_control.py`, `test_merge_orchestrator.py`, `test_logstream.py`, and `test_v3_pipeline.py`.

Implemented:

- Added `smoke`, `fast`, `web`, `browser-smoke`, `integration`, `slow`, and `heavy` tier surfaces through `scripts/test_tiers.py`.
- Auto-marked smoke, integration, slow, and heavy tests during collection in `tests/conftest.py`.
- Kept default `uv run pytest` semantics unchanged: full non-browser suite still runs before broad merges.
- Updated README, AGENTS, package scripts, and browser README with the new commands.

Measured after changes:

- `smoke`: 217 tests in 12.74s.
- `fast`: 670 tests in 61.93s.
- `web`: 129 tests in 28.90s.
- full default: 1193 tests in 189.14s.
