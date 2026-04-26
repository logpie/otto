# Otto

Otto is a local reliability harness for autonomous product-building agents. It turns an intent like “build an expense approval portal” into a managed loop: specify, build, certify, fix, review, and land.

Otto is not an editor plugin. It is a project operations layer around agent providers such as Claude and Codex, with evidence, queueing, recovery, and a web Mission Control surface for supervising the work.

## What Otto Does

- Builds greenfield or existing projects from natural language intent.
- Runs independent certification against the finished product.
- Feeds certification failures back into fix rounds.
- Queues multiple tasks into isolated git worktrees.
- Lands successful branches back into the target branch.
- Records proof, logs, token usage, changed files, and recovery state.
- Provides a local web UI for launching, monitoring, reviewing, retrying, and landing work.

## Quick Start

```bash
uv sync --extra claude --extra dev
uv run otto --help
```

From an existing git repository:

```bash
otto build "add saved searches to this dashboard" --spec
otto certify "users can save, restore, rename, and delete saved searches"
otto improve bugs "look for auth and data isolation bugs"
```

Run the web Mission Control portal:

```bash
otto web --port 9000
```

For phone or remote-device testing on a trusted network:

```bash
otto web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher
```

`otto dashboard` is kept as a compatibility alias for `otto web`. The old Textual TUI has been removed.

## Mission Control

Mission Control is the default product surface for Otto. It is a local web app backed by the same queue, run registry, logs, artifacts, and merge state used by the CLI.

Use it to:

- Create or switch managed projects.
- Add build, improve, and certify jobs.
- Start and stop the queue watcher.
- Watch running jobs and live usage.
- Open review packets with proof, logs, artifacts, and diffs.
- Try generated products when Otto can infer how to run them.
- Retry, resume, clean up, or land work.
- Review project-level history, token usage, and system health.

The web server is local by default. Remote binding requires `--allow-remote`.

## Core Commands

```bash
# Build
otto build "REST API for a todo app with SQLite"
otto build "expense approval portal" --spec
otto build "add CSV export" --provider codex --certifier-provider claude

# Certify
otto certify
otto certify "admin users can approve or reject expenses" --thorough

# Improve
otto improve bugs
otto improve feature "make the review workflow clearer"
otto improve target "all API tests pass and p95 latency < 100ms"

# Queue parallel work
otto queue build "add saved filters" --as saved-filters
otto queue build "add CSV export" --as csv-export
otto queue improve bugs "audit error handling" -- --rounds 3
otto queue run --concurrent 3 --exit-when-empty

# Land finished work
otto merge --all
otto merge --all --no-certify

# Web
otto web
otto web --project-launcher --projects-root ~/otto-projects
```

## Agent Modes

Otto supports two execution styles:

- **Agentic mode**: one long-running agent session owns build, fix, and certification loops.
- **Split mode**: spec, build, improve/fix, and certifier phases can use separate provider/model/reasoning settings.

Split mode is the default direction for reliability because it makes roles explicit and improves observability. You can mix providers across phases, for example Codex for build and Claude for certification.

Example:

```bash
otto build "add role-based access control" \
  --provider codex \
  --certifier-provider claude \
  --thorough
```

## Configuration (`otto.yaml`)

Create `otto.yaml` in the project root to set durable defaults:

```yaml
provider: codex
model: null
effort: null
split_mode: true
certifier_mode: standard
run_budget_seconds: 3600
spec_timeout: 600
max_certify_rounds: 8

# Phase-specific overrides are optional; omitted values inherit provider/model/effort.
# agents:
#   build:
#     provider: codex
#     model: null
#     effort: null
agents:
  build:
    provider: codex
    model: null
    effort: null
  certifier:
    provider: claude
    model: null
    effort: null

queue:
  concurrent: 3
  worktree_dir: .worktrees
  task_timeout_s: 4200
  merge_certifier_mode: standard
```

CLI flags override `otto.yaml` for a single invocation.

## Queue And Worktrees

`otto queue` lets you run several jobs in parallel without mixing files:

1. Each queued task gets a branch and a worktree under `.worktrees/<task-id>/`.
2. The watcher dispatches up to `queue.concurrent` tasks.
3. Each task writes logs, manifests, checkpoints, and proof artifacts.
4. Finished tasks become reviewable in Mission Control.
5. `otto merge --all` lands ready branches into the target branch.

The watcher is intentionally a foreground process. Run it in a terminal, tmux pane, or through Mission Control.

## Evidence And Review

Certification produces proof-of-work artifacts under `otto_logs/`, including:

- Per-story pass/fail results.
- Browser, API, or CLI evidence.
- Logs and phase summaries.
- Changed-file lists and diffs.
- Token usage and provider metadata when available.
- HTML proof reports for human review.

Mission Control turns those artifacts into review packets so a user can decide whether to trust and land the work.

## Recovery

Otto is designed for long-running local work where agents, browsers, processes, and budgets can fail.

Recovery primitives include:

- Checkpoints for resumable interrupted work.
- Queue state and command journals.
- Watcher heartbeat and stale-process detection.
- Cleanup actions for failed or orphaned runs.
- Structured history for completed runs.
- Preserved artifacts even when a task is removed from the active queue.

## Development

```bash
# Python tests
uv run pytest -q

# Lint
uv run ruff check otto scripts tests

# Web client
npm install
npm run web:typecheck
npm run web:build

# Browser tests
OTTO_BROWSER_SKIP_BUILD=1 uv run pytest -q -m browser -p playwright
```

The committed web bundle in `otto/web/static/` must be rebuilt after changes in `otto/web/client/`.

## Repository Layout

```text
otto/
  cli.py                 top-level CLI
  cli_queue.py           queue CLI
  pipeline.py            build/certify/fix orchestration
  agent.py               provider invocation
  token_usage.py         shared token accounting
  queue/                 queue schema, runner, worktree dispatch
  mission_control/       shared model, actions, serializers, web service
  web/                   FastAPI app and built frontend assets
tests/                   unit, integration, and browser tests
scripts/                 benchmarks and web-as-user harnesses
docs/                    design notes and audit records
```

## Current Scope

Otto is currently a local, single-user system. It does not yet provide hosted multi-user auth, cloud VM isolation, ticket-tracker integration, or team RBAC. The design focus is reliability first: evidence, recovery, provider diversity, queue isolation, and an operator UI that makes autonomous work auditable.
