# Otto — Autonomous Claude Code Agent Runner

## Overview

Otto is a CLI tool that runs autonomous Claude Code agents against a task queue with tiered verification. It processes tasks serially, commits on success, reverts on failure, and ensures the main branch never regresses.

**Core insight:** Combine autoresearch's ratchet pattern (branch only moves forward) with adversarial integration test generation (a separate Claude invocation writes tests the agent can't see or game).

---

## Design Decisions

### CLI-first, no web UI
The value is the autonomous loop, not a dashboard. CLI composes with cron, GitHub Actions, SSH, and scripts. A web UI can be added later without redesigning the core.

### Serial execution
Parallel agents introduce merge conflicts, resource contention, and coordination complexity (Symphony needs an entire Elixir/BEAM runtime for this). Serial with branch-per-task is simple, reliable, and still fully autonomous — you throw a list of tasks at it and walk away.

### Branch-per-task
Each task runs on `otto/<task-slug>`. On success, merge to main (fast-forward). On failure, delete the branch. Main never has broken code. `git log` becomes a readable history of what otto accomplished.

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

For each pending task:

```
1. Create branch otto/<task-slug>
2. Start generating integration tests concurrently (separate claude -p)
3. Run agent on the task (claude-agent-sdk, with task prompt + project context)
4. Run tiered verification:
   a. Existing test suite (auto-detected)
   b. Generated integration tests (from step 2)
   c. Custom verify command (if specified in task)
5. All pass → commit, merge branch to main, next task
6. Any fail → resume session with error context, retry (up to max_retries)
7. All retries exhausted → delete branch, log failure, next task
```

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
    prompt: "Add JWT authentication to the API"
    status: pending

  - id: 2
    prompt: "Fix memory leak in cache.py"
    verify: "python benchmark.py --check-memory"
    status: pending

  - id: 3
    prompt: "Refactor database layer to use connection pooling"
    max_retries: 5
    status: pending
```

Minimal required field: `prompt`. Everything else has defaults from `otto.yaml` or is auto-derived.

Runtime state (`status`, `attempts`, `error`, `session_id`) is written back to the task file as the system runs. No separate state database.

### Project config (`otto.yaml`)

```yaml
test_command: pytest
max_retries: 3
model: sonnet
project_dir: .
```

Created by `otto init`, which auto-detects `test_command` from the project.

---

## Tiered Verification

Three tiers, run in order. First failure stops the chain.

### Tier 1: Existing test suite

- Auto-detected from project or configured in `otto.yaml` `test_command`.
- Detection order: `pytest` → `npm test` → `go test ./...` → `cargo test` → `make test`.
- Runs the full suite. Catches regressions.
- Skipped if no test command found.

### Tier 2: Generated integration tests

- A separate `claude -p` call generates tests from the task prompt.
- Prompt instructs: real dependencies, no mocks, behavioral/integration assertions.
- Tests written to a file (e.g., `tests/otto_verify_<task_slug>.py`).
- Generated concurrently with the agent's first attempt — ready by verification time.
- Persisted in the repo — they become regression tests for future tasks.
- On retry, existing generated tests are reused (not regenerated).
- Adversarial by construction: written by a separate invocation that sees only the task prompt and project structure, not the agent's implementation.

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
otto add -f tasks.yaml             # Load tasks from a file

# Run
otto run                           # Run all pending tasks in tasks.yaml
otto run "Fix the bug in auth.py"  # One-off task, no tasks.yaml needed
otto run --dry-run                 # Show what would run without executing

# Monitor
otto status                        # Table of tasks with status, attempts, timing
otto logs <task-id>                # Show agent + verification logs for a task

# Manage
otto retry <task-id>               # Reset a failed task to pending
otto reset                         # Reset all tasks to pending
```

### Logs

Stored in `otto_logs/<task-id>/` — `agent.log`, `verify.log`, `testgen.log`. One directory per task.

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
