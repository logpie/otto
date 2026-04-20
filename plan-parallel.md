# Plan: Parallel otto via `queue` + `merge`

Status: **v5 FINAL — Plan Gate complete (4 rounds; final fixes per Codex round-4 surface)**
Author: yuxuan + Claude
Date: 2026-04-19

---

## 1. Context

Otto today runs as an atomic agent invocation: `otto build`, `otto improve`, `otto certify`. Each command operates in-place in the user's working tree, blocks the terminal, and produces work on a branch (improve only) or commits directly to the current branch (build).

This is fine for one task at a time. It's painful for two real workflows the user actually has:

1. **Multi-project batch runs** — pressure-tests, bench runs, weekly improve sweeps across N projects (35-project pressure test today runs sequentially or via manually backgrounded shell processes). No aggregate view, no slot cap, no resumability.
2. **Same-repo parallel feature work** — user has multiple ideas for one project; some can run concurrently, some must wait. Today serial only.

The atomic-only model has become a ceiling. This plan adds parallelism *without* the auto-decomposition trap that wrecked Devin's mergeability and GitHub Copilot Fleet's filesystem safety.

## 2. Research basis (compressed)

Six AI-coding harnesses surveyed; full notes in this conversation's transcript.

- **5 of 6 mature harnesses** chose: subprocess pool + per-job filesystem isolation + skip auto-decomposition entirely. Devin and Copilot `/fleet` bit off auto-decomposition and ship with documented footguns.
- **Symphony deep-dive** (code-grounded): 1655-line GenServer that polls Linear, claims issues into an in-memory `MapSet`, dispatches up to 10 concurrent Codex subprocesses with retry/stall handling. **Zero merge logic, zero inter-agent communication, zero LLM planner.** Workspaces are full git clones; coordination is "isolate aggressively, coordinate not at all."
- The hard problem nobody has solved well: **post-merge integration of N parallel branches**. Symphony delegates to per-agent `gh pr merge --squash`. Cursor explicitly does not auto-merge.

**Implication for otto**: build the proven pattern (in-process scheduler + worktree isolation + branch-based merge handoff) and add the one piece nobody else has: **AI-driven merge with story-level verification** that aligns with otto's existing trust model.

## 3. Design surface (final)

### 3.1 New commands

```bash
# Queue (Symphony-shaped scheduler over otto subcommands)
otto queue build "intent"           # enqueue a build (passes through to otto build)
otto queue improve --rounds 3       # enqueue an improve
otto queue certify --thorough       # enqueue a certify
otto queue ls                       # compact list
otto queue show <id>                # detailed task view
otto queue rm <id>                  # remove queued task (queues a remove command)
otto queue cancel <id>              # signal running task to stop (queues a cancel command)
otto queue run --concurrent N       # foreground watcher process (run in tmux pane)

# Merge (AI-aware, story-aware, deterministic-first)
otto merge --all                    # land all completed otto branches into target
otto merge <id-or-branch> [...]     # specific tasks/branches in given order
otto merge --resume                 # continue after manual conflict fix
otto merge --no-certify             # skip post-merge verification
otto merge --full-verify            # don't skip stories during triage
otto merge --fast                   # pure git merge, bail on conflict (NO LLM)
otto merge --target <branch>        # merge target other than default_branch
```

### 3.2 Modified atomic commands

- **`otto build`** policy fix: never modifies `default_branch` directly. If on `default_branch`, auto-creates `build/<intent-slug>-YYYY-MM-DD` and switches. If on any other branch, stays on it (mirrors improve's "stay on improve branch" pattern at `cli_improve.py:33`). `require_git()` is unchanged — building outside a git repo still hard-fails.
- **`otto build`** and **`otto improve`** gain opt-in `--in-worktree` flag. Implementation: same Python process does `os.chdir` to `<worktree_dir>/<mode>-<slug>-<date>/` (created via `git worktree add`) before running the pipeline. No subprocess re-entry, so the cli.py:32 worktree-venv-guard never re-fires.
- **`otto build/improve`** in queue context: skip `_commit_artifacts()` of `intent.md` and `otto.yaml` (see Decision §4 and Phase 2 Step 2.10) to avoid bookkeeping merge conflicts. Detected via `OTTO_INTERNAL_QUEUE_RUNNER=1` env var.
- **All atomic commands** emit a final `<project>/otto_logs/<run-type>/<run-id>/manifest.json` at successful exit, recording artifact paths (checkpoint, proof-of-work, branch, cost, duration). Queue and merge consume this — no path inference needed.

### 3.3 `otto.yaml` extension

```yaml
# existing keys preserved (provider, model, run_budget_seconds, …)
queue:
  concurrent: 3                # default --concurrent for `otto queue run`
  worktree_dir: .worktrees     # where per-task worktrees live (relative to project)
  on_watcher_restart: resume   # resume | fail | ask — what to do with in-flight tasks when watcher restarts
  cleanup_after_merge: false   # default: keep worktrees + per-task otto_logs/ for inspection. Opt-in via flag or `otto queue cleanup`. (Auto-cleanup deferred to v2 when log-archival is added — see Phase 6.4.)
  bookkeeping_files:           # files queue tasks should NOT commit to their branches
    - intent.md
    - otto.yaml
```

### 3.4 New file formats

**`<project>/.otto-queue.yml`** — task definitions, **strictly append-only from CLI** (CLI never modifies or removes existing entries; watcher never writes). Removal is represented in state.json (`status: removed`); editing a task is not supported in v1 (defer):
```yaml
schema_version: 1
tasks:
  - id: add-csv-export
    command_argv: ["build", "add CSV export with date range"]    # full argv passed to otto, verbatim
    after: []                          # optional: ids that must complete first
    resumable: true                    # whether watcher should respawn with --resume after watcher restart (false for certify; build/improve = true)
    added_at: "2026-04-19T14:32:01Z"
    # snapshot at enqueue time (immutable):
    resolved_intent: "add CSV export with date range"   # for improve, computed via _resolve_intent at enqueue
    focus: null
    target: null
    spec_file_path: null
    branch: build/add-csv-export-2026-04-19
    worktree: .worktrees/add-csv-export
```

For `otto queue improve bugs`, `command_argv` is `["improve", "bugs"]` — the actual subcommand structure that current CLI requires (`cli_improve.py:239`, `:258`). The watcher dispatches `subprocess.Popen([otto_bin] + command_argv, ...)` so any future otto subcommand structure works without queue code changes.

**`<project>/.otto-queue-state.json`** — runtime state (watcher = **sole writer**, including removals):
```json
{
  "schema_version": 1,
  "watcher": {"pid": 12345, "pgid": 12345, "started_at": "...", "heartbeat": "..."},
  "tasks": {
    "add-csv-export": {
      "status": "running",          // queued | running | done | failed | cancelled | removed
      "started_at": "...",
      "finished_at": null,
      "exit_code": null,
      "child": {                    // populated only when status=running; cleared on reap
        "pid": 23456,
        "pgid": 23456,
        "start_time_ns": 1734567890123456789,    // for PID-reuse detection
        "argv": ["otto", "build", "add CSV export with date range"],
        "cwd": "/path/to/project/.worktrees/add-csv-export"
      },
      "manifest_path": null,        // populated on successful exit (deterministic path; see Phase 1.4)
      "cost_usd": null,
      "duration_s": null,
      "failure_reason": null
    }
  }
}
```

PID-reuse safety: before any `killpg` or "is this our child still alive?" check, validate **all** of `pid + pgid + start_time_ns + argv[0] + cwd` against `psutil.Process(pid)` (or `/proc/<pid>` on Linux). Any mismatch → child is gone, treat as crashed. This is the standard hardening Codex round 2 flagged.

**`<project>/.otto-queue-commands.jsonl`** — append-only command log (CLI appends; watcher consumes; CLI never reads). All mutations to a task's lifecycle go through this log so the watcher remains the sole state.json writer:
```json
{"ts": "2026-04-19T14:32:01Z", "cmd": "cancel", "id": "add-csv-export"}
{"ts": "2026-04-19T14:33:10Z", "cmd": "remove", "id": "redesign-settings"}
```

**`<project>/.otto-queue.lock`** — exclusive flock for the watcher (advisory; second `otto queue run` refuses to start with clear error).

### Hard invariant — single-writer discipline (replicates OTP mailbox guarantee)

`.otto-queue.yml` and `.otto-queue-state.json` are written **ONLY by the watcher's main loop**. This is a non-negotiable implementation constraint, not a style preference.

- **Signal handlers** (SIGINT, SIGTERM, SIGCHLD) MUST only set flags or enqueue messages for the main loop. They MUST NOT directly read or mutate state files. This eliminates the SIGCHLD-arriving-mid-write race class entirely.
- **Subprocess reaping** happens via `os.waitpid(-1, WNOHANG)` *inside* the main-loop tick (Step 2.7), not in a SIGCHLD handler. Reap and state update are atomic by sequencing.
- **CLI commands** (add, rm, cancel) write only to `.otto-queue.yml` (append-only) and `.otto-queue-commands.jsonl` (append-only). They never touch state.json. The watcher consumes commands and applies them in its main loop.
- This replicates Elixir/OTP's GenServer-mailbox guarantee in Python: one logical writer, all state transitions sequenced through one execution context. The rest of the design (process groups, PID-reuse validation, etc.) only works correctly if this invariant holds.

Verify in code review: any direct `state.json` write from outside the main loop is a bug. Any signal handler that does anything beyond setting `self.shutdown_requested = True` (or similar) is a bug.

**`<project>/.gitattributes`** — added by `otto setup` (or first `otto queue run`) with two entries to make parallel-merge bookkeeping conflict-free via git's built-in merge drivers (replaces the rejected v2 stage-only approach):
```
intent.md merge=union
otto.yaml merge=ours
```
The `union` driver appends both sides line-by-line on merge — exactly what the cumulative intent log wants. The `ours` driver (built-in `git config merge.ours.driver true` is set automatically by Phase 1.6 setup) keeps target's otto.yaml on every merge. Net effect: bookkeeping never causes merge conflicts, with zero Python orchestration. Removes Phase 4.5's stage-only mechanism entirely.

### 3.5 Prompt files (per existing convention)

```
otto/prompts/
  merger-conflict.md       # per-conflict agent: resolve ONE conflict in current state
  merger-triage.md         # post-merge agent: emit verification plan from union of stories
```

Mode flags map to:
- default → both prompts used
- `--full-verify` → triage prompt produces only must-verify (no skip); same conflict prompt
- `--no-certify` → conflict prompt only; no triage call; no cert
- `--fast` → NEITHER prompt loaded; pure Python git merge; bail on first conflict

The merge orchestration is **Python-driven**. Agents are scoped, single-purpose. There is no "merger.md" mega-prompt — that was the v1 design and was rejected because the "don't touch clean merges" guarantee can't be enforced by prompt.

## 4. Decision log (the "why")

| Decision | Rationale | Alternatives rejected |
|---|---|---|
| **Wrapper syntax** `otto queue build "..."` (not `add build "..."`) | Norm for `nohup`/`time`/`tsp`/`sem` — prepend wrapper, write the command you'd write anyway. Zero new args to learn. | `add` subverb (clunky), inline DSL (over-engineered) |
| **File-watch foreground process, not daemon** | Runs in tmux pane like `vite dev`. No PID files, no service lifecycle. Restart-friendly via state file + manifest. | True daemon (complex), HTTP API (out of scope) |
| **Slug IDs from intent** (`add-csv-export`, not `t3`) | Self-documenting, survive deletion/reordering, support prefix-matching like git SHAs. | Sequential `t1/t2/...` (collapse under churn), UUIDs (unmemorable) |
| **`--after` only**, drop `--exclusive` and `--id` | `--after` solves a real workflow signal. Exclusivity is `--concurrent 1` for that window. Custom IDs covered by slug + optional `--as <name>`. | Full dependency DSL (over-engineered) |
| **`otto build` auto-branches only from `default_branch`** | Mirrors improve's existing "stay on improve branch" behavior. Never silently mutates main. If user is already on a feature branch, otto respects that. | Always auto-branch (would interrupt feature work); never auto-branch (keeps current footgun) |
| **Worktree only mandatory for queue / opt-in for atomic** | Atomic users have explicit "do it here" intent. `--in-worktree` does in-process `chdir`, NOT subprocess re-entry, so it bypasses the cli.py:32 venv guard naturally. | Worktree-by-default for atomic (breaking, slower startup) |
| **Single-writer state model** (watcher = sole writer of state.json AND queue.yml; CLI = strictly appends to queue.yml + commands.jsonl) | flock+rename prevents torn writes but NOT lost updates / invalid transitions. Single-writer makes state mutations linearizable without distributed consensus. queue.yml is append-only from CLI (definitions never mutate); removals tracked in state.json. | Both write with locks (race-prone), CRDT-style merge (over-engineered), CLI deletes from queue.yml directly (race with watcher reload) |
| **Full argv stored** (`command_argv: ["build", "..."]`) not parsed `command + args` | Current `otto improve` requires `bugs|feature|target` subcommand structure (cli_improve.py:239). Storing argv preserves any future CLI shape without queue logic changes. Watcher just `Popen([otto_bin] + command_argv)`. | Parse mode + args (locks queue to today's CLI shape, breaks for improve subcommands) |
| **Per-task `resumable: bool`** flag | `otto build` and `otto improve` support `--resume` via existing checkpoint mechanism; standalone `otto certify` has no `--resume` path (cli.py:617). Watcher must not blindly add `--resume` on respawn. | Always add --resume (breaks certify), never add --resume (loses recovery for build/improve) |
| **PID-reuse safety: validate pid+pgid+start_time+argv+cwd before kill/re-attach** | "Is PID alive" is not enough — kernel reuses PIDs aggressively. Storing start_time_ns + argv + cwd lets us prove the process is still our child. Without this, restart logic could `killpg` a totally unrelated process. | Trust pid alone (unsafe), psutil tree walk per check (heavy/slow) |
| **Bookkeeping handled via `.gitattributes` merge drivers** (`intent.md merge=union`, `otto.yaml merge=ours`) | Built-in git driver does the right thing without any Python orchestration. Replaces the v2 stage-only approach that Codex round 2 correctly identified as broken (git merge requires clean index). | Pre-merge prep commit (noisy commit on target), post-merge normalization commit (race window during merge sequence), Python staging (broken — git rejects dirty index) |
| **Conflict-agent scope enforced in Python, not just prompt** | After agent returns, orchestrator validates `git diff --name-only HEAD` is a subset of conflict files. SDK's `disallowed_tools` slot (otto/agent.py:77) blocks git tool calls. Two layers of defense. | Trust the prompt (Codex round 2 finding: unreliable), no validation (silent corruption) |
| **`OTTO_INTERNAL_QUEUE_RUNNER` is unset before nested child agent spawns** | The bypass should not transitively grant permission to subagents (Claude Code, certifier subprocesses). Otto explicitly scrubs the env var before forking any agent process. | Let it propagate (could bypass venv guard for nested invocations user didn't intend) |
| **Manifests at deterministic per-task path** `<project>/otto_logs/queue/<task-id>/manifest.json` when queue-spawned | Watcher must find the manifest by queue task id, not by otto's internal run id. Detected via `OTTO_QUEUE_TASK_ID` env var passed by watcher. Atomic mode keeps legacy path. | Scan all manifests for matching task-id field (slower, race-prone), watcher reads child stdout for path (fragile) |
| **`cleanup_after_merge: false`** as the default; explicit `otto queue cleanup` command + `--cleanup-on-success` opt-in | Auto-cleanup would silently invalidate the manifest's `checkpoint_path` and `proof_of_work_path` (per-task otto_logs/ live in the worktree). Until v2 adds log archival to project root, cleanup must be explicit. Also matches otto's "branch + report, human acts" trust posture. Asymmetric risk: silent log loss is worse than visible disk usage. | `cleanup_after_merge: true` default (silent log loss until v2), `cleanup_after_merge: ask` (interactive prompts in batch contexts are annoying), per-task cleanup_on_success default (same risk) |
| **Each task in its own process group; `cancel` uses `killpg`** | Otto tasks spawn agents and servers. Plain `os.kill(pid)` orphans the children. Process group ensures clean reap. | SIGKILL with manual cleanup (race-prone), psutil tree walk (heavy dep) |
| **Skip bookkeeping commits in queue mode** | Every build appends to `intent.md` and commits. Two parallel queue tasks → guaranteed conflict on bookkeeping before any product code is touched. Merge step normalizes these files explicitly. | Per-task `intent-<id>.md` (loses cumulative log), let merge resolve every time (wastes agent tokens) |
| **`OTTO_INTERNAL_QUEUE_RUNNER=1` env bypasses cli.py:32 venv guard** | The guard exists to catch user mistakes (running otto from a worktree against main repo's venv). Queue child processes are not user mistakes — they're the runner doing its job. Env var is the explicit trust signal. | Loosen guard for everyone (loses safety net), per-worktree venv installation (heavy setup) |
| **Manifest-driven artifact discovery** (every run writes `manifest.json`) | Today checkpoint paths differ across build (`otto_logs/builds/<id>/`), shared certify (`otto_logs/certifier/`), standalone certify (`otto_logs/certifier/<id>/`). Inferring breaks. Manifest is the explicit contract. | Plan path discovery (fragile), parse logs (fragile) |
| **Restart policy via `on_watcher_restart` config**, default `resume` | Per-task checkpoint exists; `resolve_resume()` clears it without `--resume`. Need explicit policy: `resume` re-spawns child with `--resume` flag; `fail` marks dead-letter; `ask` prompts in TUI. Never silently re-dispatch fresh. | Always resume (could mask bugs), always fail (loses recovery), no policy (silent corruption) |
| **`otto merge` orchestration is Python-driven** | One agent prompt cannot reliably enforce "don't touch clean merges." Python wraps `git merge`, detects clean vs dirty by exit code + index status, and only invokes the agent on actual conflicts. | One mega-prompt (unreliable), agent runs git itself (loses control) |
| **Per-conflict agent invocations + separate triage call** | Smaller scope per call → better quality + lower token cost than one giant prompt holding everything. Triage runs once at end. | One big prompt (overload), per-file agent (over-decomposed) |
| **`--fast` does NO LLM at all** | If user wants fast deterministic, they don't want any LLM cost or latency. Pure git, bail on first conflict, exit. | Light LLM for "git output parsing" (still costs $; pointless) |
| **Selective cert needs first-class certifier interface** | Triage produces `must_verify` subset; certifier today only takes `intent/mode/focus/target`. Need a `stories` parameter in the API + `{stories_section}` placeholder in prompts. | Hack: pass story names in intent string (fragile, prompt parses it), drop selective verify (loses the whole point) |
| **Project-level exclusive lock** for queue runner | Two watchers against the same project would silently double-claim and double-spawn. Symphony's "in-memory MapSet" assumes one process. We need to enforce that. | Document "don't do that" (users will), distributed lock (overkill) |
| **Bookkeeping normalization at merge** (intent.md, otto.yaml) | Even with skip-in-queue, branches from atomic mode can have these. Merge must explicitly handle: union-append intent.md from each branch, take main's otto.yaml. | Auto-resolve via git merge driver (more setup), exclude from PR (changes user expectations) |
| **No auto-merge to main as default** | Otto's existing trust model is "branch + report, human merges." Don't silently change that. `otto merge` is opt-in. | Symphony-style per-job auto-merge (wrong for solo trust model) |
| **Later-merged-wins on story collision** (with warning) | Predictable, simple, occasionally wrong but always recoverable. | Path-overlap heuristic (brittle), interactive resolution (v2) |
| **No inter-agent message bus** | Symphony validates: zero coordination + per-job isolation = correct enough. | Pubsub (race-prone), shared state file (last-writer-wins) |
| **Prompt files in `otto/prompts/`** for merger variants | Existing convention from CLAUDE.md — edit prompts without touching Python code. | Templating (premature), embed in code (violates convention) |

## 5. Implementation phases

Each phase is shippable on its own.

---

### Phase 1 — Foundations

**Goal**: `otto build` becomes safe to run without prior branch setup. Atomic commands gain opt-in worktree mode. `otto.yaml` schema is extended. Manifest contract added to all atomic commands.

#### Step 1.1 — `otto build` branch policy

- **Files**: `otto/cli.py` (build command), `otto/pipeline.py`, new `otto/branching.py` for shared slug + branch logic
- **Logic**:
  - Detect current branch via `git rev-parse --abbrev-ref HEAD`; read `default_branch` from `otto.yaml`
  - On `default_branch`: `git checkout -b build/<intent-slug>-YYYY-MM-DD` (or switch to it if exists from earlier same-day run)
  - On any other branch: log "Using current branch: <name>" and stay
  - `require_git()` unchanged — non-git repos still hard-fail
- **Verify**:
  - In existing repo on `main`: `otto build "test feature"` → new branch `build/test-feature-2026-04-19` exists, has commits, main untouched
  - In existing repo on `feature/x`: `otto build "test"` → no branch creation, commits land on `feature/x`
  - Re-running same intent same day: switches to existing branch instead of erroring
  - Outside git repo: still hard-fails with existing `require_git()` error

#### Step 1.2 — `--in-worktree` flag on `otto build` / `otto improve`

- **Files**: `otto/cli.py`, `otto/cli_improve.py`, new `otto/worktree.py`
- **Logic**:
  - When flag set: `git worktree add <worktree_dir>/<mode>-<slug>-<date> -b <branch>` (or use existing if branch already exists)
  - `os.chdir(worktree_path)` in same Python process (NO subprocess re-entry → cli.py:32 venv guard never re-fires)
  - Run the normal pipeline from the worktree
  - On success: print worktree path + branch + merge instructions
  - On failure: leave worktree intact for inspection
- **Verify**:
  - `otto build --in-worktree "feat X"` → `.worktrees/build-feat-x-2026-04-19/` exists with commits, original cwd's working tree unchanged, no venv guard error
  - Two simultaneous `otto build --in-worktree` against same project (different intents) → different branches, different worktrees, no collision
  - Same-intent collision → second invocation errors with clear "branch already checked out in worktree X"
  - Subprocess re-entry path is NOT taken (verified by tracing — same process throughout)

#### Step 1.3 — Extend `otto.yaml` schema with `queue:` section

- **Files**: `otto/config.py` (`DEFAULT_CONFIG`)
- **Logic**:
  - Add to defaults: `"queue": {"concurrent": 3, "worktree_dir": ".worktrees", "on_watcher_restart": "resume", "bookkeeping_files": ["intent.md", "otto.yaml"]}`
  - `load_config` deep-merges so existing otto.yaml without `queue:` keeps working
  - `otto setup` writes new keys to fresh otto.yaml
  - `worktree_dir` added to `.git/info/exclude` automatically (like `otto_logs/`)
- **Verify**:
  - Existing project's otto.yaml without `queue:` → loads with defaults, no error
  - `otto setup` in fresh project → otto.yaml contains `queue:` block
  - Schema-validation: garbage in `queue:` → load with defaults + warning

#### Step 1.4 — Manifest contract for all atomic commands

- **Files**: `otto/cli.py` (build), `otto/cli_improve.py`, `otto/cli.py` (certify), `otto/pipeline.py`
- **Logic**:
  - Determine manifest path:
    - If `OTTO_QUEUE_TASK_ID` env var set: write to `<project>/otto_logs/queue/<task-id>/manifest.json` (deterministic per-task path; queue watcher finds it directly)
    - Else (atomic mode): write to `<project>/otto_logs/<command>/<run-id>/manifest.json` (legacy path)
  - Manifest contents in BOTH cases:
    - `command`, `argv` (verbatim CLI argv list, including subcommand)
    - `queue_task_id` (from env var, or null for atomic mode)
    - `branch` (the branch this run produced commits on, or null)
    - `checkpoint_path` (full path to checkpoint.json — currently `otto_logs/builds/<id>/checkpoint.json` for build, etc.)
    - `proof_of_work_path` (full path to proof-of-work.json, if applicable)
    - `cost_usd`, `duration_s`, `started_at`, `finished_at`
    - `head_sha` (commit at end of run)
    - `resolved_intent`, `focus`, `target` (resolved values used by the run)
    - `exit_status: "success" | "failure"` (written even on graceful failure paths; subprocess exit code is the authority for crashes)
  - Manifest written atomically (`tempfile + rename`)
  - Existing log paths unchanged — manifest just *records* where they are
- **Verify**:
  - Atomic mode `otto build "X"`: `otto_logs/builds/<id>/manifest.json` exists with correct paths and `queue_task_id: null`
  - Queue mode (env var set) `OTTO_QUEUE_TASK_ID=foo otto build "X"`: `otto_logs/queue/foo/manifest.json` exists with `queue_task_id: "foo"`, paths correct
  - Manifest's `proof_of_work_path` matches actual location (which is `otto_logs/certifier/proof-of-work.json` for build, `otto_logs/certifier/<id>/proof-of-work.json` for standalone certify) — confirm via filesystem check
  - Failed runs that exit gracefully: manifest written with `exit_status: "failure"` so watcher can distinguish from crashes (no manifest at all)
  - Hard crashes (SIGKILL, OOM): no manifest at all → watcher treats absence as failure signal

#### Step 1.5 — `OTTO_INTERNAL_QUEUE_RUNNER` env bypass for venv guard, with scoped propagation

- **Files**: `otto/cli.py:32`, `otto/agent.py` (subprocess spawning)
- **Logic**:
  - When env var is `"1"`, skip the worktree-venv guard at cli.py:32. Document: "internal use only; set automatically by `otto queue run` when dispatching child processes."
  - **Scoped propagation**: when otto detects the env var on entry, it explicitly **unsets** it from the env passed to any child agent processes (Claude Code, certifier subprocess, etc.). Prevents transitive bypass of the venv guard for nested invocations the user didn't intend.
  - Implemented via a small `clean_env()` helper in agent.py that strips `OTTO_INTERNAL_QUEUE_RUNNER` from the env dict before any `subprocess.Popen` / SDK spawn.
- **Verify**:
  - `OTTO_INTERNAL_QUEUE_RUNNER=1 otto build "test"` from inside `.worktrees/foo/` → succeeds (no venv error)
  - Without the env var (normal user mistake) → still errors as today
  - Inside that otto build, any child agent process spawned does NOT inherit `OTTO_INTERNAL_QUEUE_RUNNER` (verified by inspecting `/proc/<child-pid>/environ` on Linux or `ps eww` on macOS)
  - Env var unset by default — no behavior change for any existing flow

#### Step 1.6 — `.gitattributes` setup for bookkeeping merge drivers

- **Files**: `otto/config.py` (`create_config` / `otto setup`), new `otto/setup_gitattributes.py`
- **Logic**:
  - On `otto setup` (or first `otto queue run` if `.gitattributes` doesn't yet contain otto entries), append:
    ```
    intent.md merge=union
    otto.yaml merge=ours
    ```
  - Run `git config merge.ours.driver true` once (built-in driver registration; idempotent)
  - If user already has a `.gitattributes` with conflicting rules (e.g., `intent.md merge=binary`): **hard-fail** `otto setup`/`otto queue run`/`otto merge` with clear error and resolution instructions. Do NOT overwrite. The whole queue+merge story depends on these drivers (Phase 4.5) — silently warning and continuing would make the merge path nondeterministic. (Codex round 3 finding.)
  - User opt-out: setting `queue.bookkeeping_files: []` in otto.yaml signals "I'll handle this myself" → setup skips the .gitattributes write AND queue/merge skip the precondition check.
  - Idempotent: re-running setup adds nothing if entries present
- **Verify**:
  - Fresh project after `otto setup`: `.gitattributes` contains both lines; `git config merge.ours.driver` returns `true`
  - Two parallel queue branches that both modify `intent.md` (e.g., from atomic-mode builds before queue mode existed): `git merge` of both produces no conflict on intent.md (union driver appends both sides); otto.yaml is unchanged from target
  - Existing `.gitattributes` with conflicting rule → setup hard-fails with resolution steps; user's file untouched
  - With `queue.bookkeeping_files: []` opt-out → setup skips the .gitattributes write; queue/merge skip the precondition check
  - Idempotency: run setup twice → file has each entry exactly once
  - Precondition check at `otto queue run` start: missing/conflicting bookkeeping rules → hard-fail (unless opt-out)

**Phase 1 ships**: branch-from-build, manifest contract, queue env bypass with scoped propagation, and `.gitattributes` bookkeeping fix. ~300 LOC + tests.

---

### Phase 2 — Queue MVP

**Goal**: User can queue tasks, run them in parallel via worktrees, see live status, cancel safely.

#### Step 2.1 — File schemas with single-writer model

- **Files**: new `otto/queue/schema.py`
- **Logic**:
  - Three files (semantics from §3.4): `.otto-queue.yml` (CLI appends, watcher reads), `.otto-queue-state.json` (watcher sole writer), `.otto-queue-commands.jsonl` (CLI appends, watcher consumes)
  - Atomic write helpers: `tempfile + os.rename` for state.json; append-with-flock for commands.jsonl and queue.yml
  - Schema-versioned mappings (not raw lists) for forward-compat
  - Validation on load: reject missing `schema_version`, reject unknown commands, log + skip malformed lines in commands.jsonl
- **Verify**:
  - Round-trip 0/1/100 task lists losslessly
  - Concurrent `otto queue build` calls → no lost or corrupted entries (stress test with 10 parallel adds)
  - State file integrity: even if watcher killed mid-write, file is either old or new (atomic rename)
  - Malformed commands.jsonl line → skipped + logged, doesn't break runner
  - schema_version mismatch → clear error with migration hint

#### Step 2.2 — Slug-based ID generation

- **Files**: `otto/queue/ids.py`
- **Logic**:
  - Slugify intent: lowercase, replace non-alphanumeric with `-`, collapse runs, trim, max 40 chars
  - No intent (e.g. `otto queue improve`) → `<mode>-<seq>` where seq is next unused number
  - **Dedupe against ALL prior IDs in queue.yml regardless of status** (queued + running + done + failed + cancelled + removed): append `-2`, `-3`, etc. **Task IDs are permanent for the lifetime of the queue file.** This prevents manifest-path collisions and state-key shadowing when a user re-enqueues an intent that matches a previously-removed task's slug. (Codex round 3 finding — id-reuse is a correctness hole.)
  - Reserved words (`ls`, `show`, `rm`, `cancel`, `run`) — refuse to prevent CLI ambiguity
- **Verify**:
  - Unit tests for slugification edge cases (empty, unicode, very long, all special chars)
  - Dedup correctness when multiple tasks share intent prefix
  - Reserved-word collision rejected with clear error

#### Step 2.3 — `otto queue <subcommand> [args...]` — enqueue (passthrough)

- **Files**: `otto/cli_queue.py` (new)
- **Logic**:
  - Click subcommands mirroring atomic commands; validate the FULL argv against the corresponding atomic command's signature (e.g., `otto queue improve bugs --rounds 3` validates against `otto improve bugs` from cli_improve.py:239 — `bugs|feature|target` is required)
  - **Store the full argv** in `command_argv` field (e.g., `["improve", "bugs", "--rounds", "3"]`). Watcher dispatches as `Popen([otto_bin] + command_argv)` so any future CLI shape works without queue logic changes.
  - **Reject `--resume` in user-supplied argv at enqueue time** with clear error: "queue manages resume automatically; do not include --resume in queued commands." The watcher is the **sole owner** of resume injection (Step 2.8) — accepting user `--resume` would create ambiguity on restart. (Codex round 3 finding.)
  - **Snapshot at enqueue time** (immutable in queue.yml): `resolved_intent`, `focus`, `target`, `spec_file_path`, `branch`, `worktree`, `resumable`. For improve: call `_resolve_intent()` now and store the result. `resumable: false` for `certify` (no `--resume` path exists today, cli.py:617); `true` for `build` and `improve`.
  - Append the entry to `.otto-queue.yml` atomically (advisory flock + tempfile + rename)
  - Print: `"Added <id> to queue."` + watcher status hint:
    - State.json shows watcher heartbeat <10s old: "Worker is running. Will pick up shortly."
    - Else: "Worker is not running. Start it with: otto queue run --concurrent <N>"
  - Optional `--as <name>` for explicit ID; optional `--after <id>` for dependency
- **Verify**:
  - `otto queue build "test"` → entry has `command_argv: ["build", "test"]`, `resolved_intent: "test"`, `resumable: true`, slug ID
  - `otto queue improve bugs` → entry has `command_argv: ["improve", "bugs"]`, snapshot resolved_intent captures project state NOW
  - `otto queue improve` (missing required subcommand) → validation error before write (matches `otto improve`'s own validation)
  - `otto queue certify` → entry has `resumable: false`
  - `otto queue improve --bogus-flag` → validation error
  - `--after non-existent-id` → reject with helpful error
  - Cyclic deps (`A after B after A`) → reject at enqueue (and re-validated on every queue.yml reload, per Step 2.7)

#### Step 2.4 — `otto queue ls` (compact)

- **Files**: `otto/cli_queue.py`
- **Logic**: rich.Table with columns: ID, STATUS, MODE, COST, DURATION, BLOCKED-ON. Color-code statuses. Reads queue.yml + state.json, joins by id.
- **Verify**: visual smoke test for 0/1/10 tasks; long IDs truncate with ellipsis; works when state.json is missing (no watcher ever ran)

#### Step 2.5 — `otto queue show <id>`

- **Logic**: full task detail: command, args, resolved_intent/focus/target snapshot, branch, worktree, manifest_path (if done), cost, PID/PGID (if running), last log line from `<worktree>/otto_logs/<id>.log`, dependency chain with statuses
- **Verify**: shows complete state for queued/running/done/failed tasks; gracefully shows partial info when manifest missing (e.g. failed task)

#### Step 2.6 — `otto queue rm <id>` and `otto queue cancel <id>`

- **Logic**: each appends a command to `.otto-queue-commands.jsonl` (`{"cmd": "remove"|"cancel", "id": ...}`). Watcher applies on next poll. **Watcher never deletes from queue.yml** — removal is purely a state.json transition (`status: removed`).
  - `rm`: appends remove command. Watcher: if task status is `running`, also issues a cancel first (kill process group), then sets status=removed. If queued/done/failed, just sets status=removed.
  - `cancel`: appends cancel command. Watcher killpg's the process group (with PID-reuse validation per §3.4) and sets status=cancelled.
  - `ls` hides tasks with `status: removed` by default; `--all` shows them.
  - Optimistic UX: if watcher heartbeat fresh, print "watcher is running, will apply within 2s"; else "no watcher; will apply when you next run otto queue run"
  - **No CLI-side state validation** beyond the snapshot read — the watcher is authoritative. CLI just records intent.
- **Verify**:
  - `rm` queued task → command logged; watcher sets status=removed; queue.yml UNCHANGED (definition preserved); ls hides it
  - `rm` running task → watcher cancels first, then marks removed; PID-reuse validation enforced before kill (test by manipulating state.json to bad start_time and confirming refusal to kill)
  - `cancel` running task → command logged; watcher killpg's the process group within 2s using validated PID/PGID/start_time/argv/cwd; marks cancelled in state.json
  - `cancel` already-done task → watcher logs "no-op" warning, no state change; CLI emits warning if state snapshot at request time showed done
  - **Race test**: enqueue task X via CLI at the same millisecond watcher processes a `remove X` command for a previously-queued X with same id (collision via slug) → watcher's command-log application order wins (FIFO); new entry remains queued, old one's removal applied to its state. No task disappears or resurrects.

#### Step 2.7 — `otto queue run --concurrent N` (the watcher)

- **Files**: `otto/queue/runner.py`
- **Logic**:
  - **Exclusive lock**: open `.otto-queue.lock`, `fcntl.flock(LOCK_EX | LOCK_NB)`. If held → exit with "another watcher is running (PID from state.json), aborting."
  - Foreground process with rich.live.Live status table
  - Read queue.yml + state.json + commands.jsonl at startup; reconcile state for any running tasks (see Step 2.8 below)
  - **Main loop** (poll every 2s):
    1. Reload queue.yml if mtime changed (detect new appended tasks, re-validate full dependency graph for cycles)
    2. Drain commands.jsonl (apply pending cancel/remove commands; uses PID-reuse validation per §3.4 for any kill)
    3. Reap finished children with `os.waitpid(-1, WNOHANG)`; for each:
       - Compute manifest path: `<project>/otto_logs/queue/<task-id>/manifest.json`
       - If exists + valid: status=done if `manifest.exit_status == "success"` AND child exit code 0; else failed (with reason)
       - If missing: status=failed (reason: "no manifest, child crashed")
       - Update state.json (clear `child` field, populate cost/duration/finished_at)
    4. For each `queued` task with all `after` deps `done` AND slot available:
       - Create worktree if needed: `git worktree add <worktree_dir>/<id> -b <branch>`
       - Spawn child:
         ```python
         child = subprocess.Popen(
             [otto_bin] + task.command_argv,
             cwd=worktree,
             env={**os.environ, "OTTO_INTERNAL_QUEUE_RUNNER": "1", "OTTO_QUEUE_TASK_ID": task.id},
             preexec_fn=os.setsid,
         )
         ```
       - Record in state.json's `child` field: `pid`, `pgid` (= pid since setsid), `start_time_ns` (`psutil.Process(pid).create_time()`), full `argv`, `cwd`. Set status=running.
    5. Update heartbeat in state.json (timestamp)
    6. Refresh status table
  - **Signal handlers**:
    - SIGINT (first): stop accepting new tasks, wait for in-flight to complete gracefully, then exit
    - SIGINT (second): `killpg(pgid, SIGTERM)` all in-flight tasks, exit
    - SIGTERM: same as second SIGINT
- **Verify**:
  - Queue 3 simple tasks, run with `--concurrent 2` → 2 dispatch immediately, 3rd waits, all finish, exit 0
  - Append a 4th task while running (write to queue.yml from another terminal) → picked up within 2s
  - Edit queue.yml to introduce a cycle while running → runner logs error and refuses new dispatches involving cycle
  - SIGINT once → in-flight finishes; SIGINT twice → killed within 5s
  - Crash a task with `kill -9` on its PID → state correctly marked failed (no manifest), watcher continues
  - Two simultaneous `otto queue run` → second exits immediately with "another watcher is running"
  - Child spawned with `OTTO_INTERNAL_QUEUE_RUNNER=1` → does NOT trigger cli.py:32 venv error

#### Step 2.8 — Watcher restart / orphan reconciliation

- **Files**: `otto/queue/runner.py`
- **Logic**: on watcher startup, before main loop:
  - For each task in state.json with `status: running`:
    - **Validate child** using ALL of `pid + pgid + start_time_ns + argv[0] + cwd` (per §3.4). If any mismatch → child is gone (PID may have been reused).
    - **Child still alive** (full validation passes): apply `on_watcher_restart` policy:
      - `resume` (default): re-attach (treat as in-flight; main loop will reap when it exits and reads its manifest)
      - `fail`: `killpg(pgid, SIGTERM)`, mark cancelled, surface in `ls`
      - `ask`: prompt user in TUI ("3 tasks were running before crash. Resume? [y/N]")
    - **Child gone**: 
      - If `task.resumable == false` (e.g., certify): mark failed with reason "watcher restart: child gone, command not resumable"
      - Else if checkpoint exists at `<worktree>/otto_logs/checkpoint.json` (single per worktree per `otto/checkpoint.py:20`, NOT per-command-per-id) AND `on_watcher_restart=resume`: re-spawn child with `--resume` appended to the original `command_argv`. The watcher is the sole injector of `--resume` (Step 2.3 rejects user-supplied `--resume`), so there is no ambiguity about whether the flag was already present.
      - Else: mark failed with reason "watcher restart: child gone, no checkpoint"
- **Verify**:
  - Kill watcher with `kill -9` mid-flight; restart → all running tasks reconciled correctly per policy
  - With `on_watcher_restart=resume`: child still alive → re-attached cleanly; build/improve child gone with checkpoint → re-spawned with --resume; child gone, no checkpoint → marked failed
  - **Certify task: child gone with checkpoint → marked failed (NOT respawned with --resume)** — Codex round 2 finding
  - PID-reuse safety: kill watcher, also kill the child, wait for OS to recycle PID to a different process (or simulate via state.json edit), restart watcher → child correctly identified as gone (start_time/argv mismatch)
  - Status table shows "[resumed]" indicator for re-attached/re-spawned tasks

#### Step 2.9 — Skip bookkeeping commits in queue mode

- **Files**: `otto/pipeline.py:1002` (`_commit_artifacts`)
- **Logic**: detect `OTTO_INTERNAL_QUEUE_RUNNER=1`; if set, skip git-add of files listed in `otto.yaml` `queue.bookkeeping_files`. The files are still updated locally (for inspection) but not committed.
- **Verify**:
  - In queue mode: branch's commits don't include `intent.md` or `otto.yaml`
  - In atomic mode (env var unset): existing behavior unchanged
  - `git log --diff-filter=A --name-only <branch>` on a queue-built branch → no `intent.md` / `otto.yaml`

**Phase 2 ships**: full parallel queue with safe restart, single-writer state, process-group cancellation, no bookkeeping conflicts. Manual merge still required. ~700 LOC + integration tests.

---

### Phase 3 — `--after` dependencies

**Goal**: User can express simple "B waits for A" sequencing.

#### Step 3.1 — Parse and store `--after` (already in 2.3); runtime check

- **Logic**: dispatchable iff all `after` ids have `status == done`. Re-validate on every queue.yml reload.
- **Verify**: queue A and B (after A); B blocks until A done; B then runs

#### Step 3.2 — Failed dep cascade

- **Logic**: if any `after` dep ends in `failed` or `cancelled`, dependent task is marked `failed` with reason `"dependency <id> failed/cancelled"` and never starts
- **Verify**: queue A (designed to fail), B (after A); A fails; B marked failed, never started; cascade propagates if C is after B

**Phase 3 ships**: dependency expression. ~80 LOC + tests.

---

### Phase 4 — `otto merge` MVP

**Goal**: Land N branches with one command. Python-driven git merge; agent invoked per-conflict; story-aware certification post-merge via separate triage agent.

#### Step 4.0 — Extend certifier API with story-subset support

- **Files**: `otto/certifier/__init__.py`, `otto/prompts/certifier.md`, `otto/prompts/certifier-thorough.md`, `otto/prompts/__init__.py` (renderer)
- **Logic**:
  - Add `stories: list[dict] | None` parameter to certifier's main entry function (each dict: `{name, description, source_branch}`)
  - Add `{stories_section}` placeholder to prompt template renderer
  - When `stories` is provided: render block listing each story explicitly + instruction "verify ONLY these stories"; otherwise: blank (existing behavior)
  - Store the input stories list in proof-of-work.json so merge reports can show what was tested
- **Verify**:
  - Calling certifier with `stories=None` → behaves identically to today (regression test)
  - Calling with `stories=[{name: "csv export", ...}]` → only that story is tested (PoW shows just that story, prompt visibly references it)
  - Both `standard` and `thorough` modes accept the parameter
  - Stories source-branch is preserved in PoW for diagnostic purposes

#### Step 4.1 — Python-driven merge orchestration loop

- **Files**: `otto/cli_merge.py` (new), `otto/merge/orchestrator.py`
- **Logic**:
  - **Resolve target branches**: from `--all` (read state.json + queue.yml; pick `done` tasks; supplement with recent `improve/*` `build/*` branches not yet on target) OR explicit ids/names; in given order
  - **Sanity checks**: target branch exists, working tree clean, target branch is checked out
  - **Save merge state**: `<project>/otto_logs/merge/<merge-id>/state.json` with `{started_at, target, target_head_before, branches: [...], current_branch_index, status}`
  - **For each branch in order**:
    1. `git merge --no-ff <branch>` (subprocess; capture stdout/stderr)
    2. Detect outcome:
       - Exit 0: clean merge → record `merged: true` in merge state, continue
       - Exit 1 + conflicts in `git status --porcelain` (`UU` lines): conflict → invoke conflict agent (Step 4.2)
       - Other failure: write merge state, exit non-zero
    3. After conflict agent returns: verify resolution (`git diff --check`, `git status --porcelain` shows no UU); commit with `git commit --no-edit`
    4. Update merge state
  - On any agent give-up or budget exhaustion: leave working tree as-is, write merge state, exit with `--resume` instructions
- **Verify**:
  - 3 non-conflicting branches → clean git merges, agent never invoked, merge state shows no agent calls
  - Branch with conflict → agent invoked exactly once for that branch, conflict resolved, commit lands
  - Forced agent failure (e.g., budget=1s) → working tree shows conflict markers, merge state written, `otto merge --resume` continues from the right index
  - `--target develop` → merges land on develop, not main
  - Verify Python orchestration actually controls git (no LLM observed running git commands itself)

#### Step 4.2 — Per-conflict agent invocation (Python-enforced scope)

- **Files**: `otto/prompts/merger-conflict.md`, `otto/merge/conflict_agent.py`
- **Logic**:
  - Prompt scope: ONE conflict at a time. Inputs:
    - Files in conflict (UU files from `git status --porcelain`)
    - Both branches' resolved intents (from queue.yml `resolved_intent` or `intent.md` for atomic-mode branches)
    - Both branches' stories (from manifest's `proof_of_work_path`)
    - The conflict diff (`git diff --merge`)
  - Task: resolve all conflict markers, preserve behaviors from BOTH branches' stories where compatible, prefer later branch on direct contradictions
  - Prompt constraints (advisory): "Do NOT modify files outside the conflict set. Do NOT run git commands."
  - **Mechanism enforcement** (Codex round 2 + 3 findings — prompt alone is unreliable; original `disallowed_tools` claim was overstated since otto/agent.py:258 is a passthrough and codex provider at agent.py:371 ignores it):
    - **Provider gate**: conflict agent runs ONLY on `provider: claude`. If `provider: codex`, fail merge with clear error: "conflict resolution requires claude provider; switch via otto.yaml or merge manually." (Codex provider can't honor disallowed_tools, and we won't ship a half-enforced guarantee.)
    - **Disallow Bash entirely** (not `Bash(git *)` patterns — the SDK doesn't filter Bash by argv): `disallowed_tools=["Bash"]`. Agent must use Edit/Write/MultiEdit to modify files. No shell escape → no git escape.
    - **Validation sequence (worktree-side BEFORE staging; orchestrator stages after validation)** — critical ordering: agent is forbidden from Bash so it cannot run `git add`. Orchestrator must stage resolved files itself. Staging happens AFTER all worktree-side checks pass, so retries on failure don't need to undo any index changes (index stays unmerged, retry only restores worktree contents). (Codex round 4 finding — v4 sequence had no staging step, success path was impossible.)
      1. Pre-agent: snapshot `pre_diff = git diff --name-only HEAD` (captures both UU files AND auto-merged files; the merge's natural diff)
      2. Pre-agent: snapshot `uu_set = files with UU status` and `pre_contents = {f: read(f) for f in uu_set}` (raw bytes WITH merge markers, for retry restoration of worktree only)
      3. Run agent (edits worktree files; cannot stage because Bash is disallowed)
      4. **Worktree-side validation** (no staging yet; index still unmerged):
         a. `post_diff = git diff --name-only HEAD`; delta = `post_diff - pre_diff` must be ⊆ `uu_set` (agent only allowed to touch already-conflicting files)
         b. `git diff --check` on worktree (catches unresolved markers and whitespace errors)
         c. `git rev-parse HEAD` matches pre-call HEAD (agent didn't commit/reset; can't have anyway since Bash blocked, but assert as defense-in-depth)
      5. **Stage resolved files**: `git add <uu_set>`
      6. **Index-side validation**: `git status --porcelain` shows no UU entries
      7. Commit: `git commit --no-edit`
    - **Retry restoration** (works because staging only happens at step 5 — if any step 4 check fails, index is still unmerged, no index undo needed):
      - On step 4 rejection: write back `pre_contents` to disk for each UU file (restores the original conflict markers in worktree); index unchanged because we never staged; restart agent with stricter prompt
      - On step 6 rejection (rare — agent left UU after edit + we staged but git still shows UU): `git reset HEAD <uu_set>` to unstage, then write back `pre_contents`, retry
      - 2 retries max; on third failure → orchestrator marks merge as failed at this branch, leaves conflict markers + unmerged index for human handling, exits with `--resume` instructions
  - Move to next branch
- **Verify**:
  - Synthetic 2-branch conflict on a single file → agent resolves, all 7 validation checks pass, merge commits
  - **Mixed merge (auto-merged files + 1 UU file)**: agent edits ONLY the UU file → path-scope check passes (auto-merged files were in pre_diff so don't count as agent additions). This is the v3 false-reject scenario being fixed.
  - Agent attempts to modify non-conflict file → delta = post_diff - pre_diff includes that file → reject; restore pre_contents (markers re-appear); retry
  - Agent attempts Bash → blocked at SDK level (disallowed_tools=["Bash"]); agent gets error in tool result, must use Edit instead
  - Agent leaves a conflict marker → `git diff --check` catches it; reject + restore + retry
  - **Codex provider**: configure `provider: codex` in otto.yaml, run `otto merge` with conflict → fails with clear "switch to claude" error before invoking agent
  - **Retry preserves conflict context**: capture pre_contents, run agent that fails validation, restore, run again → second agent invocation sees the original UU markers (not HEAD's version). Verify by reading the file between attempts.
  - Token budget per agent call enforced (default `run_budget_seconds`; max 5 agent invocations per merge run, configurable in otto.yaml)

#### Step 4.3 — Triage agent (separate call, after all merges)

- **Files**: `otto/prompts/merger-triage.md`, `otto/merge/triage_agent.py`
- **Logic**:
  - Invoked once after all branches merged (or after `--resume` resumes past last conflict)
  - Inputs: list of merged branches with their stories (from manifests), list of files changed by the merge (`git diff --name-only <target_head_before>..HEAD`)
  - Output: structured JSON `{must_verify: [...], skip_likely_safe: [...], flag_for_human: [...]}` with per-story rationale
  - Stored in `otto_logs/merge/<merge-id>/verify-plan.json`
  - **`--full-verify`**: same prompt with extra instruction "do not put anything in skip_likely_safe; only dedupe and resolve contradictions"
- **Verify**:
  - Two branches with overlapping stories → plan dedupes, picks later-wins, includes warning in flag_for_human if direct contradiction
  - Stories whose source files are NOT in the changed-files list → end up in skip_likely_safe with rationale
  - `--full-verify` → skip_likely_safe is empty
  - Plan JSON parses cleanly; orchestrator handles missing/invalid output (falls back to full union with warning)

#### Step 4.4 — Cert phase invocation with story subset

- **Logic**: pass `must_verify` stories to certifier (Step 4.0). On failure: same fix loop as `otto improve` (existing harness).
- **Verify**:
  - End-to-end: queue 2 builds, run, merge, confirm cert ran with subset (verify in cert's PoW that only those stories were tested)
  - Cert failure → fix loop kicks in → re-runs cert until pass or budget
  - `--no-certify` → cert phase skipped entirely; merge ends after triage

#### Step 4.5 — Bookkeeping (handled by `.gitattributes` from Phase 1.6, not Python)

- **Logic**: `.gitattributes` set up in Phase 1.6 (`intent.md merge=union`, `otto.yaml merge=ours`) makes git's built-in merge drivers handle these files automatically. Python orchestrator does NOT explicitly normalize them — this was the v2 stage-only mechanism that Codex round 2 correctly rejected (git merge requires a clean index).
- **Verify**:
  - 3 atomic-mode branches with intent.md commits → `git merge` of all 3 produces no conflict on intent.md (union driver appends each side's added lines); otto.yaml stays target's version
  - 3 queue branches (no intent.md commits per Phase 2.9) → no intent.md changes to merge
  - Mixed atomic + queue branches → atomic ones contribute their intent.md additions via union driver; queue ones contribute nothing
  - **Adversarial**: a branch where someone DELETED a line from intent.md → union driver still appends both sides (deletion not preserved). This is a known limitation of the union driver; document it. Acceptable because intent.md is append-only by convention.

#### Step 4.6 — `otto merge --resume` with two resume modes (HEAD verification + dirty-state handoff)

- **Files**: `otto/merge/orchestrator.py`
- **Logic**:
  - Detect `otto_logs/merge/<merge-id>/state.json`
  - Inspect working tree state to choose resume mode (Codex round 2 + 3 findings — `--fast` and `--resume` previously incompatible; v3 Mode A check was too weak):
    - **Mode A: clean tree, manual fix path** (committed resolution exists since pause):
      - Merge state stores `paused_at: {branch_being_merged: "build/foo", target_head_before_this_merge: "<sha>", branch_head_at_pause: "<sha-of-build/foo-when-paused>"}`
      - Verify HEAD is a **merge commit with two parents** matching exactly: `parent[0] == target_head_before_this_merge` AND `parent[1] == branch_head_at_pause` (the **frozen SHA** the branch pointed to when we paused, NOT a fresh `git rev-parse build/foo` — the branch may have advanced or been force-moved between pause and resume; we must validate against what was actually being merged). (Codex round 4 finding: v4's live-ref `git rev-parse` would false-fail correct manual merges if branch advanced.)
      - This proves the user actually completed THE paused merge with the exact tip we were trying to integrate, not just any commit.
      - On mismatch → refuse with "expected merge commit with parents X,Y; got HEAD Z with parents A,B"
      - Continue from next branch in the list
    - **Mode B: dirty tree with UU markers** (came from `--fast` aborting at conflict, or from agent giving up): invoke conflict agent on the current state. Same validation as Step 4.2. On success, continue with subsequent branches. This is the `--fast` → `--resume` handoff.
    - **Mode C: dirty tree without UU markers** (user partially edited but didn't finish, or unrelated dirty state): refuse with "complete your fix and commit, or run `git merge --abort` first"
  - Three modes from same `--resume` flag, dispatched purely by working tree state. No new flag required.
- **Verify**:
  - **Mode A**: simulate conflict via agent giveup → manually `git add` + `git merge --continue` (which produces a merge commit with the correct two parents) → `otto merge --resume` accepts and continues
  - **Mode A adversarial**: simulate conflict, abort the merge, commit something unrelated (clean tree, +1 commit, but parents don't match) → resume REFUSES with clear "expected parents X,Y; got A,B"
  - **Mode A: missing one parent**: user resolved with `git merge --abort` and made a fresh commit on target with no merge → HEAD has 1 parent, not 2 → refuse
  - **Mode B (the `--fast` → `--resume` handoff)**: `otto merge --fast --all` with conflicts → exits with dirty UU tree → `otto merge --resume` (no `--fast`) → agent resolves the conflict and continues with remaining branches
  - **Mode C**: user starts editing but leaves UU markers (e.g., partially fixed) → resume refuses with clear error pointing to either fixing or `git merge --abort`
  - User checked out a different branch between merge and resume → resume refuses; merge state's expected target branch doesn't match current

**Phase 4 ships**: end-to-end queue → run → merge with story-level verification, agent only invoked where it earns its keep. ~600 LOC + 2 prompt files + integration tests.

---

### Phase 5 — Merge mode variants

**Goal**: opt-in flags for users who want different merge tradeoffs.

#### Step 5.1 — `--full-verify`

- **Logic**: orchestrator passes `full_verify: true` to triage agent (Step 4.3) which adjusts its instruction set accordingly. No new prompt file needed.
- **Verify**: with `--full-verify`, verify-plan.json's `skip_likely_safe` is empty

#### Step 5.2 — `--no-certify`

- **Logic**: skip Steps 4.3 and 4.4 entirely. Print "Cert skipped per --no-certify; verify manually with `otto certify`."
- **Verify**: merge completes after last branch lands; no triage call, no cert call

#### Step 5.3 — `--fast` (NO LLM)

- **Logic**: pure git merge loop. On any conflict: abort, leave working tree dirty, exit non-zero with "use `otto merge --resume` (without --fast) to invoke agent, or fix manually with git mergetool"
- **Verify**: with `--fast` flag, conflict triggers immediate exit; no LLM tokens spent (verified by checking cost in merge report = $0); user can switch to `--resume` to bring agent in

**Phase 5 ships**: ~80 LOC; the modes mostly toggle Python branches, not new prompts.

---

### Phase 6 — Polish

#### Step 6.1 — `otto queue ls --post-merge-preview`

- **Logic**: for each `done` task, `git diff --name-only <target>...<branch>`; compute pairwise file intersections; flag tasks with overlapping files
- **Verify**: queue 2 tasks editing same file; preview reports overlap; no overlap when files disjoint

#### Step 6.2 — Integration with `otto history`

- **Logic**: `otto history --queue` shows batch runs grouped; merge runs appear with their constituent branches and verify-plan summary
- **Verify**: queue → run → merge sequence; `otto history --queue` shows the batch as a unit with cost rollup

#### Step 6.3 — Logging & observability

- **Logic**:
  - Per-task logs at `<worktree>/otto_logs/...` (existing layout, isolated by worktree)
  - Batch-level log at `<project>/otto_logs/queue/<batch-id>/queue.log` — dispatch decisions, retries, cancels
  - Merge log at `<project>/otto_logs/merge/<merge-id>/` — agent transcripts, verify plan, cert results, state.json
- **Verify**: per-task logs isolated; batch log captures every decision; nothing leaks into agent prompts (verified via grep on agent transcripts and `git show HEAD`)

#### Step 6.4 — Explicit worktree cleanup (`otto queue cleanup` + `otto merge --cleanup-on-success`)

- **Files**: `otto/cli_queue.py`, `otto/cli_merge.py`
- **Logic**:
  - **`otto queue cleanup [--done | --all | <id>...]`** — explicit user-invoked removal:
    - `--done` (default): remove worktrees for tasks with `status: done` only
    - `--all`: also include `failed`, `cancelled`, `removed` tasks
    - `<id>...`: specific task ids
    - For each: `git worktree remove <path>` (refuse if dirty unless `--force`); update state.json `worktree_cleaned_at` field
    - Print summary: "Removed N worktrees, freed X MB"
    - **Does NOT delete the branch** (user can still `git checkout <branch>` or re-create the worktree)
  - **`otto merge --cleanup-on-success`** — when set, after a successful merge run, the orchestrator runs the equivalent of `otto queue cleanup --done` for the tasks that were just merged (only those, not the whole queue).
  - Both honor `cleanup_after_merge: true` from otto.yaml as an alias for "always pass `--cleanup-on-success` to merge."
- **Verify**:
  - `otto queue cleanup --done`: removes worktrees only for done tasks; running/queued/failed/cancelled untouched; branches preserved (verifiable via `git branch -a`); manifest preserved at `<project>/otto_logs/queue/<task-id>/`
  - Dirty worktree → refuse without `--force`; print path + uncommitted files
  - `otto merge --all --cleanup-on-success`: after merge, only the just-merged tasks' worktrees are removed; other queue tasks' worktrees untouched
  - `cleanup_after_merge: true` in otto.yaml + `otto merge --all` → behaves as if `--cleanup-on-success` was passed
  - **Audit trail intact after cleanup**: the manifest still exists at `<project>/otto_logs/queue/<task-id>/manifest.json`. NOTE for v2: `checkpoint_path` and `proof_of_work_path` in the manifest become 404s after cleanup. This is documented behavior until v2 adds log archival.

**Phase 6 ships**: ~250 LOC (200 for logging/observability + 50 for cleanup).

---

## 6. Open questions remaining (post-v5)

**None.** All design questions resolved.

The one remaining product question — worktree cleanup default — is now decided: **`cleanup_after_merge: false`** with explicit `otto queue cleanup` command and `--cleanup-on-success` opt-in flag (Step 6.4). Rationale in §4 decision log: silent log loss is worse than visible disk usage; v2 will revisit when log archival is added.

## 7. Out of scope (explicit)

To keep the implementation focused — these are NOT in this plan:

- **Auto-merge to main as default** — opt-in only
- **Remote/server queue** (multi-machine) — defer to v2 if demand emerges
- **Web dashboard** — terminal table is enough for v1
- **Story TTL / cumulative spec** — let union grow; mitigate via triage
- **`--exclusive` and `--id` queue flags** — covered by `--concurrent 1` and slug + `--as`
- **Auto-decomposition** of single intents into sub-tasks — Symphony's lesson
- **Cross-task semantic dedup** at land time (e.g. "two branches added similar utils, pick one") — v2
- **Per-task branch protection / required-reviews integration** — out of scope for local-merge v1
- **AI-assisted bookkeeping merging** — bookkeeping is normalized algorithmically (Step 4.5), no agent

## 8. Verify (overall acceptance)

- All existing tests pass
- New tests cover: queue file r/w + concurrency, ID slugging + dedup, dependency resolution, worktree isolation, single-writer state model, process-group cancel, manifest contract, watcher restart policies, merge orchestration ladder, conflict agent invocation, triage agent, story-subset cert, bookkeeping normalization
- Manual smoke test: in a real test repo, queue 3 builds with intentional file overlap, `otto queue run --concurrent 2`, watch them complete, `otto merge --all` resolves conflicts (or surfaces clearly), cert reports per-story status, all on `main`
- **Specific anti-regressions** (from Codex round 1):
  - `otto queue run` dispatching into `.worktrees/...` succeeds (no cli.py:32 venv error)
  - After killing watcher mid-task and restarting: task resumed with `--resume` if checkpoint exists, else marked failed; never silently re-run from scratch
  - Merge can recover each task's resolved intent, cost, and stories AFTER the worktree has been removed (manifests preserved at `<project>/otto_logs/queue/<task-id>/`, not in worktree)
  - `otto merge --resume` records and checks expected HEAD; refuses if user diverged
  - Two unrelated queued tasks do NOT create merge conflicts in `intent.md`/`otto.yaml` (handled by `.gitattributes` merge drivers)
- **Specific anti-regressions** (from Codex round 2):
  - **Race test**: enqueue task X via CLI at the same moment watcher processes a `remove X` command → no task disappears or resurrects (FIFO command-log application is authoritative)
  - **Restart test for `otto queue certify`**: kill watcher mid-certify, restart → task is marked failed (NOT respawned with `--resume`, which doesn't exist for certify)
  - **`--fast` → `--resume` handoff**: `otto merge --fast` exits with dirty UU tree → `otto merge --resume` invokes agent on the dirty state and continues with subsequent branches
  - **Adversarial conflict-agent escape**: agent attempts to (a) edit a non-conflict file, (b) run a git command, (c) commit/reset HEAD → all three blocked or rejected by Python validation; merge marked failed after 2 retries
  - **PID-reuse safety** (synthetic, deterministic): edit state.json's `child.start_time_ns` to a value that doesn't match the actual process; restart watcher → child correctly identified as gone via start_time mismatch; no killpg attempted (Codex round 4: actual PID recycling is non-deterministic; mock via state edit instead)
  - **Env scoping** (test via spawn-helper mock): unit-test the env dict passed to `subprocess.Popen` for child spawn; assert `OTTO_INTERNAL_QUEUE_RUNNER` is absent from the env when otto detects it on entry and forks an agent process (Codex round 4: `/proc` introspection is platform-dependent)
- **Specific anti-regressions** (from Codex round 3):
  - **Permanent task ID identity**: enqueue `add-csv-export`, run it, `rm` it, try to enqueue same intent again → second enqueue gets `add-csv-export-2`; old manifest at `otto_logs/queue/add-csv-export/manifest.json` preserved untouched; new manifest at `add-csv-export-2/`. State.json keys never collide.
  - **`--resume` rejection at enqueue**: `otto queue build "..." --resume` (or any subcommand with --resume in argv) → CLI rejects with clear error before write
  - **Mixed merge auto-merge + conflict**: branch with 5 cleanly-mergeable files + 1 conflicting file → conflict agent edits only the 1 conflicting file; v3-style false reject does NOT trigger (path-scope check uses delta-since-pre-snapshot, not absolute diff)
  - **Retry preserves conflict markers**: forced-fail conflict agent twice → second invocation reads files with original UU markers intact (verified by snapshot comparison between attempts)
  - **Codex provider gate**: configure `provider: codex`, run `otto merge` with conflicting branches → fails with "switch to claude provider" error before any agent invocation
  - **Mode A parent-SHA validation**: simulate conflict pause; on resume, commit something unrelated (clean tree, parents don't match expected) → refuse with parent-SHA mismatch error
  - **`.gitattributes` precondition hard-fail**: delete the `merge=union` line from `.gitattributes` → next `otto queue run` and `otto merge` BOTH refuse to start with clear "missing required merge driver" error pointing to `otto setup`
  - **Bookkeeping opt-out**: set `queue.bookkeeping_files: []` in otto.yaml → `otto setup` skips .gitattributes write; queue/merge skip precondition check; user can manage bookkeeping themselves
- No otto runtime files (`otto_logs/`, `.worktrees/`, `.otto-queue*`) leak into agent prompts or git commits (per CLAUDE.md, verified via grep on agent transcripts and `git show HEAD`)

## 9. Estimated scope

- Phase 1: ~250 LOC + tests (foundation: branch policy, --in-worktree, schema, manifest, env bypass)
- Phase 2: ~700 LOC + integration tests (queue runner, single-writer state, process groups, restart policy)
- Phase 3: ~80 LOC + tests
- Phase 4: ~600 LOC + 2 prompt files + integration tests (Python orchestration, conflict agent, triage agent, certifier extension, bookkeeping normalization)
- Phase 5: ~80 LOC + tests
- Phase 6: ~200 LOC + tests

Total: ~1900 LOC + 2 new prompt files + extended certifier interface + ~40 new tests. ~3 weeks sequential; phases 4-6 parallelize after 1-3 land.

## 10. Migration / rollout

- All new commands additive — no breaking changes to existing `otto build/improve/certify` invocations
- **One behavior change** (Phase 1.1): `otto build` no longer commits to default_branch. Users who relied on this should: switch to a feature branch first, OR use the same workflow they use today which already required a branch.
- **One API change** (Phase 4.0): certifier function gains `stories` parameter. Default `None` preserves existing behavior. Callsites in bench scripts may need none/no-op update.
- Feature releases phase-by-phase; each phase's user-facing surface is independently usable
- No database migration; otto.yaml additions are deep-merged with defaults

## 11. Adversarial review trail (Plan Gate)

### Round 1 — Codex (read-only review of v1)

11 substantive findings. All accepted; v2 addresses each:

- **[CRITICAL]** Worktree venv guard at `cli.py:32` blocks queue subprocesses → fixed via `OTTO_INTERNAL_QUEUE_RUNNER=1` env bypass (Phase 1.5) + atomic mode uses in-process `chdir` not subprocess
- **[CRITICAL]** Wrong log paths (`runs/<id>/` doesn't exist) → fixed via manifest contract (Phase 1.4); merge consumes manifests not inferred paths
- **[CRITICAL]** Selective certifier interface doesn't exist → added Phase 4.0 to extend certifier API + prompt template with `stories` parameter
- **[IMPORTANT]** State model race-prone with two writers → fixed via single-writer model: watcher = sole writer of state.json; CLI = appends to commands.jsonl (§3.4, Phase 2.1, 2.6)
- **[IMPORTANT]** Branch policy collides with `require_git()` and improve's "stay on improve branch" pattern → fixed via explicit policy: only auto-branch from default_branch, mirror improve's behavior elsewhere (§3.2, Phase 1.1)
- **[IMPORTANT]** Improve resolves intent dynamically; queue must snapshot → fixed via dispatch-time snapshot in QueueTask (§3.4 task fields, Phase 2.3)
- **[IMPORTANT]** Bookkeeping (intent.md, otto.yaml) causes parallel merge conflicts → fixed via Phase 2.9 (skip commits in queue mode) + Phase 4.5 (normalize at merge)
- **[IMPORTANT]** Restart/resume not designed → fixed via explicit `on_watcher_restart` policy + Phase 2.8 (orphan reconciliation)
- **[IMPORTANT]** Cancel via `os.kill(pid)` doesn't reap subagents → fixed via process groups (`os.setsid` + `killpg`), Phase 2.7
- **[IMPORTANT]** Merge agent overloaded; `--fast` shouldn't use LLM → fixed via Python-driven orchestration + per-conflict agent + separate triage call + `--fast` is pure git (Phase 4.1-4.3, 5.3)
- **[MINOR]** Queue file format inconsistency (top-level list vs schema_version) → fixed: wrapped in mapping with `schema_version` + `tasks` (§3.4)

Plus open-question feedback addressed:
- Watcher exclusivity made mandatory (Phase 2.7 lock)
- Cycle re-validation on every reload (Phase 2.7)
- Manifest survival after worktree cleanup (Phase 1.4: manifests live under project's `otto_logs/`, not in worktree)
- Process-group cancel safety (Phase 2.6, 2.7)

### Round 2 — Codex (review of v2)

7 new findings. All accepted; v3 addresses each:

- **[CRITICAL]** Single-writer model broken by `rm` (watcher writes queue.yml) → fixed: queue.yml is strictly append-only from CLI; removal is purely a state.json transition (`status: removed`) (§3.4, Step 2.6)
- **[CRITICAL]** Manifest discoverability across worktree boundary — watcher couldn't find manifests by queue task id → fixed: `OTTO_QUEUE_TASK_ID` env var contract; manifests at deterministic `<project>/otto_logs/queue/<task-id>/manifest.json` (Step 1.4)
- **[CRITICAL]** CLI shape mismatch (`otto queue improve --rounds 3` doesn't match `otto improve bugs|feature|target`) AND certify has no `--resume` → fixed: store full `command_argv` not parsed mode+args; per-task `resumable: bool` flag false for certify (§3.4, Step 2.3, Step 2.8)
- **[IMPORTANT]** PID-reuse unsafe (trusting "PID alive + exe is otto" can hit unrelated processes) → fixed: validate pid+pgid+start_time_ns+argv+cwd before any kill or re-attach (§3.4)
- **[IMPORTANT]** Stage-only bookkeeping normalization can't survive `git merge` (dirty index rejected) → fixed: replaced with `.gitattributes` `merge=union` for intent.md and `merge=ours` for otto.yaml; built-in git driver handles it; zero Python orchestration (Step 1.6, Step 4.5)
- **[IMPORTANT]** `--fast` and `--resume` previously incompatible (--fast leaves dirty tree, --resume requires committed state) → fixed: --resume now has two modes dispatched by working-tree state (clean = post-fix continue; dirty UU = agent takes over) (Step 4.6)
- **[IMPORTANT]** Conflict-agent scope only enforced by prompt → fixed: SDK `disallowed_tools` blocks git tool calls; orchestrator validates `git diff --name-only HEAD` is subset of conflict files; HEAD invariance check post-call; rollback + retry on violation (Step 4.2)

Open-question feedback:
- v2 #1 (manifest atomicity) → reframed; root cause was discoverability, fixed in Step 1.4
- v2 #2 (NFS lock) → not a v1 blocker; documented as unsupported
- v2 #3 (triage retry) → decided: 2 retries → fall back to full union
- v2 #4 (stage-only) → not open; replaced with .gitattributes
- v2 #5 (worktree cleanup) → product decision; default `cleanup_after_merge: true`, opt-out flag
- v2 #6 (cost lag) → acceptable for v1; documented
- v2 #7 (env leakage) → real, fixed in Step 1.5 (scoped propagation)
- v2 #8 (certifier callsite) → trivial; grep + add optional param

### Round 3 — Codex (review of v3)

7 new findings (Codex respected the no-re-raise rule). All accepted; v4 addresses each:

- **[CRITICAL]** Task ID reuse correctness hole — re-enqueuing a removed task's slug would shadow old manifest/state → fixed: dedup against ALL prior IDs regardless of status (Step 2.2). Task IDs are permanent for the lifetime of queue.yml. Old test for "race on same id" removed; new test for "permanent identity preserved across rm + re-enqueue."
- **[CRITICAL]** Restart checkpoint path was still wrong (`<worktree>/otto_logs/<command>/<id>/checkpoint.json` doesn't exist; actual path per checkpoint.py:20 is `otto_logs/checkpoint.json` — single per worktree) AND queue should reject user `--resume` to avoid double-injection on restart → fixed: corrected path in Step 2.8; Step 2.3 rejects user-supplied `--resume` at enqueue
- **[CRITICAL]** `disallowed_tools` claim was overstated — agent.py:258 is a passthrough; agent.py:371 (codex) ignores it entirely; SDK doesn't filter Bash by argv pattern → fixed: provider gate (claude only); disallow Bash entirely (force Edit/Write); Codex provider explicitly rejected with clear error (Step 4.2)
- **[IMPORTANT]** `.gitattributes` "warn and continue" too weak when Phase 4 depends on it → fixed: hard-fail at `otto setup`/`queue run`/`merge` if conflicting rules; explicit opt-out via `queue.bookkeeping_files: []` (Step 1.6)
- **[IMPORTANT]** Post-agent file-scope validation false-rejected mixed merges (auto-merged + conflict files) → fixed: snapshot pre_diff (includes auto-merged), validate delta = post_diff - pre_diff ⊆ uu_set (Step 4.2)
- **[IMPORTANT]** `git checkout HEAD -- <conflict files>` destroys conflict markers, breaking retry → fixed: snapshot raw `pre_contents` of UU files before agent call; restore from snapshot on retry (Step 4.2)
- **[IMPORTANT]** Mode A "+1 commit" check too weak — user could commit anything → fixed: validate HEAD is a merge commit with parent[0] = expected target_head_before_merge AND parent[1] = expected branch tip (Step 4.6)

§6 trimmed to 1 genuinely-open product question (worktree cleanup default).

### Round 4 — Codex (review of v4; final round per 4-round max)

3 substantive findings + 2 verification-test downgrades. Codex declined to open round 5 ("would not open another review round for broad redesign") — these are surfaced as final fixes for v5.

All accepted; v5 addresses each:

- **[CRITICAL]** Conflict resolution success path was impossible — agent can't run `git add` (Bash blocked) so UU markers in the index never clear; v4 sequence had no orchestrator-side staging step → fixed: orchestrator stages after worktree-side validation passes (Step 4.2 sequence: agent edits → validate worktree → `git add <uu_set>` → check no UU in index → commit). Retry restoration unchanged because staging only happens at step 5 — failures at steps 1-4 leave index unmerged, retry only restores worktree contents.
- **[IMPORTANT]** Mode A keyed off live branch ref, not paused tip — if branch advanced between pause and resume, valid manual merge would false-fail validation → fixed: merge state stores `branch_head_at_pause` as a frozen SHA; Mode A compares HEAD's parent[1] against that snapshot, not against live `git rev-parse <branch>` (Step 4.6).
- **[IMPORTANT]** Retry restoration would need index restoration after fix #1 → resolved by ordering: staging only happens after all worktree validations pass (step 5), so any pre-stage rejection leaves index untouched. The earlier concern was about a hypothetical sequence where staging happened before validation; v5 stages after, so worktree restoration alone is sufficient for the common case. Step 6 rejection (rare) explicitly handles index reset.

Verification gap downgrades (per Codex):
- PID-recycling test → synthetic state.json edit, not actual recycling (deterministic, CI-friendly)
- Env-scoping test → mock the env dict at spawn helper, not `/proc` introspection (platform-independent)

### Plan Gate complete

4 rounds. v1 → v5 covers ~30 substantive fixes from Codex. The plan is implementation-ready modulo the 1 product question in §6 (worktree cleanup default) which is a UX choice for the user, not a correctness blocker.

Round-by-round defect count:
- Round 1: 11 issues (5 critical, 5 important, 1 minor)
- Round 2: 7 issues (3 critical, 4 important)
- Round 3: 7 issues (3 critical, 4 important)
- Round 4: 3 issues (1 critical, 2 important) + 2 test-spec polish

Convergent: defect count fell each round, severity reduced, scope narrowed. APPROVED for implementation pending user sign-off on §6 question.

---

*End of plan v5. Implementation may begin.*
