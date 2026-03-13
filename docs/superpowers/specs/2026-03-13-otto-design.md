# Otto — Autonomous Claude Code Agent Runner

## Overview

Otto is a CLI tool that runs autonomous Claude Code agents against a task queue with tiered verification. It processes tasks serially, commits on success, reverts on failure, and ensures the main branch never regresses.

**Core insight:** Combine autoresearch's ratchet pattern (branch only moves forward) with adversarial integration test generation (a separate Claude invocation writes tests the agent can't see or game).

**Migration note:** This is a ground-up rewrite. The v2 system (tasks.json, FastAPI dashboard, worker.py) is replaced entirely. No backward compatibility layer.

**Platform:** macOS and Linux only. Uses `flock` and `os.setpgrp` (POSIX).

---

## Design Decisions

### CLI-first, no web UI
The value is the autonomous loop, not a dashboard. CLI composes with cron, GitHub Actions, SSH, and scripts. A web UI can be added later without redesigning the core.

### Serial execution
Parallel agents introduce merge conflicts, resource contention, and coordination complexity (Symphony needs an entire Elixir/BEAM runtime for this). Serial with branch-per-task is simple, reliable, and still fully autonomous — you throw a list of tasks at it and walk away.

### Branch-per-task
Each task gets a stable key (12-char hex from uuid4, e.g., `a1b2c3d4e5f6`) assigned at creation, checked for uniqueness against existing tasks. Used for branch names (`otto/a1b2c3d4e5f6`), log directories, and testgen artifacts. The numeric `id` field is display-only for human convenience. Keys survive import/reset without collision. On success, merge to default branch (fast-forward). On failure, delete the branch. The default branch never has broken code.

### Session resume on retries
The agent sees what went wrong on previous attempts via `--resume <session_id>`. Strictly more information than starting fresh. Prevents repeating the same mistake.

### Tiered verification
Not all tasks have hand-written verification. The system adapts:
1. Existing tests (free, instant)
2. Generated integration tests (adversarial, persistent)
3. Custom verify command (optional)

### Generated integration tests, not rubrics
LLM-as-judge is vibes. Generated pytest/jest tests are deterministic, runnable, and persist as part of the codebase — compound value over time.

---

## Core Loop

Otto acquires a process-level lock (`otto.lock` via flock) before entering the loop. If another `otto run` is already active, exit with error "another otto process is running." This prevents two runners from picking up the same task. The lock uses `flock` (automatically released on process exit, including SIGKILL/crash) — no stale lock file issue.

For each pending task:

```
0. Baseline check (once, before processing any tasks):
   Run the existing test suite on the default branch. If it fails, abort with
   "baseline tests failing — fix before running otto." This prevents attributing
   pre-existing failures to the agent.

1. Preflight: require clean working tree (no uncommitted changes). Abort if dirty.

2. Create branch otto/<key> from current default branch HEAD.
   If branch already exists: check if it was preserved from a "main diverged" failure.
   If so, refuse to overwrite — user must manually resolve or `otto reset` first.
   If it's from an interrupted run (status != failed with diverge error), delete and recreate.

3. Start generating integration tests concurrently (separate claude -p).
   Testgen captures a file tree snapshot via `git ls-files` BEFORE the agent starts.
   Testgen writes the generated test file to .git/otto/testgen/<task-id>/ (outside working tree).
   The file is copied into the project test directory only at verification time (step 6b).
   This keeps generated tests invisible to the agent on ALL attempts, including retries.

4. Run agent via claude-agent-sdk:
   - Prompt: task prompt + "You are working in <project_dir>. Do NOT create git commits."
   - Working directory: project root (on the task branch)
   - Tools: all (dangerously-skip-permissions — isolated branch is the safety net)
   - Model: from otto.yaml or task-level override
   The agent must NOT commit. Otto owns all commits. If the agent creates commits,
   otto squashes them into a single otto-authored commit at step 7.

5. Await testgen if still running (block until complete or timeout at 120s).
   If testgen times out or fails, skip Tier 2 — log warning, continue with Tiers 1 and 3.

6. Run tiered verification in a DISPOSABLE WORKTREE:
   a. Create a temporary worktree from the current task branch HEAD.
   b. Copy generated test from .git/otto/testgen/ into the temp worktree.
   c. Run Tier 1 (existing tests) in temp worktree.
   d. Run Tier 2 (generated tests) in temp worktree.
   e. Run Tier 3 (custom verify command) in temp worktree.
   f. Remove the temp worktree regardless of pass/fail.
   This completely isolates verification side effects from the agent's working tree.
   The agent never sees the generated test file, even on retries.

7. All pass → prepare final commit:
   a. Copy generated test file from .git/otto/testgen/ into the project test directory
      (on the task branch).
   b. If the agent created commits, `git reset --mixed <base_sha>` to unstage everything,
      then explicitly `git add` intended files + generated test (not `git add -A`).
      If the agent did not commit, just `git add` the changed files + generated test.
   c. Commit with message "otto: <first 60 chars of task prompt> (#<id>)".
   d. Merge to default branch (fast-forward), delete branch, next task.
   If fast-forward fails (default branch diverged), preserve the branch and mark task
   as `failed` with error "branch diverged — otto/<key> preserved, manual rebase needed."

8. Any fail → feed verification output (stdout/stderr only, NOT the test source code)
   to agent via session resume, retry (up to max_retries).
   Resume uses the MOST RECENT session_id (updated after each attempt in tasks.yaml).
   Each resume builds on the previous attempt's context.
   The resumed prompt includes:
   "Verification failed. <tier name> output: <stderr/stdout>. Fix the issue."

9. All retries exhausted → git reset --hard, git checkout default branch,
   delete branch, log failure, next task.
```

### Default branch detection

Otto auto-detects the default branch at init (`git symbolic-ref refs/remotes/origin/HEAD` or fallback to `main`/`master`). Stored in `otto.yaml` as `default_branch`.

### One-off mode

`otto run "prompt"` creates an ephemeral task (not written to tasks.yaml), runs the same loop on a temporary branch `otto/adhoc-<timestamp>-<pid>` (PID prevents same-second collisions), and cleans up on completion. Logs go to `otto_logs/adhoc-<timestamp>-<pid>/`.

### Signal handling

On SIGINT/SIGTERM, otto catches the signal, kills the entire process group (agent, testgen, and any verification subprocesses), runs `git reset --hard && git checkout <default_branch>`, and deletes the task branch. The task is marked `failed` with error "interrupted". All subprocesses are started in their own process group (`os.setpgrp`) so they can be killed together.

---

## Task Lifecycle

### States

```
pending → running → passed | failed | skipped
```

Retries happen within `running`. No separate retry state.

### Task file (`tasks.yaml`)

```yaml
tasks:
  - id: 1
    key: a1b2c3d4
    prompt: "Add JWT authentication to the API"
    status: pending

  - id: 2
    key: e5f6a7b8
    prompt: "Fix memory leak in cache.py"
    verify: "python benchmark.py --check-memory"
    status: pending

  - id: 3
    key: c9d0e1f2
    prompt: "Refactor database layer to use connection pooling"
    max_retries: 5
    status: pending
```

`key` is auto-generated on task creation. Branch name: `otto/<key>`. Log dir: `otto_logs/<key>/`. Testgen dir: `.git/otto/testgen/<key>/`.

Minimal required field: `prompt`. Everything else has defaults from `otto.yaml` or is auto-derived.

Runtime state (`status`, `attempts`, `error`, `session_id`) is written back to the task file as the system runs. No separate state database.

**Git hygiene:** `tasks.yaml` and `otto_logs/` are runtime-only — added to `.git/info/exclude` by `otto init`. `otto.yaml` is project config and SHOULD be committed (shareable across clones, CI, team members). `.git/otto/` is inherently invisible to git. Step 7 stages files explicitly (never `git add -A`).

**Init and run:** `otto init` creates `otto.yaml` (intended to be committed) and updates `.git/info/exclude` for runtime files. After init, the user should commit `otto.yaml` before running `otto run` (which requires a clean tree).

### Project config (`otto.yaml`)

```yaml
test_command: pytest
max_retries: 3
model: sonnet
project_dir: .
default_branch: main          # auto-detected by otto init
verify_timeout: 300            # seconds per verification tier
```

Created by `otto init`, which auto-detects `test_command` from the project.

---

## Tiered Verification

Three tiers, run in order. First failure stops the chain.

### Tier 1: Existing test suite

- Configured in `otto.yaml` `test_command` (preferred), or auto-detected from project.
- Detection order: `pytest` → `npm test` → `go test ./...` → `cargo test` → `make test`.
- If auto-detection is ambiguous (multiple candidates), warn and require explicit config.
- Runs the full suite. Catches regressions.
- Skipped with warning if no test command found or configured.

### Tier 2: Generated integration tests

- A separate `claude -p` call generates tests from the task prompt.
- Prompt instructs: behavioral/integration assertions using real dependencies where available.
  Mocks/fakes are allowed only when the project already provides them (e.g., test fixtures, local test servers).
  No external network calls unless explicitly allowlisted. Tests must be deterministic and hermetic.
- Tests written to a file in the project's test directory, using the project's test framework.
  Detection: pytest → `tests/otto_verify_<key>.py`, jest → `__tests__/otto_verify_<key>.test.js`, go → `otto_verify_<key>_test.go`.
- Testgen prompt receives: task prompt, file tree listing (via `git ls-files`), and the project's test framework.
  It does NOT receive file contents or the agent's changes — only structure.
  Uses `git ls-files` (not `find`) to exclude .git, build artifacts, secrets, and vendored deps.
- Generated concurrently with the agent's first attempt.
  If testgen completes before the agent, tests are ready immediately.
  If the agent completes first, runner awaits testgen (up to 120s timeout, then skips Tier 2).
- Persisted in the repo — on task success, they merge with the task branch and become regression tests.
  On task failure, they are deleted with the branch.
- On retry, existing generated tests are reused (not regenerated).
- Adversarial by construction: testgen runs from a snapshot of the project BEFORE the agent starts, so it writes tests based on the spec (task prompt), not the implementation.

### Tier 3: Custom verify command

- Optional `verify:` field in the task definition.
- Runs as a bash command, exit 0 = pass.
- For things tests can't easily cover (performance benchmarks, visual checks, etc.).

### Failure feedback

On any verification failure, the output (which tier failed, stderr, assertion errors) is fed back to the agent via session resume. The agent sees exactly what broke and can act on it.

---

## Module Structure

```
otto/
  __init__.py
  cli.py        # Entrypoint: otto add, otto run, otto status, otto init, otto logs
  config.py     # Load/create otto.yaml, auto-detect test command
  tasks.py      # Read/write tasks.yaml, state transitions, ID generation
  runner.py     # Core loop: branch → agent → verify → merge/revert
  verify.py     # Tiered verification: existing tests, generated tests, custom
  testgen.py    # Generate integration tests via claude -p
```

### Dependency flow (no cycles)

```
cli → runner → verify → testgen
         ↓        ↓
       tasks    config
```

### Module responsibilities

- **cli.py** — Thin layer. Parses args, calls into `runner` and `tasks`. No business logic.
- **config.py** — Loads `otto.yaml`, provides defaults, auto-detects test command. Pure data.
- **tasks.py** — CRUD on `tasks.yaml` with file locking. State transitions (pending → running → passed/failed). ID generation. No agent logic.
- **runner.py** — The core loop. Owns git branch management (create, merge, delete). Calls `verify.run()`. Handles retries with session resume. ~300 lines.
- **verify.py** — Runs the three tiers in order. Returns structured pass/fail with error details per tier.
- **testgen.py** — Single responsibility: takes a task prompt + project dir, returns a test file path. Uses `claude -p` with a test generation prompt.

Each file targets ~200 lines, `runner.py` ~300 max.

---

## CLI Interface

```bash
# Initialize project config
otto init                          # Creates otto.yaml with auto-detected settings

# Add tasks
otto add "Add JWT auth"            # Appends to tasks.yaml, auto-generates ID
otto add -f other_tasks.yaml       # Import tasks from a file (new IDs auto-assigned, ignoring any IDs in source)

# Run
otto run                           # Run all pending tasks in tasks.yaml
otto run "Fix the bug in auth.py"  # One-off task, no tasks.yaml needed
otto run --dry-run                 # List pending tasks, detected test command, config — no execution

# Monitor
otto status                        # Table of tasks with status, attempts, timing
otto logs <task-id>                # Show agent + verification logs for a task

# Manage
otto retry <task-id>               # Reset a failed task to pending, clear session_id
otto reset                         # Reset all tasks to pending, delete otto/* branches, clear logs
```

### Logs

Stored in `otto_logs/<task-id>/` — one directory per task. Files:
- `attempt-<n>-agent.log` — agent output per attempt
- `attempt-<n>-verify.log` — verification output per attempt
- `testgen.log` — test generation output (once per task)

### Exit codes

- 0: all tasks passed
- 1: any task failed
- 2: config error

Composable in scripts and CI.

---

## Key Patterns from Research

| Pattern | Source | How otto uses it |
|---------|--------|-----------------|
| Ratchet (branch only moves forward) | autoresearch | Merge on green, delete on failure, main never regresses |
| Policy/implementation separation | autoresearch | Human defines task + verify criteria, agent does implementation |
| Adversarial verification | cc-autonomous v2 | Separate Claude invocation generates tests, agent can't see them |
| Branch-per-task | Composio | Isolation without worktree overhead |
| Session resume on retry | cc-autonomous v2 | Agent keeps context of what failed |
| Auto-detect test command | Composio | Zero-config for projects with existing tests |
| Generated tests persist | otto (new) | Compound value — each task's tests guard future tasks |
