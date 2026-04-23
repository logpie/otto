---
name: otto-as-user
description: Run Otto's CLI and Mission Control TUI as a real user would, against throwaway repos and a real provider. Two tiers: daily (37 short toy scenarios with asciinema recordings, $10-$15 full) and nightly (5 seeded-fixture scenarios with hidden-test oracles, $12-$18 full). Default to daily unless the user explicitly asks for nightly / seeded / real-world coverage.
---

# Otto As User

## Overview

Drive Otto end to end as a real user, using real LLM runs and real Otto entrypoints.

Mission Control is the primary TUI now: a generic 3-pane app for live runs, history, and detail/logs. `otto queue dashboard` still exists, but it is only a queue-filtered compatibility wrapper around the same Mission Control app.

Use this skill when unit tests are not enough and you need a user-level regression pass over build, resume, queue, merge, cancel, cleanup, or Mission Control behavior.

## Tiers

| Tier | Harness | Scenarios | Best for | Cost | Wall time |
|------|---------|-----------|----------|------|-----------|
| daily | `scripts/otto_as_user.py` | 37 (`A-E`, `U`) | fast CLI/TUI smoke with recordings | `$3-$4` quick, `$10-$15` full | `15m` quick, `60m` full |
| nightly | `scripts/otto_as_user_nightly.py` | 5 (`N1`, `N2`, `N4`, `N8`, `N9`) | seeded fixtures, hidden invariants, real-world regressions | `$12-$18` full | `85-100m` full |

If the user says "run otto-as-user" without a qualifier, use daily. If they say "nightly", "seeded", "real-world", or want hidden-oracle coverage, use nightly.

## Mission Control

```bash
otto dashboard
otto queue dashboard
otto cleanup <run_id>
```

`otto dashboard` opens the full app. `otto queue dashboard` opens the same app with a queue filter applied. `otto cleanup <run_id>` removes one terminal live record while preserving history.

Mission Control keybinds:

| Keys | Action |
|------|--------|
| `Tab` / `1` / `2` / `3` | focus Live / History / Detail |
| `j` / `k` or arrows | move selection |
| `Enter` | open Detail |
| `space` | multi-select live queue rows |
| `/` | filter modal from Live or History |
| `f` | cycle history outcome filter |
| `t` | cycle type filter |
| `[` / `]` | history pagination |
| `Ctrl-F` | log search in Detail |
| `n` / `N` | next / previous log match |
| `o` | cycle log file |
| `e` | open selected artifact in `$EDITOR` |
| `c` / `r` / `R` / `x` | cancel / resume / retry-requeue / cleanup |
| `m` / `M` | merge selected queue rows / merge all done queue rows |
| `?` | help |

## Commands

Assume repo root.

Daily harness:

```bash
.venv/bin/python scripts/otto_as_user.py --list
.venv/bin/python scripts/otto_as_user.py --mode quick
.venv/bin/python scripts/otto_as_user.py --mode full --provider codex
.venv/bin/python scripts/otto_as_user.py --scenario U2
.venv/bin/python scripts/otto_as_user.py --scenario A1,B3,C1
.venv/bin/python scripts/otto_as_user.py --group B,D
.venv/bin/python scripts/otto_as_user.py --keep-failed-only
.venv/bin/python scripts/otto_as_user.py --bail-fast
.venv/bin/python scripts/otto_as_user.py --mode quick --scenario-delay 10
```

Nightly harness:

```bash
.venv/bin/python scripts/otto_as_user_nightly.py --list
.venv/bin/python scripts/otto_as_user_nightly.py --dry-run
.venv/bin/python scripts/otto_as_user_nightly.py --scenario N4
.venv/bin/python scripts/otto_as_user_nightly.py --scenario N9
.venv/bin/python scripts/otto_as_user_nightly.py --scenario N1,N2,N4,N8,N9 --scenario-delay 10
```

- "test with Claude" -> `--provider claude`
- "test with Codex" -> `--provider codex`

## Scenario Focus

Daily high-signal path:

- `U2` is the daily Mission Control smoke. It covers one live build, cancel acknowledgment, a cancelled history row, and clean quit behavior. Mention it explicitly when the user wants "the new TUI smoke" without paying nightly cost.

Nightly scenarios:

- `N1` evolving product loop: build feature -> improve bugs -> improve target on a seeded multi-user task tracker. Hidden tests check user-data isolation and query-count regression.
- `N2` semantic auth merge: queue two overlapping auth branches, run them, merge them, and verify both flows plus baseline login behavior.
- `N4` certifier trap: build CSV bulk import from product-language intent only. Hidden tests enforce tenant isolation and idempotency.
- `N8` stale merge context: three-branch queue around a file rename plus stale edits and tests. Hidden tests ensure the final merge keeps all three intents coherent.
- `N9` realistic operator session: open Mission Control, watch one standalone build finish naturally, enqueue two concurrent queue tasks, inspect heartbeat/log updates mid-flight, cancel one queue task via TUI, open History and `$EDITOR` on the cancelled snapshot, then merge the succeeded queue row via `m`. This is the nightly end-to-end gate for real TUI actions, live registry coherence, terminal history integrity, and post-merge artifact preservation under real LLM runs.

Nightly fixtures live under `scripts/fixtures_nightly/<scenario>/` and include `intent.md`, `otto.yaml`, app code, visible tests, hidden tests, and `restore.sh`.

## Results

```text
bench-results/as-user/<run-id>/<scenario>/
```

- `recording.cast`
- `debug.log`
- `run_result.json`
- `verify.json`

```text
bench-results/as-user-nightly/<run-id>/<scenario>/
```

- `debug.log`
- `tests-visible.log`
- `tests-hidden.log` when the scenario runs hidden fixture tests
- `run_result.json`
- `verify.json`
- `attempt.json`

Mission Control and registry paths worth checking during failures:

- `otto_logs/cross-sessions/runs/live/<run_id>.json`
- `otto_logs/cross-sessions/runs/gc/tombstones.jsonl`
- `otto_logs/sessions/<run_id>/commands/requests.jsonl`
- `otto_logs/sessions/<run_id>/commands/acks.jsonl`
- `otto_logs/merge/commands/requests.jsonl`
- `otto_logs/merge/commands/acks.jsonl`
- `otto_logs/cross-sessions/history.jsonl`

```bash
asciinema play bench-results/as-user/<run-id>/<scenario>/recording.cast
agg bench-results/as-user/<run-id>/<scenario>/recording.cast out.gif
```

## Failure Triage

Result categories:

- `PASS`: verification succeeded
- `FAIL`: real Otto bug or scenario/test bug
- `INFRA`: transient auth, rate-limit, network, or provider issue; auto-retried once

Common INFRA signatures:

- `Not logged in` or `Please run /login`
- `rate limit` or `429`
- `Command failed with exit code 1` plus near-zero cost and sub-2s duration

Common real failures:

- `asciinema` missing or install failed before a recorded run starts
- wrong interpreter or PATH, so Otto is not running from this repo's `.venv`
- Mission Control or queue scenarios timing out before a live row reaches `running`
- cancel request persisted but no ack arrived within `max(4s, 2 * heartbeat)`
- resume scenario completed before the interruption signal actually landed
- cross-run memory wrote a file but the prompt marker never appeared in `messages.jsonl`

Triage order: `recording.cast` for daily scenarios, then `debug.log`, then `verify.json`, then live-record / command-channel / history artifacts. To reduce INFRA frequency, increase `--scenario-delay` or run smaller batches.

## Dependencies

- `asciinema` is required for daily recordings
- the harness checks `PATH` and `.venv/bin/asciinema`
- if missing, it tries `uv pip install --python .venv/bin/python asciinema`, then `brew install asciinema`

## Adding Scenarios

Daily:

- add `setup_*`, `run_*`, and `verify_*` in `scripts/otto_as_user.py`
- register the scenario in `SCENARIOS`
- keep quick mode limited to the highest-signal set
- prefer tiny repos and tiny intents
- for TUI scenarios, perform the interaction inside the recorded PTY run so Mission Control is captured in `recording.cast`

Nightly:

- create `scripts/fixtures_nightly/<name>/` with `intent.md`, `otto.yaml`, app code, visible tests, hidden tests, and `restore.sh`
- visible tests must pass on the initial fixture
- hidden tests must fail on the initial fixture
- add `setup_*`, `run_*`, and `verify_*` in `scripts/otto_as_user_nightly.py`
- register the scenario in both `SCENARIOS` and `SCENARIO_SPECS`
- keep `intent.md` in product language when certifier inference matters

The weekly candidates `#3`, `#5`, `#6`, and `#7` remain deferred; nightly keeps the highest-signal seeded scenarios in the regular rotation.
