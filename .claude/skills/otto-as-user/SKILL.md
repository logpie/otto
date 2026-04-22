---
name: otto-as-user
description: Run Otto's CLI and queue dashboard as a real user would, using a real provider and saving per-scenario asciinema recordings plus logs under bench-results/as-user.
---

# Otto As User

## Description

Drive Otto end to end as a real user, against throwaway git repos, with real LLM runs and per-scenario terminal recordings for review.

## When to invoke

- When you need a real-user regression pass over Otto's CLI or queue dashboard
- When you want video artifacts for a build, resume, queue, or merge scenario
- When a change touched queue/dashboard/merge/resume behavior and unit tests are not enough
- When the user says "test with codex" or "test with claude" and wants Otto exercised through its own CLI

## How to invoke

From the repo root:

```bash
.venv/bin/python scripts/otto_as_user.py --list
.venv/bin/python scripts/otto_as_user.py --mode quick
.venv/bin/python scripts/otto_as_user.py --mode full
.venv/bin/python scripts/otto_as_user.py --mode full --provider codex
.venv/bin/python scripts/otto_as_user.py --scenario A1,B3,C1
.venv/bin/python scripts/otto_as_user.py --group B,D
.venv/bin/python scripts/otto_as_user.py --keep-failed-only
.venv/bin/python scripts/otto_as_user.py --bail-fast
.venv/bin/python scripts/otto_as_user.py --mode quick --scenario-delay 10  # slower, less rate-limit risk
```

Provider mapping:

- "test with Claude" -> `--provider claude`
- "test with Codex" -> `--provider codex`

Dependency:

- `asciinema` is required for recordings
- The runner checks `PATH` and `.venv/bin/asciinema`
- If missing, it tries `uv pip install --python .venv/bin/python asciinema` first, then `brew install asciinema`

## Cost expectations

- `--mode quick`: roughly 5 scenarios, about `$2`, around `10m`
- `--mode full`: all scenarios, roughly `$10-15`, around `30m`

## Reading results

Artifacts land under:

```text
bench-results/as-user/<run-id>/<scenario>/
```

Key files:

- `recording.cast`: asciinema capture
- `debug.log`: combined runner output for that scenario
- `run_result.json`: raw scenario metadata
- `verify.json`: verification outcome

Playback:

```bash
asciinema play bench-results/as-user/<run-id>/<scenario>/recording.cast
```

Sharing hints:

```bash
agg bench-results/as-user/<run-id>/<scenario>/recording.cast out.gif
```

Or upload the `.cast` to an asciinema-compatible player.

## Result categories

Each scenario reports one of three outcomes:

- `PASS` — verification succeeded
- `FAIL` — real Otto bug (or test bug); investigate `recording.cast` + `debug.log` + `verify.json`
- `INFRA` — transient infra issue (subscription rate-limit, auth degradation, network). Auto-retried once after 30s; if the retry also INFRAs, the scenario is reported as INFRA. Process exits 0 if all failures are INFRA-only — INFRA does NOT count as a real failure for CI purposes.

Common INFRA signatures the harness detects:
- `Not logged in · Please run /login` in narrative.log (subscription token under load)
- `rate limit` / `429` near throttle wording
- `Command failed with exit code 1` + zero-cost + sub-2s duration (the smoking-gun "transient agent crash" pattern)

To reduce INFRA frequency in batch runs: increase `--scenario-delay` (default 5s) or run scenarios in smaller batches.

## Common failure modes (real FAIL)

- `asciinema` missing or install failed before the run starts
- `otto` invoked from the wrong interpreter instead of this repo's `.venv`
- Queue/dashboard scenarios timing out because the task never reached `running`
- Resume scenarios failing because the interrupted run completed before the signal landed
- Cross-run memory verification writing the memory file but not exposing the prompt marker in `messages.jsonl`

When a scenario fails (FAIL, not INFRA), review `recording.cast` first, then `debug.log`, then `verify.json`.

## Adding new scenarios

- Add a new `setup_*`, `run_*`, and `verify_*` trio in `scripts/otto_as_user.py`
- Register it in `SCENARIOS` with cost, duration, quick/full membership, and `requires_pty`
- Keep quick mode to the highest-signal 4-6 scenarios
- Prefer tiny repos and tiny intents so the scenario stays cheap
- For TUI scenarios, make the PTY interaction happen inside the internal recorded run so `recording.cast` captures the live dashboard
