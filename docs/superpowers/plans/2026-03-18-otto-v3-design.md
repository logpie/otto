# Otto v3 — Design Vision

## Mission

Reliability harness that makes Claude safe to run, 24/7, unattended, to turn intent into product.

## Core Insight

All verification artifacts (tests, specs, rubrics) are LLM-generated approximations of user intent. None are ground truth. Treating any of them as absolute truth leads to reward hacking (removing features to pass tests) and stuck loops (retrying against broken tests).

The user's words are the closest thing to truth, and even those are ambiguous.

## Design Principles

1. **Spec is a contract, tests are tools.** The spec formalizes user intent. Tests validate implementation. When they conflict, the spec wins.
2. **Tests are evidence, not gates.** Multiple signals contribute to confidence — tests, diff review, spec compliance, agent assessment. No single signal is authoritative.
3. **Each agent is as capable as `claude -p` within its role.** Don't artificially weaken agents then build external systems to compensate.
4. **Roles are orthogonal.** Spec gen formalizes intent. Coding agent plans and implements. Pilot orchestrates and judges. No overlap.
5. **Escalate, don't fail silently.** When confidence is low, produce a clear report instead of marking "failed."

## Current Architecture (v2)

```
User prompt → spec gen → spec items
                              ↓
                    ┌─────────┼──────────┐
                    ↓                    ↓
              testgen agent        coding agent
              (writes tests        (sees spec + code,
               from spec)          plans, implements)
                    ↓                    ↓
              test file ────→ verification (binary pass/fail)
                                         ↓
                                   pilot (orchestrates)
                                         ↓
                                   merge or fail
```

### v2 Problems

- **Tests as hard gates**: one broken test blocks everything. Coding agent burns $2-10 retrying against unfixable test bugs.
- **Testgen has implementation bias**: writes tests that are too aggressive (adversarial framing) or semantically wrong (cached-only when spec says all lookups).
- **Binary verification**: pass/fail with no nuance. A task that meets 6/7 spec items is "failed" the same as one that meets 0/7.
- **Pilot reward-hacks**: when coding agent fails, pilot previously coded directly (removing features to pass tests).
- **No escalation**: tasks either pass or fail. No middle ground of "mostly done, needs human input."
- **Coding agent was artificially weak**: "don't write tests, don't modify tests, don't plan" — then external systems compensated.

## v3 Architecture

v3 is simpler than v2 — it removes agents and pipeline stages, not adds them.

```
User prompt → spec gen (independent PM voice, optional)
                  ↓
              spec items (the contract)
                  ↓
              pilot (orchestrator — ordering, parallelism, merge decisions)
                  ↓
              coding agent (strong, autonomous — plans, codes, self-tests)
                  ↓
              verification (existing test suite in clean worktree)
                  ↓
              pilot checks spec compliance on diff
                  ├─ confident → merge
                  ├─ not confident → retry with feedback
                  └─ stuck → escalation report
```

### What's removed from v2 (simpler, not more complex)

| Removed | Why | Savings |
|---------|-----|---------|
| Testgen as mandatory step | Coding agent writes own tests — its strongest self-correction tool | ~200 lines orchestration |
| Tamper detection | Coding agent trusted to fix test bugs, spec is the real contract | 15 lines + tamper-revert bugs |
| "Don't write tests" constraint | Artificially weakened the coding agent | Prompt complexity + workarounds |
| Holistic testgen (required) | Optional at most — coding agent handles its own tests | ~150 lines parallel loop |
| Test diagnosis agent | No adversarial tests = no test bugs to diagnose | ~50 lines |
| Pilot coding directly | Pilot orchestrates, never codes | Role boundary violation removed |
| Single-attempt mode | Agent handles own retries with full context | Pilot micromanagement removed |
| Separate review agent | Pilot does cross-task review + spec compliance — one role, not two | Removes an agent |

### What's kept / refined

**1. Spec gen (independent PM voice)**
- Formalizes user intent without implementation bias
- Extracts hard constraints, preserves them verbatim
- The contract that coding agent and pilot both reference
- Benchmarked: "spec" framing produces 2x faster, better constraint preservation than "rubric"
- Optional for simple tasks, valuable when user prompt is casual/ambiguous

**2. Coding agent (strong, autonomous)**
- Plans before coding ("can current architecture meet all requirements?")
- Writes its own tests to validate approach
- Can fix test bugs (broken imports, wrong stdlib) but not weaken assertions
- Handles retries internally with full context
- Receives spec items directly — optimizes for the spec, uses tests as feedback

**3. Pilot (orchestrator — no coding, no micromanaging)**
- Decides task ordering and parallelism
- Checks spec compliance on diff after coding agent passes
- Cross-task consistency review (no separate review agent needed)
- Escalates when stuck instead of failing silently
- Does NOT use Edit/Write/Bash — only orchestration tools

**4. Verification (simple, not layered)**
- Coding agent's own tests (written during implementation)
- Existing test suite in clean worktree (catches regressions)
- Custom verify command (user-provided, optional)
- Pilot spec compliance check (semantic, on the diff)

**5. Escalation instead of silent failure**
- When confidence is too low after max retries
- Structured report: what was built, what's met, what needs input
- Critical for 24/7 unattended — user wakes up to clarity, not mystery

**6. Testgen (optional, not default)**
- `--tdd` mode: testgen runs first, provides independent cross-check
- Default: coding agent writes own tests as part of implementation
- Both modes: pilot does spec compliance check

## Progressive Verification

Not every task needs the same level of scrutiny:

| Task Complexity | Verification Level | Cost |
|----------------|-------------------|------|
| Simple (add CLI command) | Agent self-tests + mechanical verification | Low |
| Medium (new feature) | + spec compliance check | Medium |
| Complex (architectural change) | + integration tests + escalation readiness | Higher |
| Visual/GUI | Approximate (can't verify visually yet) | Report-based |

The pilot decides verification level based on task complexity and spec requirements.

## Persistent Project Memory

Otto should get better at YOUR project over time. First run is cold. Each subsequent run benefits from accumulated knowledge.

### What gets persisted

```
otto_arch/
  conventions.md        — coding patterns (architect-generated, living doc)
  file-plan.md          — dependency plan (per-run)
  learnings.md          — accumulated discoveries across ALL tasks and runs
                          "Open-Meteo API returns Celsius not Fahrenheit"
                          "tkinter requires DISPLAY env var in CI"
                          "Store uses file locking — don't hold locks across network calls"
                          "sync HTTP can't meet <300ms — need async or stale-while-revalidate"
  task-notes/
    {key}.md            — per-task notes from the coding agent
                          what approach was taken, what failed, key decisions
                          survives across retries within the same task
```

### Who writes, who reads

| File | Written by | Read by | When |
|------|-----------|---------|------|
| conventions.md | Architect agent | All coding agents | Start of each task |
| file-plan.md | Architect agent | Runner (dep injection) | Before task execution |
| learnings.md | Coding agents (append) | All coding agents | Start of each task |
| task-notes/{key}.md | Coding agent for that task | Same agent on retry, future tasks | Start of task, between retries |

### How it helps different scenarios

**Retries within a task:**
- Attempt 1 writes: "tried synchronous fetch, 650ms — too slow"
- Attempt 2 reads this, tries async instead of rediscovering the dead end
- Saves $0.50-1.00 per avoided dead-end retry

**Sequential tasks:**
- Task #1 discovers: "CLI uses Click with @main.command() pattern"
- Task #2 reads this, follows the same pattern without exploring
- Consistency without architect overhead

**Across runs (next day):**
- Yesterday: 3 tasks done, learnings accumulated
- Today: 3 more tasks. Coding agents read yesterday's learnings
- "The test suite uses tmp_path fixtures, not hardcoded /tmp"
- Like a new team member reading the project wiki

**Greenfield projects:**
- First task: discovers nothing (empty project). Writes foundational notes
- By task #5: rich project knowledge — patterns, conventions, API quirks
- The test suite and learnings grow together

### Parallel execution

Multiple coding agents in parallel worktrees can't write to the same file. Pattern:

- Each agent writes to `otto_arch/task-notes/{key}.md` (one file per agent, no conflict)
- Each agent reads ALL existing notes before starting
- After a parallel batch merges, learnings are visible to the next batch
- Within a parallel batch, agents DON'T see each other's notes (no real-time sharing)
- On retry after merge failure, the retried agent DOES see the merged state

This matches the scratchpad pattern from v2 but with richer content.

### Learnings vs CLAUDE.md

Learnings are otto-specific project knowledge discovered during task execution. They complement CLAUDE.md (user-maintained project instructions) but don't replace it:

- CLAUDE.md: user writes, human-curated, high-level ("use uv not pip", "test with pytest")
- learnings.md: agents write, machine-discovered, concrete ("Store.list_tasks returns Task objects not dicts", "the --db flag defaults to ~/.taskflow/data.json")

Both are included in the coding agent prompt. CLAUDE.md is authoritative (user's voice). Learnings are supplementary (may be wrong, but useful).

## MCP Tools and Skills

The coding agent should inherit the user's MCP configuration. If the user has database, browser, or API MCP servers configured, the coding agent can use them:

- "Add a feature that queries our Postgres database" → agent uses database MCP
- "Build a web scraper" → agent tests with browser MCP
- "Integrate with our Slack workspace" → agent uses Slack MCP

### Safety for unattended operation

MCP access in unattended mode needs guardrails:
- Read-only MCP access by default (query database, fetch URLs)
- Write access requires explicit opt-in per MCP server in otto.yaml
- The pilot should NOT have MCP access — only coding agents (role boundary)
- Cost/rate limits on MCP calls to prevent runaway usage

### Skills

The coding agent runs via Agent SDK which supports Claude Code's full tool set. If the user has skills installed (browser automation, research, etc.), the agent can use them. This extends otto's capabilities without changing otto's code.

## User Interaction Patterns

Otto should support how real users actually work:

**Batch mode (unattended):**
```bash
otto add -f features.md    # import 10 tasks from spec doc
otto run                    # go to sleep
# wake up → otto status shows results + escalation reports
```

**Iterative mode:**
```bash
otto add "add search"       # one task at a time
otto run                    # watch it work
# see result, adjust
otto add "search should also match tags, not just titles"
otto run
```

**Collaborative mode:**
```bash
otto add "add the data model and API layer"    # otto handles the routine
# user manually implements the tricky UI in their editor
otto add "add tests for the UI I just built"   # otto tests what user wrote
otto run
```

**Existing project mode:**
```bash
cd my-existing-project
otto init                   # otto learns the project structure
otto add "fix the N+1 query in the user dashboard"
otto run                    # benefits from existing test suite as regression gate
```

Each mode works with the same architecture. The difference is how much context otto has (greenfield vs existing), how many tasks (batch vs iterative), and how much the user is involved (unattended vs collaborative).

## Future: Visual Verification

Some tasks produce visual output (GUI, charts, styling) that can't be verified by tests. v3 should support:
- Screenshot capture of GUI output
- LLM-based visual comparison ("does this look like Apple Weather?")
- Human-in-the-loop for visual sign-off
- Approximate verification: "the tkinter window renders without errors" (testable) vs "the gradient looks good" (visual review)

## Implementation Sequence

1. **Coding agent prompt refinement** — "optimize for spec, tests are feedback"
2. **Confidence scoring in verification** — per-spec-item assessment
3. **Escalation protocol** — structured report when confidence is low
4. **Optional testgen** — `--tdd` flag, default is agent writes own tests
5. **Visual verification** — screenshot + LLM review for GUI tasks
