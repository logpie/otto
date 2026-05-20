# Otto

Otto is a local intent-to-product system for autonomous software work. It
turns a request like "build an expense approval portal" into a managed run:
compiles a flat product spec, decomposes the work, builds in parallel
worktrees, merges the children into an integration branch, verifies the
result against behavior journeys, and renders proof for a human to review.

Otto is not an editor plugin. It is a project-operations layer around
provider agents such as Codex and Claude, with durable logs, queueing,
recovery, evidence, and a web Mission Control surface for supervising the
work.

## What Otto Does

- Compiles natural-language intent into an editable flat product spec.
- Runs a root Lead that either builds inline or decomposes into child tasks.
- Dispatches up to N child tasks in parallel, each on its own worktree.
- Merges children upward into per-parent integration branches; eventually
  to `main`.
- Verifies behavior journeys (API + UI) against the integrated product.
- Records proof packets, logs, token usage, changed files, screenshots, and
  recovery state under `otto_logs/sessions/<session-id>/`.
- Queues runs into isolated git worktrees for parallel/dependent work.
- Provides local web Mission Control for launch, review, retry, cleanup,
  and run inspection.

## Quick Start

```bash
uv sync --extra claude --extra dev
uv run otto --help
```

From an existing git repository:

```bash
otto run "add saved searches to this dashboard"
```

The default provider is `claude`. Override with `--provider codex-app-server`
(Codex App Server's thread/turn protocol with local Codex subscription
auth), or `--provider codex` for the older `codex exec --json` adapter.

Run the web Mission Control portal:

```bash
otto web --port 9000
```

For phone or remote-device testing on a trusted network:

```bash
otto web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher
```

## Core Commands

```bash
# Intent-to-product
otto run "REST API for a todo app with SQLite"
otto run "expense approval portal" --budget 3600
otto run "build a markdown notebook" --review-first-decomp
otto run "add saved filters" --fresh                  # refuse to resume

# Queue parallel work
otto queue run "add saved filters" --as saved-filters
otto queue run "add CSV export" --as csv --tier modular
otto queue ls
otto queue start --concurrent 3 --exit-when-empty     # foreground watcher

# Review paused decomposition tasks (--review-first-decomp)
otto list-pending
otto review approve --task <id>
otto review cancel --task <id>
otto review edit --task <id> --intent "new intent"

# Proof and diagnostics
otto proof list
otto proof open
otto proof path <session-id>
otto proof render <session-id>
otto proof cleanup <run-id>
otto debug narrative <session-id>

# Web
otto web
otto web --project-launcher --projects-root ~/otto-projects
```

The legacy `otto build`, `otto certify`, and `otto improve` verbs were
removed; they now print a migration error pointing at `otto run`.

## Mission Control

Mission Control is the default product surface for Otto. It is a local web
app backed by the same queue, run registry, logs, artifacts, and session
state used by the CLI.

Use it to:

- Create or switch managed projects.
- Launch and monitor runs.
- Start and stop the queue watcher.
- Review live and completed run state.
- Inspect proof packets, logs, artifacts, diffs, screenshots, and token
  usage.
- Pause, resume, abort children, retry, requeue, clean up, or recover
  runs.
- Review project history and system health.

The web server binds to localhost by default. Remote binding requires
`--allow-remote`.

## Intent-To-Product Pipeline

`otto run` is the canonical surface:

```text
intent
  -> compile flat spec (behavior journeys + non-goals + done criteria)
  -> root Lead decomposes (or builds inline)
  -> dispatch child tasks on worktrees, in parallel
  -> verify each child against its journeys
  -> merge child branches upward to integration
  -> clean-deploy oracle (bootable + coherent)
  -> render proof-packet.html and proof-packet.json
```

The spec is the product contract for a run. It captures project kind,
behavior journeys, owned paths, non-goals, and done criteria.

Decomposition tiers (`--tier`):

- `solo` — force inline build, no children.
- `lead` — allow subtasks.
- `modular` — require architecture-first thinking.
- `auto` — Lead chooses based on intent complexity (default).

## Configuration (`otto.yaml`)

Provider defaults live in `otto.yaml`; CLI flags override them for one
run.

```yaml
provider: claude
model: null
effort: null
run_budget_seconds: 3600
max_turns_per_call: 200

# Optional per-agent overrides inherit the global provider/model/effort.
# agents:
#   build:     {provider: claude, model: null, effort: null}
#   certifier: {provider: claude, model: null, effort: null}
#   spec:      {provider: claude, model: null, effort: null}
#   fix:       {provider: claude, model: null, effort: null}

queue:
  concurrent: 3
  worktree_dir: .worktrees
  task_timeout_s: 4200
```

Useful one-off overrides:

```bash
otto run "add billing exports" \
  --budget 5400 \
  --max-turns 200 \
  --tree-budget-usd 50
```

## Queue And Worktrees

`otto queue` runs several jobs without mixing files:

1. Each queued task gets a branch and a worktree under
   `.worktrees/<task-id>/`.
2. `otto queue start` is a foreground watcher that dispatches up to
   `queue.concurrent` tasks.
3. Each task writes logs, manifests, checkpoints, and proof artifacts
   under its own session directory.
4. `otto queue ls / show / rm / cancel / resume / cleanup` expose
   status and recovery actions (also surfaced in Mission Control).

Run the watcher in a terminal, tmux pane, or through Mission Control.

## Evidence And Review

Every session writes its durable record under
`otto_logs/sessions/<session-id>/`. Important files include:

- `spec/spec.json` — compiled flat spec (validator-passed).
- `spec-state.jsonl` — append-only event journal of slice/group lifecycle.
- `build/narrative.log`, `build/messages.jsonl` — phase logs.
- `proof-packet.html`, `proof-packet.json` — final proof for human review.
- `summary.json`, `manifest.json`, and provider usage metadata.
- `queue/<slug>/repair-packet.json` — repair-agent feedback per child.

Use `otto proof open <session-id>` or Mission Control's run view for
human review.

## Recovery

Otto is designed for long-running local work where agents, browsers,
processes, budgets, and laptops can fail.

Recovery primitives include:

- Implicit resume: a second `otto run` against the same project picks up
  a paused or `merge_blocked` session. Use `--fresh` to refuse resume.
- `--review-first-decomp` pauses after the root Lead emits children so a
  human can approve/edit/cancel/replace them before they dispatch.
- Queue resume and cleanup for interrupted worktree tasks.
- Watcher heartbeat and stale-process detection.
- Session history preserved separately from live queue records.
- `otto debug narrative` to regenerate human-readable logs from raw
  messages.

## Development

```bash
# Fast local gates
uv run python scripts/test_tiers.py smoke
uv run python scripts/test_tiers.py fast

# Web gates
npm run web:typecheck
npm run web:build
uv run python scripts/test_tiers.py web
uv run python scripts/check_bundle_committed.py

# Lint
uv run ruff check otto scripts tests

# Browser tests
uv run python scripts/test_tiers.py browser-smoke
uv run python scripts/test_tiers.py browser

# Pre-push gate
uv run python scripts/test_tiers.py prepush
```

Use the smallest tier that matches the edit while iterating. `smoke` is
the smallest confidence gate. `fast` skips slow, integration, browser,
and heavy system tests. Run the `web` tier for Mission Control and
frontend/backend web changes, and run browser tests for user-visible
interaction changes.

The committed web bundle in `otto/web/static/` must be rebuilt after
changes in `otto/web/client/`;
`scripts/check_bundle_committed.py` verifies the committed bundle
against the current sources.

## Repository Layout

```text
otto/
  cli.py                 top-level CLI group + venv guard
  cli_run.py             canonical `otto run` intent-to-product CLI
  cli_review.py          `otto list-pending` / `otto review` commands
  cli_queue.py           queue CLI and watcher controls
  cli_proof.py           proof/debug artifact commands
  cli_options.py         shared click option validators
  agent/                 provider invocation + event parsing + bash safety
  v5_runner.py           orchestrator entry point (run_v5_pipeline)
  v5/
    dispatch.py          parallel child dispatch loop + lease
    merge.py             child-branch merge + integration propagation
    preflight_oracle.py  preflight + clean-deploy oracle
    repair.py            repair loops + amendment lifecycle
  v5_common.py           shared low-level helpers (git_capture, iso_now, …)
  spec_compile_flat.py   flat spec compiler (schema v4)
  lead.py                Lead primitive (run_lead)
  journey_*.py           behavior-journey contracts + executors
  queue/                 queue schema, runner, worktree dispatch
  mission_control/       shared model, actions, serializers, run view
  web/                   FastAPI app, React client, built static assets
tests/                   unit, integration, and browser tests
scripts/                 gates, fixtures, dogfood, and web-as-user harnesses
docs/                    architecture notes, RUA reports, design records
```

## Current Scope

Otto is currently a local, single-user system. It does not provide
hosted multi-user auth, cloud VM isolation, ticket-tracker integration,
or team RBAC. The design focus is reliability first: product specs,
evidence, recovery, provider diversity, queue isolation, and an operator
UI that makes autonomous work auditable.
