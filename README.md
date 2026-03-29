# Otto

Autonomous coding agent that makes Claude safe to run unattended.

Otto wraps Claude Code in a reliability harness: task queue, git isolation, structured QA, and evidence-based verification. You describe what you want, Otto handles the rest.

## How it works

```
otto add "Add search that matches case-insensitively"
otto run
```

For each task, Otto:

1. **Runs a bare CC coding agent** — raw prompt, no spec bottleneck. The coding agent explores the codebase, implements the feature, and runs tests on its own.
2. **Generates acceptance spec in parallel** — `[must]` (gating) and `[should]` (advisory) criteria with `◈` markers for visual/subjective items. Runs in a separate thread alongside coding.
3. **Verifies externally** — runs all tests in a clean disposable worktree.
4. **QA agent reviews** — two-part testing: VERIFY (check every [must] spec with evidence) then BREAK (adversarial boundary testing beyond specs). Browser available for visual items.
5. **Merges** — squash merge to main, clean git history.

Independent tasks run **in parallel** using git worktrees (`max_parallel: 2+`). Merge conflicts are resolved by re-running the coding agent on updated main with the previous diff as context — same intelligence, full retry budget.

Failed tasks get structured retry: the coding agent receives a focused failure excerpt (not 847K of raw test output), and pre-existing flaky tests are excluded from retry decisions.

## What you see

```
  ● Running  #1  abc12345
  17:08:20  ✓ prepare  16s  baseline: 109 tests passing

  17:08:20  ● coding  (bare CC)  · spec gen
      ● Bash  find src -type f -name "*.tsx" | sort
      ● Read  src/types/weather.ts
      ... explored 13 files
      ● Write  src/components/Feature.tsx
        + "use client";
        + import { getData } from "@/lib/data";
          ...60 more lines
      ● Edit  src/components/App.tsx
        - import OldComponent from "./OldComponent";
        + import Feature from "./Feature";
      ● Bash  npx next build 2>&1 | tail -20
  17:11:14  ✓ coding  174s  $0.81  3 files  +92  -10
  17:11:52  ✓ test  22s  838 passed
  17:11:52  ✓ spec gen  68s  $0.26  8 items (5 must, 3 should) (ready)
      [must] A large emoji is displayed based on current conditions
      [must] The emoji has a visible repeating pulse animation
      ... +5 more

  17:11:52  ● qa  (full - adversarial testing)
      Reading src/components/Feature.tsx
      Testing: npx jest --no-coverage 2>&1 | tail -30
      Building project
      ✓ [must] Feature renders correctly
        Component maps weather codes to emojis: 0→☀️, 3→☁️, 45-48→🌫️
      ✓ [must] Animation runs continuously
        globals.css defines --animate-weatherPulse: 3s ease-in-out infinite
      ✓ [must ◈] Positioned prominently in weather section
        Uses text-6xl, placed between condition name and temperature
      · [should ◈] Animation is subtle and smooth
        ease-in-out over 3s with modest scale(1.08)
      · [should] Accessible with aria-label
        role='img' and aria-label present
  17:13:41  ✓ qa  109s  $0.39  tier 2  5 specs passed  5/5 proved
  17:13:41  ✓ passed  5m36s  $1.47
    3 files · 8 specs verified
       proofs: /tmp/project/otto_logs/abc12345/qa-proofs/proof-report.md

  1/1 tasks passed
```

## Quick start

```bash
# Install
uv pip install -e .

# In any git repo — add tasks
cd your-project
otto add "Add a search function that matches case-insensitively"
otto add "Fix the slow API response — must be under 300ms"

# Run
otto run
```

No `otto init` needed — auto-initializes on first `add` or `run`.

## CLI reference

```
otto add "prompt"       Add a task (spec generated at runtime, parallel with coding)
otto add --spec "prompt"  Pre-generate spec before run
otto add -f file        Import from .md/.txt/.yaml
otto run                Run all pending tasks (parallel if max_parallel > 1)
otto run "prompt"       One-off: add + run in single command
otto run --no-spec      Skip spec generation
otto run --no-qa        Skip QA (merge after tests pass)
otto run --no-test      Skip testing (merge after coding)
otto run --max-parallel N  Override max_parallel for this run
otto plan               Show execution plan without running
otto status             Show task table with specs, cost, timing
otto show <id>          Show task details + QA verdict
otto retry <id>         Reset a failed task to pending
otto retry --force <id> "feedback"  Reset any task with feedback
otto logs <id>          Show agent logs for a task
otto diff <id>          Show git diff for a task
otto drop <id>          Remove a task from the queue (code stays on main)
otto drop --all         Remove all tasks + clean otto/* branches
otto revert <id>        Undo one task's git commit on main
otto revert --all       Undo all otto commits + clear queue
```

## Architecture

> **Full pipeline reference with debugging guide:** [`docs/architecture.md`](docs/architecture.md)

### v5 pipeline

Otto is infrastructure, not intelligence. The intelligence is Claude's. Otto provides:

```
    ┌─────────────────────────────────────────────────────────┐
    │  1. Smart Planner                                       │
    │     Analyzes task relationships:                        │
    │     INDEPENDENT → parallel  DEPENDENT → serialize       │
    │     ADDITIVE (same file) → serialize                    │
    │     CONTRADICTORY → flag + separate batches (never drop) │
    │                                                         │
    │  2. Per-task pipeline (parallel worktrees):             │
    │     Coding Agent → Tests → Verify (no QA yet)           │
    │     Spec generation deferred to batch QA                │
    │                                                         │
    │  3. Merge phase                                         │
    │     git merge → conflict? → coding agent re-apply       │
    │     (full diff as context, agent adapts intelligently,  │
    │      one agent — trust it to self-regulate)             │
    │                                                         │
    │  4. Batch QA (one session, combined specs)              │
    │     Verify ALL [must] items on integrated codebase      │
    │     Generate cross-task integration tests               │
    │     If [must] fails → retry (up to max_retries rounds)  │
    │     Each round: re-code → re-merge → re-QA             │
    │     Still failing? → rollback batch, continue run       │
    │     Smart replan re-schedules rolled-back tasks         │
    │                                                         │
    │  5. Pass → proof report with commit SHA                 │
    │     Fail → retry with failure excerpt                   │
    └─────────────────────────────────────────────────────────┘
```

### Smart planner

Before coding starts, the planner classifies task relationships:

```
Tasks: [A: add search] [B: add dark mode] [C: add search filters] [D: rewrite search]
  │
  Planner analysis:
  ├─ A ↔ B: INDEPENDENT → parallel batch
  ├─ A ↔ C: DEPENDENT (C needs A) → C in later batch
  ├─ A ↔ D: CONTRADICTORY → flag + schedule in separate batches
  └─ B ↔ C: INDEPENDENT → parallel batch
  │
  Plan: Batch 1 [A, B] → Batch 2 [C]
  Conflicts: [A, D] — "Both rewrite search with incompatible goals"
```

### Parallel batch execution

Independent tasks within a batch run concurrently in git worktrees:

- **Within-batch** = parallel (tasks are independent, each in its own worktree)
- **Cross-batch** = serial (later batches depend on earlier results)
- **Same-file tasks** = serialized (additive overlap causes reliable merge conflicts)
- **Merge conflict** = coding agent re-applies with full diff as context (one agent, adapts intelligently — simple conflicts resolve fast, complex ones get more exploration)
- **Batch QA** = one session on integrated codebase with combined specs, behavioral testing required

Task states: `pending → running → verified → merged → passed`
(or `→ failed` / `→ merge_failed → auto-retry` / `→ conflict`)

### Spec binding model

Each spec item has a binding level and verifiability marker:

- **`[must]`** — gating. QA blocks merge if failed.
- **`[must ◈]`** — gating + visual/subjective. Requires browser verification.
- **`[should]`** — advisory. QA notes observations but doesn't block.
- **`[should ◈]`** — advisory + visual. Noted with evidence.

### QA verdict

QA produces structured JSON with per-item evidence and proof:

```json
{
  "must_passed": true,
  "must_items": [
    {
      "spec_id": 1,
      "criterion": "Banner appears when wind > 60 km/h",
      "status": "pass",
      "evidence": "detectSeverityConditions checks all 4 thresholds",
      "proof": [
        "ran jest weatherAlerts: 'detects wind > 60 km/h' passes",
        "browser: banner visible after injecting extreme data",
        "screenshot: qa-proofs/screenshot-banner.png"
      ]
    }
  ]
}
```

### Proof of work

Each task produces reproducible evidence in `otto_logs/<key>/qa-proofs/`:

```
qa-proofs/
  proof-report.md          Human-readable proof per must item
  regression-check.sh      Re-runnable verification commands
  must-1.md ... must-N.md  Per-item criterion + status + evidence
  screenshot-*.png         Browser screenshots (visual ◈ items)
```

The **proof report** shows per-item proof with coverage:

```markdown
## ✓ [1] Banner appears when wind > 60 km/h
Evidence: detectSeverityConditions checks all 4 thresholds
Proof:
- ran jest weatherAlerts: 'detects wind > 60 km/h' passes
- browser: banner visible after injecting extreme data
- screenshot-banner.png — Red banner at top with all conditions

---
Proof coverage: 6/6 must items have proof recorded
```

The **regression script** is independently runnable ground truth — a third party can re-run it without trusting the agent:

```bash
bash otto_logs/<key>/qa-proofs/regression-check.sh
# → Test Suites: 43 passed, Tests: 997 passed
```

### Display

Semantic color hierarchy for scannability:

- **Green `● Write`/`● Edit`** — code changes (key actions)
- **Cyan `● Bash`** — commands
- **Dim `● Read`** — background exploration
- **Cyan `[must]`** — gating requirements
- **Magenta `◈`** — visual/subjective marker
- **Bold `Testing:`/`Curl:`** — QA verification actions
- **Bold magenta `Browser:`** — visual testing

## Configuration

`otto.yaml` (auto-created):

```yaml
max_retries: 3
default_branch: main
verify_timeout: 300       # seconds for test suite
max_task_time: 3600       # 1hr circuit breaker per task
qa_timeout: 3600          # QA agent timeout
max_parallel: 1           # 1 = serial (default), 2+ = parallel worktrees
install_timeout: 120      # seconds for npm ci / pip install in worktrees

# Per-agent setting scopes (comma-separated: user, project)
coding_agent_settings: "user,project"   # reads user + project CLAUDE.md
spec_agent_settings: "project"          # project CLAUDE.md only
qa_agent_settings: "project"            # project CLAUDE.md only
```

Auto-detected: test_command, default_branch. Override anything in `otto.yaml`.

### Parallel execution

Set `max_parallel: 2` (or higher) to run independent tasks concurrently. Each task gets its own git worktree at `.otto-worktrees/otto-task-<key>/`. Worktrees are cleaned up after each run. Serial is the default — parallel is opt-in.

## Project structure

```
otto/
  cli.py             — CLI (add, run, plan, status, show, retry, drop, revert, logs)
  runner.py           — v4.5 pipeline: bare CC coding, structured QA, retry
  spec.py             — Spec generation with [must]/[should]/◈ classification
  testing.py          — Testing in disposable worktrees
  tasks.py            — Task CRUD with file locking
  config.py           — Config loading, multi-framework test detection
  orchestrator.py     — Batch execution: parallel worktrees, serial merge, auto-retry
  display.py          — Live terminal display with semantic color hierarchy
  display_preview.py  — HTML preview tool for display debugging
  claim_verify.py     — Regex audit of agent log vs verify evidence
  retry_excerpt.py    — Failure extraction from test output for retries
  flaky.py            — Pre-existing flaky test detection
  context.py          — Cross-task learnings with provenance
  theme.py            — Shared console and styling constants
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository
- Optional: chrome-devtools MCP for browser-based QA testing

## License

MIT
