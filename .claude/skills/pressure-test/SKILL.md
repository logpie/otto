---
name: otto-pressure-test
description: Use when otto needs deep validation beyond simple e2e tests — run 30+ complex real-world projects simulating actual user scenarios. Checks reliability, display, speed, orchestration, race conditions. Fixes bugs inline. NOT a quick smoke test — this is a multi-hour intensive audit.
---

# Otto Pressure Test

Heavy-duty validation that simulates real users building real projects. NOT a quick e2e test — this takes hours and produces deep analysis.

## When to Use

**Full pressure test:**
- After architecture changes, NOT after trivial fixes
- Before releases
- When the user says "pressure test"
- Periodically for regression detection

**Bad cases regression (fast mode):**
- After fixing an otto bug — verify the fix helps on the cases that exposed it
- Before releases — quick confidence check on known trouble spots
- Daily — more signal than smoke tests, much faster than full pressure

**NOT for quick validation** — use `bench/smoke/run.sh` for that.

**For regression runs** — use `bench/bad-cases.yaml` to re-run only previously failed cases. Fast, targeted signal.

## Difference from E2E Tests

| | E2E / Smoke Test | Bad Cases Regression | Pressure Test |
|---|---|---|---|
| Projects | 4 simple | Only prior failures | 30+ complex |
| Time | ~20 min | 30-90 min | 4-10 hours |
| Depth | Pass/fail only | Pass/fail + log check | Full log analysis, timing profiling, display audit |
| Projects | Fibonacci, bugfix | Whatever broke last time | JWT auth, WebSocket chat, data pipelines, concurrency |
| Signal | "Does it work?" | "Did we fix what broke?" | "Where does it break under stress?" |
| Output | results.json | Updated bad-cases.yaml | 200+ line analysis report |

## Canonical Project Set

**28 projects, 34 tasks** checked into `bench/pressure/projects/`. Same format as smoke tests: each project has `setup.sh` + `tasks.txt`. See `bench/pressure/README.md` for the full inventory.

**DO NOT generate projects on the fly.** Use the canonical set. This ensures:
- Reproducible runs (same inputs → comparable results)
- Real-repo projects pinned at specific commits
- Bad cases can reference projects by name across runs

### Categories

| Category | Count | Languages | Key signals |
|----------|-------|-----------|-------------|
| Python greenfield | 5 | Python | Threading, SQLite, parsing, data pipelines |
| Node.js greenfield | 5 | JS | Express, WebSocket, streams, queues |
| TypeScript greenfield | 4 | TS | Generics, type inference, monads, DI |
| Bug fix (synthetic) | 3 | Python, JS | 6 bugs each, pre-seeded broken code |
| Bug fix (real repos) | 3 | Python, JS | cachetools, node-semver, python-box |
| Feature add (real repos) | 3 | Python, TS | tinydb, radash, citty |
| Multi-task | 2 | Python, JS | 3 sequential tasks each |
| Edge cases | 3 | Python, JS, TS | Empty repo, large spec, conflicting tasks |

### Real-Repo Projects

These clone from GitHub at a pinned commit SHA. The task is a real bug fix or feature that was actually implemented — we pin to the commit before it happened.

| Project | Repo | Commit | Task type |
|---------|------|--------|-----------|
| real-cachetools-bugfix | tkem/cachetools | 3b3167a | Cache stampede threading bug |
| real-semver-bugfix | npm/node-semver | 2677f2a | Prerelease parsing regex |
| real-box-bugfix | cdgriffith/Box | 91cc956 | box_dots get() regression |
| real-tinydb-feature | msiemens/tinydb | 9394412 | persist_empty tables |
| real-radash-feature | sodiray/radash | 32a3de4 | inRange() utility |
| real-citty-feature | unjs/citty | 69252d4 | Subcommand aliases |

### Adding New Projects

To add a project to the canonical set:
1. Create `bench/pressure/projects/<name>/setup.sh` + `tasks.txt`
2. For real repos: pin at a specific commit SHA, copy source into working repo
3. Update `bench/pressure/README.md`
4. Run it once manually to verify setup works

## Logging and Traceability

**Every run must produce artifacts for post-mortem analysis:**

### Per-Project Artifacts
```
/tmp/pt-PROJECT/
  output.txt          # Full terminal output (otto run 2>&1 | tee output.txt)
  tasks.yaml          # Final task state
  otto.yaml           # Config used
  otto_logs/
    pilot_debug.log   # Timestamped phase events
    pilot_results.jsonl  # All progress events (JSON)
    live-state.json   # Last live state (if crashed mid-run)
    <task_key>/
      attempt-*-agent.log    # What the coding agent did
      attempt-*-verify.log   # Test output
      qa-report.md           # QA findings
      verify.log             # Final verify status
      progress.txt           # Agent progress stream
```

### What to Extract from Logs

**From `pilot_debug.log`** (timestamped):
```
[HH:MM:SS] [PHASE] message
```
- Time between phases = overhead
- Time per coding attempt = agent efficiency
- Number of retries = task difficulty signal
- Stale events from previous runs = JSONL reader bug

**From `pilot_results.jsonl`** (structured):
- Count `event: "phase"` transitions per task
- Count `event: "agent_tool"` calls — how many Read/Write/Bash per attempt
- Check `event: "qa_finding"` — are specs being checked?
- Check final `tool: "run_task_with_qa"` result — cost, timing, success

**From `output.txt`** (terminal display):
- Grep for ANSI escape codes that shouldn't be visible: `\033[`, `[2K`, `[2m`
- Check phase lines appear in order: prepare → coding → test → qa → merge
- Check tool calls visible during coding (not just spinner)
- Check QA findings visible during QA phase
- Check summary shows per-phase timing

### Profiling Script

Run after each project to extract timing:
```bash
# Phase timing from debug log
grep -E "\[EXEC\].*(started|done|FAILED)" otto_logs/pilot_debug.log | \
  awk '{print $1, $3}' | column -t

# Cost from tasks.yaml
grep "cost_usd:" tasks.yaml | awk '{sum+=$2} END {print "$"sum}'

# Retry count
grep "attempts:" tasks.yaml | awk '{print "attempts:", $2}'

# Agent tool call count
grep "agent_tool" otto_logs/pilot_results.jsonl | wc -l

# QA findings count
grep "qa_finding" otto_logs/pilot_results.jsonl | wc -l
```

## Deep Analysis Checklist

### Reliability Analysis
- [ ] Pass rate by category (Python vs Node.js vs TypeScript)
- [ ] Failure categorization (coding fail, verify fail, QA fail, timeout, crash)
- [ ] Retry distribution (what % need 1/2/3/4+ attempts)
- [ ] Are failures systematic or random?

### Speed Profiling
- [ ] Pilot overhead: time from `otto run` to first `run_task_with_qa`
- [ ] Coding agent time: breakdown of thinking vs tool calls vs waiting
- [ ] QA time: is it re-running tests it shouldn't?
- [ ] Verify time: is dep installation slow?
- [ ] Merge time: any git issues?
- [ ] Compare: same-category projects — why is one 2x slower?

### Display Audit
- [ ] Phase transitions visible and in order
- [ ] Tool calls shown during coding (not just spinner)
- [ ] QA findings streamed (spec results, not raw Bash/Read)
- [ ] No garbled ANSI in captured output
- [ ] Summary per-phase timing accurate
- [ ] `otto status -w` shows live state during run (test from second terminal)
- [ ] `otto show <id>` works after run
- [ ] `otto logs <id>` shows structured output

### Orchestration Audit
- [ ] `run_task_with_qa` is the sole execution path
- [ ] Multi-task projects execute in correct order
- [ ] Conflicting-task project handles sequential execution
- [ ] Retry hints are specific (not generic)
- [ ] `abort_task` guardrail works (refuses if < max_retries)

### Resource Cleanup
- [ ] No orphaned Claude processes after run
- [ ] No orphaned chrome-devtools processes
- [ ] Lock files cleaned up
- [ ] live-state.json deleted after completion
- [ ] Stale branches cleaned up
- [ ] No temp files left in /var/folders/

## Fixing Bugs

**Fix inline, don't defer.** The whole point is to find AND fix.

1. Document: symptom, expected behavior, root cause
2. Fix in otto source (`/Users/yuxuan/work/cc-autonomous/otto/`)
3. Run tests: `.venv/bin/python -m pytest tests/ -x -q --tb=short`
4. Commit with descriptive message referencing the project that exposed it
5. For critical bugs: dispatch Codex MCP review with `sandbox: "read-only"`
6. Continue testing — fix applies to subsequent projects

## Report

Write to `/tmp/otto-pressure-report.md`. Must include:

### Summary Table
All projects with: category, tasks, pass/fail, cost, time, attempts, notes.

### Reliability Section
Pass rates by category. Failure root causes. Retry distribution chart.

### Speed Section
Per-phase timing averages. Bottleneck identification. Slowest projects with WHY.
Compare: simple vs complex, Python vs Node.js vs TypeScript.

### Display Section
Terminal output snippets showing bugs. Before/after if fixed.

### Bugs Found
Each bug: severity, symptom, root cause, fix status, which project exposed it.

### QA Effectiveness
Real bugs caught vs false positives. QA time as % of total. Value assessment.

### Recommendations
Top 5 actionable improvements ranked by impact.

### Bad Cases Update
After writing the report, update `bench/bad-cases.yaml` — add new failures, increment pass streaks, graduate stable cases. This is NOT optional — the bad cases file is the durable output that makes the next run faster.

## Bad Cases Collector

Failed and problematic projects are accumulated into `bench/bad-cases.yaml` as a persistent regression suite. This turns expensive pressure test failures into fast, targeted signal.

### What Gets Collected

Add a case to `bench/bad-cases.yaml` when a project:
- **Fails** — coding, verify, QA, timeout, or crash
- **Is flaky** — passes sometimes, fails others
- **Is abnormally slow** — 2x+ slower than similar-category projects
- **Exposes an otto bug** — even if the project itself passes after a fix

Do NOT collect cases that fail due to one-off infra issues (network timeout, disk full).

### Format

```yaml
# bench/bad-cases.yaml
# Accumulated from pressure test runs. Used for fast regression testing.
# Cases graduate out after 3 consecutive passes.

cases:
  - name: thread-safe-rate-limiter
    category: python
    date_added: "2026-03-21"
    failure_type: coding_fail  # coding_fail | verify_fail | qa_fail | timeout | crash | flaky | slow | otto_bug
    description: "Race condition in token bucket — agent didn't add locking"
    pass_streak: 0  # incremented on pass, reset to 0 on fail, graduated at 3
    spec: |
      Build a thread-safe rate limiter using token bucket algorithm.
      Support sliding window, configurable burst. Must handle 1000
      concurrent threads without race conditions. Include tests
      with concurrent access patterns.
    tasks: 1  # number of tasks
    last_run: "2026-03-21"
    last_result: fail

  - name: websocket-chat-server
    category: nodejs
    date_added: "2026-03-21"
    failure_type: slow
    description: "Took 25min vs 10min avg — agent kept rewriting auth middleware"
    pass_streak: 1
    spec: |
      ...
    tasks: 1
    last_run: "2026-03-21"
    last_result: pass
```

### During Pressure Test

After each project completes:

1. **If failed/problematic**: Check if already in `bad-cases.yaml`
   - If exists: reset `pass_streak` to 0, update `last_run`, `last_result`, `description`
   - If new: append entry with full spec, category, failure type
2. **If passed cleanly**: Check if in `bad-cases.yaml`
   - If exists: increment `pass_streak`, update `last_run`, `last_result`
   - If `pass_streak >= 3`: graduate — move to `graduated` section (keep for history, don't re-run)

### Running Bad Cases Only (Regression Mode)

For quick regression after a fix:

```bash
# Parse bad-cases.yaml and run only non-graduated cases
# Same setup/teardown as pressure test, but skip project generation —
# specs come from the yaml file
```

Run this:
- After fixing an otto bug (verify the fix actually helps)
- Before releases (fast confidence check)
- As a daily smoke-ish test (more signal than `bench/smoke`, much faster than full pressure)

### Graduation

Cases graduate after **3 consecutive passes** — meaning the fix stuck across multiple runs, not just one lucky pass. Graduated cases move to a `graduated:` section in the same file:

```yaml
graduated:
  - name: thread-safe-rate-limiter
    category: python
    date_added: "2026-03-21"
    date_graduated: "2026-04-02"
    original_failure: "Race condition in token bucket"
    total_runs: 7
```

This keeps history without cluttering active regression runs.

## Common Pitfalls

- **Simple projects waste time** — fibonacci gives zero signal. Make every project count.
- **Shell quoting** — complex prompts with quotes break `otto add`. Use single quotes.
- **Node.js default test script** — `npm init -y` creates `exit 1`. MUST fix.
- **CLAUDECODE env var** — always prefix `env CLAUDECODE=` for all otto commands.
- **Logs only analyzed if you read them** — don't just check pass/fail. Read pilot_debug.log, agent logs, QA reports for every failed AND every slow project.
- **Display bugs invisible in piped output** — ANSI escapes captured as raw bytes. Need to check for garbled sequences manually.
