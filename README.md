# Otto

Autonomous coding agent that makes Claude safe to run unattended.

Otto wraps Claude Code in a reliability harness: task queue, git isolation, external verification, smart retry, and behavioral testing. You describe what you want, Otto handles the infrastructure to make it happen autonomously.

## How it works

```
otto add "Add search that matches case-insensitively"    →  spec generated
otto run                                                  →  pilot orchestrates everything
```

For each task, Otto:

1. **Generates acceptance spec** — testable criteria from your task description, classified as verifiable or visual
2. **Runs a coding agent** — unconstrained Claude that plans, implements, writes tests (red-green TDD), and iterates
3. **Verifies externally** — runs all tests in a clean disposable worktree
4. **Behavioral testing** — pilot browses the app like a real user (screenshots, clicks, edge cases via chrome-devtools MCP)
5. **Merges** — squash merge to main, clean git history

The coding agent has full autonomy — it reads the codebase, writes its own tests, runs them, and fixes failures. Otto controls the infrastructure around it: branches, worktrees, verification, retry, and cleanup.

## Quick start

```bash
# Install
uv pip install -e .

# In any git repo — add tasks naturally
cd your-project
otto add "Add a search function that matches case-insensitively"
otto add "Fix the slow API response time — must be under 300ms"

# Or import from markdown
otto add -f features.md

# Run — watch the pilot orchestrate
otto run
```

No `otto init` needed — auto-initializes on first `add` or `run`.

## What you see

```
────────────────────────────────────────────────────────────
  Pilot taking control — LLM-driven execution
  The pilot will drive coding → verify → merge

  📋 Loading task state

  🔨 Coding  task abc123
    ✓ passed · $0.31

  Spec Compliance Check:
  ✓ [verifiable] search is case-insensitive — test: test_case_insensitive
  ✓ [verifiable] returns matching results — test: test_search_returns_matches
  ◉ [visual] clean UI with no layout issues

  Behavioral Testing:
  ✓ Behavioral: search "hello" works, "New Jersey" shows US results
  ✓ Behavioral: empty search handled gracefully

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Run complete  2m6s  $0.31
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓ #1  Add search functionality

  1/1 tasks passed
```

## CLI reference

```
otto add "prompt"       Add a task (auto-generates spec with classification)
otto add --no-spec      Skip spec generation
otto add -f file        Import from .md/.txt/.yaml
otto run                Run all pending tasks
otto run --dry-run      Show what would run
otto run --tdd          Generate adversarial tests before coding (optional)
otto status             Show task table
otto show <id>          Show task details
otto retry <id>         Reset a failed task to pending
otto retry --force <id> "feedback"  Reset any task with feedback
otto delete <id>        Remove a task
otto logs <id>          Show logs for a task
otto diff <id>          Show git diff for a task
otto arch               Analyze codebase (for humans, standalone)
otto reset              Clear all tasks, branches, logs
otto reset --hard       Also revert otto commits
```

## Architecture

### Reliability harness

Otto is infrastructure, not intelligence. The intelligence is Claude's. Otto provides:

```
┌─────────────────────────────────────────────────────────┐
│                    otto v3                              │
│  "Reliability harness that makes Claude safe to run     │
│   unattended while you sleep"                          │
└──────────────┬──────────────────────────────────────────┘
               │
    ┌──────────▼──────────┐
    │   Pilot Agent       │   (LLM — strategic decisions)
    │                     │
    │ • Task ordering     │
    │ • Spec compliance   │
    │ • Behavioral test   │
    │ • Smart retry       │
    │ • Doom-loop detect  │
    └──────────┬──────────┘
               │
    ┌──────────▼────────────────────────────────┐
    │     Per-Task Loop                         │
    │                                           │
    │  1. Create branch                         │
    │  2. Coding Agent (UNCONSTRAINED)          │
    │     - reads codebase                      │
    │     - writes tests first (red-green TDD)  │
    │     - implements feature                  │
    │     - runs tests, fixes failures          │
    │  3. External verify (clean worktree)      │
    │  4. Pass → squash merge to main           │
    │     Fail → pilot decides retry strategy   │
    └───────────────────────────────────────────┘
```

### Spec classification

Each spec item is classified as verifiable or visual:

```yaml
spec:
  - text: "search is case-insensitive"
    verifiable: true
    test_hint: "search 'HELLO' and 'hello', verify same results"
  - text: "Apple Weather-style gradient backgrounds"
    verifiable: false
```

- **Verifiable** — coding agent must write a test that proves it
- **Visual** — pilot judges via browser screenshots (chrome-devtools MCP)

### Behavioral testing

After a task passes verification, the pilot uses the app like a real user:

- **Web apps**: opens browser via chrome-devtools MCP, navigates, clicks, takes screenshots
- **CLI tools**: runs commands with real inputs, checks output
- **APIs**: curls endpoints with real payloads

Findings are documented in `otto_logs/<task_key>/behavioral-test.md`.

### Verification

Three layers:

1. **Agent's own tests** — the coding agent writes and runs tests during implementation
2. **Existing test suite** — run in a clean disposable worktree after the agent finishes
3. **Custom verify command** — user-provided validation (`otto add --verify "curl localhost:8080/health"`)

Test framework auto-detected: pytest, npm test, go test, cargo test, maven, gradle, cmake, make. Mixed-language projects chain all detected commands.

## Configuration

`otto.yaml` (auto-created):

```yaml
max_retries: 3
default_branch: main
verify_timeout: 300
max_turns: 200          # coding agent turn limit
effort: high            # coding agent thinking effort
```

Auto-detected: test_command, default_branch. Override anything by adding to `otto.yaml`:

```yaml
model: opus
test_command: "npm test && pytest"
```

## Project structure

```
otto/
  cli.py        — CLI (add, run, status, retry, logs, reset, arch)
  runner.py     — Core execution loop, red-green TDD, agent streaming
  pilot.py      — LLM pilot orchestrator, behavioral testing, MCP tools
  spec.py       — Spec generation with constraint classification
  verify.py     — Verification in disposable worktrees
  tasks.py      — Task CRUD with file locking
  config.py     — Config loading, multi-framework test detection
  architect.py  — Codebase analysis (standalone, for humans)
  testgen.py    — Test generation (used in --tdd mode)
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository
- `mcp` Python package (`pip install mcp`)
- Optional: chrome-devtools MCP for browser-based behavioral testing

## License

MIT
