---
name: otto-as-user
description: "Run Otto's redesigned intent-to-product flow as a real user would, against throwaway repos or real projects with real Claude or Codex providers when needed. Use for user-level regression passes over compile, build, audit, proof, resume, queue, merge, cancel, cleanup, and Mission Control behavior. Default to the smallest real user-level harness that answers the question, but use real provider/project runs when the user asks for live, paid, redesign, pressure-test, or end-to-end proof."
---

# Otto As User

## Purpose

Drive Otto end to end from the user surface, not from unit-test internals.

Use this when ordinary tests are not enough and the user wants evidence that
Otto works through real commands, real subprocesses, real provider calls, real
web servers, browser automation, or web Mission Control interactions.

For the redesigned i2p stack, the user-facing product path is:

```text
intent -> compile spec -> build Groups -> audit integrated product -> render proof
```

Treat that product path, the live CLI, and `otto_logs/sessions/<session-id>/`
as authority. Historical scenario harnesses below are useful for Mission
Control coverage, but they are not a substitute for real i2p runs on real
projects when the task is about redesign readiness.

## Ground Rules

- Run from the active worktree. Start with `pwd`, `git branch --show-current`,
  and `git status --short` if the task involves fixes or long tests.
- Prefer `uv run --extra dev python ...` in this repo. Use `.venv/bin/python`
  only when the user or environment clearly requires it.
- Be explicit about provider choice:
  - "test with Codex" means add `--provider codex`.
  - "test with Claude" means add `--provider claude`.
  - "compare providers" means run the same scenario set with both providers.
- For i2p runs, verify the provider actually propagated into compile, build,
  audit, repair, and merge-repair agents. If a run requested Codex but a
  run-owned Claude child appears, stop and classify the run as an Otto bug.
- Treat real provider runs as paid/slow. `scripts/web_as_user.py` requires
  `OTTO_ALLOW_REAL_COST=1`; choose the smallest scenario set that answers the
  question unless the user asks for broad coverage.
- After failures, inspect artifacts before classifying them. Do not call a run
  successful from exit code alone.
- External verification must run outside Otto before claiming success. Use the
  target repo's native tests/builds and direct product assertions, not only
  Otto's final verdict.

## Redesigned i2p Runs

Use the live CLI entrypoints and force i2p when testing redesign behavior:

```bash
uv run --extra dev python -m otto.cli build "build a product..." --i2p --provider codex --budget 1800 --max-turns 120 --verbose
uv run --extra dev python -m otto.cli improve feature "add/fix behavior..." --i2p --provider codex --budget 1800 --max-turns 120 --verbose
uv run --extra dev python -m otto.cli certify --i2p --provider codex --budget 1200 --max-turns 80 --verbose
```

Useful i2p flags:

- `--resume`: continue an interrupted i2p checkpoint.
- `--reset-budget`: do not count prior attempt spend on resume.
- `--review-gate`: pause after compile until spec review approves.
- `--auto-approve`: make scripted runs explicit about skipping review gate.
- `--break-lock`: clear a stale project lock before starting.
- `--allow-dirty`: run in a repo with local changes when the test requires it.

For real project pressure tests, record at minimum:

- exact Otto command, provider/model if visible, session id, wall time, cost,
  and Otto verdict
- project path/source and why it is a real/non-toy workload
- external verifier command/result
- proof packet path and relevant browser/video/screenshot artifacts
- bugs found, logs inspected, root cause, generic fix, regression tests, gates
  run, and decision to escalate/retry/fix/defer/stop

Primary i2p artifacts:

```text
otto_logs/sessions/<session-id>/summary.json
otto_logs/sessions/<session-id>/spec/spec.json
otto_logs/sessions/<session-id>/spec-state.jsonl
otto_logs/sessions/<session-id>/proof-packet.html
otto_logs/sessions/<session-id>/proof-packet.json
otto_logs/sessions/<session-id>/**/narrative.log
otto_logs/sessions/<session-id>/audit/**/feature-verdicts.json
otto_logs/sessions/<session-id>/audit/**/screenshots/
otto_logs/sessions/<session-id>/audit/**/videos/
```

## Harnesses

| Harness | Command | Best For | Cost/Time |
| --- | --- | --- | --- |
| Web E2E | `scripts/e2e_web_mission_control.py` | Browser-level Mission Control regressions with seeded local state | minutes, no provider cost |
| Live web-as-user | `scripts/web_as_user.py` | Real web server plus real LLM/queue/provider workflows | slow and paid; guarded by `OTTO_ALLOW_REAL_COST=1` |

Default to the focused Web E2E scenario for ordinary Mission Control UI bugs.
Use live web-as-user when the user asks for as-user proof across CLI/web,
provider, queue runner, merge, outage recovery, or nightly/weekly coverage.

## Mission Control

Mission Control is Otto's web app.

Useful launch forms:

```bash
otto web --no-open
otto web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open
otto dashboard --no-open
```

Notes:

- `otto dashboard` is only a deprecated compatibility alias for `otto web`.
- `otto queue dashboard` has been removed. Use `otto web` for Mission Control.
- The queue runner is still CLI-driven with `otto queue run --no-dashboard`.

## Common Commands

List live provider scenarios:

```bash
uv run --extra dev python scripts/web_as_user.py --list
```

Run focused browser E2E coverage:

```bash
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario ready-land --artifacts /tmp/otto-web-e2e-ready-land --viewport 1440x900
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario all --artifacts /tmp/otto-web-e2e-mission-control-all --viewport 1440x900
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario control-tour --artifacts /tmp/otto-web-e2e-control-tour-mobile --viewport 390x844
```

Run live web-as-user scenarios:

```bash
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --mode quick --provider codex
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --scenario W1 --provider claude
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --scenario W11 --provider codex --bail-fast --keep-failed-only
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --tier nightly --provider claude --scenario-delay 10
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --tier weekly --provider codex
```

Provider comparison pattern:

```bash
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --provider codex --scenario W1,W11 --scenario-delay 0 --keep-failed-only --bail-fast
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --provider claude --scenario W1,W11 --scenario-delay 0 --keep-failed-only --bail-fast
```

## Scenario Focus

High-signal browser E2E choices:

- `fresh-queue`: submit a first build from the web UI before watcher start.
- `ready-land`: review and land a clean completed task.
- `watcher-stop-ui`: cancel and confirm watcher stop from the visible UI.
- `job-submit-matrix`: submit improve and certify jobs with advanced options.
- `bulk-land`: land multiple ready tasks through the bulk action.
- `long-log-layout`: inspect large logs and artifacts in bounded layouts.
- `control-tour`: click through main controls, dialogs, inspectors, and tabs.

High-signal live web-as-user choices:

- `W1`: first-time user, create project, submit build, inspect tabs.
- `W7`: mobile/iPhone version of the W1 flow.
- `W11`: operator day, CLI/web interop, queue, watcher, cancel, merge.
- `W4`: merge happy path.
- `W5`: merge blocked with a clear reason.
- `W12b`: CLI-queued task through web start, run, and merge.
- `W13`: restart `otto web` mid-build and verify recovery.

Live tier mappings:

```text
quick   = W1 + W11
nightly = W11 + W1 + W7
weekly  = W1..W13
```

Nightly fixtures for hidden-invariant product scenarios live under
`scripts/fixtures_nightly/<scenario>/`.

## Artifacts

Web E2E:

```text
<artifacts>/<NN>-<scenario>/
  screenshots and failure evidence
<artifacts>/summary.json
<artifacts>/coverage-model.json
```

Live web-as-user:

```text
bench-results/web-as-user/<run-id>/<scenario>/
  debug.log
  final-state.json
  run_result.json
  verify.json
  screenshots and browser/provider artifacts
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

## Failure Triage

Classify carefully:

- `PASS`: verification succeeded.
- `FAIL`: likely Otto bug, product bug, or scenario bug. Inspect artifacts.
- `INFRA`: auth, rate limit, network, browser tool, provider outage, or setup failure.

Common infra signatures:

- `Not logged in` or `Please run /login`
- `rate limit` or `429`
- provider exits before a meaningful run starts
- `agent-browser is required`
- near-zero duration/cost with command-launch failure

Common real failures:

- wrong interpreter/PATH, so Otto is not running from this repo's environment
- web server is stale or serving an old static bundle
- Mission Control row never reaches the expected queued/running/ready state
- cancel request persisted but no ack arrived
- resume/retry flow completes before interruption lands
- CLI and web disagree about run, queue, or merge state
- provider emits token usage but no USD cost; do not interpret `cost_usd: 0.0`
  as free execution when token usage is present

Triage order: debug log, screenshots, `verify.json`, `final-state.json`, live
records, command-channel acks, then `history.jsonl`.

## Adding Scenarios

Web E2E:

- add a `scenario_*` function in `scripts/e2e_web_mission_control.py`
- register it in `scenarios()`
- update `COVERAGE_MODEL` when the scenario owns new user-visible states/actions
- use seeded repos and browser assertions that exercise the actual UI flow

Live web-as-user:

- add a `_run_w*` function in `scripts/web_as_user.py`
- register the scenario in `SCENARIOS`
- keep `TIER_NIGHTLY`, `TIER_WEEKLY`, and `QUICK_SCENARIOS` intentional
- use `RunFailures` soft assertions for long paid scenarios
- mine artifacts after the run so one paid attempt finds multiple defects
