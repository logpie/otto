# Otto Architecture

Status: current runtime reference as of May 2026, covering both the i2p
groups pipeline (`otto run`) and the v5 hierarchical Lead pipeline
(`otto v5 run`).

Older design and audit documents may still say "slice", "PoW", or
"v3 pipeline". The current runtime uses **Group** for i2p build units,
**Lead** for v5 agent sessions, **proof packet** for rendered evidence,
and `otto run` / `otto v5 run` as the canonical direct intent-to-product
commands.

## Product Model

Otto is a local intent-to-product control system. Two pipelines coexist:

```text
i2p (groups) — otto run
  intent
    -> spec
    -> grouped build work on per-group branches
    -> dependency-ordered merge lane into integrated worktree
    -> integrated-product audit
    -> repair loop when needed
    -> proof packet

v5 (hierarchical Lead) — otto v5 run
  intent
    -> flat spec (intent + behavior_journeys)
    -> root Lead session
         ├─ inline build, OR
         └─ recursive decomposition into child Leads
    -> CHARTER.md + decisions.md authored at the root
    -> per-parent integration Leads with structured merge drivers
    -> verify_from_clean on every level
    -> root verdict + summary
```

The main surfaces are:

- CLI for direct runs, existing-product improvement, certification,
  queueing, proof inspection, and diagnostics.
- Mission Control web UI for managed projects, live run review, spec review,
  proof inspection, queue watcher control, and recovery actions.
- File-backed logs and state under `otto_logs/` so every run can be audited
  without a database.

## CLI Surface

Current top-level commands:

```text
otto run        canonical i2p intent-to-product run
otto v5 run     hierarchical-Lead intent-to-product run
otto improve    brownfield bug/feature/target work
otto certify    brownfield independent audit
otto queue      file-backed queue and worktree watcher
otto web        local Mission Control
otto proof      proof packet and run-artifact inspection
otto debug      developer diagnostics for existing sessions
otto setup      project instruction bootstrap
```

Compatibility commands remain visible for agents and older scripts:

```text
otto build      compatibility build command; prefer `otto run`
otto history    alias for `otto proof list`
otto render     alias for `otto proof render`
otto pow        alias for `otto proof open/path`
otto replay     alias for `otto debug narrative`
otto cleanup    alias for `otto proof cleanup`
otto dashboard  alias for `otto web`
```

The top-level CLI does not expose a standalone `otto merge` surface. The
i2p direct run has an internal merge lane in `merge_queue.py`; v5 has
its own integration Leads (`v5_runner.py`) backed by structured merge
drivers (`v5_merge_drivers.py`).

## i2p (Groups) Pipeline

`otto run` drives the full i2p product pipeline:

```text
┌────────┐
│ intent │
└───┬────┘
    ▼
┌─────────────────────┐
│ compile spec         │  spec_compile.py
│ - project kind       │
│ - structure          │
│ - groups             │
│ - checks             │
└───┬─────────────────┘
    │ optional --review-gate
    ▼
┌─────────────────────┐
│ build groups         │  build.py
│ - branch/worktree    │
│ - owned paths        │
│ - deterministic gate │
│ - retry same group   │
└───┬─────────────────┘
    ▼
┌─────────────────────┐
│ merge lane           │  merge_queue.py
│ - dependency order   │
│ - serial integration │
│ - blocked groups     │
└───┬─────────────────┘
    ▼
┌─────────────────────┐
│ audit integrated app │  audit.py
│ - feature verdicts   │
│ - findings           │
│ - evidence requests  │
└───┬─────────────────┘
    │ if repairable failure
    ▼
┌─────────────────────┐
│ repair / re-audit    │  audit_loop.py
└───┬─────────────────┘
    ▼
┌─────────────────────┐
│ render proof packet  │  render.py
└─────────────────────┘
```

`runner.py` is the phase coordinator. It owns phase transitions, pause
checks, budget propagation, resume behavior, and proof rendering.

### i2p Spec Contract

The spec is the durable product contract for an i2p session.

Canonical persisted path:

```text
otto_logs/sessions/<session-id>/spec/spec.json
```

Important fields:

- `intent`: user-visible goal.
- `project_kind`: `webapp`, `cli`, `library`, or `api`.
- `structure`: project-kind-specific routing/API/component/file decisions.
- `groups`: build units with dependencies, owned paths, features, and checks.
- `cross_group_checks`: checks that matter only after integration.
- `non_goals` and `done_means`: boundaries and completion criteria.

Historical docs use "slice" for the same rough concept. Runtime code and
web surfaces use "Group".

### i2p Checks

Otto prefers deterministic checks during build and focused LLM judgment
during audit.

Check kinds include:

- `RepoTestCheck`: arbitrary repo command with expected exit/stdout.
- `PytestCheck`: pytest selector or expression.
- `ApiProbe`: HTTP request/response contract.
- `BrowserJourney`: browser-level journey and screenshot evidence.
- `StateInvariant`: durable state predicate or project-layout invariant.

The compiler produces checks fit for the project kind. Webapps need
browser/HTTP evidence when feasible; CLI and library projects lead with
terminal output, repo tests, usage examples, and durable state evidence.

## v5 (Hierarchical Lead) Pipeline

`otto v5 run` drives the universal-agent pipeline. The core primitive is a
**Lead** — one `query()` call against a provider SDK with Otto's MCP
tools attached. Same runner at every level; only the prompt differs.

```text
┌──────────────────────────┐
│ intent                   │
└───┬──────────────────────┘
    ▼
┌──────────────────────────┐
│ compile_flat_spec        │  spec_compile_flat.py
│ - intent                 │
│ - behavior_journeys      │
└───┬──────────────────────┘
    ▼
┌──────────────────────────┐         ┌──────────────────────────┐
│ root Lead (lead.md)      │────────▶│ inline build             │
│ - reads CHARTER/decisions│         │ - writes code + tests    │
│ - writes CHARTER.md      │         │ - calls mcp__otto__verify│
│ - decides decomposition  │         └───────────┬──────────────┘
└───┬──────────────────────┘                     │
    │ submit_subtask × N                         │
    ▼                                            │
┌──────────────────────────┐                     │
│ child Leads (in parallel │                     │
│ up to --max-parallel,    │                     │
│ honoring depends_on)     │                     │
│ - i2p/build/<task-id>    │                     │
│ - per-child worktree     │                     │
│ - own CHARTER respect    │                     │
└───┬──────────────────────┘                     │
    │ children resolve                           │
    ▼                                            │
┌──────────────────────────┐                     │
│ structured merge drivers │  v5_merge_drivers.py│
│ - package.json union     │                     │
│ - requirements.txt union │                     │
│ - .gitignore union       │                     │
│ - decisions.md union     │                     │
│ - lockfile discard       │                     │
└───┬──────────────────────┘                     │
    ▼                                            │
┌──────────────────────────┐                     │
│ integration Lead         │                     │
│ (lead-integration.md)    │                     │
│ - i2p/integ/<parent-id>  │                     │
│ - cross-stack E2E        │                     │
│ - mcp__otto__verify      │                     │
└───┬──────────────────────┘                     │
    │                                            │
    ▼                                            ▼
┌────────────────────────────────────────────────┐
│ verify_from_clean primitive                    │  v5_clean_verify.py
│ - copy project to temp (no node_modules/.git)  │
│ - install deps fresh                           │
│ - scaffold scope: run build only               │
│ - subtree scope:  build + start.sh + probes    │
└─────────────────────────┬──────────────────────┘
                          ▼
              ┌─────────────────────────┐
              │ root verdict + summary  │
              └─────────────────────────┘
```

`v5_runner.py` is the hierarchical coordinator. It owns the asyncio task
loop, depends_on resolution, integration-Lead dispatch, tree-cost budgeting,
and best-effort recovery on child crashes.

### Lead Verdicts

Every Lead writes `verdict.json` to its session dir. Possible verdicts:

| Verdict | Meaning |
| --- | --- |
| `pass` | `mcp__otto__verify` returned pass; this scope works. |
| `partial` | Some journeys pass, some fail; honest mixed result. |
| `unverified` | Lead returned without calling `verify`; downgraded automatically. |
| `merge_blocked` | Integration Lead could not produce a clean merge. |
| `pending_children` | Lead emitted subtasks; waiting for them to resolve. |
| `catastrophic` | Uncaught exception; summary written best-effort. |

The runner refuses to mark a Lead `pass` purely on text claims. The MCP
`verify` tool is the only PASS gate.

### CHARTER.md and decisions.md

Two files at the repo root underwrite v5 hierarchical work:

- **CHARTER.md** — the architect's slow-changing design doc: stack,
  conventions, inter-subsystem contracts (endpoints, schemas, ports).
  Authored by the root Lead during architect-first decomposition.
  Binding on every descendant.
- **decisions.md** — an append-only, union-mergeable log of boundary
  decisions made by sibling agents and arbitrations made by parent
  agents. Each entry is a single line. Past entries are binding for
  descendants.

Both files are written into the project tree (not `otto_logs/`) so they
become part of the produced product when desired.

### v5 MCP Tool Surface

`otto/mcp_tools.py` exposes an SDK MCP server to every Lead session:

| Tool | Purpose |
| --- | --- |
| `mcp__otto__submit_subtask` | Emit a child task. `parent_task_id` is recorded automatically. Returns `task_id`. Idempotent on `(parent, intent_hash)`. |
| `mcp__otto__begin_inline` | Commit to building this scope inline (no children). |
| `mcp__otto__verify` | Run the audit's behavior journeys; return verdict + evidence. The PASS gate. |
| `mcp__otto__certify_scaffold` | Lightweight verify for the Architect phase — run a build command, capture results. |

### v5 Branch Namespaces

To avoid collisions between child build worktrees and parent integration
worktrees, v5 splits branches into two namespaces:

- `i2p/build/<task-id>` — child build branches (one per Lead).
- `i2p/integ/<parent-task-id>` — parent integration branches.

`integration_branch_name(parent_task_id)` and `child_branch_name(task_id)`
in `v5_branching.py` are the single source of truth.

### Pre-flight Checks

`v5_preflight.py` runs deterministic checks before any child dispatches:

- Architect must inline (not sub-decompose).
- CHARTER.md must exist after architect-pass.
- DAG cycles in `depends_on` are blocked.
- Duplicate task IDs are blocked.
- Scaffold must compile (via `verify_from_clean(scope=scaffold)`).
- Smoke clean-deploy must succeed (via `verify_from_clean(scope=subtree)`).

Pre-flight issues at `block` severity refuse dispatch; `error` issues
log and emit events; `warn` issues are advisory.

### Decomposition Tiers

`otto v5 run --tier` controls how the root Lead behaves:

- `solo` — force inline. Single-scope products.
- `lead` — allow subtasks. Multi-area products.
- `modular` — require architecture-first thinking. Multi-subsystem products.
- `auto` (default) — Lead chooses.

### Review-First Decomposition

`--review-first-decomp` pauses the run after the root Lead emits children.
`otto v5 list-pending` shows the queue; `otto v5 review approve <id>`
resumes. Sub-Leads' decompositions remain autonomous.

## Providers

Provider settings are read from `otto.yaml` and can be overridden by CLI
flags.

Global overrides:

```text
--provider
--model
--effort
--budget
--max-turns
```

Phase-specific overrides (i2p):

```text
--build-provider / --build-model / --build-effort
--certifier-provider / --certifier-model / --certifier-effort
--fix-provider / --fix-model / --fix-effort
```

Provider choices today:

- `codex-app-server`: default for i2p. Otto starts `codex app-server` over
  stdio, uses its structured thread/turn protocol, preserves local
  ChatGPT/Codex subscription auth, captures app-server token usage / diff
  events, routes approval requests through Otto's provider safety checks,
  and passes structured output schemas to `turn/start`.
- `codex`: fallback Codex CLI subprocess integration. Otto runs `codex exec
  --json`, normalizes JSONL into Otto's message/log format.
- `claude`: Claude SDK integration. Current default for `otto v5 run`.
  Required for the MCP-tool flow because v5 depends on the Claude Agent
  SDK's `create_sdk_mcp_server`.

The API-key based `openai-agents` experiment remains available in code for
explicit local experiments, but it is not a normal Mission Control or CLI
path and is not the default subscription-backed provider.

The outer orchestrator is durable Otto state; provider sessions are
disposable inner workers.

## Mission Control

Mission Control is a FastAPI + React application:

```text
otto/web/app.py                      FastAPI app and route wiring
otto/web/run_view_routes.py          run-view API
otto/web/spec_review_routes.py       spec review/edit API
otto/mission_control/run_view.py     server-side run-view model
otto/mission_control/model.py        live/history/project state
otto/mission_control/actions.py      queue/run/spec/recovery actions
otto/web/client/src/                 React/TypeScript client
otto/web/static/                     committed production bundle
```

The frontend has been through the 2026-05-05 RUA campaign. Audit reports
and screenshots live under `docs/rua/`. The local skills that drive the
audit live at `.codex/skills/otto-frontend-rua/SKILL.md` and
`.claude/skills/otto-frontend-rua/SKILL.md`; both require product-level
first-screen inspection, not just component or route checks.

Key UI concepts:

- `RunListLanding`: project landing and run list.
- `RunDrawer` / `RunViewPage`: live and historical run inspection.
- `VerdictHeader`: proof/audit status summary.
- `StageTimeline`: phase status and progression.
- `SpecReviewPage`: editable spec review gate.
- `SpecDiffPage`: version diff and approval context.
- `AppShell`: global layout, navigation, and chrome.

The committed web bundle must match source. Use:

```bash
npm run web:typecheck
npm run web:build
uv run python scripts/check_bundle_committed.py
```

## Queue And Worktrees

`otto queue` is a file-backed dispatcher for parallel work.

Core files:

```text
.otto-queue.yml                  queued task definitions
.otto-queue-state.json           task status and child metadata
.otto-queue-commands.jsonl       watcher command journal
.worktrees/<task-id>/            isolated task worktree
```

The watcher is a foreground process:

```bash
otto queue run --concurrent 3
```

The watcher:

1. Drains command requests.
2. Reaps children.
3. Dispatches new work up to the concurrency cap.
4. Tracks child PID/PGID/cwd with PID-reuse-safe metadata.
5. Updates live run registry and queue state.
6. Applies cancellation, cleanup, and resume commands.

Queued child commands still invoke Otto CLI commands (`build`, `improve`,
or `certify`) for compatibility. New direct product builds should prefer
`otto run` or `otto v5 run`; queue support for a first-class `run` task is
a future cleanup area.

## Artifacts And State

Canonical session directory:

```text
otto_logs/sessions/<session-id>/
```

Important i2p artifacts:

| Path | Purpose |
| --- | --- |
| `spec/spec.json` | compiled product contract |
| `spec-state.jsonl` | append-only phase/group/review journal |
| `build/` | group build logs and provider messages |
| `merge/` | merge-lane logs and evidence |
| `audit/` | integrated audit logs and findings |
| `repair/` | targeted fix/re-audit evidence |
| `proof-packet.html` | human proof packet |
| `proof-packet.json` | machine-readable proof packet |
| `summary.json` | final verdict, cost, timing, provider metadata |
| `manifest.json` | session artifact index |

Important v5 artifacts:

| Path | Purpose |
| --- | --- |
| `spec/flat-spec.json` | flat compiled spec (intent + journeys) |
| `task-graph.json` | hierarchical task graph: tasks, parents, depends_on, verdicts, costs |
| `v5_pending.jsonl` | queue of ready-to-dispatch child tasks |
| `<task-id>/` | per-Lead session subdir with `verdict.json`, agent messages, `verify/` outputs |
| `verify/verify-result.json` | per-scope verify run result |
| `summary.json` | root verdict, total tree cost, duration |

Use:

```bash
otto proof list
otto proof open <session-id>
otto proof path <session-id>
otto proof render <session-id>
otto debug narrative <session-id>
```

`otto debug narrative` regenerates readable narrative logs from raw
`messages.jsonl` without rerunning providers.

## Resume And Recovery

i2p resume state is session-scoped:

```text
otto_logs/paused -> otto_logs/sessions/<session-id>
otto_logs/sessions/<session-id>/checkpoint.json
```

Recovery rules:

- `otto run --resume` reuses the paused session's spec and journal.
- `--force` bypasses spec-hash validation when the operator accepts drift.
- `--reset-budget` ignores prior spend for the resumed attempt.
- Spec review wait time is not charged against build budget.
- Pause/resume/abort group events are recorded in `spec-state.jsonl`.
- Aborted groups become blocked and are not merge candidates.
- Interrupted queue tasks can be resumed or cleaned up through queue
  commands and Mission Control actions.

v5 recovery:

- `otto v5 list-pending` shows tasks awaiting review or dispatch.
- `otto v5 review approve <task-id>` resumes after a
  `--review-first-decomp` pause.
- Child crashes promote that child's verdict to `catastrophic`; the
  parent's integration Lead still runs against whatever children produced.
- A tree-level cost cap (`--tree-budget-usd`) refuses new dispatches once
  exceeded.

## Testing Gates

Recommended local gates:

```bash
uv run python scripts/test_tiers.py smoke
uv run python scripts/test_tiers.py fast
uv run python scripts/test_tiers.py web
npm run web:typecheck
npm run web:build
uv run python scripts/check_bundle_committed.py
```

Use browser tiers for user-visible Mission Control changes:

```bash
uv run python scripts/test_tiers.py browser-smoke
uv run python scripts/test_tiers.py browser
```

`scripts/test_tiers.py web` is the backend/frontend web confidence gate.
It typechecks the client and runs run-view, spec-review, Mission Control,
bundle, cache, queue, and watcher tests.

## Key Modules

| Module | Purpose |
| --- | --- |
| `otto/cli_run.py` | canonical `otto run` (i2p) CLI and orchestration |
| `otto/cli_v5.py` | `otto v5` hierarchical Lead CLI |
| `otto/runner.py` | i2p compile/build/merge/audit/repair/render phase coordinator |
| `otto/spec_compile.py` | i2p spec dataclasses, validation, compiler entrypoint |
| `otto/spec_compile_flat.py` | v5 flat-spec compiler (intent + behavior_journeys) |
| `otto/spec_state.py` | i2p event journal and replay helpers |
| `otto/build.py` | i2p group build execution and retry |
| `otto/merge_queue.py` | i2p dependency-aware merge lane |
| `otto/audit.py` | i2p integrated-product audit |
| `otto/audit_loop.py` | i2p repair and re-audit layering |
| `otto/render.py` | proof packet JSON/HTML renderer |
| `otto/resume.py` | paused session planning and recovery |
| `otto/agent.py` | provider invocation and message normalization |
| `otto/lead.py` | v5 universal Lead primitive |
| `otto/lead_verify.py` | v5 verify-tool implementation |
| `otto/v5_runner.py` | v5 hierarchical run coordinator |
| `otto/v5_branching.py` | v5 build/integ branch namespaces and merge helpers |
| `otto/v5_merge_drivers.py` | structured union drivers for runtime config files |
| `otto/v5_clean_verify.py` | unified `verify_from_clean` primitive |
| `otto/v5_preflight.py` | deterministic task-graph pre-flight checks |
| `otto/v5_review.py` | v5 pending-review workflow |
| `otto/mcp_tools.py` | Otto's SDK MCP server (`submit_subtask`, `verify`, ...) |
| `otto/queue/task_graph.py` | hierarchical task graph persistence |
| `otto/queue/subtask.py` | child enqueue + ready-to-dispatch helpers |
| `otto/queue/runner.py` | queue watcher and child process lifecycle |
| `otto/queue/schema.py` | queue/task/state persistence |
| `otto/mission_control/` | server-side web models, serializers, actions |
| `otto/web/client/src/` | React Mission Control client |

## Current Limits

Otto is local and single-user. It does not yet provide hosted multi-user
auth, cloud VM isolation, ticket tracker integration, or team RBAC.

Known architectural limits still documented elsewhere include:

- Broader cross-group check generation in i2p.
- Richer screenshot/video capture for non-web projects.
- Large-repo contract planning.
- First-class queue support for the canonical `otto run` / `otto v5 run`
  surfaces (queue today still invokes `build` / `improve` / `certify`).
- v5 children run in-process (asyncio) rather than as fresh subprocesses;
  context-isolated subprocess dispatch is a future cleanup if deep trees
  hit context-budget limits.
