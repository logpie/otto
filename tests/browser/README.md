# Mission Control browser tests

Playwright-driven pytest suite covering the Mission Control web UI end-to-end
against the real built bundle. Phase 3 of `plan-mc-audit.md`.

## Quick start

```bash
# from repo root
uv pip install -e ".[dev]"            # installs pytest-playwright + playwright + freezegun
uv run playwright install chromium webkit
uv run pytest tests/browser/ -m browser -p playwright -v
```

The first run verifies the SPA bundle (`npm run web:verify`) once for the
session; subsequent tests in the same session reuse it.

## Skipping the build

For fast iteration when the bundle on disk is known good:

```bash
OTTO_BROWSER_SKIP_BUILD=1 uv run pytest tests/browser/ -m browser -p playwright -v
```

The build-bundle fixture still verifies that `otto/web/static/assets/`
contains JS+CSS — a totally missing bundle still fails fast.

## Browser binary cache

`playwright install` writes to `~/Library/Caches/ms-playwright` on macOS and
`~/.cache/ms-playwright` on Linux. CI should cache that path keyed on the
`playwright` version pinned in `pyproject.toml`.

## Tracing failures

Pass `--tracing=retain-on-failure` to capture a Playwright trace (DOM
snapshots, network log, screenshots, console) for every failing test:

```bash
uv run pytest tests/browser/test_smoke.py -m browser -p playwright --tracing=retain-on-failure
```

Open the resulting `test-results/.../trace.zip` with:

```bash
uv run playwright show-trace test-results/<dir>/trace.zip
```

Other useful pytest-playwright flags: `--video=retain-on-failure`,
`--screenshot=only-on-failure`, `--headed`, `--browser=webkit`.

## Fixture catalog

| Fixture                            | Scope    | Purpose |
|------------------------------------|----------|---------|
| `build_bundle`                     | session  | Run `npm run web:verify` once; verify assets exist |
| `mc_backend`                       | function | Spin up `create_app(...)` on an atomically-bound free port; isolated `projects_root` |
| `monkeypatch_watcher_subprocess`   | function | Replace `subprocess.Popen` in the watcher path with a fake; track spawned argv |
| `frozen_clock`                     | function | Pin Python wall clock to `2026-04-25T12:00:00Z` UTC via freezegun |
| `disable_animations`               | function | Returns `apply(page)` that injects CSS killing all animations/transitions |
| `mc_page`                          | function | `mc_backend` + `page` navigated, hydrated, animations disabled |
| `pages_two`                        | function | Two independent contexts/pages on the same backend (two-tab tests) |
| `viewport_desktop`                 | function | 1920×1080 (Mac mini) viewport dict |
| `viewport_mba`                     | function | 1440×900 (MacBook Air) viewport dict |
| `viewport_iphone`                  | function | `playwright.devices["iPhone 14"]` profile |
| `console_must_be_clean`            | function | Returns attacher; fails on unexpected console.error/warning |
| `network_must_be_clean`            | function | Returns attacher; fails on any 4xx/5xx response outside allowlist |

## Recorded fixtures (Phase 3.5 — coming soon)

`tests/browser/_fixtures/recorded-runs/<scenario>/` will hold sanitized
snapshots of real otto activity (queue state, run registry, events JSONL,
git bundle). `tests/browser/_helpers/seed.py` will hydrate one into a
tmp project. The function signature is already final; tests can be
written against it before recordings exist.

## Anti-patterns

Don't:

- assert `is not None` when None is the failure mode — assert the actual value
- catch `Exception` broadly — assert the specific exception class
- rely on test ordering — every test must build its own state
- use real `subprocess.Popen` for watcher tests — always request
  `monkeypatch_watcher_subprocess`
- skip the build-bundle fixture — stale assets cause silent false-passes

See `plan-mc-audit.md` Phase 3D for the full list.
