# Phase 1A — Server Endpoint Catalog

Source of truth: `otto/web/app.py` (FastAPI factory `create_app`). Service layer: `otto/mission_control/service.py`. Adapters: `otto/mission_control/adapters/`.

All `/api/*` JSON routes share one error contract via the `MissionControlServiceError` exception handler:

```json
{ "ok": false, "message": "<text>", "severity": "error" }
```

…with the status code carried by `exc.status_code` (default 400). FastAPI emits its own validation errors (422) for query/body type coercion failures.

The `_service()` helper guards every endpoint that needs a project: if no project is selected (`app.state.project_dir` is None — possible only when `project_launcher=True`), it raises `MissionControlServiceError("No project selected. Create or open a managed project first.", status_code=409)`. All endpoints below marked **Requires project** ride this guard.

---

## 1. `GET /`

- **Response**: `FileResponse` of `otto/web/static/index.html` (HTML, not JSON).
- **Side effects**: none.
- **Preconditions**: none.
- **Failure modes**: 500 if static dir missing.

## 2. `GET /api/project`

- **Response**: project descriptor.
  - When no project selected: `{"path": null, "name": null, "branch": null, "dirty": false, "head_sha": null}`.
  - Otherwise (`serialize_project`):
    ```json
    {
      "path": "<absolute path>",
      "name": "<dir basename>",
      "branch": "<git branch --show-current> | null",
      "dirty": true|false,
      "head_sha": "<short sha> | null",
      "defaults": {
        "provider": "<str>",
        "model": "<str>",
        "reasoning_effort": "<str>",
        "certifier_mode": "<str>",
        "skip_product_qa": false,
        "config_file_exists": true|false,
        "config_error": null|"<str>"
      }
    }
    ```
- **Side effects**: shells out to `git branch --show-current`, `git status --porcelain`, `git rev-parse --short HEAD` (each 2s timeout). Reads `otto.yaml`.
- **Preconditions**: none.
- **Failure modes**: never raises; git failures yield `null` fields, config failures populate `defaults.config_error`.

## 3. `GET /api/projects`

- **Response**:
  ```json
  {
    "launcher_enabled": true|false,
    "projects_root": "<absolute path>",
    "current": <serialize_project | null>,
    "projects": [ { ...serialize_project, "managed": true }, ... ]
  }
  ```
- **Side effects**: `mkdir -p` of `projects_root`. Iterates immediate children, filters to dirs containing `.git`, runs the same git subprocesses as `/api/project` per child.
- **Preconditions**: none.
- **Failure modes**: lets `OSError` propagate (500). Children without `.git` are silently skipped.

## 4. `POST /api/projects/create`

- **Body** (`application/json`, optional):
  ```json
  { "name": "<str>" }
  ```
  `name` is required (after slugification). Slug regex: `[^a-z0-9]+ → -`, trimmed of leading/trailing `-`.
- **Response**: `{"ok": true, "project": <serialize_project>, "projects": [...] }`.
- **Side effects** (filesystem + git):
  - `mkdir -p` `<projects_root>/<slug>`.
  - `git init -q -b main`, `git config user.email/user.name`.
  - Writes `README.md`, `otto.yaml`, `.gitignore`.
  - `git add . && git commit -q -m "Initial Otto project"`.
  - Mutates `app.state.project_dir` and replaces `app.state.service`.
- **Preconditions**: none (works whether or not a project was previously selected).
- **Failure modes**:
  - 400 — empty/whitespace name (after slugify).
  - 400 — slug escapes `projects_root`.
  - 409 — target path exists and is not a dir, or exists and is non-empty.
  - 500 — any git subprocess fails (wrapped: `"Failed to create managed project: <stderr>"`).

## 5. `POST /api/projects/select`

- **Body**:
  ```json
  { "path": "<absolute path under projects_root>" }
  ```
- **Response**: `{"ok": true, "project": <serialize_project>, "projects": [...] }`.
- **Side effects**: replaces `app.state.project_dir` and `app.state.service` with a fresh `MissionControlService`. Validates path with `git rev-parse --show-toplevel`.
- **Preconditions**: none.
- **Failure modes**:
  - 400 — `path` missing/empty.
  - 403 — `path` not under `projects_root`.
  - 404 — target dir does not exist.
  - 400 — target is not a git repo.

## 6. `POST /api/projects/clear`

- **Body**: ignored.
- **Response**: `{"ok": true, "current": null, "projects": [...] }`.
- **Side effects**: sets `app.state.project_dir = None`, `app.state.service = None`. Does not touch the filesystem.
- **Preconditions**: none.
- **Failure modes**: only filesystem errors enumerating `projects_root`.

## 7. `GET /api/state` — **Requires project**

- **Query params** (all optional):
  - `active_only`: `bool`, default `false`.
  - `type` (alias of `type_filter`): one of `all|build|improve|certify|merge|queue`, default `all`.
  - `outcome`: one of `all|success|failed|interrupted|cancelled|removed|other`, default `all`.
  - `query`: free-text `str`, default `""`.
  - `history_page`: `int >= 0`, default `0`.
- **Response** (`service.state` → `serialize_state` + watcher/landing/runtime/events):
  ```json
  {
    "project": <serialize_project>,
    "filters": {"active_only", "type", "outcome", "query", "history_page"},
    "focus": "<pane name>",
    "selection": {"run_id", "origin_pane", "artifact_index", "log_index"},
    "selected_run_ids": ["..."],
    "live": {
      "items": [ <serialize_live_item>, ... ],
      "total_count": int,
      "active_count": int,
      "refresh_interval_s": float
    },
    "history": {
      "items": [ <serialize_history_item>, ... ],
      "page": int,
      "page_size": int,
      "total_rows": int,
      "total_pages": int
    },
    "banner": <str|null>,
    "watcher": <see /api/watcher>,
    "landing": {
      "target": "<branch>",
      "items": [...],
      "counts": {"ready", "merged", "blocked", "total"},
      "collisions": [...],
      "merge_blocked": bool,
      "merge_blockers": [...]
    },
    "runtime": <see /api/runtime>,
    "events": <see /api/events with limit=50>
  }
  ```
  `live.items[*]` carry: `run_id`, `domain`, `run_type`, `command`, `display_name`, `status`, `terminal_outcome`, `project_dir`, `cwd`, `queue_task_id`, `merge_id`, `branch`, `worktree`, `provider`, `model`, `reasoning_effort`, `adapter_key`, `version`, plus `display_status`, `active`, `display_id`, `branch_task`, `elapsed_s`, `elapsed_display`, `cost_usd`, `cost_display`, `last_event`, `row_label`, `overlay`.
  `history.items[*]` carry: `run_id`, `domain`, `run_type`, `command`, `status`, `terminal_outcome`, `queue_task_id`, `merge_id`, `branch`, `worktree`, `summary`, `intent`, `completed_at_display`, `outcome_display`, `duration_s`, `duration_display`, `cost_usd`, `cost_display`, `resumable`, `adapter_key`.
- **Side effects**: many read-only — git diffs vs target branch, queue/state file reads, `paths.logs_dir` traversal, certifier-memory reads. No writes. Internally calls `serialize_state`, `watcher_status`, `landing_status`, `runtime_status`, `events(limit=50)`.
- **Failure modes**:
  - 400 from `filters_from_params` for invalid `type` or `outcome`.
  - 409 if no project selected.

## 8. `GET /api/runs/{run_id}` — **Requires project**

- **Path param**: `run_id` (string; URL-decoded by FastAPI).
- **Query params**: same `type`, `outcome`, `query`, `history_page` as `/api/state` (note: filters are accepted but `_detail_view` discards them and uses default filters internally — this is a mild inconsistency worth a Playwright assertion).
- **Response** (`serialize_detail` + `review_packet` + landing context):
  ```json
  {
    ...<_record_summary fields>,
    "display_status": "...",
    "active": bool,
    "source": "...",
    "title": "...",
    "summary_lines": [...],
    "overlay": null|{"level","label","reason","writer_alive"},
    "artifacts": [ {"index","label","path","kind","exists"}, ... ],
    "log_paths": [...],
    "selected_log_index": int,
    "selected_log_path": "...|null",
    "legal_actions": [ {"key","label","enabled","reason","preview"}, ... ],
    "record": { ...full RunRecord.to_dict() },
    "review_packet": { "headline","status","summary","readiness","checks","next_action","certification","changes","evidence","failure" },
    "landing_state": null|"merged",
    "merge_info": { "merge_id","status","merge_run_status","target","merge_commit","diff_base","target_head_before", ... } // present only when merged
  }
  ```
  When `landing_state == "merged"`, the matching legal action with key `"m"` is force-disabled with `reason = "Already merged into <target>."`.
- **Side effects**: git plumbing (`git diff --name-only`, etc.) to compute changed files; reads of proof-of-work JSON and certifier summary.
- **Failure modes**:
  - 404 — `run not found`.
  - 400 — invalid filter values.
  - 409 — no project.

## 9. `GET /api/runs/{run_id}/logs` — **Requires project**

- **Path param**: `run_id`.
- **Query params**:
  - `log_index`: `int`, default `0`. Clamped to `[0, len(log_paths)-1]`.
  - `offset`: `int`, default `0`. Negative values floored to `0`. Used as a byte offset.
- **Response** (`asdict(LogReadResult)`):
  ```json
  {
    "path": "<absolute path|null>",
    "offset": int,
    "next_offset": int,
    "text": "<utf-8 decoded chunk, errors=replace>",
    "exists": bool
  }
  ```
  `limit_bytes` is fixed at `128_000` server-side. A queue-failure fallback (`_queue_failure_log_fallback`) may take precedence and return synthetic content.
- **Side effects**: opens the file in binary, seeks, reads. No writes.
- **Failure modes**:
  - 404 — run not found.
  - 403 — log path resolves outside project root (`_validated_artifact_path`).
  - 409 — no project.

## 10. `GET /api/runs/{run_id}/artifacts` — **Requires project**

- **Path param**: `run_id`.
- **Response**:
  ```json
  {
    "run_id": "...",
    "artifacts": [ {"index","label","path","kind","exists"}, ... ]
  }
  ```
- **Side effects**: none beyond detail materialization (reads).
- **Failure modes**: 404 run not found; 409 no project.

## 11. `GET /api/runs/{run_id}/artifacts/{artifact_index}/content` — **Requires project**

- **Path params**: `run_id` (string), `artifact_index` (int).
- **Response**:
  ```json
  {
    "artifact": {"index","label","path","kind","exists"},
    "content": "<utf-8 text, errors=replace>",
    "truncated": bool
  }
  ```
  `limit_bytes` is fixed at `256_000`.
- **Side effects**: file read only.
- **Failure modes**:
  - 404 — `artifact index out of range`.
  - 400 — `artifact is a directory`.
  - 403 — artifact path outside project.
  - 404 — run not found.
  - 409 — no project.

## 12. `GET /api/runs/{run_id}/proof-report` — **Requires project**

- **Path param**: `run_id`.
- **Response**: `FileResponse` (HTML, `media_type="text/html"`) of the proof-of-work HTML.
- **Side effects**: streams a file from disk.
- **Failure modes**:
  - 404 — `proof-of-work HTML report not found` (missing record entry, missing file, or path is a directory).
  - 403 — path outside project.
  - 404 — run not found.
  - 409 — no project.

## 13. `GET /api/runs/{run_id}/diff` — **Requires project**

- **Path param**: `run_id`.
- **Response**:
  ```json
  {
    "run_id": "...",
    "branch": "<task branch|null>",
    "target": "<merge target branch>",
    "command": "git diff <target>...<branch>" | "<merged-task command>" | null,
    "files": [ "<path>", ... ],
    "file_count": int,
    "text": "<diff text, possibly truncated>",
    "error": null | "<git error string>",
    "truncated": bool
  }
  ```
  `limit_chars` fixed at `240_000`. Branch=target → `command = null`, `text = ""`. If the task is already merged, uses `<diff_base>..<merge_commit>` instead.
- **Side effects**: shells out to `git diff --name-only` and full `git diff`. No writes.
- **Failure modes**:
  - 404 — run not found.
  - 409 — no project.
  - Git errors surface in `error` (200 OK).

## 14. `POST /api/runs/{run_id}/actions/{action}` — **Requires project**

- **Path params**:
  - `run_id`: string.
  - `action`: one of long names (`cancel|resume|retry|requeue|cleanup|remove|merge|open`) or single-letter keys (`c|r|R|x|m|e`). Mapping in `_action_key`. Unknown → falls through and is rejected as unavailable.
- **Body** (optional):
  ```json
  {
    "selected_queue_task_ids": ["task-id", ...],
    "artifact_index": 0
  }
  ```
  - `selected_queue_task_ids` is forwarded to `execute_action` (used by merge action to pick which ready tasks to land).
  - `artifact_index` is required (defaults to `0`) only for `open` (`e`); range-checked.
- **Response** (`serialize_action_result`):
  ```json
  {
    "ok": bool,
    "message": "<str|null>",
    "severity": "information|info|success|warning|error",
    "modal_title": "<str|null>",
    "modal_message": "<str|null>",
    "refresh": bool,
    "clear_banner": bool
  }
  ```
- **Side effects** (depend on action):
  - `cancel` (`c`): sends signal/command to running queue task — subprocess + queue command file write.
  - `resume` (`r`): re-spawns an interrupted run via `otto` CLI subprocess.
  - `retry`/`requeue` (`R`): writes a new queue task (filesystem write, locked).
  - `cleanup`/`remove` (`x`): removes queue/state entries, may delete worktree, git operations.
  - `merge` (`m`): runs the merge pipeline (git checkout/merge/push + state writes). Pre-checks: not already merged, `_ensure_merge_unblocked` (no dirty repo). Async result posts a follow-up event.
  - `open` (`e`): launches the OS opener for `artifact_index`. Path is validated via `_validated_artifact_path`.
  - Always appends one `run.<event_action>` event to `mission-control/events.jsonl`. Async actions also append `run.<event_action>.completed`.
- **Failure modes**:
  - 404 — `action unavailable` (key not in detail.legal_actions) or run not found, or `artifact index out of range`.
  - 409 — action disabled (returns `legal[key].reason`); merge: `Already merged into <target>.`; or merge preflight blocked.
  - 409 — no project.
  - Action-internal failures may bubble up as `MissionControlServiceError` with various codes.

## 15. `POST /api/actions/merge-all` — **Requires project**

- **Body**: ignored.
- **Response**: `serialize_action_result` (same shape as #14).
- **Side effects**: runs `execute_merge_all` — iterates ready tasks and merges each (git ops, queue/state writes, async post-result events `merge.all.completed`). Records a `merge.all` event synchronously.
- **Preconditions**: `_ensure_merge_unblocked(project_dir)` — repo must be clean.
- **Failure modes**:
  - 409 — preflight failure (`Commit, stash, or revert local project changes…`).
  - 409 — no project.

## 16. `GET /api/watcher` — **Requires project**

- **Response**:
  ```json
  {
    "alive": bool,
    "watcher": null | { "pid", "started_at", "heartbeat", "concurrent", ... },
    "counts": {
      "queued","starting","initializing","running","terminating",
      "interrupted","done","failed","cancelled","removed": int
    },
    "health": {
      "state": "running|stale|stopped",
      "blocking_pid": int|null,
      "watcher_pid": int|null,
      "watcher_process_alive": bool,
      "lock_pid": int|null,
      "lock_process_alive": bool,
      "heartbeat": "<iso|null>",
      "heartbeat_age_s": float|null,
      "started_at": "<iso|null>",
      "log_path": "<absolute path>",
      "next_action": "<str>"
    }
  }
  ```
- **Side effects**: reads `.otto-queue.yml`, `.otto-queue-state.json`, queue lock (`fcntl` probe). No writes.
- **Failure modes**: 409 no project. Otherwise tolerant — corrupt files yield zeroed counts.

## 17. `GET /api/runtime` — **Requires project**

- **Response** (`build_runtime_status`):
  ```json
  {
    "status": "healthy|attention",
    "generated_at": "<iso utc>",
    "queue_tasks": int|null,
    "state_tasks": int|null,
    "command_backlog": {
      "pending": int, "processing": int, "malformed": int,
      "items": [ ... up to 8 ... ]
    },
    "files": {
      "queue":      {"path","exists","size","mtime","error"},
      "state":      {"path","exists","size","mtime","error"},
      "commands":   {"path","exists","size","mtime","line_count","malformed_count","error"},
      "processing": {"path","exists","size","mtime","line_count","malformed_count","error"}
    },
    "supervisor": { ...read_supervisor() result... },
    "issues": [ {"severity","label","detail","next_action"}, ... ]
  }
  ```
  `runtime_status` re-derives watcher/landing internally if not passed (it is, when reached via `/api/state`); standalone calls trigger fresh queries.
- **Side effects**: filesystem stats and JSONL counts only.
- **Failure modes**: 409 no project.

## 18. `GET /api/events` — **Requires project**

- **Query**:
  - `limit`: `int`, ge=1, le=500, default `80`. FastAPI returns 422 on out-of-range.
- **Response**:
  ```json
  {
    "path": "<absolute path to events.jsonl>",
    "items": [ {"schema_version","event_id","created_at","kind","severity","message","run_id","task_id","actor":{"source","pid"},"details":{...}}, ... ],
    "total_count": int,
    "malformed_count": int,
    "limit": int,
    "truncated": bool
  }
  ```
- **Side effects**: tails up to 512KB from `otto_logs/<…>/mission-control/events.jsonl`.
- **Failure modes**: 422 on bad `limit`; 409 no project.

## 19. `POST /api/watcher/start` — **Requires project**

- **Body** (optional):
  ```json
  { "concurrent": 3, "exit_when_empty": false }
  ```
  `concurrent` clamped `max(1, int(concurrent or 3))`. `exit_when_empty` is coerced via `bool()`.
- **Response**:
  ```json
  {
    "ok": true,
    "message": "watcher already running" | "watcher started" | "watcher launch requested",
    "refresh": true,
    "watcher": <see /api/watcher>,
    "log_path": "<absolute>",       // present when a new process was spawned
    "pid": int,                      // present when a new process was spawned
    "supervisor": { ... } | null     // present when a new process was spawned
  }
  ```
- **Side effects**:
  - Subprocess spawn: `otto queue run --no-dashboard --concurrent N [--exit-when-empty]`, `start_new_session=True`, env `OTTO_NO_TUI=1`, cwd=`project_dir`.
  - Appends to `otto_logs/web/watcher.log` (creates dir).
  - Writes supervisor metadata.
  - Records `mission-control/events.jsonl` entries (`watcher.start.skipped` | `watcher.started` | `watcher.start.failed` | `watcher.start.blocked` | `watcher.launch.requested`).
- **Failure modes**:
  - 409 — health is `stale` (a stale watcher must be stopped first).
  - 500 — `OSError` on spawn or process exited immediately.
  - 409 — no project.

## 20. `POST /api/watcher/stop` — **Requires project**

- **Body**: ignored.
- **Response**:
  ```json
  {
    "ok": true,
    "message": "watcher is not running" | "watcher already stopped" | "watcher stop requested" | "stale watcher stop requested",
    "refresh": true,
    "watcher": <see /api/watcher>,
    "supervisor": { ... }   // optional
  }
  ```
- **Side effects**:
  - `os.kill(pid, SIGTERM)` against the watcher PID (if owned by Mission Control supervisor or it owns the queue lock).
  - Updates supervisor metadata.
  - Appends events to `mission-control/events.jsonl` (`watcher.stop.skipped` | `watcher.stop.requested` | `watcher.stop.blocked` | `watcher.stop.failed`).
- **Failure modes**:
  - 409 — refusing to stop unowned PID (supervisor mismatch).
  - 403 — `PermissionError` from `os.kill`.
  - 409 — no project.

## 21. `POST /api/queue/{command}` — **Requires project**

- **Path param**: `command` (lowercased): one of `build|improve|certify`. Anything else → 404 `unsupported queue command`.
- **Body** (shape varies per command):
  - Common optional: `as` or `task_id` (`str`), `after` (`list[str]`), `extra_args` (`list[str]`).
  - `build`: required `intent` (`str`).
  - `improve`: required `subcommand` ∈ {`bugs`, `feature`, `target`}; optional `focus` or `goal` (`str`).
  - `certify`: optional `intent` (`str`).
- **Response**:
  ```json
  {
    "ok": true,
    "message": "queued <task_id>",
    "task": { ...QueueTask dataclass fields... },
    "warnings": [ "<str>", ... ],
    "refresh": true
  }
  ```
- **Side effects**:
  - Writes to `.otto-queue.yml` and `.otto-queue-state.json` under the queue lock (`enqueue_task`).
  - Appends a `queue.<command>` event.
  - For `improve`/`certify`, snapshots the current intent (`resolve_intent_for_enqueue`) — reads `intent.md` and may copy into the task record.
- **Failure modes**:
  - 400 — missing `intent` (build), missing `subcommand` (improve), invalid argv passed through `_validate_inner_command_args`.
  - 400 — invalid `improve` subcommand.
  - 400 — `ValueError` from `enqueue_task` (e.g. invalid `after`/`as`).
  - 404 — unsupported command name.
  - 409 — no project.

---

## Coverage check

Every `@app.<verb>(...)` decorator listed below is documented above. Counts: **22 total** (1 root + 21 `/api/*`).

| # | Decorator (verbatim from `otto/web/app.py`) | Section |
|---|---|---|
| 1 | `@app.get("/")` | 1 |
| 2 | `@app.get("/api/project")` | 2 |
| 3 | `@app.get("/api/projects")` | 3 |
| 4 | `@app.post("/api/projects/create")` | 4 |
| 5 | `@app.post("/api/projects/select")` | 5 |
| 6 | `@app.post("/api/projects/clear")` | 6 |
| 7 | `@app.get("/api/state")` | 7 |
| 8 | `@app.get("/api/runs/{run_id}")` | 8 |
| 9 | `@app.get("/api/runs/{run_id}/logs")` | 9 |
| 10 | `@app.get("/api/runs/{run_id}/artifacts")` | 10 |
| 11 | `@app.get("/api/runs/{run_id}/artifacts/{artifact_index}/content")` | 11 |
| 12 | `@app.get("/api/runs/{run_id}/proof-report")` | 12 |
| 13 | `@app.get("/api/runs/{run_id}/diff")` | 13 |
| 14 | `@app.post("/api/runs/{run_id}/actions/{action}")` | 14 |
| 15 | `@app.post("/api/actions/merge-all")` | 15 |
| 16 | `@app.get("/api/watcher")` | 16 |
| 17 | `@app.get("/api/runtime")` | 17 |
| 18 | `@app.get("/api/events")` | 18 |
| 19 | `@app.post("/api/watcher/start")` | 19 |
| 20 | `@app.post("/api/watcher/stop")` | 20 |
| 21 | `@app.post("/api/queue/{command}")` | 21 |

Plus `@app.exception_handler(MissionControlServiceError)` (line 54) — not a route, documented in the preamble.

`grep -c '@app\.\(get\|post\|put\|delete\|patch\|options\|head\)' otto/web/app.py` → **21 routes**, all listed. The `index()` `GET /` brings the user-visible surface to 22. No `put/delete/patch` routes exist. Count matches.

## Notes for Playwright authors

- **Project bootstrap**: When `project_launcher=True`, the app starts with no project; every API except `/api/project*` returns 409. Tests must `POST /api/projects/create` first. When `project_launcher=False`, the project is fixed at startup.
- **Filter validation hits before service work** — `/api/state` and `/api/runs/{id}` raise 400 on bad `type`/`outcome` before any disk read.
- **`/api/runs/{id}` filter params are accepted but ignored** by `_detail_view` (it discards `filters` via `del filters` at line 670). If a test depends on filtered detail responses, this is a known no-op.
- **Subprocess spawns** happen only in: `POST /api/projects/create` (git), `POST /api/watcher/start` (otto queue run), and most `POST /api/runs/{id}/actions/*` paths (otto CLI variants). Tests should clean up the projects root or run against an isolated `OTTO_PROJECTS_ROOT`.
- **Event log writes** happen as a side effect of every action and watcher endpoint. Tests asserting events should poll `/api/events?limit=…` after the triggering call.
- **Path traversal**: `_validated_artifact_path` enforces project containment; any `path` field returned from one endpoint can be safely round-tripped to another so long as it stays under `project_dir`.
- **Truncation**: log endpoint caps at 128 KB per call, artifact content at 256 KB, diff text at 240 K chars, events at ~512 KB tail. Tests for "large file" UX must set up files exceeding these and assert `truncated`/`next_offset` semantics.
