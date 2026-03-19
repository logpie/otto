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
User prompt → spec gen (independent PM voice)
                  ↓
              spec items (the contract) + verification intents
                  ↓
              pilot (explores codebase, plans, orchestrates)
                  ├─ analyzes file overlaps → injects dependencies
                  ├─ decides execution order (serial/parallel)
                  ↓
              coding agent (strong, autonomous — plans, codes, self-tests)
                  ↓
              verification (verifiers generated from spec intents)
                  ↓
              pilot checks spec compliance on diff
                  ├─ confident → merge
                  ├─ not confident → retry with feedback
                  └─ stuck → escalation report
```

### What's removed from v2 (simpler, not more complex)

| Removed | Why | Savings |
|---------|-----|---------|
| Architect agent | Pilot explores codebase and does file-plan analysis as part of planning | One fewer agent |
| Testgen as mandatory step | Coding agent writes own tests — its strongest self-correction tool | ~200 lines orchestration |
| Tamper detection | Coding agent trusted to fix test bugs, spec is the real contract | 15 lines + tamper-revert bugs |
| "Don't write tests" constraint | Artificially weakened the coding agent | Prompt complexity + workarounds |
| Holistic testgen (required) | Optional at most — coding agent handles its own tests | ~150 lines parallel loop |
| Test diagnosis agent | No adversarial tests = no test bugs to diagnose | ~50 lines |
| Pilot coding directly | Pilot orchestrates, never codes | Role boundary violation removed |
| Single-attempt mode | Agent handles own retries with full context | Pilot micromanagement removed |
| Separate review agent | Pilot does cross-task review + spec compliance — one role, not two | Removes an agent |

### What's kept / refined

**1. Spec gen (independent PM voice — always runs)**
- Formalizes user intent without implementation bias
- Extracts hard constraints, preserves them verbatim
- Generates verification intents alongside spec items
- The contract that coding agent and pilot both reference
- Benchmarked: "spec" framing produces 2x faster, better constraint preservation than "rubric"
- Needed because coding agent has implementation bias — it might soften specs toward what's easy to build

**2. Coding agent (strong, autonomous)**
- Plans before coding ("can current architecture meet all requirements?")
- Writes its own tests to validate approach
- Can fix test bugs (broken imports, wrong stdlib) but not weaken assertions
- Handles retries internally with full context
- Receives spec items directly — optimizes for the spec, uses tests as feedback

**3. Pilot (orchestrator + codebase-aware planner)**
- Explores codebase to understand structure (Read, Glob, Grep — but NOT Edit/Write)
- Analyzes file overlaps across tasks → injects serial dependencies (absorbs architect's file-plan role)
- Writes/updates conventions.md as a living doc (absorbs architect's conventions role)
- Checks spec compliance on diff after coding agent passes
- Cross-task consistency review on combined diff
- Escalates when stuck instead of failing silently
- Does NOT write implementation code — only orchestration + planning artifacts

**4. Verification (natural language verifiers)**

Users express verification intent in natural language as part of the task prompt:
```
"make sure the API responds with valid JSON on /health"
"the search should return results within 300ms"
"the login page should show email and password fields"
```

Spec gen formalizes these into spec items. Each spec item gets a verification approach
generated automatically — the user never writes YAML or test commands:

| User says | Verification generated |
|-----------|----------------------|
| "API returns JSON on /health" | `curl -s localhost:8080/health \| python -m json.tool` |
| "latency < 300ms" | `time.perf_counter()` measurement in test |
| "login page shows form" | Playwright: navigate + check for input elements |
| "export CSV has correct headers" | Read file + validate header row |
| "GUI shows weather data" | Screenshot + LLM visual check (future) |

The coding agent generates executable verifiers as part of its test-writing.
The pilot runs them as part of spec compliance review. New verifier types
(browser, visual, API) extend naturally without changing otto's architecture.

**5. Escalation instead of silent failure**
- When confidence is too low after max retries
- Structured report: what was built, what's met, what needs input
- Critical for 24/7 unattended — user wakes up to clarity, not mystery

**6. Testgen (optional, not default)**
- `--tdd` mode: testgen runs first, provides independent cross-check
- Default: coding agent writes own tests as part of implementation
- Both modes: pilot does spec compliance check

## Progressive Verification

Not every task needs the same level of scrutiny. The pilot decides based on spec requirements:

| Spec requirement | Verifier type | Generated how |
|-----------------|--------------|---------------|
| "function returns correct result" | Unit test | Coding agent writes pytest |
| "CLI command works" | Subprocess test | Coding agent writes subprocess.run test |
| "API responds correctly" | HTTP check | Coding agent generates curl/requests test |
| "completes in < 300ms" | Timing test | Coding agent wraps with perf_counter |
| "page shows login form" | Browser test | Playwright script (generated by agent) |
| "GUI looks like mockup" | Visual test | Screenshot + LLM comparison (future) |
| "data exports correctly" | Output diff | Compare output file against expected |

All verifier types work the same way: the coding agent generates them as executable tests during implementation. No special infrastructure per type — just different test patterns.

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
| conventions.md | Pilot (during planning) | All coding agents | Start of each task |
| file-plan.md | Pilot (during planning) | Runner (dep injection) | Before task execution |
| learnings.md | Coding agents (append) | Pilot + all coding agents | Start of each task |
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

## Implementation Plan

### Phase 1: Simplify (remove v2 complexity)

**Files to delete or gut:**
- `otto/architect.py` — delete agent, keep `parse_file_plan()` and `load_design_context()` as utilities
- `otto/test_validation.py` — delete (no generated tests to validate in default mode)
- Testgen orchestration in `otto/runner.py` — remove the 200-line testgen-before-coding flow
- Tamper detection in `otto/runner.py` — already removed
- Test diagnosis (`_diagnose_test_bug`) in `otto/runner.py` — remove
- Holistic testgen loop in `otto/runner.py` `run_all()` — remove

**Files to simplify:**
- `otto/testgen.py` — keep `run_testgen_agent()` for `--tdd` mode, remove the rest (holistic, generate_tests, generate_integration_tests)
- `otto/runner.py` — `run_task()` becomes much simpler: create branch → run coding agent → external verify → pass/fail. No testgen, no tamper check, no test diagnosis.

### Phase 2: Strengthen coding agent

**Key principle: the coding agent optimizes for the spec, uses tests as feedback.** Tests are tools the agent writes and runs to validate its own approach. The spec is the goal. When they conflict, the spec wins.

**`otto/runner.py` — rewrite coding agent prompt:**
```
{task prompt}

ACCEPTANCE SPEC:
{spec items}

You are working in {work_dir}. Do NOT create git commits.

APPROACH:
1. PLAN — read the spec and codebase. Can current architecture meet ALL requirements?
   If not, note what needs to change.
2. IMPLEMENT your plan.
3. WRITE TESTS that verify each spec item.
4. RUN TESTS and fix failures. Iterate until all pass.
5. Write notes to otto_arch/task-notes/{key}.md:
   - What approach you took and why
   - What you learned about the codebase
   - Any gotchas for future tasks

{optional: design conventions from otto_arch/conventions.md}
{optional: learnings from otto_arch/learnings.md}
{optional: previous task notes from otto_arch/task-notes/}
{optional: failure feedback from previous attempt}
{optional: relevant source context — pre-loaded}
```

### Phase 3: Pilot as planner + orchestrator

**`otto/pilot.py` — rewrite pilot prompt:**

Planning phase (absorbs architect):
- Pilot reads codebase structure (file tree, key modules)
- Analyzes file overlaps across tasks
- Writes `otto_arch/file-plan.md` and `otto_arch/conventions.md`
- Outputs execution plan

Execution phase:
- Calls `run_coding_agent` for each task (respecting dependency order)
- After each task: reads diff, checks spec compliance
- Decides: merge / retry with feedback / escalate

Post-run:
- Cross-task consistency review on combined diff
- Escalation report if any tasks have low confidence

**Pilot MCP tools (simplified):**
- `get_run_state` — current task statuses
- `run_coding_agent(task_key, hint?)` — runs full task lifecycle
- `read_verify_output(task_key)` — see what failed
- `merge_task(task_key)` — merge to main
- `abort_task(task_key, reason)` — give up with explanation
- `save_run_state(phase, notes)` — persist state
- `finish_run(summary)` — done

Removed MCP tools:
- `run_holistic_testgen` — no mandatory testgen
- `run_per_task_testgen` — only in `--tdd` mode
- `run_coding_agents` (parallel) — pilot calls `run_coding_agent` individually, runner handles worktrees
- `run_integration_gate_tool` — pilot does review directly
- `run_architect_tool` — pilot does planning directly
- `run_verify` — coding agent verifies internally

### Phase 4: External verification

**`otto/verify.py` — keep but simplify:**
- Run existing test suite in clean disposable worktree (unchanged)
- Run custom verify command if configured (unchanged)
- Return pass/fail + output (unchanged)

This is the regression gate. The coding agent's own tests are committed to the project and run as part of the test suite. No separate "generated test" vs "project test" distinction.

### Phase 5: Persistent memory

**New in coding agent prompt:** read/write learnings and task notes
**New in pilot:** read learnings for cross-task context
**Files:** `otto_arch/learnings.md`, `otto_arch/task-notes/{key}.md`

### Phase 6: Spec compliance + confidence scoring

The pilot checks spec compliance after each task passes verification. This is a prompt instruction, not new code:

**Pilot prompt addition:**
```
After coding agent reports success:
1. Read the diff
2. For each spec item, assess: clearly met / approximately met / not met
3. Watch for spec-dodging (meeting constraint by removing the feature)
4. If any spec item is "not met" → retry with specific feedback
5. If all "clearly met" or "approximately met" → merge
```

The confidence is per-spec-item, not a numeric score. The pilot makes a judgment call — same as a human reviewer approving a PR.

### Phase 7: Escalation protocol

When a task fails after max retries, the pilot produces a structured report:

```
## Escalation Report — Task #{id}

### What was built
{diff summary}

### Spec compliance
- ✓ Item 1: clearly met
- ~ Item 2: approximately met (used mock data instead of real API)
- ✗ Item 3: not met (300ms constraint impossible with sync HTTP)

### What was tried
- Attempt 1: synchronous fetch → 650ms (too slow)
- Attempt 2: added caching → cached is <300ms, uncached still >500ms
- Attempt 3: tried async → import errors in test environment

### Recommended options
A. Accept with known limitation (uncached >300ms)
B. Restructure to async architecture (multi-task change)
C. Use stale-while-revalidate pattern (show cached, refresh in background)
```

This replaces the current binary "failed — max retries exhausted" with actionable information.

### Phase 8: Optional testgen (`--tdd` mode)

When `otto run --tdd` is passed:
- Testgen agent runs before coding agent (same as v2)
- Generated tests are committed and serve as additional constraint
- Coding agent still writes its own tests too
- Pilot compliance check still applies

Default (no `--tdd`): coding agent writes all tests. No separate testgen step.

### Phase 9: Visual verification (future)

Not in initial v3 implementation. Design for later:
- Screenshot capture via Playwright or tkinter after GUI task completes
- LLM-based visual comparison against user description
- Pilot includes visual assessment in spec compliance check
- Coding agent can use browser MCP tools for web app verification

### What stays unchanged

- `otto/cli.py` — CLI commands (add, run, status, etc.)
- `otto/rubric.py` — spec gen (renamed prompt, same code)
- `otto/tasks.py` — task CRUD
- `otto/config.py` — configuration
- `otto/display.py` — output helpers
- Branch management (create_task_branch, merge_to_default, cleanup_branch)
- Worktree management (_setup_task_worktree, _teardown_task_worktree)
- Git safety (check_clean_tree, snapshot_untracked, auto-stash)
- Process locking
- Signal handling

### Verification criteria

1. `python -m pytest tests/ -x -q` passes (update tests for new architecture)
2. `otto run` on taskflow project: 3 tasks, all pass, coding agent writes own tests
3. `otto run` on weather project: single task with performance constraint, agent plans approach
4. Coding agent can fix test bugs without tamper detection blocking it
5. Pilot detects spec-dodging on diff review
6. Task notes persist across retries
7. `otto run --tdd` still generates tests before coding (backward compat)
8. Parallel tasks work with worktrees (file-plan deps injected by pilot)
9. Escalation report produced when task is infeasible
