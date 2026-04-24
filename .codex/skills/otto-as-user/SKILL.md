---
name: otto-as-user
description: "Run Otto's CLI and Mission Control TUI as a real user would, against throwaway repos and real Claude or Codex providers. Use for user-level regression passes over build, certify, resume, queue, merge, cancel, cleanup, and Mission Control behavior. Default to the daily harness unless the user explicitly asks for nightly, seeded, hidden-oracle, or real-world coverage."
---

# Otto As User

## Purpose

Drive Otto end to end from the user surface, not from unit-test internals.

Use this when ordinary tests are not enough and the user wants evidence that
Otto works through real commands, real subprocesses, real provider calls, PTY
recordings, or Mission Control TUI interactions.

## Ground Rules

- Run from the active worktree. Start with `pwd`, `git branch --show-current`,
  and `git status --short` if the task involves fixes or long tests.
- Prefer `uv run --extra dev python ...` in this repo. Use `.venv/bin/python`
  only when the user or environment clearly requires it.
- Be explicit about provider choice:
  - "test with Codex" means add `--provider codex`.
  - "test with Claude" means add `--provider claude`.
  - "compare providers" means run the same scenario set with both providers.
- Treat real provider runs as paid/slow. Choose the smallest scenario set that
  answers the question unless the user asks for full coverage.
- After failures, inspect artifacts before classifying them. Do not call a run
  successful from exit code alone.

## Harness Tiers

| Tier | Harness | Best For | Cost/Time |
| --- | --- | --- | --- |
| daily | `scripts/otto_as_user.py` | Fast CLI/TUI smoke with asciinema recordings | quick batches are minutes; full batches can be costly |
| nightly | `scripts/otto_as_user_nightly.py` | Seeded fixtures, hidden invariants, realistic Mission Control operator workflows | slower and more expensive |

Default to daily. Use nightly when the user says "nightly", "seeded",
"hidden tests", "real-world", "N9", or wants hidden-oracle coverage.

## Mission Control

Mission Control is Otto's primary TUI:

```bash
otto dashboard
otto queue dashboard
otto cleanup <run_id>
```

`otto queue dashboard` is a queue-filtered compatibility wrapper around the
same Mission Control app.

Useful keybinds:

| Keys | Action |
| --- | --- |
| `Tab` / `1` / `2` / `3` | focus Live / History / Detail |
| `j` / `k` or arrows | move selection |
| `Enter` | open Detail |
| `space` | multi-select live queue rows |
| `/` | filter modal from Live or History |
| `f` / `t` | cycle outcome / type filters |
| `[` / `]` | history pagination |
| `Ctrl-F`, `n`, `N` | log search and match navigation |
| `o` | cycle log file |
| `e` | open selected artifact in `$EDITOR` |
| `y` | copy selected row metadata or selected log text |
| `c` / `r` / `R` / `x` | cancel / resume / retry-requeue / cleanup |
| `m` / `M` | merge selected queue rows / merge all done queue rows |
| `?` | help |

## Common Commands

List scenarios:

```bash
uv run --extra dev python scripts/otto_as_user.py --list
uv run --extra dev python scripts/otto_as_user_nightly.py --list
```

Daily examples:

```bash
uv run --extra dev python scripts/otto_as_user.py --mode quick
uv run --extra dev python scripts/otto_as_user.py --mode full --provider codex
uv run --extra dev python scripts/otto_as_user.py --scenario U2
uv run --extra dev python scripts/otto_as_user.py --scenario A1,B3,C1
uv run --extra dev python scripts/otto_as_user.py --group B,D
uv run --extra dev python scripts/otto_as_user.py --keep-failed-only
uv run --extra dev python scripts/otto_as_user.py --bail-fast
uv run --extra dev python scripts/otto_as_user.py --mode quick --scenario-delay 10
```

Nightly examples:

```bash
uv run --extra dev python scripts/otto_as_user_nightly.py --dry-run
uv run --extra dev python scripts/otto_as_user_nightly.py --scenario N4
uv run --extra dev python scripts/otto_as_user_nightly.py --scenario N9
uv run --extra dev python scripts/otto_as_user_nightly.py --scenario N1,N2,N4,N8,N9 --scenario-delay 10
```

Provider comparison pattern:

```bash
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario U2,B2 --scenario-delay 0 --keep-failed-only --bail-fast
uv run --extra dev python scripts/otto_as_user.py --provider claude --scenario U2,B2 --scenario-delay 0 --keep-failed-only --bail-fast
```

## Scenario Focus

High-signal daily choices:

- `U2`: Mission Control basic live-build/cancel/history/quit flow.
- `B2`: queue cancel path through real provider behavior.
- `A1`: basic build/certify provider path.
- `A4`: provider/config path.
- `U1,U3,U4,U5,U6,U7,U8,U9`: Mission Control focused UI and artifact flows.

Nightly choices:

- `N1`: evolving product loop with hidden tests.
- `N2`: semantic auth merge conflict scenario.
- `N4`: certifier trap with hidden tenant/idempotency invariants.
- `N8`: stale merge context around rename plus stale edits.
- `N9`: realistic Mission Control operator session with standalone build,
  queue tasks, cancel, history/artifact inspection, and merge.

Nightly fixtures live under `scripts/fixtures_nightly/<scenario>/`.

## Artifacts

Daily:

```text
bench-results/as-user/<run-id>/<scenario>/
  recording.cast
  debug.log
  run_result.json
  verify.json
```

Nightly:

```text
bench-results/as-user-nightly/<run-id>/<scenario>/
  debug.log
  tests-visible.log
  tests-hidden.log
  run_result.json
  verify.json
  attempt.json
```

Mission Control and registry paths worth checking:

```text
otto_logs/cross-sessions/runs/live/<run_id>.json
otto_logs/cross-sessions/runs/gc/tombstones.jsonl
otto_logs/cross-sessions/history.jsonl
otto_logs/sessions/<run_id>/commands/requests.jsonl
otto_logs/sessions/<run_id>/commands/acks.jsonl
otto_logs/merge/commands/requests.jsonl
otto_logs/merge/commands/acks.jsonl
```

Replay recordings:

```bash
asciinema play bench-results/as-user/<run-id>/<scenario>/recording.cast
agg bench-results/as-user/<run-id>/<scenario>/recording.cast out.gif
```

## Failure Triage

Classify carefully:

- `PASS`: verification succeeded.
- `FAIL`: likely Otto bug, product bug, or scenario bug. Inspect artifacts.
- `INFRA`: auth, rate limit, network, provider outage, or setup failure.

Common infra signatures:

- `Not logged in` or `Please run /login`
- `rate limit` or `429`
- provider exits before a meaningful run starts
- near-zero duration/cost with command-launch failure

Common real failures:

- wrong interpreter/PATH, so Otto is not running from this repo's environment
- Mission Control row never reaches `running`
- cancel request persisted but no ack arrived
- resume scenario completes before interruption lands
- cross-run memory marker missing from `messages.jsonl`
- provider emits token usage but no USD cost; do not interpret `cost_usd: 0.0`
  as free execution when token usage is present

Triage order: `recording.cast`, `debug.log`, `verify.json`, live records,
command-channel acks, then `history.jsonl`.

## Adding Scenarios

Daily:

- add `setup_*`, `run_*`, and `verify_*` in `scripts/otto_as_user.py`
- register the scenario in `SCENARIOS`
- keep quick mode limited to the highest-signal set
- use tiny repos and tiny intents
- for TUI scenarios, drive interactions inside the recorded PTY

Nightly:

- create `scripts/fixtures_nightly/<name>/`
- include `intent.md`, `otto.yaml`, app code, visible tests, hidden tests, and
  `restore.sh`
- visible tests must pass on the initial fixture
- hidden tests should fail on the initial fixture when testing feature-building
- add `setup_*`, `run_*`, and `verify_*` in `scripts/otto_as_user_nightly.py`
- register the scenario in `SCENARIOS` and `SCENARIO_SPECS`
