---
name: otto-as-user
description: Run Otto's CLI and queue dashboard as a real user would, using a real provider. Two tiers — daily (37 short toy scenarios with asciinema recordings, ~$12) and nightly (4 medium-fixture seeded scenarios with hidden-test oracles, ~$10). Pick the tier the user asks for; default to daily if unspecified.
---

# Otto As User

## Description

Drive Otto end to end as a real user, against throwaway git repos, with real LLM runs.

Two harness tiers are available:

| Tier | Harness | Scenarios | Style | Per-run cost | Wall time |
|------|---------|-----------|-------|--------------|-----------|
| **daily** | `scripts/otto_as_user.py` | 37 (groups A–E, U) | toy projects, short, asciinema-recorded | ~$12 full / ~$2 quick | ~60min full / ~10min quick |
| **nightly** | `scripts/otto_as_user_nightly.py` | 5 (N1, N2, N4, N8, N9) | seeded medium fixtures, hidden-test oracles, no recording | ~$12 | ~80min |

If the user says "run otto-as-user" without qualifier → use **daily**. If they say "nightly" or "real-world" or "seeded" → use **nightly**.

## When to invoke

- When you need a real-user regression pass over Otto's CLI or queue dashboard
- When you want video artifacts for a build, resume, queue, or merge scenario
- When a change touched queue/dashboard/merge/resume behavior and unit tests are not enough
- When the user says "test with codex" or "test with claude" and wants Otto exercised through its own CLI

## How to invoke

### Daily tier (37 toy scenarios, recorded)

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

### Nightly tier (4 seeded medium-fixture scenarios, hidden oracles)

```bash
.venv/bin/python scripts/otto_as_user_nightly.py --dry-run        # show plan + fixture paths, no spend
.venv/bin/python scripts/otto_as_user_nightly.py                  # run all 4 sequentially
.venv/bin/python scripts/otto_as_user_nightly.py --scenario N4    # cheapest single ($1.2-1.8, 3-10min)
.venv/bin/python scripts/otto_as_user_nightly.py --scenario N1,N2,N4,N8 --scenario-delay 10
```

Nightly scenarios:
- **N1** — evolving product loop: build feature → improve bugs → improve target on a multi-user task tracker. Hidden tests check user-data isolation + N+1 query elimination.
- **N2** — semantic auth merge: queue 2 branches (password reset + remember-me) touching the same auth code, merge, hidden tests verify both flows + login still works.
- **N4** — certifier trap: build CSV bulk import where intent uses product language ("customers should not see each other's data"), not engineering terms. Hidden tests enforce tenant isolation + idempotency.
- **N8** — stale merge context: 3-branch queue (rename → edit-old-location → add-new-tests). Hidden tests verify the rename, the merged logic, and the regression tests all coexist on main.
- **N9** — Mission Control workflow: drives the canonical run registry, durable cancel command protocol, and history v2 snapshots end-to-end. Standalone build (to be cancelled) + 2 queue tasks + merge. Hidden tests verify registry coherence, terminal_outcome=cancelled (not failure) on the cancelled build, no zombie writers, all artifact paths resolve. Proves the TUI substrate works under real-LLM cross-process load.

Nightly fixtures live in `scripts/fixtures_nightly/<scenario>/` with `intent.md`, `otto.yaml`, `tests/visible/` (Otto sees), `tests/hidden/` (oracle, run after Otto exits).

Provider mapping:

- "test with Claude" -> `--provider claude`
- "test with Codex" -> `--provider codex`

Dependency:

- `asciinema` is required for recordings
- The runner checks `PATH` and `.venv/bin/asciinema`
- If missing, it tries `uv pip install --python .venv/bin/python asciinema` first, then `brew install asciinema`

## Cost expectations

Daily tier:
- `--mode quick`: roughly 5 scenarios, about `$2`, around `10m`
- `--mode full`: all scenarios, roughly `$10-15`, around `30m`

Nightly tier:
- `--scenario N4`: ~`$1.5`, `3-10m` (cheapest validation that the harness still works)
- all 4: ~`$8-12`, `~60m`

## Reading results

Daily artifacts:

```text
bench-results/as-user/<run-id>/<scenario>/
```

- `recording.cast`: asciinema capture
- `debug.log`: combined runner output for that scenario
- `run_result.json`: raw scenario metadata
- `verify.json`: verification outcome

Nightly artifacts (no `recording.cast` — long real-LLM runs are mostly progress dots):

```text
bench-results/as-user-nightly/<run-id>/<scenario>/
```

- `debug.log`: combined runner output
- `tests-visible.log` / `tests-hidden.log`: pytest output from the oracle pass
- `run_result.json` / `verify.json` / `attempt.json`: structured outcome

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

### Daily tier (toy projects, recorded)

- Add a new `setup_*`, `run_*`, and `verify_*` trio in `scripts/otto_as_user.py`
- Register it in `SCENARIOS` with cost, duration, quick/full membership, and `requires_pty`
- Keep quick mode to the highest-signal 4-6 scenarios
- Prefer tiny repos and tiny intents so the scenario stays cheap
- For TUI scenarios, make the PTY interaction happen inside the internal recorded run so `recording.cast` captures the live dashboard

### Nightly tier (seeded fixtures, hidden oracles)

- Create `scripts/fixtures_nightly/<name>/` with `intent.md`, `otto.yaml`, `app/`, `tests/visible/`, `tests/hidden/`, `restore.sh`
- Visible tests must PASS on the initial fixture state (precondition for Otto's job)
- Hidden tests must FAIL on the initial fixture state (otherwise the trap is misdesigned)
- Add the scenario's `setup_*`, `run_*`, `verify_*` trio in `scripts/otto_as_user_nightly.py`
- Register in `SCENARIOS` and the nightly fixture spec map
- Use product language in `intent.md` (not engineering terms) when testing certifier inference
- Reference design rationale: 4 nightly scenarios were picked from Codex's 8-scenario design (#1, #2, #4, #8 had highest bug-finding density). The other 4 (#3 dual-migration, #5 discovery-heavy, #6 cross-module-refactor, #7 long-horizon-pause-resume) are deferred to a future weekly tier.
