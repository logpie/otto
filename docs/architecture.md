# Otto Architecture

Status: current runtime reference for the i2p redesign as of May 2026.

Older design and audit documents may still say "slice", "PoW", "v3
pipeline", or "standalone merge CLI". The current runtime uses **Group** for
i2p build units, **proof packet** for rendered evidence, and `otto run` as the
canonical direct intent-to-product command.

## Product Model

Otto is a local intent-to-product control system:

```text
intent
  -> spec
  -> grouped build work
  -> integrated product audit
  -> repair loop when needed
  -> proof packet
```

The main surfaces are:

- CLI for direct runs, existing-product improvement, certification, queueing,
  proof inspection, and diagnostics.
- Mission Control web UI for managed projects, live run review, spec review,
  proof inspection, queue watcher control, and recovery actions.
- File-backed logs and state under `otto_logs/` so every run can be audited
  without a database.

## CLI Surface

Current top-level commands:

```text
otto run        canonical intent-to-product run
otto improve    brownfield bug/feature/target work
otto certify    brownfield independent audit
otto queue      file-backed queue and worktree watcher
otto web        local Mission Control
otto proof      proof packet and run-artifact inspection
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

The current top-level CLI does not expose the old standalone `otto merge`
surface. The i2p direct run has an internal merge lane in `merge_queue.py`;
queue and Mission Control landing/recovery code should be treated as a
separate compatibility area when changed.

## Direct I2P Pipeline

`otto run` drives the full product pipeline:

```text
┌────────┐
│ intent │
└───┬────┘
    ▼
┌─────────────────────┐
│ compile spec         │  `spec_compile.py`
│ - project kind       │
│ - structure          │
│ - groups             │
│ - checks             │
└───┬─────────────────┘
    │ optional `--review-gate`
    ▼
┌─────────────────────┐
│ build groups         │  `build.py`
│ - branch/worktree    │
│ - owned paths        │
│ - deterministic gate │
│ - retry same group   │
└───┬─────────────────┘
    ▼
┌─────────────────────┐
│ merge lane           │  `merge_queue.py`
│ - dependency order   │
│ - serial integration │
│ - blocked groups     │
└───┬─────────────────┘
    ▼
┌─────────────────────┐
│ audit integrated app │  `audit.py`
│ - feature verdicts   │
│ - findings           │
│ - evidence requests  │
└───┬─────────────────┘
    │ if repairable failure
    ▼
┌─────────────────────┐
│ repair / re-audit    │  `audit_loop.py`
└───┬─────────────────┘
    ▼
┌─────────────────────┐
│ render proof packet  │  `render.py`
└─────────────────────┘
```

`runner.py` is the phase coordinator. It owns phase transitions, pause checks,
budget propagation, resume behavior, and proof rendering.

## Spec Contract

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

Historical docs use "slice" for the same rough concept. Runtime code and web
surfaces should use "Group".

## Checks

Otto prefers deterministic checks during build and focused LLM judgment during
audit.

Check kinds include:

- `RepoTestCheck`: arbitrary repo command with expected exit/stdout.
- `PytestCheck`: pytest selector or expression.
- `ApiProbe`: HTTP request/response contract.
- `BrowserJourney`: browser-level journey and screenshot evidence.
- `StateInvariant`: durable state predicate or project-layout invariant.

The compiler should produce checks that fit the project kind. Webapps need
browser/HTTP evidence when feasible; CLI and library projects should lead with
terminal output, repo tests, usage examples, and durable state evidence instead
of empty screenshot grids.

## Providers

Provider settings are read from `otto.yaml` and can be overridden by CLI flags.

Global overrides:

```text
--provider
--model
--effort
--budget
--max-turns
```

Phase-specific overrides:

```text
--build-provider / --build-model / --build-effort
--certifier-provider / --certifier-model / --certifier-effort
--fix-provider / --fix-model / --fix-effort
```

The Codex integration is CLI-subprocess based and normalizes provider JSONL
into Otto's message/log format. Claude remains supported where configured.
The outer orchestrator is durable Otto state; provider sessions are disposable
inner workers.

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

The frontend has been through the 2026-05-05 RUA campaign. Audit reports and
screenshots live under `docs/rua/`. The local skill that drove the audit lives
at `.claude/skills/otto-frontend-rua/SKILL.md`.

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

Queued child commands still invoke Otto CLI commands (`build`, `improve`, or
`certify`) for compatibility. New direct product builds should prefer
`otto run`; queue support for a first-class `run` task is a future cleanup area.

## Artifacts And State

Canonical session directory:

```text
otto_logs/sessions/<session-id>/
```

Important artifacts:

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

I2P resume state is session-scoped:

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
- Interrupted queue tasks can be resumed or cleaned up through queue commands
  and Mission Control actions.

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

`scripts/test_tiers.py web` is the backend/frontend web confidence gate. It
typechecks the client and runs run-view, spec-review, Mission Control, bundle,
cache, queue, and watcher tests.

## Key Modules

| Module | Purpose |
| --- | --- |
| `otto/cli_run.py` | canonical `otto run` command and CLI orchestration |
| `otto/runner.py` | compile/build/merge/audit/repair/render phase coordinator |
| `otto/spec_compile.py` | spec dataclasses, validation, compiler entrypoint |
| `otto/spec_state.py` | event journal and replay helpers |
| `otto/build.py` | group build execution and retry |
| `otto/merge_queue.py` | dependency-aware i2p group merge lane |
| `otto/audit.py` | integrated-product audit prompt/parser/result |
| `otto/audit_loop.py` | repair and re-audit layering |
| `otto/render.py` | proof packet JSON/HTML renderer |
| `otto/resume.py` | paused session planning and recovery |
| `otto/agent.py` | provider invocation and message normalization |
| `otto/queue/runner.py` | queue watcher and child process lifecycle |
| `otto/queue/schema.py` | queue/task/state persistence |
| `otto/mission_control/` | server-side web models, serializers, actions |
| `otto/web/client/src/` | React Mission Control client |

## Current Limits

Otto is local and single-user. It does not yet provide hosted multi-user auth,
cloud VM isolation, ticket tracker integration, or team RBAC.

Known architectural limits still documented elsewhere include broader
cross-group check generation, richer screenshot/video capture for non-web
projects, large-repo contract planning, and first-class queue support for the
canonical `otto run` surface.
