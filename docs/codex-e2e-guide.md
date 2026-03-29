# Codex E2E Testing Guide for Otto

How to run real otto e2e tests from a Codex sandbox or fresh machine.

## Why Previous Runs Failed

The 2026-03-29 Codex audit runs failed because:
1. `.venv/bin/otto` console script was used — sandbox env vars polluted the SDK auth
2. `env=dict(os.environ)` was passed explicitly, forwarding sandbox restrictions to the agent subprocess
3. Otto's `_subprocess_env()` already handles env construction correctly — don't override it

## Correct Way to Run Otto

**Always use `uv run --project`** — it manages the env correctly:

```bash
# Clone and install
git clone --depth 1 https://github.com/logpie/otto otto-audit
cd otto-audit

# Single-task run (the simplest test)
cd /path/to/test-repo
uv run --project /path/to/otto-audit otto run "Add subtract(a, b) to calc.py"

# Multi-task run
cd /path/to/test-repo
uv run --project /path/to/otto-audit otto add "Task one description"
uv run --project /path/to/otto-audit otto add "Task two description"
uv run --project /path/to/otto-audit otto run
```

**Do NOT use:**
```bash
# These may fail in sandboxed environments:
/path/to/otto-audit/.venv/bin/otto run "..."
/path/to/otto-audit/.venv/bin/python -c "from claude_agent_sdk import ..."
```

## CLI Commands

| Command | What it does |
|---------|-------------|
| `otto run "prompt"` | One-off: creates temp task, runs through full pipeline, cleans up |
| `otto add "prompt"` | Adds a task to tasks.yaml (pending) |
| `otto run` | Runs all pending tasks through planner → coding → test → QA → merge |
| `otto plan` | **Preview only** — shows execution plan, does NOT persist or affect `otto run` |
| `otto status` | Shows current task states |
| `otto status -w` | Live watch mode |

**`otto plan` is informational.** `otto run` re-plans from scratch. Running `otto plan` before `otto run` is fine but unnecessary — they're independent.

## Minimal Test Repo Setup

```bash
tmpdir=$(mktemp -d)
cd "$tmpdir"
git init -b main
git config user.email test@test.com
git config user.name Test

cat > calc.py <<'EOF'
def add(a, b):
    return a + b
EOF

mkdir -p tests
cat > tests/test_calc.py <<'EOF'
from calc import add

def test_add():
    assert add(2, 3) == 5
EOF

cat > otto.yaml <<'EOF'
default_branch: main
max_retries: 1
verify_timeout: 300
max_parallel: 1
test_command: python -m pytest tests/ -q
skip_qa: true
EOF

git add . && git commit -m init
```

Then run:
```bash
uv run --project /path/to/otto-audit otto run "Add subtract(a, b) that returns a - b. Add tests."
```

Expected output:
- Worktree created
- Coding agent adds subtract function + tests
- Tests pass in disposable worktree
- Merged to main
- `tasks.yaml` shows `status: passed`

## Multi-task Test

```bash
uv run --project /path/to/otto-audit otto add "Add subtract(a, b). Add tests."
uv run --project /path/to/otto-audit otto add "Add multiply(a, b). Add tests."
uv run --project /path/to/otto-audit otto run
```

Expected:
- Planner classifies tasks (INDEPENDENT → parallel batch, or ADDITIVE → serial)
- Each task runs in its own worktree
- Batch QA verifies integrated result
- Both tasks show `status: passed`

## Inspecting Results

```bash
# Task states
cat tasks.yaml

# What happened
cat otto_logs/orchestrator.log

# Per-task details
cat otto_logs/{task_key}/qa-agent.log
cat otto_logs/{task_key}/qa-proofs/proof-report.md
cat otto_logs/{task_key}/task-summary.json

# Planner decisions
cat otto_logs/planner.log
```

## Pressure Test Runner

For systematic testing across many projects:

```bash
cd /path/to/otto-audit
bash bench/pressure/run.sh                    # all 43 projects
bash bench/pressure/run.sh real-iniconfig-parse  # single project
```

Results go to `/tmp/otto-pressure-results/{timestamp}/`.

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Not logged in" | Claude CLI not authenticated | Run `claude /login` |
| "baseline tests fail" | Project deps not importable in worktree | Check `_install_deps` — may need editable install |
| Planner falls back to serial | LLM call failed (auth, timeout) | Check `otto_logs/planner.log` |
| QA says pass but verify.sh fails | Spec didn't cover the edge case | Check spec-agent.log for what was specced |
| $0.00 cost, instant failure | Infrastructure error (auth, import) | Check `otto_logs/{key}/live-state.json` for error |

## Environment Requirements

- `git`
- `uv` (Python package manager)
- `python >= 3.11` (via uv)
- `node` + `npm` (for Node.js test repos)
- `claude` CLI installed and authenticated (`claude /login`)
