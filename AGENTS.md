# Repository Guidelines

## Project Structure & Module Organization

Otto is a Python project with a React/TypeScript web client. Core Python code lives in `otto/`; queue, merge, mission-control, provider, and certifier behavior are split by module. The web client source is in `otto/web/client/src/` and builds into `otto/web/static/`. Tests live in `tests/`. Operational notes and audits live in `docs/`, `audits/`, and `DEBUG.md`.

## Build, Test, and Development Commands

- `uv run python scripts/test_tiers.py smoke`: smallest fast confidence gate.
- `uv run python scripts/test_tiers.py fast`: day-to-day non-browser gate, excluding slow/process/integration/heavy system tests.
- `uv run python scripts/test_tiers.py web`: TypeScript plus Mission Control backend/model tests.
- `uv run pytest -q --maxfail=10`: full default non-browser Python suite; use before broad merges.
- `uv run ruff check otto scripts tests`: lint Python code.
- `npm run web:typecheck`: type-check the web client.
- `npm run web:build`: build the static web bundle.
- `.venv/bin/python3 -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open`: launch Mission Control locally.

## Coding Style & Naming Conventions

Follow existing module patterns before adding abstractions. Keep Python typed where surrounding code is typed. Use clear queue/run/status names, and preserve immutable queue task definitions. Web code should stay TypeScript-first, with UI state represented in typed API shapes from `types.ts`.

## Testing Guidelines

Add focused regression tests for every behavioral fix. Use the smallest test tier while iterating, then escalate before merge: smoke for low-risk Python edits, `test_tiers.py fast` for ordinary changes, `test_tiers.py web` for Mission Control backend/client changes, full pytest for broad infra changes, and browser-level user flows for interactive UI behavior. Do not rely only on screenshots or API checks for interactive UI bugs.

## Debugging Policy

Use direct fixes for obvious compiler, lint, typo, or simple UI polish issues. Use lightweight reproduce-inspect-fix-test for ordinary bugs. Use the full `debug-hypothesis` workflow only for ambiguous, stateful, flaky, process/runtime, queue/resume/merge, browser, persistence, performance, or repeated-failure bugs.

## Commit & Pull Request Guidelines

Keep commits scoped and describe the user-visible behavior fixed or added. Include tests run in PR notes. Do not merge or push `main` unless explicitly requested. Work in the active worktree and preserve unrelated user changes.

## Agent-Specific Instructions

Web Mission Control is the primary product surface. Do not revive deprecated TUI work except where needed to keep existing CLI/queue behavior correct. For `$code-health`, use parallel subagents and multiple rounds unless explicitly told otherwise.
