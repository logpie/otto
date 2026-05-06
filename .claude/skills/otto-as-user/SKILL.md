---
name: otto-as-user
description: "Dogfood Otto through its real user surfaces: `otto run`, queue/Mission Control, proof artifacts, browser flows, and real Claude or Codex provider runs. Use when unit tests are not enough and the user wants evidence from live CLI/web workflows, logs, artifacts, external verifiers, or paid end-to-end pressure tests."
---

# Otto As User

## Purpose

Drive Otto end to end from the user surface, not from unit-test internals.

Use this skill when ordinary tests are not enough and the user wants evidence
that Otto works through real commands, real subprocesses, real provider calls,
real web servers, browser automation, queue execution, proof packets, or web
Mission Control interactions.

For the redesigned intent-to-product stack, the core product path is:

```text
intent -> compile spec -> build Groups -> merge -> audit integrated product -> render proof
```

Treat the live CLI, Mission Control, and `otto_logs/sessions/<session-id>/` as
authority. Historical harness names such as `scripts/otto_as_user.py`,
`scripts/otto_as_user_nightly.py`, and `otto queue dashboard` are stale and
must not be used as current evidence.

## Is This Skill Still Useful?

Yes, but only as a dogfooding discipline, not as a fixed scenario cookbook.

Keep it when the question is "does Otto work like a user would use it?" That
means testing the public CLI/web surface, collecting session logs and proof
artifacts, and verifying the target project outside Otto. Do not use this skill
for ordinary unit-test selection, static code review, or one-off internal
function checks.

## Ground Rules

- Run from the active worktree. Start with `pwd`, `git branch --show-current`,
  and `git status --short --branch` before long tests or fixes.
- Prefer `uv run --extra dev python ...` in this repo. Use `.venv/bin/python`
  only when the user or environment clearly requires it.
- Be explicit about provider choice:
  - "test with Codex" means pass `--provider codex`.
  - "test with Claude" means pass `--provider claude`.
  - "compare providers" means run the same scenario set with both providers.
- For i2p runs, verify the provider propagated into compile, build, audit,
  repair, and merge-repair agents. If a run requested Codex but a run-owned
  Claude child appears, classify it as an Otto bug.
- Treat real provider runs as paid/slow. `scripts/web_as_user.py` requires
  `OTTO_ALLOW_REAL_COST=1`.
- After failures, inspect logs and artifacts before classifying them. Do not
  call a run successful from exit code alone.
- External verification must run outside Otto before claiming product success.
  Use the target repo's native tests/builds and direct product assertions, not
  only Otto's final verdict.

## Core i2p CLI

Use `otto run` for direct compile/build/merge/audit/render evidence. It exposes
provider, model, effort, budget, and max-turn controls directly:

```bash
uv run --extra dev python -m otto.cli run "build a product..." --project-kind webapp --provider codex --budget 3600 --max-turns 160 --verbose
uv run --extra dev python -m otto.cli run "build a CLI tool..." --project-kind cli --provider codex --effort high --budget 2400
uv run --extra dev python -m otto.cli run --from-spec otto_logs/sessions/<id>/spec/spec.json --provider codex --budget 2400
uv run --extra dev python -m otto.cli run --resume --provider codex --auto-approve
```

Useful `otto run` flags:

- `--provider`, `--model`, `--effort`: override every agent.
- `--build-provider`, `--build-model`, `--build-effort`: override build agents.
- `--certifier-provider`, `--certifier-model`, `--certifier-effort`: override
  audit/certifier agents.
- `--fix-provider`, `--fix-model`, `--fix-effort`: override repair agents.
- `--budget`: total wall-clock budget in seconds.
- `--max-turns`: max agent turns per call, capped at 200.
- `--review-gate`: pause after compile until spec review approval.
- `--auto-approve`: make scripted runs explicit about skipping review gate.
- `--resume`: resume the paused i2p session at `otto_logs/paused`.
- `--reset-budget`: on resume, ignore prior attempt spend against the cap.
- `--force`: on resume, bypass the spec hash check.
- `--break-lock`: clear a stale project lock before starting.
- `--base-url`: feed HTTP probe checks.
- `--from-spec`: drive from an existing `spec.json`.

`otto build`, `otto improve`, and `otto certify` remain public compatibility or
specialized surfaces. Use them only when the user specifically wants that
surface tested. For general redesigned i2p pressure, prefer `otto run`.
Standalone `otto certify` is diagnostics unless the task explicitly asks to
test certification as a user command.

For projects that require environment variables, put those variables on the
exact Otto command and verify child agent logs used the same environment:

```bash
DATABASE_URL=postgres://... uv run --extra dev python -m otto.cli run "..." --provider codex --budget 4500
```

## Queue And Mission Control

Mission Control is Otto's web app. Use it when the test needs queue behavior,
review/land, live status, browser evidence, or user workflow proof.

Useful launch forms:

```bash
uv run --extra dev python -m otto.cli web --no-open
uv run --extra dev python -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open
uv run --extra dev python -m otto.cli dashboard --no-open
```

Notes:

- `otto dashboard` is a compatibility alias for `otto web`.
- `otto queue dashboard` has been removed. Use `otto web`.
- The queue runner remains CLI-driven with `otto queue run --no-dashboard`.
- When dogfooding queue/Mission Control, queue a real task, let the runner
  execute build/certify/fix or proof-repair, then review/land through Mission
  Control instead of only inspecting backend state.

## Harnesses

| Harness | Command | Best For | Cost/Time |
| --- | --- | --- | --- |
| Web E2E | `scripts/e2e_web_mission_control.py` | Browser-level Mission Control regressions with seeded local state | minutes, no provider cost |
| Live web-as-user | `scripts/web_as_user.py` | Real web server plus real LLM/queue/provider workflows | slow and paid; guarded by `OTTO_ALLOW_REAL_COST=1` |

Default to focused Web E2E for ordinary Mission Control UI bugs. Use live
web-as-user when the user asks for live, paid, provider, queue-runner, outage
recovery, nightly/weekly, or end-to-end as-user proof.

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
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --tier nightly --provider codex --scenario-delay 10
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --tier weekly --provider codex
```

Provider comparison pattern:

```bash
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --provider codex --scenario W1,W11 --scenario-delay 0 --keep-failed-only --bail-fast
OTTO_ALLOW_REAL_COST=1 uv run --extra dev python scripts/web_as_user.py --provider claude --scenario W1,W11 --scenario-delay 0 --keep-failed-only --bail-fast
```

## Scenario Focus

High-signal browser E2E choices:

- `project-launcher`: create/open projects from the launcher.
- `fresh-queue`: submit a first build from the web UI before watcher start.
- `ready-land`: review and land a clean completed task.
- `dirty-blocked`: verify dirty-worktree blocking behavior.
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
weekly  = all W scenarios
```

## Real-Project Pressure Tests

For real i2p pressure tests, avoid toy projects unless the user explicitly
requests a smoke test. Record at minimum:

- exact Otto command, provider/model if visible, session id, wall time, cost,
  and Otto verdict
- project path/source and why it is a real workload
- external verifier command/result
- proof packet path and browser/video/screenshot artifacts when applicable
- bugs found, logs inspected, root cause, generic fix, regression tests, gates
  run, and decision to escalate, retry, fix, defer, or stop

Escalate after passes. Passing one real project proves only that tier.

## Artifacts

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
- `INFRA`: auth, rate limit, network, browser tool, provider outage, or setup
  failure.

Common infra signatures:

- `Not logged in` or `Please run /login`
- `rate limit` or `429`
- provider exits before a meaningful run starts
- browser automation dependency missing
- near-zero duration/cost with command-launch failure

Common real failures:

- wrong interpreter/PATH, so Otto is not running from this repo environment
- web server is stale or serving an old static bundle
- Mission Control row never reaches the expected queued/running/ready state
- cancel request persisted but no ack arrived
- resume/retry flow completes before interruption lands
- CLI and web disagree about run, queue, merge, or proof state
- provider emits token usage but no USD cost; do not interpret `cost_usd: 0.0`
  as free execution when token usage is present

Triage order: session `summary.json`, narrative logs, audit logs, proof packet,
screenshots/videos, harness `debug.log`, `verify.json`, `final-state.json`,
live records, command-channel acks, then `history.jsonl`.

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
