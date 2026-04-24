# Web Mission Control Plan

## Summary

Otto should add a web Mission Control as the primary task management surface, while keeping the existing CLI and Textual TUI as local power-user surfaces. The web UI should not fork Mission Control logic. It should sit on the same run registry, history, queue state, and action primitives already used by the TUI.

The near-term goal is a local, browser-based console for one project:

- See live runs, history, queue rows, status, provider, branch, worktree, elapsed time, cost, and latest event.
- Drill into a run and inspect metadata, logs, artifacts, certifier evidence, and replay links.
- Launch queue jobs and perform safe actions: cancel, resume, retry, cleanup, merge selected, merge all.
- Verify the product with agent-browser and API tests instead of terminal-byte automation alone.

This is not a rewrite of Otto. It is a new client over a shared control plane.

## Why Web

Web is a better primary task management surface for Otto's product direction:

- It is easier to test with agent-browser semantic locators, snapshots, screenshots, videos, and network inspection.
- It can show rich evidence: diffs, screenshots, proof-of-work JSON, cast replays, logs, cost, and token reports.
- It is better for 24x7 operation: remote viewing, notifications, multiple projects, and long-running job timelines.
- It lets Otto feel like a production console rather than an agent terminal session.

The TUI should stay useful, but it should not be the only high-fidelity operations surface.

## Current Foundation

The current code already has the right boundary for a web UI:

- `otto.tui.mission_control_model.MissionControlModel` reads and projects live registry plus history state.
- `otto.tui.mission_control_actions` centralizes mutating actions like cancel, resume, retry, cleanup, and merge.
- `otto.runs.registry` provides canonical live run records.
- `otto.queue.schema` provides queue task and state files.
- `otto.cli_queue` provides enqueue and watcher commands.

The first implementation should reuse these pieces. The web work should extract only where needed to remove TUI naming or CLI-only coupling.

## Goals

1. Build a local web Mission Control with the same functional coverage as the TUI for task management.
2. Make the web API stable enough that TUI, CLI, and web can share behavior and tests.
3. Make agent-browser the primary UI automation path for Mission Control.
4. Preserve local-first operation: `otto web` should work inside an existing repo without cloud infrastructure.
5. Keep the first version secure for local use: bind to localhost by default, avoid arbitrary file access, and validate all action targets.

## Non-Goals For MVP

- Multi-user auth and RBAC.
- Hosted cloud deployment.
- Database migration away from the current file-backed registry.
- Replacing the TUI.
- Real-time collaboration.
- A generic project management tool detached from Otto runs.

## Recommended Stack

Use a Python backend first:

- FastAPI app factory: `otto.web.app:create_app(project_dir)`.
- Uvicorn runner behind `otto web`.
- JSON API over existing Mission Control model and action helpers.
- Static frontend served by FastAPI for the first slice.

For the frontend, start with a no-build TypeScript/React-free implementation only if speed matters, but the target product should be a typed component UI. The clean path is:

- Phase 1: static HTML/CSS/ES modules for fast integration and no Node dependency.
- Phase 2: introduce React/Vite/TypeScript once UI complexity grows beyond simple panels and polling.

Do not let frontend stack choice delay the API boundary. The API is the durable part.

## Package Layout

Add:

```text
otto/web/
  __init__.py
  app.py              # create_app(project_dir, settings)
  api.py              # route registration
  schemas.py          # response/request dataclasses or pydantic models
  serializers.py      # MissionControlModel -> JSON
  services.py         # web-facing orchestration over model/actions/queue
  security.py         # path validation, localhost/auth-token checks
  static/
    index.html
    app.js
    styles.css
```

Tests:

```text
tests/test_web_api.py
tests/test_web_security.py
tests/test_web_actions.py
tests/test_web_static.py
scripts/e2e_web_mission_control.py
```

Later, if React/Vite is introduced:

```text
web/
  package.json
  src/
  tests/
```

Keep that as a later step unless the first UI becomes hard to maintain.

## CLI Shape

Add:

```bash
otto web --host 127.0.0.1 --port 8765 --open
otto dashboard --web
otto queue dashboard --web
```

Recommended implementation order:

1. Add `otto web`.
2. Add `otto dashboard --web` as an alias after the base command works.
3. Add `otto queue dashboard --web` alias with queue filter preselected.

Default binding must be `127.0.0.1`. If a user binds to `0.0.0.0`, print a security warning and require an explicit flag such as `--allow-remote`.

## API Contract

Use stateless read endpoints where possible. Selection, tab, and panel layout should be client state.

Initial read endpoints:

```http
GET /api/project
GET /api/state?type=all&outcome=all&active_only=false&query=&history_page=0
GET /api/runs/{run_id}
GET /api/runs/{run_id}/logs
GET /api/artifacts/{run_id}
GET /api/artifacts/{run_id}/content?artifact_index=0
```

Initial mutation endpoints:

```http
POST /api/actions/{run_id}/cancel
POST /api/actions/{run_id}/resume
POST /api/actions/{run_id}/retry
POST /api/actions/{run_id}/cleanup
POST /api/actions/{run_id}/merge
POST /api/actions/merge-all
POST /api/queue/build
POST /api/queue/improve
POST /api/queue/certify
POST /api/queue/run
```

A mutation response should always include:

```json
{
  "ok": true,
  "message": "cancel requested",
  "severity": "information",
  "refresh": true
}
```

For launch-style actions that spawn a subprocess, return quickly with a process/run hint and rely on `/api/state` polling for progress.

## State Response Shape

The web state should be a serialized form of `MissionControlState` plus the selected detail when requested:

```json
{
  "project": {
    "path": "/repo",
    "branch": "main",
    "dirty": false
  },
  "filters": {
    "type": "all",
    "outcome": "all",
    "active_only": false,
    "query": ""
  },
  "live": {
    "items": [],
    "active_count": 0,
    "refresh_interval_s": 1.5
  },
  "history": {
    "items": [],
    "page": 0,
    "total_pages": 1,
    "total_rows": 0
  },
  "banner": null
}
```

Run rows should include stable IDs and all display data needed by the frontend:

- `run_id`
- `domain`
- `run_type`
- `status`
- `terminal_outcome`
- `display_id`
- `queue_task_id`
- `branch`
- `worktree`
- `cwd`
- `elapsed_display`
- `cost_display`
- `provider`
- `model`
- `reasoning_effort`
- `last_event`
- `overlay`
- `legal_actions`

The frontend should not reconstruct status rules that already exist in the backend.

## Live Updates

Start with polling:

- `/api/state` every 500 ms while active runs exist.
- `/api/state` every 1500 to 3000 ms when idle.
- `/api/runs/{run_id}/logs?offset=N` for incremental log tailing.

Do not start with WebSockets. Polling maps cleanly onto the current file-backed registry and is much easier to test. Add SSE later only if polling becomes wasteful or UX visibly suffers.

## UI Product Shape

First screen should be the actual console, not a landing page.

Layout:

- Left rail: project path, branch, dirty status, provider summary, active count.
- Main top: command bar with "New job", filters, search, refresh status.
- Main center: live runs table.
- Main lower: history table.
- Right panel: selected run detail, actions, artifacts, logs, evidence.

Required views for MVP:

- Live runs.
- History.
- Run detail.
- Log viewer with search and copy.
- Artifact viewer for text/JSON/Markdown.
- New queue job modal.
- Action confirmation for destructive or high-impact actions.

Avoid decorative dashboard cards. This is an operations console; dense, calm, scannable information matters more than marketing layout.

## Action Semantics

Reuse `mission_control_actions` for existing actions. Do not duplicate cancel, retry, resume, cleanup, or merge logic in web-specific code.

For actions that currently shell out, keep that behavior initially, but wrap it in a service interface:

```python
class MissionControlService:
    def snapshot(filters) -> StateResponse: ...
    def detail(run_id) -> DetailResponse: ...
    def execute(run_id, action, payload) -> ActionResponse: ...
    def enqueue(command, payload) -> ActionResponse: ...
```

This lets the web API and future TUI refactors depend on the same service layer.

## Security Rules

The web server controls local processes and files, so treat it as sensitive even when local-only.

MVP rules:

- Bind to `127.0.0.1` by default.
- Optional random local token for browser requests if remote binding is enabled.
- Never accept arbitrary filesystem paths from the client.
- Artifact and log reads must resolve from backend-known `ArtifactRef` values only.
- Validate `run_id` against live/history records before actions.
- Validate queue task IDs before action execution.
- Do not expose environment variables.
- Do not expose full home directory paths except known project/worktree/artifact paths needed for operations.

Add explicit tests for path traversal attempts.

## Implementation Phases

### Phase 0: Shared Contract Cleanup

Purpose: make the current Mission Control model serializable and reusable.

Tasks:

- Add serializers for `MissionControlState`, `DetailView`, `ActionState`, and `ArtifactRef`.
- Add provider/model/reasoning fields to run row serialization when available.
- Add project git summary helper: branch, dirty state, ahead/behind if cheap.
- Add unit tests that serialize realistic live, history, queue, stale, and empty states.

Exit criteria:

- `MissionControlModel` can produce complete JSON for web without importing Textual widgets.
- No web endpoint exists yet.

### Phase 1: Read-Only Web API

Purpose: prove the backend API over real Otto state.

Tasks:

- Add FastAPI dependency and `otto.web.app:create_app`.
- Add `/api/project`, `/api/state`, `/api/runs/{run_id}`, `/api/runs/{run_id}/logs`, and artifact listing.
- Add `otto web --no-open` command.
- Add httpx/FastAPI tests using temp repos and fake run records.

Exit criteria:

- `uv run otto web --no-open` serves state for the current repo.
- API tests cover empty, live, terminal, history, queue compat, malformed records, and log tail behavior.

### Phase 2: Read-Only Web UI

Purpose: provide a usable browser view before mutating actions.

Tasks:

- Add static UI with live table, history table, detail pane, log viewer, artifact list, filters, and search.
- Poll `/api/state` and `/api/runs/{run_id}/logs`.
- Preserve selection in client state across refreshes.
- Add agent-browser E2E against a fake project with seeded records and logs.

Exit criteria:

- A user can run `otto web`, open the page, inspect runs, logs, artifacts, and history.
- agent-browser verifies navigation, filtering, detail selection, log tailing, artifact display, and empty state.

### Phase 3: Existing Actions

Purpose: make web Mission Control operational, not just observational.

Tasks:

- Expose cancel, resume, retry, cleanup, merge selected, and merge all endpoints.
- Use the existing action legality rules in detail responses.
- Add confirmation UI for cancel, cleanup, retry, merge.
- Add API tests with mocked process launch and command journals.
- Add agent-browser tests for action buttons and optimistic/refresh behavior.

Exit criteria:

- Web action results match TUI behavior for the same seeded states.
- Tests prove illegal actions are disabled and rejected server-side.

### Phase 4: Queue Job Launch

Purpose: make web useful as a task manager.

Tasks:

- Add "New job" modal for queue build, improve, and certify.
- Initially call the existing queue command path through a service wrapper.
- Extract queue enqueue logic from `cli_queue` into a non-Click service if the wrapper becomes awkward.
- Add watcher controls: show watcher status, start watcher, and explain when no watcher is running.

Exit criteria:

- A user can launch queue jobs from the browser, start processing, inspect progress, and cancel/retry/merge.
- agent-browser runs a fake-project launch-to-done flow.

### Phase 5: Evidence And Replay

Purpose: make Otto's reliability claims visible.

Tasks:

- Add proof-of-work panel for certifier outputs.
- Add screenshot and cast replay links when present.
- Add method/integrity indicators, including real UI event vs JS injection evidence.
- Add provider/model/reasoning/token/cost detail.
- Add "copy report" and "open artifact" actions.

Exit criteria:

- A reviewer can answer: what happened, what evidence proves it, what it cost, and what remains risky.

### Phase 6: Multi-Project And 24x7

Purpose: prepare for always-on operation.

Tasks:

- Add project registry and recent projects.
- Add background daemon option.
- Add notifications.
- Add remote-safe auth.
- Add persistent event timeline if file scans become insufficient.

Exit criteria:

- Web Mission Control can monitor more than one repo without being launched from each terminal.

## Verification Plan

Unit and API:

- Serializer parity tests for state/detail/action legality.
- API contract tests with FastAPI test client.
- Security tests for run ID validation, artifact path validation, and traversal attempts.
- Action tests with mocked `subprocess.Popen`, command journal, and queue state.

Browser:

- agent-browser smoke: page loads, no console errors, empty state renders.
- agent-browser state: seeded live/history/queue records render correctly.
- agent-browser interaction: filters, search, selection, detail, log tail, artifact viewer.
- agent-browser actions: cancel/retry/resume/merge buttons call endpoints and refresh state.
- agent-browser launch flow with fake Otto wrapper.

Parity:

- For the same seeded repo, compare TUI `MissionControlModel` JSON to web `/api/state`.
- Keep current Textual tests for TUI behavior.
- Keep PTY tests for terminal integration.

Performance:

- API state endpoint under 150 ms for 100 live/history rows on local disk.
- Browser remains usable with 500 history rows.
- Polling stops or slows when no active runs exist.

Real-world E2E:

- Run a small existing repo through queue build, cancel, resume/retry, merge.
- Run both Claude and Codex providers enough to verify provider/model/cost fields display correctly.
- Preserve screenshots, agent-browser videos or snapshots, and server logs as artifacts.

## MVP Definition

The web MVP is shippable when this command works:

```bash
otto web --open
```

And a user can:

1. See current project status and live/history runs.
2. Inspect a run's logs, metadata, artifacts, worktree, branch, provider, model, and cost.
3. Enqueue a queue build/improve/certify job.
4. Start or detect the watcher.
5. Cancel, retry, resume, cleanup, merge selected, and merge all where legal.
6. Verify behavior through agent-browser E2E and API tests.

The first MVP can be local-only and single-project. It must be reliable enough that agent-browser is the primary regression suite for Mission Control.

## Main Risks

- Duplicating TUI logic in web code. Mitigation: serializers and service layer over `MissionControlModel` and action helpers.
- Unsafe file exposure. Mitigation: artifact reads only from backend-known artifact refs.
- Web launch actions diverging from CLI behavior. Mitigation: extract queue services gradually and keep CLI as a thin wrapper.
- Polling overhead on large repos. Mitigation: reuse model cache, slow idle polling, add SSE later if needed.
- UI scope creep. Mitigation: first UI is an operations console, not an analytics dashboard.

## First Concrete Slice

Start with Phase 0 and Phase 1 only:

1. Add `otto/web/serializers.py`.
2. Add `otto/web/app.py`.
3. Add `/api/project`, `/api/state`, and `/api/runs/{run_id}`.
4. Add `otto web --no-open`.
5. Add API tests over seeded temp repos.

That slice proves the web surface can read the same truth as the TUI without taking on frontend complexity yet.
