---
name: otto-pressure-test
description: Use when otto needs deep validation beyond simple e2e tests — run 30+ complex real-world projects simulating actual user scenarios. Checks reliability, display, speed, orchestration, race conditions. Fixes bugs inline. NOT a quick smoke test — this is a multi-hour intensive audit.
---

# Otto Pressure Test

Heavy-duty validation that simulates real users building real projects. NOT a quick e2e test — this takes hours and produces deep analysis.

## When to Use

- After architecture changes, NOT after trivial fixes
- Before releases
- When the user says "pressure test"
- Periodically for regression detection

**NOT for quick validation** — use `bench/smoke/run.sh` for that.

## Difference from E2E Tests

| | E2E / Smoke Test | Pressure Test |
|---|---|---|
| Projects | 4 simple | 30+ complex |
| Time | ~20 min | 4-10 hours |
| Depth | Pass/fail only | Full log analysis, timing profiling, display audit |
| Projects | Fibonacci, bugfix | JWT auth, WebSocket chat, data pipelines, concurrency |
| Signal | "Does it work?" | "Where does it break under stress?" |
| Output | results.json | 200+ line analysis report |

## Project Complexity Requirements

Simple projects (fibonacci, hello world) give NO useful signal. Every project must:

- Require **multiple files** (not a single function)
- Have **non-trivial dependencies** (requests, express, SQLAlchemy, not just stdlib)
- Include **edge cases in the spec** (error handling, concurrency, validation)
- Take a competent human **15+ minutes** to implement manually
- Test a **different otto capability** than other projects in the same category

### Example: Bad vs Good

```
BAD:  "Create a function that adds two numbers"
      → trivial, no deps, no edge cases, tests nothing

GOOD: "Build a thread-safe rate limiter using token bucket algorithm.
       Support sliding window, configurable burst. Must handle 1000
       concurrent goroutines without race conditions. Include tests
       with concurrent access patterns."
      → multiple files, concurrency, edge cases, real-world complexity
```

## Project Categories (minimum 3 each for 30-project run)

### 1. Python — Real Applications
- CLI tools with SQLite persistence, argument parsing, subcommands
- Data pipelines with CSV/JSON parsing, validation, transformation
- Libraries with thread safety, concurrency, complex algorithms
- Web scrapers with error handling, rate limiting, retry logic

### 2. Node.js — Production-Style APIs
- Express/Fastify with JWT auth, middleware, error handling
- WebSocket servers with rooms, reconnection, message queues
- File handling APIs with validation, streaming, cleanup
- Task queues with priorities, retries, dead letter handling

### 3. TypeScript — Type-Safe Libraries
- Schema validators with generics, custom rules, error messages
- State management with computed values, middleware, subscriptions
- Utility libraries with proper type inference, overloads

### 4. Bug Fix — Pre-Seeded Broken Code
Write 50-100 lines of buggy code with 3+ distinct bugs:
- Logic errors (off-by-one, wrong comparison)
- Missing error handling (division by zero, null access)
- Concurrency bugs (race conditions, missing locks)
- Edge case failures (empty input, boundary values)

### 5. Multi-Task — Sequential Dependencies
3+ tasks that build on each other. Each task should be substantial:
- Task 1: Core data model + CRUD + tests
- Task 2: Business logic layer using task 1's model
- Task 3: API/CLI interface using task 2's logic

### 6. Edge Cases — Stress Otto's Internals
- **Greenfield**: completely empty repo, complex task
- **Large spec**: 15+ acceptance criteria, some visual
- **Conflicting tasks**: 3 tasks modifying same file
- **Performance constraint**: measurable requirement (sorting speed, response time)
- **Failing baseline**: project with pre-existing test failures

### 7. Real-World Mixed
- Flask/FastAPI + database + HTML templates
- CLI tools that parse real formats (git logs, JSON APIs)
- Full-stack: backend API + frontend that calls it

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

## Common Pitfalls

- **Simple projects waste time** — fibonacci gives zero signal. Make every project count.
- **Shell quoting** — complex prompts with quotes break `otto add`. Use single quotes.
- **Node.js default test script** — `npm init -y` creates `exit 1`. MUST fix.
- **CLAUDECODE env var** — always prefix `env CLAUDECODE=` for all otto commands.
- **Logs only analyzed if you read them** — don't just check pass/fail. Read pilot_debug.log, agent logs, QA reports for every failed AND every slow project.
- **Display bugs invisible in piped output** — ANSI escapes captured as raw bytes. Need to check for garbled sequences manually.
