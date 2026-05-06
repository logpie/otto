# Otto

Otto is a local intent-to-product system for autonomous software work. It turns
a request like "build an expense approval portal" into a managed run: compile a
reviewable product spec, build it in scoped groups, merge the groups, audit the
integrated product, repair what fails, and render proof for a human to review.

Otto is not an editor plugin. It is a project operations layer around provider
agents such as Codex and Claude, with durable logs, queueing, recovery,
evidence, and a web Mission Control surface for supervising the work.

## What Otto Does

- Compiles natural-language intent into an editable product spec.
- Builds greenfield products and improves/certifies existing projects.
- Runs group-based build work with deterministic checks and bounded retries.
- Merges groups into the integrated worktree during `otto run`.
- Audits the integrated product independently and repairs failed features.
- Records proof packets, logs, token usage, changed files, screenshots, and
  recovery state under `otto_logs/sessions/<session-id>/`.
- Queues build, improve, and certify jobs into isolated git worktrees.
- Provides local web Mission Control for launch, review, retry, cleanup, spec
  review, and run inspection.

## Quick Start

```bash
uv sync --extra claude --extra dev
uv run otto --help
```

From an existing git repository:

```bash
otto run "add saved searches to this dashboard" --provider codex
otto improve bugs "look for auth and data isolation bugs" --provider codex
otto certify "users can save, restore, rename, and delete saved searches" --provider codex
```

Run the web Mission Control portal:

```bash
otto web --port 9000
```

For phone or remote-device testing on a trusted network:

```bash
otto web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher
```

`otto dashboard` remains as a compatibility alias for `otto web`. The old
Textual TUI has been removed.

## Core Commands

```bash
# Intent-to-product
otto run "REST API for a todo app with SQLite"
otto run "expense approval portal" --provider codex --budget 3600
otto run --project-kind cli "a small linter"
otto run --review-gate "build a markdown notebook"
otto run --resume --auto-approve

# Existing-product workflows
otto improve bugs "find broken recovery and data isolation paths"
otto improve feature "make the review workflow clearer"
otto improve target "all API tests pass and p95 latency < 100ms"
otto certify "admin users can approve or reject expenses" --standard

# Queue parallel work
otto queue build "add saved filters" --as saved-filters
otto queue improve bugs "audit error handling" -- --rounds 3
otto queue certify "release candidate" -- --standard
otto queue run --concurrent 3 --exit-when-empty

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

Compatibility aliases are still discoverable in `otto --help` for older
scripts and agents:

- `otto build` routes to the i2p stack when selected by config or `--i2p`;
  prefer `otto run` for new direct intent-to-product usage.
- `otto history` is an alias for `otto proof list`.
- `otto render` is an alias for `otto proof render`.
- `otto pow` is an alias for `otto proof open` / `otto proof path`.
- `otto replay` is an alias for `otto debug narrative`.
- `otto cleanup` is an alias for `otto proof cleanup`.

## Mission Control

Mission Control is the default product surface for Otto. It is a local web app
backed by the same queue, run registry, logs, artifacts, and session state used
by the CLI.

Use it to:

- Create or switch managed projects.
- Launch build, improve, certify, and run workflows.
- Start and stop the queue watcher.
- Review live and completed run state.
- Inspect proof packets, logs, artifacts, diffs, screenshots, and token usage.
- Edit and approve specs at the review gate.
- Pause, resume, abort groups, retry, requeue, clean up, or recover runs.
- Review project history and system health.

The web server binds to localhost by default. Remote binding requires
`--allow-remote`.

## Intent-To-Product Pipeline

`otto run` is the canonical direct i2p surface:

```text
intent
  -> compile spec
  -> optional spec review gate
  -> seed/audit fixtures when needed
  -> build groups on branches
  -> merge eligible groups into the integrated worktree
  -> audit the integrated product
  -> repair failed features when possible
  -> render proof-packet.html and proof-packet.json
```

The spec is the product contract for a run. It captures project kind,
structure, groups, dependencies, owned paths, checks, non-goals, and done
criteria. Runtime terminology is **Group**: older design notes may still use
"slice" in historical context.

## Providers And Runtime Controls

Provider defaults live in `otto.yaml`; CLI flags override them for one run.

```yaml
provider: codex
model: null
effort: null
run_budget_seconds: 3600
max_turns_per_call: 200

agents:
  build:
    provider: codex
    model: null
    effort: null
  certifier:
    provider: codex
    model: null
    effort: null
  fix:
    provider: codex
    model: null
    effort: null

queue:
  concurrent: 3
  worktree_dir: .worktrees
  task_timeout_s: 4200
```

Useful one-off overrides:

```bash
otto run "add billing exports" \
  --provider codex \
  --build-effort high \
  --certifier-effort high \
  --budget 5400 \
  --max-turns 200
```

## Queue And Worktrees

`otto queue` runs several jobs without mixing files:

1. Each queued task gets a branch and a worktree under `.worktrees/<task-id>/`.
2. The foreground watcher dispatches up to `queue.concurrent` tasks.
3. Each task writes logs, manifests, checkpoints, and proof artifacts.
4. Mission Control and `otto queue ls/show` expose status and recovery actions.
5. Cleanup removes finished or abandoned queue worktrees without deleting the
   preserved session history.

The watcher is intentionally a foreground process. Run it in a terminal, tmux
pane, or through Mission Control.

## Evidence And Review

Every session writes its durable record under `otto_logs/sessions/<session-id>/`.
Important files include:

- `spec/spec.json` and spec review sidecars.
- `spec-state.jsonl` for phase, pause/resume, abort, and group events.
- `build/`, `merge/`, `audit/`, and `repair/` phase logs.
- `proof-packet.html` and `proof-packet.json`.
- `summary.json`, `manifest.json`, and provider usage metadata.
- Raw provider messages when debug logging is enabled.

Use `otto proof open <session-id>` or Mission Control's run view for human
review.

## Recovery

Otto is designed for long-running local work where agents, browsers, processes,
budgets, and laptops can fail.

Recovery primitives include:

- `otto run --resume` for paused i2p sessions.
- Spec review approval/regeneration through Mission Control.
- Queue resume and cleanup for interrupted worktree tasks.
- Watcher heartbeat and stale-process detection.
- Session history preserved separately from live queue records.
- `otto debug narrative` to regenerate human-readable logs from raw messages.

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

Use the smallest tier that matches the edit while iterating. `smoke` is the
smallest confidence gate. `fast` skips slow, integration, browser, and heavy
system tests. Run the `web` tier for Mission Control and frontend/backend web
changes, and run browser tests for user-visible interaction changes.

The committed web bundle in `otto/web/static/` must be rebuilt after changes in
`otto/web/client/`; `scripts/check_bundle_committed.py` verifies the committed
bundle against the current sources.

## Repository Layout

```text
otto/
  cli.py                 top-level compatibility and brownfield commands
  cli_run.py             canonical `otto run` intent-to-product CLI
  cli_queue.py           queue CLI and watcher controls
  cli_proof.py           proof/debug artifact commands
  agent.py               provider invocation and message normalization
  build.py               group build orchestration
  merge_queue.py         i2p group merge lane
  audit.py               integrated-product audit
  audit_loop.py          repair/re-audit loop
  runner.py              compile -> build -> merge -> audit -> repair -> render
  spec_compile.py        intent/spec schema and compiler entrypoint
  spec_state.py          append-only run event journal
  render.py              proof packet renderer
  queue/                 queue schema, runner, worktree dispatch
  mission_control/       shared model, actions, serializers, run view
  web/                   FastAPI app, React client, built static assets
tests/                   unit, integration, and browser tests
scripts/                 gates, fixtures, dogfood, and web-as-user harnesses
docs/                    architecture notes, RUA reports, design records
```

## Current Scope

Otto is currently a local, single-user system. It does not provide hosted
multi-user auth, cloud VM isolation, ticket-tracker integration, or team RBAC.
The design focus is reliability first: product specs, evidence, recovery,
provider diversity, queue isolation, and an operator UI that makes autonomous
work auditable.
