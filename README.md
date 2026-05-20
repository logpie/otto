# Otto

Otto is a local intent-to-product system for autonomous software work. It
turns a request like "build an expense approval portal" into a managed run:
compile a reviewable product spec, build it, integrate the pieces, audit the
integrated product, repair what fails, and render proof for a human to review.

Otto is not an editor plugin. It is a project operations layer around
provider agents (Claude, Codex App Server, Codex CLI) with durable logs,
queueing, worktree isolation, recovery, evidence, and a local web Mission
Control surface for supervising the work.

## Two Build Pipelines

Otto currently ships two intent-to-product pipelines side by side:

| Command | Pipeline | When to use |
| --- | --- | --- |
| `otto run` | **i2p (groups)** — flat group decomposition, dependency-ordered merge lane, integrated audit, repair loop | Greenfield product builds, brownfield improve/certify flows, all queue tasks today |
| `otto v5 run` | **v5 (hierarchical Lead)** — one universal Lead agent decomposes recursively, integration Lead per parent, CHARTER.md + decisions.md for arbitration | Multi-subsystem / multi-feature greenfield where a single agent can't hold the whole product in context |

The two share infrastructure: providers, MCP tools, session directories,
proof artifacts, Mission Control, recovery primitives. They differ in how
work is decomposed and how integration is reached.

If you don't know which to use, start with `otto run`.

## What Otto Does

- Compiles natural-language intent into an editable product spec
  (i2p) or a flat behavior-journeys + Lead session (v5).
- Builds greenfield products and improves/certifies existing projects.
- Runs work in scoped branches/worktrees with deterministic checks and
  bounded retries.
- Integrates pieces — either via a dependency-aware merge lane (i2p) or via
  hierarchical integration Leads with structured merge drivers (v5).
- Audits the integrated product independently and repairs failed features.
- Records proof packets, logs, token usage, changed files, screenshots, and
  recovery state under `otto_logs/sessions/<session-id>/`.
- Queues build/improve/certify jobs into isolated git worktrees.
- Provides local web Mission Control for launch, review, retry, cleanup, spec
  review, and run inspection.

## Quick Start

```bash
uv sync --extra claude --extra dev
uv run otto --help
```

From an existing git repository:

```bash
# Direct intent-to-product (i2p groups pipeline)
otto run "add saved searches to this dashboard"

# Brownfield workflows
otto improve bugs "look for auth and data isolation bugs"
otto certify "users can save, restore, rename, and delete saved searches"

# Hierarchical Lead pipeline (v5)
otto v5 run "URL shortener with admin dashboard and analytics" --tier modular
```

The default provider is `codex-app-server`, which uses Codex App Server's
thread/turn protocol with local Codex subscription auth. Other explicit
provider choices are `codex` for the `codex exec --json` adapter and
`claude` where configured. v5 currently defaults to `claude`. The API-key
based `openai-agents` experiment remains in the codebase but is not a normal
CLI or Mission Control path.

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
# Intent-to-product (i2p groups)
otto run "REST API for a todo app with SQLite"
otto run "expense approval portal" --budget 3600
otto run --project-kind cli "a small linter"
otto run --review-gate "build a markdown notebook"
otto run --resume --auto-approve

# Hierarchical Lead pipeline (v5)
otto v5 run "URL shortener with admin + analytics" --tier modular
otto v5 run "tiny CLI for X" --tier solo --phase1-only
otto v5 run "multi-feature SPA" --review-first-decomp
otto v5 list-pending
otto v5 review approve <task-id>

# Broken-state recovery (saves $100+ vs fresh re-run when bugs cascade)
otto v5 status                                # diagnostic: per-task verdicts + resume eligibility
otto v5 plan-resume                           # read-only: what would `otto v5 run` do? + cost/wall estimate
otto v5 reset-verdict --task <id>             # clear a verdict (correction)
otto v5 retry-children --task <id> --dry-run  # atomic targeted retry — validate plan
otto v5 retry-children --task <id>            # execute the validated plan

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

Mission Control is the default product surface for Otto. It is a local web
app backed by the same queue, run registry, logs, artifacts, and session
state used by the CLI.

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

## i2p (Groups) Pipeline

`otto run` drives the original product pipeline:

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

The spec is the product contract: project kind, structure, groups,
dependencies, owned paths, checks, non-goals, and done criteria. Runtime
terminology is **Group**. Older design notes may still say "slice".

## v5 (Hierarchical Lead) Pipeline

`otto v5 run` drives the universal-agent pipeline:

```text
intent
  -> compile flat spec (intent + behavior_journeys)
  -> root Lead session
       ├─ inline build  (mcp__otto__begin_inline)
       └─ decompose     (mcp__otto__submit_subtask × N)
  -> Lead writes CHARTER.md + decisions.md at repo root
  -> children dispatched up to --max-parallel, respecting depends_on
  -> per-parent integration Lead (i2p/integ/<task_id>) when children resolve
       └─ structured merge drivers union package.json, requirements.txt,
          .gitignore, pytest.ini, tsconfig.json, decisions.md
  -> verify_from_clean primitive: scaffold compile + smoke deploy
  -> root Lead's verdict becomes the run verdict
  -> proof / summary written to otto_logs/sessions/<session-id>/
```

A **Lead** is one universal agent session — same runner at every level of
the tree, only the prompt differs. Leads decide their own decomposition
depth via two MCP tools: `submit_subtask` (emit a child) or
`begin_inline` (build this scope myself). Verification is mandatory and
gated by an MCP `verify` tool — text claims of "pass" without `verify`
are downgraded to `unverified`.

**CHARTER.md** is the architect's slow-changing design doc (stack,
conventions, inter-subsystem contracts). **decisions.md** is an
append-only union-mergeable log of boundary decisions. Both live at the
repo root; they're written by the root Lead and consumed by every
descendant.

Decomposition tiers control how the root Lead behaves:

- `solo` — force inline. Single-scope products.
- `lead` — allow subtasks. Multi-area products.
- `modular` — require architecture-first thinking. Multi-subsystem products.
- `auto` (default) — Lead chooses.

`--review-first-decomp` pauses the run after the root Lead emits children so
you can inspect or edit the graph via `otto v5 list-pending` / `otto v5
review` or Mission Control before children dispatch.

## Configuration (`otto.yaml`)

Provider defaults live in `otto.yaml`; CLI flags override them for one run.

```yaml
provider: codex-app-server
model: null
effort: null
run_budget_seconds: 3600
max_turns_per_call: 200

# Optional per-agent overrides inherit the global provider/model/effort.
# agents:
#   build:     {provider: codex-app-server, model: null, effort: null}
#   certifier: {provider: codex-app-server, model: null, effort: null}
#   spec:      {provider: codex-app-server, model: null, effort: null}
#   fix:       {provider: codex-app-server, model: null, effort: null}

queue:
  concurrent: 3
  worktree_dir: .worktrees
  task_timeout_s: 4200

build:
  group_concurrent: 3
```

Useful one-off overrides:

```bash
otto run "add billing exports" \
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

The watcher is intentionally a foreground process. Run it in a terminal,
tmux pane, or through Mission Control.

v5 child Leads use a distinct branch namespace:

- `i2p/build/<task-id>` — child build branches
- `i2p/integ/<task-id>` — parent integration branches

This keeps v5 work isolated from the queue's task worktrees.

## Evidence And Review

Every session writes its durable record under `otto_logs/sessions/<session-id>/`.
Important files include:

- `spec/spec.json` (i2p) or `spec/flat-spec.json` (v5) and spec review sidecars.
- `spec-state.jsonl` for phase, pause/resume, abort, and group events (i2p).
- `task-graph.json` and `v5_pending.jsonl` for the hierarchical task graph (v5).
- `build/`, `merge/`, `audit/`, and `repair/` phase logs (i2p).
- Per-Lead session dirs with `verdict.json`, `verify-result.json` (v5).
- `proof-packet.html` and `proof-packet.json` (i2p).
- `summary.json`, `manifest.json`, and provider usage metadata.
- Raw provider messages when debug logging is enabled.

Use `otto proof open <session-id>` or Mission Control's run view for human
review.

## Recovery

Otto is designed for long-running local work where agents, browsers,
processes, budgets, and laptops can fail.

Recovery primitives include:

- `otto run --resume` for paused i2p sessions.
- Spec review approval/regeneration through Mission Control.
- v5 `--review-first-decomp` pause + `otto v5 review` resume.
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
system tests. Run the `web` tier for Mission Control and frontend/backend
web changes, and run browser tests for user-visible interaction changes.

The committed web bundle in `otto/web/static/` must be rebuilt after changes
in `otto/web/client/`; `scripts/check_bundle_committed.py` verifies the
committed bundle against the current sources.

## Repository Layout

```text
otto/
  cli.py                 top-level compatibility and brownfield commands
  cli_run.py             canonical `otto run` (i2p groups) CLI
  cli_v5.py              `otto v5` hierarchical Lead CLI
  cli_queue.py           queue CLI and watcher controls
  cli_proof.py           proof/debug artifact commands

  # i2p (groups) pipeline
  runner.py              compile -> build -> merge -> audit -> repair -> render
  spec_compile.py        spec schema and compiler entrypoint
  spec_state.py          append-only run event journal
  build.py               group build orchestration
  merge_queue.py         i2p group merge lane
  audit.py               integrated-product audit
  audit_loop.py          repair/re-audit loop

  # v5 hierarchical pipeline
  lead.py                universal Lead primitive
  lead_verify.py         verify-tool implementation
  v5_runner.py           hierarchical run coordinator
  v5_branching.py        i2p/build vs i2p/integ branch namespaces
  v5_merge_drivers.py    structured union drivers (package.json, etc.)
  v5_clean_verify.py     verify_from_clean primitive
  v5_preflight.py        deterministic task-graph pre-flight checks
  v5_review.py           pending-review workflow

  # Shared
  agent.py               provider invocation and message normalization
  mcp_tools.py           Otto's SDK MCP server (submit_subtask, verify, ...)
  render.py              proof packet renderer
  paths.py               session/artifact path construction
  queue/                 queue schema, runner, worktree dispatch, task_graph
  mission_control/       shared model, actions, serializers, run view
  web/                   FastAPI app, React client, built static assets
  prompts/               Lead, build, audit, spec, certifier prompt templates

tests/                   unit, integration, and browser tests
scripts/                 gates, fixtures, dogfood, and web-as-user harnesses
docs/                    architecture notes, RUA reports, design records
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system architecture for
  both pipelines, runtime modules, MCP tools, recovery, providers.
- [`docs/workflows.md`](docs/workflows.md) — user-facing workflow recipes:
  greenfield, brownfield, queue, recovery, debugging.
- [`docs/intent-to-product-design.md`](docs/intent-to-product-design.md) —
  original i2p design doc.
- [`docs/rua/`](docs/rua/) — Mission Control RUA (real-user audit) reports.

## Current Scope

Otto is currently a local, single-user system. It does not provide hosted
multi-user auth, cloud VM isolation, ticket-tracker integration, or team RBAC.
The design focus is reliability first: product specs, evidence, recovery,
provider diversity, queue isolation, and an operator UI that makes
autonomous work auditable.
