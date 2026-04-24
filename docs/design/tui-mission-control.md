# TUI Mission Control
Status: proposed
Owner: TUI/runtime path
Primary files:
- `otto/paths.py`
- `otto/history.py`
- `otto/queue/schema.py`
- `otto/queue/runtime.py`
- `otto/queue/runner.py`
- `otto/queue/dashboard.py`
- `otto/merge/state.py`
- `otto/merge/orchestrator.py`
- `otto/cli.py`
- `otto/cli_improve.py`
- `otto/cli_queue.py`
- `otto/cli_merge.py`
- `otto/config.py`
- `otto/pipeline.py`
- `otto/checkpoint.py`
- `otto/manifest.py`
Planned new files:
- `otto/runs/__init__.py`
- `otto/runs/schema.py`
- `otto/runs/registry.py`
- `otto/runs/history.py`
- `otto/tui/mission_control.py`
- `otto/tui/mission_control_model.py`
- `otto/tui/mission_control_actions.py`
## Summary
The converged decision between Codex and Claude is:
- Product framing: the TUI evolves into "Project Mission Control" — a persistent,
  project-scoped human interface where multiple long-running tasks come and go inside a
  single TUI session. The user opens it, works through their day, closes it.
- Architecture framing: build it on a "C-shaped" runtime substrate — a canonical run
  registry that normalizes state across all long-running Otto commands
  (`build` / `improve` / `certify` / `queue` / `merge`), with single-writer +
  command-append discipline per domain.
- Explicitly skip a "TUI as transient launcher" B-style intermediate phase. That step
  adds little architectural value and mostly creates UI that gets deleted once the real
  substrate exists.
- CLIs persist. They remain the API for scripting, skills, and programmatic use. The
  TUI is the human operator interface. Both endure.
The expensive part is not Textual. The expensive part is normalizing run state across
queue, merge, and the atomic commands that currently expose only fragments through
`summary.json`, `checkpoint.json`, `manifest.json`, queue state, and merge state. In
Otto's actual delivery model, that is not a multi-week human-engineer estimate; it is
roughly `3-5` days of agent dispatch over `2-3` calendar days, with the critical path
set by gate cycles, real-LLM validation, and reviewer attention rather than raw coding
throughput. Today already demonstrated roughly `2-3` weeks of human-equivalent work in
about 10 hours of agent dispatch.
## Why This Change Exists
Otto already has one good operator substrate: queue. `otto/queue/schema.py` separates
definitions, watcher-owned runtime state, and append-only commands. `otto/queue/runner.py`
enforces a real single-writer model. `otto/queue/dashboard.py` proves that a Textual UI
can sit above file-backed state without a daemon.
The rest of Otto is not on that shape yet.
`otto build`, `otto improve`, and standalone `otto certify` write useful artifacts, but
they do not write one normalized live record that another shell can discover quickly.
`otto merge` has partial durable state in `otto/merge/state.py` and `merge.log`, but it
does not have a queue-style command channel, a heartbeat protocol, or a universal viewer
contract. The result is that today's TUI can answer only a queue question, not a project
question.
Mission Control exists to answer the project question:
> what is running in this repo right now, what already happened today, and what can I
> do about it without dropping to three different dashboards and five ad hoc log paths?
## Vision
Mission Control is Otto's persistent human control plane for one project. The intended
workflow is:
1. The user opens the TUI at the start of a work session.
2. The TUI shows all live Otto work in that repo, regardless of where it started.
3. The user keeps the TUI open while launching work from the CLI, from queue, and from
   merge flows.
4. Runs appear, update, pause, fail, finish, and move into history without the user
   reopening the UI.
5. The user drives a small set of operator actions from the keyboard.
6. The user closes the TUI when they are done. No daemon remains.
This is intentionally closer to `htop` plus a project event console than to a wizard.
## Goals
- One TUI surface covering queue tasks, standalone builds, standalone cert runs, improve
  runs, merge runs, interrupted work, and resumable work.
- One canonical live run registry with one normalized record per long-running command.
- One mutation contract per domain: one writer and append-only commands.
- Cross-process discovery: a CLI launched in another shell appears in the TUI within
  1-2 seconds.
- A history pane that is cross-session, append-only, and keyed by the same run ids as
  the live registry.
- A small, opinionated action surface: cancel, resume, retry/requeue, remove/cleanup,
  merge selected/all, open logs, open file in editor.
- No daemon in v1.
- No breakage for existing queue-dashboard users while the universal viewer lands.
## Non-Goals
Mission Control v1 is not:
- a daemon or background service
- a deep spec review UI
- more than three core panes
- a full queue authoring or dependency editing UI
- a replacement for the CLI API
- a solution to terminal disconnect persistence beyond normal `tmux` / screen usage
The explicit v1 exclusions are:
- deep spec review UI; viewer plus open-in-editor only
- more than 3 core panes (Live Runs / History / Detail+Logs)
- full queue authoring inside the TUI
- daemon / background service
## Design Principles
- Build the substrate first. If the runtime state is not coherent, the TUI is just a
  nicer wrapper around fragmentation.
- Use `otto/paths.py` as the filesystem choke point for new runtime paths.
- Keep one canonical record per run even when a domain has richer private state.
- Preserve domain-local authority. Queue, merge, and atomic commands do not need one
  monolithic writer; they do need one project-global registry that normalizes what they
  publish for discovery. The registry is the canonical discovery surface, but repair
  always flows from the domain authority back into the registry, never the other way
  around.
- Use append-only commands for cross-process mutations. The TUI should either shell the
  CLI or append a durable command record; it should never mutate runtime state in memory
  and hope the owning process notices.
- Let readers derive staleness. In a daemonless model, writers can crash. The TUI may
  display `STALE` based on heartbeat age and dead PIDs, but it should not rewrite the
  canonical record itself.
## What Stays And What Goes
What stays:
- `summary.json`, `checkpoint.json`, and `manifest.json`
- queue's existing split between definitions, state, and commands
- merge's own state file and merge lock
- CLI entrypoints and flags
- append-only history
What goes:
- queue dashboard as the only rich live operator surface
- UI code that has to reverse-engineer five incompatible storage layouts
- the assumption that "latest session" is enough to find active work
- the idea that the TUI can be treated as a transient launcher and later upgraded into
  Mission Control without redoing the architecture
## Substrate Design: The C-Shaped Foundation
The substrate has four irreducible pieces:
1. Canonical run registry.
2. One mutation contract per domain.
3. Cross-process discovery via file heartbeats and append-only events.
4. Unified history keyed by run id.
Those pieces are the actual D-v1. The Textual layer sits on top of them.
## Canonical Run Registry
### Target file layout
Use `otto/paths.py` to name the registry and command paths. The target layout is:
```text
otto_logs/
├── sessions/
│   └── <run_id>/
│       ├── intent.txt
│       ├── summary.json
│       ├── checkpoint.json
│       ├── manifest.json
│       ├── commands/
│       │   ├── requests.jsonl          # new: atomic-domain commands
│       │   ├── requests.jsonl.processing
│       │   └── acks.jsonl
│       ├── build/
│       ├── certify/
│       ├── improve/
│       └── spec/
├── merge/
│   ├── merge.log
│   ├── commands/
│   │   ├── requests.jsonl              # new: merge-domain commands
│   │   ├── requests.jsonl.processing
│   │   └── acks.jsonl
│   └── <merge_id>/
│       └── state.json
├── queue/
│   ├── <task_id>/
│   │   └── manifest.json
│   └── watcher.log
├── cross-sessions/
│   ├── history.jsonl
│   ├── certifier-memory.jsonl
│   └── runs/
│       ├── live/
│       │   └── <run_id>.json
│       └── gc/
│           └── tombstones.jsonl
├── latest
├── paused
└── .lock
```
The queue files `.otto-queue-state.json` and `.otto-queue-commands.jsonl` should stay
where they are in v1 for compatibility, but they should stop being hardcoded. `paths.py`
should grow wrappers like `queue_state_path(...)` and `queue_commands_path(...)` even if
the paths still resolve to the current project-root filenames.
### Required `otto/paths.py` additions
Add helpers rather than more literals:
```python
def runs_dir(project_dir: Path) -> Path
def live_runs_dir(project_dir: Path) -> Path
def live_run_path(project_dir: Path, run_id: str) -> Path
def run_gc_dir(project_dir: Path) -> Path
def run_gc_tombstones_jsonl(project_dir: Path) -> Path
def session_commands_dir(project_dir: Path, run_id: str) -> Path
def session_command_requests(project_dir: Path, run_id: str) -> Path
def session_command_acks(project_dir: Path, run_id: str) -> Path
def merge_commands_dir(project_dir: Path) -> Path
def merge_command_requests(project_dir: Path) -> Path
def merge_command_acks(project_dir: Path) -> Path
def queue_state_path(project_dir: Path) -> Path
def queue_commands_path(project_dir: Path) -> Path
def queue_command_acks_path(project_dir: Path) -> Path
```
### Canonical live schema
The run record should be keyed by the operator's needs, not by one domain's internal
storage model. Recommended v1 schema:
```json
{
  "schema_version": 1,
  "run_id": "2026-04-22-184533-acde12",
  "domain": "atomic",
  "run_type": "build",
  "command": "build",
  "display_name": "build: add csv export",
  "status": "running",
  "terminal_outcome": null,
  "project_dir": "/repo",
  "cwd": "/repo",
  "writer": {
    "pid": 12345,
    "pgid": 12345,
    "writer_id": "atomic:2026-04-22-184533-acde12",
    "boot_id": "8f1f0b5c-...",
    "process_start_time_ns": 1713811533000000000
  },
  "identity": {
    "queue_task_id": null,
    "merge_id": null,
    "parent_run_id": null
  },
  "source": {
    "invoked_via": "cli",
    "argv": ["build", "add csv export", "--fast"],
    "resumable": true
  },
  "timing": {
    "started_at": "2026-04-22T18:45:33Z",
    "updated_at": "2026-04-22T18:45:39Z",
    "heartbeat_at": "2026-04-22T18:45:39Z",
    "finished_at": null,
    "duration_s": 6.2,
    "heartbeat_interval_s": 2.0,
    "heartbeat_seq": 4
  },
  "git": {
    "branch": "build/add-csv-export-2026-04-22",
    "worktree": null,
    "target_branch": null,
    "head_sha": null
  },
  "intent": {
    "summary": "add csv export",
    "intent_path": "/repo/intent.md",
    "spec_path": null
  },
  "artifacts": {
    "session_dir": "/repo/otto_logs/sessions/2026-04-22-184533-acde12",
    "manifest_path": "/repo/otto_logs/sessions/.../manifest.json",
    "checkpoint_path": "/repo/otto_logs/sessions/.../checkpoint.json",
    "summary_path": "/repo/otto_logs/sessions/.../summary.json",
    "primary_log_path": "/repo/otto_logs/sessions/.../build/narrative.log",
    "extra_log_paths": []
  },
  "metrics": {
    "cost_usd": 0.14,
    "stories_passed": null,
    "stories_tested": null
  },
  "adapter_key": "atomic.build",
  "last_event": "writing checkout logic",
  "version": 4
}
```
Why this shape:
- `domain` tells us who owns mutation and heartbeat semantics.
- `run_type` tells the UI how to label the row.
- `identity` ties a normalized run back to queue or merge state.
- `artifacts` give adapters stable roots; they do not try to pre-render every domain's
  detail panel inside the registry row.
- `adapter_key` lets the viewer dispatch to per-domain adapters instead of turning the
  universal model into a god-object.
- command ack state is deliberately kept out of the live record; it belongs in the
  command ack journal so replay stays durable and auditable.
### Standard status vocabulary
The registry should map all domains onto one user-facing status set:
- `queued`
- `starting`
- `running`
- `terminating`
- `paused`
- `interrupted`
- `done`
- `failed`
- `cancelled`
- `removed`
`stale` is not a persisted status. It is a reader-derived overlay when the live record is
non-terminal but the heartbeat is old and the writer PID is dead.
### Lifecycle
The record lifecycle should be:
1. Allocate `run_id`.
2. Create the first registry record before expensive work starts.
3. Update the record on heartbeat and major state transitions.
4. Write summary / manifest / checkpoint as today.
5. Finalize the record with terminal outcome.
6. Append one terminal snapshot to history.
7. Keep the live record in place for a short retention window.
8. GC the live record later.
Queue needs one extra rule: the queue task id is not the run id. A queue task should
get a new `run_id` each execution attempt while preserving the stable queue task id under
`identity.queue_task_id`. Merge should use `merge_id` as `run_id` in v1 because that
keeps the operator model simple.
### Queue Attempt Identity
Plan Gate note (1): this section replaces the prior "new run id per attempt" sentence
with an explicit watcher-to-child handshake and crash repair contract.

Queue attempts should use watcher-allocated `attempt_run_id`, not child-side allocation.
That keeps queue attempt identity compatible with queue's single-writer model and avoids
calling `paths.new_session_id()` from a queue child that does not hold the project lock.

Allocation contract:
1. The watcher allocates `attempt_run_id` before spawn via a new registry allocator such
   as `otto/runs/registry.py::allocate_run_id(...)`.
2. That allocator must not rely on `project_lock(...)`; it should reserve identity with
   `O_CREAT|O_EXCL` or an equivalent tempdir reservation under
   `otto_logs/cross-sessions/runs/live/`.
3. The watcher writes the chosen `attempt_run_id` into queue state before spawn under the
   task's current attempt record.
4. During the rollout window, the watcher injects the id into the child via
   `OTTO_RUN_ID=<attempt_run_id>` only. Environment propagation is the wire format because
   it crosses version boundaries cleanly; older Click children that do not understand
   `--run-id` simply ignore the env var and continue allocating their own session id.
5. Once every atomic child binary supports an explicit `--run-id <attempt_run_id>` flag,
   the watcher may pass that flag as a clarity/redundancy aid, but `OTTO_RUN_ID` remains
   the compatibility contract between watcher and child.
6. Atomic child commands that understand run-id injection should treat the supplied id,
   whether sourced from `OTTO_RUN_ID` or a future `--run-id`, as authoritative and must
   not allocate a replacement id when present.

Propagation contract:
- the queue watcher remains the sole writer of the queue live registry record
- a child that understands `OTTO_RUN_ID` owns the session directory and writes
  `summary.json`, `checkpoint.json`, and `manifest.json` using the injected
  `attempt_run_id`
- a pre-run-id child keeps current behavior, allocates its own session id, and exposes
  that actual child id through its normal artifacts
- the watcher mirrors child progress into the queue-owned live record keyed by the
  watcher's `attempt_run_id`; if the child allocated a different id, the run record must
  preserve both the watcher-expected id and the child-actual id and surface a
  `child predates run-id` warning in detail/status

Crash reconciliation:
- if the watcher allocates an id but crashes before spawn, the reserved id is left
  `unclaimed`; a later watcher startup may either reuse the same queued task with a fresh
  attempt id or GC the abandoned reservation after no child/session artifacts appear
- if the child starts and writes artifacts but the watcher crashes before mirroring, the
  restarted watcher adopts the recorded `attempt_run_id` from queue state and rebuilds the
  queue live record from queue state + child artifacts
- if queue state says an attempt is running but the child never wrote artifacts and no
  matching process identity exists, startup repair marks that attempt interrupted per the
  watcher's existing restart policy and appends history only if a terminal outcome can be
  proven
### Atomicity guarantees
The registry must preserve queue's current correctness bar:
- readers see old or new contents, never partial writes
- two writers never contend for the same persisted live file
Concretely:
- each live record is owned by exactly one writer process
- live record updates use tempfile + `os.replace(...)`
- command logs use append + `flock(LOCK_EX)` + `fsync`
- history appends use a dedicated durability primitive, not best-effort log writes
- readers never take write locks
This is enough for a daemonless v1.
### GC policy
The registry should not grow forever in `live/`. Recommended policy:
- keep active records indefinitely while non-terminal
- keep terminal records in `live/` for 24 hours
- keep `history.jsonl` forever
- append one tombstone row to `cross-sessions/runs/gc/tombstones.jsonl` when GC removes a
  live record so the cleanup remains auditable
GC should be opportunistic. Acceptable triggers are TUI startup, queue watcher startup,
atomic run startup, and merge startup. GC must never delete session artifacts, manifests,
summaries, or history.
## One Mutation Contract Per Domain
The governing rule is simple: one writer owns domain state, and every cross-process
mutation request is durable and append-only.
### Common command envelope
All command files should use the same envelope:
```json
{
  "schema_version": 1,
  "command_id": "cmd-2026-04-22T18:45:39Z-12345-1",
  "run_id": "2026-04-22-184533-acde12",
  "domain": "atomic",
  "kind": "cancel",
  "requested_at": "2026-04-22T18:45:39Z",
  "requested_by": {
    "source": "tui",
    "pid": 12345
  },
  "args": {}
}
```
Writers should treat `command_id` as an idempotency key. Duplicate command rows are fine.
Invalid commands should be ignored and logged, not treated as fatal runtime corruption.
### Durable command protocol
Plan Gate note (3): this section adds the missing read/apply/ack/replay contract and
spells out how pending vs lost commands are distinguished after crashes.

Every domain should implement the same durable command lifecycle:
1. A client appends one envelope to the domain's request log.
2. The owning writer drains requests by renaming the active request log to
   `.processing`, then reading from `.processing`.
3. The writer applies each unacked command idempotently.
4. The writer durably persists the resulting state mutation first.
5. Only after the mutation is durable does the writer append an ack row.
6. The writer may then unlink or compact `.processing`.

Required file layout:
- queue: keep `.otto-queue-commands.jsonl`, add `.otto-queue-commands.jsonl.processing`,
  and add `.otto-queue-commands.acks.jsonl`
- atomic: `otto_logs/sessions/<run_id>/commands/requests.jsonl`,
  `requests.jsonl.processing`, and `acks.jsonl`
- merge: `otto_logs/merge/commands/requests.jsonl`, `requests.jsonl.processing`, and
  `acks.jsonl`

Ack row shape:
```json
{
  "schema_version": 1,
  "command_id": "cmd-2026-04-22T18:45:39Z-12345-1",
  "run_id": "2026-04-22-184533-acde12",
  "acked_at": "2026-04-22T18:45:40Z",
  "writer_id": "atomic:2026-04-22-184533-acde12",
  "outcome": "applied",
  "state_version": 12,
  "note": null
}
```

Replay contract:
- startup must first drain any leftover `.processing` file before reading new requests
- startup must load `acks.jsonl` into an in-memory set of acked `command_id`s
- any command present in `requests.jsonl` or `.processing` without an ack must be replayed
- replay must be idempotent on `command_id`, so a crash after state mutation but before
  ack append is safe
- the TUI should render `pending` when a request row exists without an ack and the writer
  is still live, and `unacked` when the writer is gone so recovery is operator-visible
### Queue domain
Writer identity:
- `otto/queue/runner.py` remains the sole writer of `.otto-queue-state.json`
- the same runner also becomes the writer of queue-backed live registry records
Command file:
- keep `.otto-queue-commands.jsonl` in v1
- name it in `otto/paths.py`
- add a sibling ack journal and preserve queue's existing rename-to-`.processing` drain
Conflict handling:
- `cancel`, `remove`, and `resume` remain idempotent
- invalid commands are ignored with a warning
- queue state remains authoritative; the registry is the normalized mirror
Registry mapping:
- each queue attempt publishes one live run record
- the record stores both `queue_task_id` and the per-attempt `run_id`
- retries or resumes create new run ids while preserving queue task identity
- queue startup repair replays every unacked command before spawning new work
### Merge domain
Writer identity:
- the process inside `merge_lock(project_dir)` is the sole writer
- it owns both `otto_logs/merge/<merge-id>/state.json` and the merge run record
Command file:
- add `otto_logs/merge/commands/requests.jsonl` plus `acks.jsonl`
Supported in-process v1 commands:
- `cancel`
Explicitly unsupported in-process v1 merge commands:
- `resume`
- `retry`
Those remain disabled or shell-to-CLI actions until merge resume becomes a real CLI
contract. The TUI should not invent one.
### Atomic domain: build / improve / certify
Writer identity:
- the actual command process is the sole writer of its run record
- it already owns the session directory
- it already has natural heartbeat points in the agent loop and checkpoint writes
Command file:
- add `otto_logs/sessions/<run_id>/commands/requests.jsonl` plus `acks.jsonl`
Supported in-process v1 commands:
- `cancel`
Actions that remain shell-to-CLI rather than in-process mutations:
- `resume`
- `retry`
Why split them this way:
- cancel is a message to the active writer
- resume and retry are fresh command entrypoints with config, argument, and worktree logic
Polling points should be added in:
- `otto/pipeline.py`
- `otto/spec.py`
- `otto/certifier/__init__.py`
If an atomic run does not acknowledge a `cancel` command within one heartbeat interval,
the TUI may fall back to a helper that sends `SIGTERM` to the recorded `pgid`. The signal
path is a safety valve, not the canonical mutation path.
## Cross-Process Discovery
The v1 rule is:
- file-based heartbeats
- file-based command logs
- polling readers
- no daemon
### Discovery source
The TUI should discover live work by polling only:
- `otto_logs/cross-sessions/runs/live/*.json`
- the selected log paths named by the chosen run record
- `history.jsonl` for the History pane
The TUI should not rescan every session dir every tick. That recreates the exact
storage-coupling that the registry is supposed to remove.
### Heartbeat protocol
Every live writer should update:
- `timing.heartbeat_at`
- `timing.updated_at`
- `timing.heartbeat_seq`
- `last_event` when it has a meaningful short status string
Registry heartbeat should not depend on log file writes. A build can be quiet in the
narrative log while still healthy, and merge can sit inside git operations with no
narrative output at all.
Recommended defaults:
- writer heartbeat every 2 seconds
- TUI poll interval 500ms while active work exists
- TUI poll interval 1.5s when idle
### Staleness detection
Plan Gate note (4): this replaces the naive wall-clock + `kill(pid, 0)` rule with a
writer-identity and sequence-based contract that tolerates suspend and clock jumps.

Every writer must persist stable process identity:
- `writer.pid`
- `writer.pgid`
- `writer.process_start_time_ns`
- `writer.boot_id` when the platform exposes it; otherwise the process start time becomes
  the identity tie-breaker

Reader rules:
- if the record is terminal, do nothing
- clamp negative wall-clock age to `0` so NTP or manual clock rollback never creates
  synthetic staleness
- if `heartbeat_seq` advanced since the prior poll, treat the run as healthy regardless of
  apparent wall-clock skew and reset the local stale timer
- if the reader observes a suspend or wall-clock jump larger than 30 seconds between polls,
  enter one grace window of `max(3 * heartbeat_interval_s, 15s)` during which records may
  show `LAGGING` but not `STALE`
- display `LAGGING` when the heartbeat is overdue but the same writer identity is still
  alive, or when the grace window is active
- display `STALE` only when all of the following are true:
  1. the record is non-terminal
  2. `heartbeat_seq` has not advanced for at least
     `max(3 * heartbeat_interval_s, 15s)` of local monotonic reader time
  3. the writer identity no longer matches a live process

The persisted file stays unchanged. The TUI renders the overlay. That preserves the
single-writer rule in a daemonless system and prevents healthy runs from being labeled
stale after laptop sleep.
### Cleanup
Because stale is reader-derived, cleanup must be explicit. Allowed paths are:
- operator uses a remove/cleanup action on a stale terminal-capable record
- the next domain writer startup notices and repairs or finalizes its own abandoned record
- GC eventually removes old terminal records after history is safely written
Not allowed:
- TUI rewriting arbitrary domain state on sight
## Repair Precedence
Plan Gate note (5): this section resolves the prior ambiguity about which file wins after
crash recovery or mixed partial writes.

The registry is the canonical discovery surface for Mission Control. It is not the
ultimate source of truth for domain semantics. Repair always proceeds from domain truth to
registry truth using the rules below.

### Queue
Source of truth:
- `.otto-queue-state.json`

Secondary:
- live registry record keyed by `attempt_run_id`

Tertiary:
- child session artifacts for the injected `attempt_run_id`

Startup repair:
1. Load queue state.
2. Drain unacked queue commands and replay them idempotently.
3. For each task attempt, rebuild or repair the queue live record from queue state.
4. If queue state says `running` but the child artifacts prove terminal, finalize queue
   state and history from the proved terminal outcome.
5. If queue state says `running` and no matching child identity or artifacts exist, mark
   interrupted/fail per watcher restart policy and update the registry accordingly.

### Atomic build / improve / certify
Source of truth:
- session-local durable artifacts in `checkpoint.json`, `manifest.json`, and
  `summary.json`

Secondary:
- live registry record

Tertiary:
- `history.jsonl`

Startup repair:
1. Recreate or refresh the live registry record from checkpoint + manifest when resuming or
   when a child restarts.
2. If checkpoint or summary proves a terminal outcome, that terminal outcome overrides a
   stale non-terminal live record.
3. If history exists but the live record is missing, do not recreate live state; history is
   enough because the run is already terminal.

### Merge
Source of truth:
- `otto_logs/merge/<merge_id>/state.json`

Secondary:
- live registry record

Tertiary:
- `history.jsonl`

Startup repair:
1. Load merge state.
2. Replay unacked merge commands.
3. Repair the live registry row from merge state.
4. If merge state is terminal, append or repair history and allow GC of the live record.

### History
Source of truth:
- `history.jsonl` is authoritative for the History pane only

Secondary:
- terminal fields from domain-local state when repairing a missing history row

Repair rule:
- never let `history.jsonl` rewrite queue state, checkpoint state, or merge state
- do allow a domain startup repair pass to append a missing terminal history row when the
  terminal outcome is already proved elsewhere
## History
History is not optional. Mission Control without a history pane would still feel like a
better queue dashboard, not like a project operator console.
### File and schema
Keep `otto_logs/cross-sessions/history.jsonl`. Do not create a separate TUI history file.
Instead, evolve the schema from a build-summary helper into a run-terminal-snapshot helper.
Recommended v2 row:
```json
{
  "schema_version": 2,
  "history_kind": "terminal_snapshot",
  "run_id": "2026-04-22-184533-acde12",
  "domain": "atomic",
  "run_type": "build",
  "command": "build",
  "queue_task_id": null,
  "merge_id": null,
  "timestamp": "2026-04-22T18:55:03Z",
  "started_at": "2026-04-22T18:45:33Z",
  "finished_at": "2026-04-22T18:55:03Z",
  "status": "done",
  "terminal_outcome": "success",
  "intent": "add csv export",
  "branch": "build/add-csv-export-2026-04-22",
  "worktree": null,
  "cost_usd": 0.68,
  "duration_s": 570.1,
  "resumable": true,
  "manifest_path": "/repo/otto_logs/sessions/.../manifest.json",
  "summary_path": "/repo/otto_logs/sessions/.../summary.json",
  "primary_log_path": "/repo/otto_logs/sessions/.../build/narrative.log"
}
```
### History Durability
Plan Gate note (2): this replaces the prior hand-wave about reusing
`append_history_entry()` with a correctness-grade append primitive.

`otto/history.py` should expose a dedicated append primitive such as
`append_history_snapshot(...)` with these guarantees:
- open `history.jsonl` itself in append mode and take `flock(LOCK_EX)` on that file
- serialize exactly one newline-terminated JSON row per append
- flush and `fsync` before unlocking
- return the persisted row including a stable `dedupe_key`

Recommended `dedupe_key`:
- terminal snapshots: `terminal_snapshot:<run_id>`
- future non-terminal history events: `<history_kind>:<run_id>:<event_seq>`

This primitive must be strict by default. History is no longer best-effort observability;
it is part of the correctness path. Log helpers remain appropriate for diagnostic logs,
not for cross-session history.
### Writer rules
One writer per run should append exactly one terminal snapshot on terminalization:
- atomic runs append their own snapshot
- queue appends when a queue attempt becomes terminal
- merge appends when the merge attempt becomes terminal
`otto/history.py` should become the shared helper for this write. The existing build and
certify summary writes in `otto/pipeline.py` and `otto/certifier/__init__.py` should be
migrated behind the richer helper rather than left as a parallel history system.
### Backwards compatibility
`otto/cli_logs.py` should continue to read:
- existing v1 build / improve / certify rows
- new v2 terminal snapshots
No destructive rewrite migration is needed. Readers should be additive and tolerant.
### Dedup rules
Because history is append-only, duplicates will eventually happen. Reader rules should be:
- primary key is `run_id`
- prefer the newest row for the same `dedupe_key`
- prefer the newest `history_kind="terminal_snapshot"` row
- if both an old summary row and a new snapshot row exist for the same run id, prefer the
  new row
- queue retries are not duplicates because they should have new `run_id`s
## TUI Surface Design
The TUI should be one screen with three core panes and only short-lived modals for help
and confirmation.
### Domain adapters
Plan Gate note (7): this section keeps the registry narrow and moves type-specific row,
detail, and action logic behind adapters.

The universal viewer should not treat the registry row as a giant union of every domain's
UI needs. Instead, add one adapter per `adapter_key` with a minimal interface:

```python
class MissionControlAdapter(Protocol):
    def row_label(self, record: RunRecord) -> str: ...
    def history_summary(self, history_row: HistoryRow) -> str: ...
    def artifacts(self, record: RunRecord) -> list[ArtifactRef]: ...
    def legal_actions(self, record: RunRecord) -> list[ActionState]: ...
    def detail_panel_renderer(self, record: RunRecord) -> DetailModel: ...
```

Registry responsibility:
- stable common facts only: identity, status, timing, command label, primary artifact
  roots, and writer identity

Adapter responsibility:
- row formatting details
- history summary text
- artifact expansion and ordering
- legal action enable/disable decisions
- detail panel sections that depend on queue state, checkpoint state, or merge state

That keeps the model additive as new domains arrive and avoids baking queue/merge/atomic
special cases into one flat viewer object.
### Layout
```text
+----------------------+----------------------+----------------------------------+
| 1. Live Runs         | 2. History           | 3. Detail + Logs                 |
|                      |                      |                                  |
| active + recent      | terminal snapshots   | metadata top                     |
| sorted by recency    | filtered / paged     | live tail bottom                 |
+----------------------+----------------------+----------------------------------+
```
Suggested width split:
- Live Runs: 32%
- History: 28%
- Detail + Logs: 40%
The right pane is intentionally larger because it holds both metadata and the active log.
### Live Runs pane
Purpose:
> what is happening right now in this project?
One row per live registry record. Recommended columns:
- status
- type
- id
- branch/task
- elapsed
- cost
- event
Display rules:
- `id` shows `queue_task_id` when present, else `run_id`
- `type` shows `build`, `improve`, `certify`, `merge`, or `queue`
- `branch/task` shows branch if meaningful, else task id, else `-`
- `event` shows a short `last_event`
Sort rules:
- non-terminal before terminal
- newest `updated_at` first
- running before queued before interrupted before recently terminal
Retention rules:
- keep terminal rows visible in Live Runs for 5 minutes so a just-finished run does not
  vanish instantly
- after 5 minutes, remove them from Live Runs but keep them in History
Minimal v1 filters:
- `a` toggles active-only vs all visible live rows
- `t` cycles type filter: all / build / improve / certify / merge / queue
### History pane
Purpose:
> what already happened, and what can I reopen?
Data source:
- `history.jsonl` only
Recommended columns:
- completed_at
- outcome
- type
- id
- duration
- cost
- summary
Where `summary` is:
- intent summary for build / improve / certify
- target plus branch count for merge
- task id or branch when no better intent summary exists
History must be paginated. Recommended controls:
- `[` previous page
- `]` next page
- `PageUp` / `PageDown` when History is focused
Page size:
- 50 rows
Minimal v1 filters:
- `f` cycles outcome: all / success / failed / interrupted / cancelled
- `t` cycles type filter
- `/` opens a substring filter over intent, branch, task id, and run id
### Detail + Logs pane
This is one core pane with two fixed regions.
Metadata top should show:
- run id
- status
- type
- started / finished
- duration / cost
- branch / worktree / cwd
- resumability
- artifact paths
- available actions
The artifact list should be selectable. Expected rows are:
- `intent.md` or resolved `intent.txt`
- `spec.md` when present
- `manifest.json`
- `summary.json`
- `checkpoint.json`
- primary log
- secondary log if present
Live tail bottom should tail `artifacts.primary_log_path` and allow cycling across
`extra_log_paths`.
Behavior:
- auto-follow by default
- `End` resumes follow
- `Home` jumps to top
- `s` toggles follow mode
- `o` cycles logs
### Cross-pane navigation
The TUI must be fully keyboard-driven. Recommended contract:
- `Tab` / `Shift-Tab` cycles panes
- `1`, `2`, `3` focus Live Runs, History, Detail + Logs
- `Left` / `Right` moves focus across panes
- `Up` / `Down` or `j` / `k` move within the focused pane
- `Enter` pins the selected run and moves focus to Detail + Logs
- `Esc` returns focus to the originating list pane
- `?` opens help
### State transitions
Boot:
1. Load live snapshot.
2. Load first history page.
3. Focus Live Runs.
4. Select the newest active run if any, else the newest history row.
Selection change:
- update metadata immediately
- retarget the log tailer
- preserve follow mode unless the user disabled it
Live-to-history transition:
- if the selected run becomes terminal, keep it selected
- if it leaves the Live Runs window, preserve selection through its history row
Cross-process appearance:
- new runs should appear without stealing focus
- a small footer banner is enough to say "new run detected"
Stale overlay:
- if a selected non-terminal run becomes stale, keep it visible, show `STALE`, and
  disable actions that require a live writer
## Operator Action Mapping
Every in-TUI action must map either to a CLI shell-out or to an append-only command file.
No action should require the TUI to mutate domain runtime state directly.
| Action | Key | Scope | Underlying operation | Failure UX |
|---|---|---|---|---|
| Cancel | `c` | live queue task | append queue `cancel` to `.otto-queue-commands.jsonl` | toast + detail banner if writer missing or task not cancellable |
| Cancel | `c` | live atomic run | append `cancel` to `sessions/<run_id>/commands/requests.jsonl`; fallback `SIGTERM` helper after one heartbeat if unacked | toast + stale warning if writer is gone |
| Cancel | `c` | live merge run | append `cancel` to `otto_logs/merge/commands/requests.jsonl` | toast + stale warning if merge writer is gone |
| Resume | `r` | interrupted queue task | shell `otto queue resume <task_id>` | modal with stderr; keep row selected |
| Resume | `r` | interrupted build | shell `otto build --resume` from stored `cwd` | modal with stderr or disabled reason if checkpoint missing |
| Resume | `r` | interrupted improve | shell `otto improve <subcommand> --resume` from stored `cwd` | modal with stderr |
| Resume | `r` | standalone certify | disabled in v1 | footer says standalone certify has no resume path |
| Resume | `r` | merge | disabled in v1 | footer says merge `--resume` is deferred |
| Retry / Requeue | `R` | queue task | shell reconstructed `otto queue ...` using stored task definition | modal on malformed queue record or task-id collision |
| Retry / Requeue | `R` | atomic build / improve / certify | shell reconstructed original argv from `source.argv` | modal on invalid argv or missing cwd |
| Remove | `x` | queued queue task | shell `otto queue rm <task_id>` | toast if task already terminal or watcher rejects |
| Cleanup | `x` | terminal queue task with worktree | shell `otto queue cleanup <task_id>` | modal with git worktree error output |
| Cleanup | `x` | terminal atomic / merge run | shell thin cleanup CLI over registry GC | toast on missing record or missing artifacts |
| Merge selected | `m` | selected done queue rows | shell `otto merge <task_id...>` | modal with merge stderr |
| Merge all | `M` | project scope | shell `otto merge --all` | modal with merge stderr |
| Open logs | `o` | selected run | cycle current log inside Detail + Logs | placeholder if no logs |
| Open file in editor | `e` | selected artifact row | shell `$EDITOR <path>` | modal if `$EDITOR` unset or launch fails |
Notes:
- `r` means resume the same durable run if the CLI actually supports resume.
- `R` means launch a fresh attempt with the same stored argv or queue definition.
- The TUI should not reimplement config loading, worktree policy, or argument validation
  for resume/retry. That is why those actions shell the existing CLI.
- `$EDITOR` is the only editor integration in v1. No embedded editor.
## Migration Plan
The migration should be substrate first, viewer second, mutations third, polish last.
Doing it in the opposite order mostly creates throwaway UI work. In Otto's actual
delivery model, these are agent-dispatch phases, not human-engineer week estimates.
Expect roughly one day of implementation dispatch per phase plus another `0.5-1` day of
gate review and real-LLM validation where required.
### Phase 1: substrate
Goal: canonical run registry, universal record format, queue/merge/atomic publishers.
Estimated agent dispatch: `1-2` days.
Dispatch focus:
- add path helpers in `otto/paths.py`
- add `otto/runs/schema.py`
- add `otto/runs/registry.py`
- extend `otto/history.py` and `otto/cli_logs.py` for schema-v2 history rows
- add unit tests for atomic record writes, heartbeat interpretation, repair precedence,
  and GC
- retrofit queue runner to publish queue attempt records
- retrofit merge orchestrator to publish merge records
- retrofit build/improve/certify startup, heartbeat, and finalize points
- publish artifact paths, adapter keys, and action metadata inputs
Files touched:
- `otto/cli.py`
- `otto/cli_improve.py`
- `otto/config.py`
- `otto/paths.py`
- `otto/history.py`
- `otto/cli_logs.py`
- `otto/queue/schema.py`
- `otto/queue/runtime.py`
- `otto/queue/runner.py`
- `otto/merge/state.py`
- `otto/merge/orchestrator.py`
- `otto/pipeline.py`
- `otto/checkpoint.py`
- `otto/certifier/__init__.py`
- `otto/spec.py`
- `otto/manifest.py`
- new `otto/runs/schema.py`
- new `otto/runs/registry.py`
- new tests such as `tests/test_run_registry.py`
Exit criteria:
Plan Gate note (6): the touched-file inventory now explicitly includes
`otto/cli_improve.py` and `otto/config.py`, which the prior draft missed.

- every long-running Otto command writes one live registry record
- every terminal run appends one normalized history row
- queue runs include both `task_id` and `run_id`
- merge publishes heartbeat and artifact paths
- standalone build / improve / certify appear in the registry without queue
- cross-shell visibility is within 1-2 seconds locally
- required gate exit: simultaneous registration races cannot produce duplicate run ids or
  torn live records
- required gate exit: writer crash after command read but before ack replays idempotently
  on restart
- required gate exit: suspend / clock-jump scenarios do not mislabel healthy runs as
  `STALE`
- required gate exit: mixed-version upgrade during active work preserves queue execution
  and discovery
- required gate exit: history corruption recovery skips malformed lines, preserves later
  rows, and never rewrites domain truth
What breaks if Phase 2 ships first:
- the generic TUI has to read queue state, merge state, checkpoints, manifests, and
  summaries directly
- domain-specific adapters get embedded in UI code
- most of that work gets rewritten once the registry exists
### Phase 2: universal viewer
Goal: one dashboard above the registry; deprecate queue-only internals.
Estimated agent dispatch: `1` day.
Dispatch focus:
- add `otto/tui/mission_control_model.py`
- add `otto/tui/mission_control.py`
- build the three-pane layout
- port log tailing and artifact inspection from `otto/queue/dashboard.py`
- keep `otto queue dashboard` as a compatibility wrapper that opens the new app with a
  queue filter
Files touched:
- `otto/queue/dashboard.py`
- `otto/cli_queue.py`
- optionally `otto/cli.py` if we add `otto dashboard`
- new `otto/tui/mission_control.py`
- new `otto/tui/mission_control_model.py`
Exit criteria:
- the dashboard shows queue, atomic, and merge runs in one surface
- History renders terminal snapshots from `history.jsonl`
- Detail can tail logs for queue and non-queue runs
- `otto queue dashboard` still works
What breaks if Phase 3 ships first:
- mutations get attached to the wrong queue-centric UI model
- the project still lacks a true mission-control surface
### Phase 3: mutations
Goal: port resume, retry, remove, cleanup, and merge launch into the TUI while the CLI
remains authoritative.
Estimated agent dispatch: `0.5-1` day.
Dispatch focus:
- add `otto/tui/mission_control_actions.py`
- wire cancel for queue / merge / atomic
- wire shell-out actions for resume / retry / merge / cleanup
- render capability-dependent help and disabled states
Files touched:
- `otto/cli_queue.py`
- `otto/cli_merge.py`
- `otto/queue/runner.py`
- `otto/merge/orchestrator.py`
- `otto/pipeline.py`
- `otto/certifier/__init__.py`
- `otto/spec.py`
- new `otto/tui/mission_control_actions.py`
Exit criteria:
- every enabled keybind maps to a CLI shell-out or a durable command append
- disabled actions say why they are disabled
- cancel works for queue, merge, and atomic runs
- resume works where the CLI already supports it
- retry / requeue launches a fresh attempt from stored metadata
- merge selected / merge all can be launched from the TUI
What breaks if Phase 4 ships first:
- the TUI becomes a nicer viewer, but not an operator console
### Phase 4: history + editor hooks
Goal: unified history pane and `$EDITOR` integration.
Estimated agent dispatch: `0.5-1` day.
Dispatch focus:
- finalize history schema-v2 writers
- add history pagination and filtering
- add artifact selection in Detail metadata
- shell `$EDITOR` for intent, spec, manifest, summary, and logs
- add registry cleanup helper for old terminal records
Files touched:
- `otto/history.py`
- `otto/cli_logs.py`
- `otto/tui/mission_control.py`
- `otto/tui/mission_control_model.py`
- `otto/paths.py`
Exit criteria:
- History navigates recent runs without rescanning all sessions
- artifacts open correctly in `$EDITOR`
- missing editor configuration produces a clear error
- every run type exposes useful artifact paths in Detail
What breaks if Phase 5 ships first:
- we polish the wrong model before the core inspection workflow is complete
### Phase 5: polish
Goal: keyboard discoverability, log search, theming, and refresh hardening.
Estimated agent dispatch: `1` day.
Dispatch focus:
- improve help and discoverability
- add log search
- add light theming and status color rules
- performance pass on refresh cadence and file caching
Optional follow-up dispatch:
- compatibility cleanup for `otto queue dashboard`
- sharp-edge fixes from nightly scenarios
Files touched:
- `otto/tui/mission_control.py`
- `otto/tui/mission_control_model.py`
- `otto/theme.py`
- `otto/queue/dashboard.py` if the wrapper still needs polish
Exit criteria:
- the TUI is fully keyboard-driven without hidden critical actions
- log search works
- refresh remains stable with multiple concurrent runs
- queue-dashboard users can move over without losing muscle memory
## Compatibility During Rollout
Plan Gate note (8): this section makes mixed-version behavior explicit instead of
hand-waving rollout safety.

#### Scenario A: old watcher + new viewer
Meaning:
- the queue watcher still writes only legacy queue state, but the new Mission Control
  binary is launched

Acceptance criteria:
- the viewer still renders queue rows through a queue-only compatibility adapter
- queue rows clearly indicate `legacy queue mode` and disable cross-domain actions that
  require registry data
- no stale or missing-row regression compared with `otto queue dashboard`

Test:
- launch the old watcher fixture, then open the new viewer and verify queue visibility,
  cancel behavior, and history fallback

#### Scenario B: new watcher + old child
Meaning:
- the upgraded watcher injects `OTTO_RUN_ID`, but a still-old atomic child does not
  consume it and continues allocating its own session id

Acceptance criteria:
- the child starts successfully because no unsupported CLI flag is injected
- the watcher records both its expected `attempt_run_id` and the child's actual
  self-allocated id in the attempt/run detail
- the operator sees a clear `child predates run-id` warning explaining why ids differ
- the task remains inspectable instead of fail-fasting on process launch

Test:
- run a fixture watcher against an intentionally old child command that allocates its own
  session id, assert the process launches successfully, and verify the queue detail
  records both ids plus the compatibility warning

#### Scenario C: new viewer + mixed history rows
Meaning:
- existing projects already contain old v1 summary rows while new v2 terminal snapshots
  are being appended

Acceptance criteria:
- the History pane reads both shapes
- duplicate rows collapse by `dedupe_key` / `run_id`
- old rows never block new snapshots from rendering

Test:
- seed fixtures with interleaved v1 and v2 history rows and verify paging, filtering, and
  selection

#### Scenario D: active upgrade during queue work
Meaning:
- a watcher restart or binary swap happens while queue tasks are already in flight

Acceptance criteria:
- in-flight tasks are rediscovered without allocating new attempt ids
- unacked commands replay cleanly after restart
- no terminal history row is duplicated

Test:
- start queue work, append a command, restart into the new watcher, and verify
  reconciliation and replay
## Delivery Model And Critical Path
Plan Gate note (9): this replaces the prior human-estimate framing with the actual
agent-driven economics of Otto work.

Observed reality:
- this codebase is implemented by AI agents under human review, not by one engineer
  hand-coding linearly
- recent work already demonstrated roughly `2-3` weeks of human-equivalent output in
  about 10 hours of agent dispatch

Realistic D-v1 dispatch estimates:
- Phase 1 substrate: `1-2` days of dispatch sessions
- Phase 2 universal viewer + adapters: `1` day
- Phase 3 mutations: `0.5-1` day
- Phase 4 history pane + editor hooks: `0.5-1` day
- Phase 5 polish: `1` day
- total: about `3-5` days of agent dispatch over `2-3` calendar days

The real cost is review density:
- Phase 1 touches every long-running Otto command: `build`, `improve`, `certify`,
  `queue`, and `merge`
- every phase should pass both Plan Gate and Implementation Gate before it is considered
  done
- real-LLM validation is the irreducible bottleneck: roughly `$5-15` and `10-30` minutes
  per cycle, with nightly `N1 + N2 + N4 + N8` after the substrate retrofit costing about
  `$10` and taking about 60 minutes
- user decisions on open questions or design changes can still stall the lane even when
  coding throughput is high

Critical path:
- Phase 1 substrate retrofit has the highest blast radius and must clear Implementation
  Gate before Phase 2 ships
- after Phase 1, run the real-LLM nightly on `N1 + N2 + N4 + N8` and require it to pass
- after that, each remaining phase is roughly one day of agent work plus `0.5-1` day of
  validation and review
## Recommended Sequence
Do not ship viewer first or polish first. Ship in this order:
1. substrate
2. universal viewer
3. mutations
4. history/editor
5. polish
That puts the highest architectural risk under test earliest.
## Risk Register
### Multi-process coherence under concurrent CLI usage from multiple shells
Risk:
- duplicate or missing live rows
- stale rows that never clear
- actions targeting the wrong process
Mitigation:
- land registry atomicity and writer ownership before building the generic UI
- use one writer per live file
- keep stale as a reader-derived overlay
- add multi-process integration tests in Phase 1
### Atomic build retrofit
Risk:
- `otto build` is the default workflow
- build/improve/certify already have subtle resume and summary semantics
Mitigation:
- add small registry adapter calls at lifecycle seams rather than threading raw dict
  writes through the pipeline
- keep checkpoint and summary logic intact
- test build, improve, and standalone certify separately before the TUI depends on them
### Migration of queue dashboard users
Risk:
- queue already has a working TUI
- regressions here will feel like Mission Control made Otto worse
Mitigation:
- keep `otto queue dashboard` as a compatibility wrapper
- preserve queue-flavored keybinds where they still make sense
- keep explicit backwards-compat tests
### State file schema evolution
Risk:
- registry records, history rows, and command files will evolve
Mitigation:
- version every new schema
- make readers additive and tolerant
- prefer append-only migration over rewrite migration
- keep fixture-based tests for old and new history rows
## Testing Strategy
Mission Control needs unit tests, multi-process integration tests, and a real-LLM nightly.
### Required gate exits
Plan Gate note (10): these are not optional hardening tasks; they are approval gates for
the rollout.

- Exit A: simultaneous registration races across atomic CLI startup and queue dispatch do
  not create duplicate ids, orphaned reservations, or split live rows
- Exit B: writer crash after command read but before ack leaves a replayable unacked
  command and converges correctly on restart
- Exit C: suspend, NTP jump, or laptop sleep does not produce false `STALE` for a healthy
  writer
- Exit D: mixed-version upgrade during active work keeps existing work visible and
  controllable within the declared compatibility envelope
- Exit E: history corruption recovery skips malformed rows, preserves appendability, and
  does not invent or overwrite domain truth
### Unit tests
Add focused tests such as:
- `tests/test_run_registry.py`
- `tests/test_run_history.py`
- `tests/test_mission_control_model.py`
Cover:
- atomic live-record writes
- run-id reservation and release
- schema parsing and additive field tolerance
- heartbeat age calculation
- stale overlay derivation
- suspend / clock-jump grace handling
- legal action calculation via adapters
- GC and tombstone append
- history append with `flock` + `fsync`
- history dedupe by `dedupe_key`
Extend queue tests to verify:
- each queue attempt produces a registry record
- queue `resume` creates a new run id but preserves task id
- cancel and remove update registry state coherently
- watcher-allocated `attempt_run_id` is injected into the child and recovered on restart
- unacked queue commands replay after a crash in the `.processing` window
Extend atomic tests to verify:
- build startup writes a live record
- checkpoint heartbeat refreshes it
- terminal summary writes one history row
- interrupted build exposes resume capability
- standalone certify exposes no resume capability
- `OTTO_RUN_ID` suppresses child-side allocation for updated children, while older
  children remain launch-compatible and allocate their own ids
Extend merge tests to verify:
- merge startup writes a live record
- merge heartbeat refreshes it
- merge failure finalizes with useful artifact paths
- merge cancel command file is consumed correctly
### Multi-process integration tests
Add tests that:
1. Spawn a standalone `otto build` in one subprocess.
2. Spawn `otto queue run --concurrent 1` plus a queued task in another subprocess.
3. Spawn `otto merge --all` or a fixture merge run in a third subprocess.
4. Poll the registry from the parent process.
5. Assert that all three runs appear within 1-2 seconds.
Also cover:
- one run finishing while others continue
- stale detection when a child process is killed
- history append after terminalization
- queue watcher restart while a command is unacked
- mixed-version upgrade fixtures during active queue work
- malformed history line recovery without truncating later good lines
The registry is the main integration seam. The TUI does not need to be the first place we
prove runtime coherence.
### TUI integration tests
Once the generic TUI exists, add focused Textual-level tests for:
- three-pane focus cycling
- selection updating the Detail pane
- log tail switching
- disabled actions showing a reason
- `otto queue dashboard` launching the mission-control app with queue filter defaults
### Real-LLM nightly scenario
One nightly scenario should exercise the actual mission-control workflow:
1. Open Mission Control in a repo fixture.
2. Launch a standalone build from another shell.
3. Enqueue two queue tasks and start the watcher.
4. Trigger a merge batch after one task completes.
5. Observe all runs in the same TUI session.
6. Cancel one live run.
7. Reopen a prior log from history.
Pass criteria:
- all runs appear
- state changes are coherent
- cancel is reflected within 2 seconds
- history rows exist for terminal runs
- existing `otto queue dashboard` keybinds still work through the compatibility path
- required gate exits A-E are green in CI before the phase is called done
## Open Questions
These are real product decisions, not gaps in the substrate recommendation.
### What should `otto` with no args do?
Options:
- keep current help behavior
- open Mission Control when attached to a TTY
- print a landing page that points to `otto dashboard`
Bias:
- do not auto-open Mission Control in D-v1
- add an explicit `otto dashboard`
- revisit no-args behavior after the TUI is stable
### Should Mission Control survive SSH disconnect?
Bias:
- document `tmux` compatibility
- do not solve persistence beyond the terminal in v1
Reason:
- daemonless Mission Control should stay honest about its lifecycle
### Do we keep `otto queue dashboard` or unify under `otto dashboard`?
Bias:
- add `otto dashboard`
- keep `otto queue dashboard` as a compatibility alias through D-v1
- decide later whether to deprecate it
### How aggressive should heartbeat polling be?
Candidate default:
- 500ms UI poll while active
- 1.5s UI poll while idle
- 2s writer heartbeat
This should be measured on real laptop repos before calling it final.
### Should merge resume stay disabled in the TUI until the CLI exists?
Yes. The only open question is whether to hide the keybind or show it as disabled with a
reason. Bias: show it disabled so the capability model is visible without overselling
merge maturity.
### Do we add a dedicated retry CLI, or reconstruct argv internally?
Bias:
- start by reconstructing the original CLI from persisted argv
- add a first-class retry CLI later only if that reconstruction proves brittle
### How much spec context belongs in Detail?
Bias:
- show path and current phase only
- open the file in `$EDITOR` for anything deeper
That keeps the "no deep spec review UI" non-goal intact.
## Recommendation
Ship D-v1 as a generic, persistent, project-scoped TUI backed by a canonical run
registry. Keep the runtime file-based and daemonless. Keep the CLI as the API. Evolve the
queue dashboard into the generic viewer instead of growing a second queue-adjacent UI.
Do not spend a release on a transient launcher TUI. That delays the hard work without
reducing it. The correct migration is to make Otto's runtime coherent first, then put the
human interface on top of that coherence.
## Plan Review

### Round 1 — Codex
- [ISSUE] Queue attempt run_id ownership not designed — fixed: explicit watcher-allocated `attempt_run_id` handshake, child injection, and crash reconciliation (see `Queue Attempt Identity`)
- [ISSUE] History helper isn't correctness-grade — fixed: new history append primitive specified with `flock` / append-only writes / `fsync` / `dedupe_key` (see `History Durability`)
- [ISSUE] Command protocol has no ack/replay contract — fixed: request log, `.processing`, ack journal, and startup replay semantics are now specified (see `Durable command protocol`)
- [ISSUE] Heartbeat/staleness too naive — fixed: `heartbeat_seq`, writer identity, suspend grace rules, and a precise `STALE` definition are now specified (see `Staleness detection`)
- [ISSUE] No source-of-truth precedence — fixed: per-domain repair order and startup repair rules are now explicit (see `Repair Precedence`)
- [ISSUE] Migration plan omits files — fixed: `otto/cli_improve.py` and `otto/config.py` were added to the primary file list and Phase 1 touched files
- [ISSUE] Universal viewer drifting toward god-object — fixed: per-domain adapter interface now owns row/detail/action specialization (see `Domain adapters`)
- [ISSUE] Backward compat hand-waved — fixed: named rollout scenarios, acceptance criteria, and tests are now specified (see `Compatibility During Rollout`)
- [ISSUE] Cost estimate used human-engineer framing — fixed: replaced with agent-dispatch estimates, review-density costs, and gate-driven critical path (see `Delivery Model And Critical Path`)
- [ISSUE] Test plan misses hard cases — fixed: required gate exits now include registration races, unacked replay, suspend false-stale, mixed-version upgrade, and history corruption recovery (see `Required gate exits`)

### Round 2 — Codex
- [IMPORTANT] Scenario B unrealistic — fixed: rewrote around env-only `OTTO_RUN_ID` propagation; Click children without `--run-id` flag remain compatible by ignoring env or allocating their own ID
