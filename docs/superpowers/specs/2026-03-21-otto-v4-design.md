# Otto v4: PER Pipeline Architecture

**Date**: 2026-03-21
**Status**: Draft
**Authors**: Yuxuan + Claude

---

## Problem Statement

Otto v3's pilot (LLM-driven orchestrator) adds 3-5 minutes of overhead per run. A trivial fibonacci task takes 818s (13m37s) — roughly 85% is structural overhead, not useful work. The overhead comes from:

1. **Pilot reasoning on the happy path**: 5-8 LLM turns for deterministic decisions ("run the next task, it passed, done") at ~25s each.
2. **Nested subprocess spawning**: `query(pilot)` → MCP tool → `query(coding_agent)` → each spawning a CLI subprocess loading ~50K tokens of system prompt.
3. **Blocked parallelism**: The pilot can't dispatch concurrent work — it blocks on each MCP tool call, making "parallel research" mostly theoretical.
4. **ToolSearch overhead**: MCP tool discovery adds 30-60s at startup.

The native subagent approach (e8c9d0d) was tried and reverted (08f5421) because it traded subprocess overhead for more pilot reasoning overhead (5 tool calls per task instead of 1). The hybrid approach (current) reduced pilot calls but kept the nested subprocess problem.

### What We Want to Keep

The pilot's intelligence IS valuable for:
- **Strategic task ordering**: Semantic understanding beyond topological sort (e.g., "run the infrastructure task first because later tasks build on it").
- **Failure strategy**: Analyzing errors, crafting targeted retry hints, deciding between retry/research/abort.
- **Cross-task learning**: Failure in task 2 informs task 3's approach.
- **Research dispatch**: Pre-researching hard tasks while coding runs.
- **Adaptive replanning**: Reordering remaining tasks based on what was learned.

### What We Want to Eliminate

- LLM reasoning for deterministic decisions (task ordering when obvious, "yes run the next one").
- Subprocess nesting (pilot → MCP → coding agent → each a separate CLI process).
- Blocked execution (pilot can't dispatch concurrent work).
- Pilot overhead on the happy path (everything passes first try).

---

## Industry Research Summary

Research across 20+ frameworks (LangGraph, CrewAI, AutoGen, OpenAI Swarm, Temporal, Google ADK, Amazon Bedrock, Claude Code, Codex CLI, OpenHands, Cursor, Aider, Goose, Devin, Roo Code, Kilo Code, Amp, SWE-agent) reveals a clear consensus:

**No production framework uses a pure LLM orchestrator.** The industry has converged on "deterministic backbone with selective intelligence injection" — code handles flow control, LLMs are invoked only at genuine decision points.

Key patterns:

| Pattern | Used By | Relevance to Otto |
|---|---|---|
| **Deterministic backbone + intelligence injection** | LangGraph, CrewAI, Google ADK, Temporal | Core architecture for v4 |
| **Plan-Execute-Replan** | LangGraph orchestrator-workers, Cursor plan-then-execute | The PER pattern |
| **Effort scaling rules** | Anthropic multi-agent research | Pilot decides effort per task |
| **Shared state coordination** | OpenHands event stream, Temporal activities, LangGraph state | PipelineContext for agent coordination |
| **Parallel isolated execution** | Cursor (8 worktrees), Claude Code (background subagents) | Parallel coding in worktrees |
| **Model routing** | Aider (Architect/Editor), Amp (Oracle), Roo Code (per-mode models) | Different models for different roles |

Full research reports: `research-perf.md`, `research-agent-orchestration-patterns.md`, `research-coding-agent-frameworks.md`.

---

## Architecture: Plan-Execute-Replan (PER) Pipeline

### Overview

Replace the long-running pilot LLM session with a **deterministic Python orchestrator** that invokes a **planner** LLM only at decision points (planning and failure recovery). Agents coordinate through **in-memory shared state** (PipelineContext). A write-only JSONL log provides observability.

### Naming

| v3 name | v4 name | Why |
|---|---|---|
| Pilot (long-running LLM, 100 turns) | **Planner** (2-3 focused `query()` calls) | It plans, it doesn't pilot |
| pilot.py | **planner.py** (new) + **pilot_v3.py** (preserved) | Clean break |
| EventChannel | **Telemetry** (JSONL log) + **PipelineContext** (shared state) | Separated concerns |
| run_task_with_qa | **coding_loop** | Async coroutine, not monolithic function |

### Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│              Python Orchestrator (orchestrator.py)            │
│              deterministic, no LLM, asyncio event loop        │
│                                                              │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────────┐  │
│  │  PLAN    │ →  │   EXECUTE    │ →  │ REPLAN (batch     │  │
│  │ 1 query()│    │ N query()    │    │  boundary)        │  │
│  │ planner  │    │ parallel     │    │ 1 query() planner │  │
│  └──────────┘    └──────┬───────┘    └───────────────────┘  │
│                         │                                    │
│              ┌──────────┴──────────┐                         │
│              │  PipelineContext     │                         │
│              │  (in-memory shared   │                         │
│              │   state — dicts,     │                         │
│              │   lists, no pub/sub) │                         │
│              └──────────┬──────────┘                         │
│         ┌───────┬───────┴────┬────────────┐                  │
│         ▼       ▼            ▼            ▼                  │
│     coding_1  coding_2   researcher   Telemetry              │
│    (worktree) (worktree) (async)      (JSONL write-only,     │
│     query()   query()    query()       humans read)          │
│     has QA    has QA                                         │
│     inline    inline                                         │
└──────────────────────────────────────────────────────────────┘
```

### Communication Model

No event bus. No pub/sub. No message passing. Plain Python function calls:

```
Planner → orchestrator:  returns ExecutionPlan (function return)
Orchestrator → agents:   passes hints via prompt (function argument)
Agents → orchestrator:   returns TaskResult (function return)
Research → coding:       writes to context.research dict, coding reads on retry
Orchestrator → planner:  passes accumulated context on replan (function argument)
Telemetry:               write-only JSONL log, fire-and-forget, never read for coordination
```

All comms are instantaneous (in-memory dict writes, function returns). Zero serialization overhead.

### Phase 1: PLAN

A single `query()` call to the **planner**. It sees all tasks, specs, project metadata, and codebase context. Produces a structured **ExecutionPlan**.

```python
@dataclass
class TaskPlan:
    task_key: str
    strategy: str           # "direct" | "research_first"
    research_query: str     # if strategy == "research_first"
    hint: str               # initial hint for coding agent
    skip_qa: bool           # skip QA for simple tasks
    effort: str             # "low" | "medium" | "high"

@dataclass
class Batch:
    tasks: list[TaskPlan]   # run in parallel within this batch

@dataclass
class ExecutionPlan:
    batches: list[Batch]    # run sequentially (batch N+1 after batch N completes)
    learnings: list[str]    # cross-task context to carry forward
```

The planner's `query()` call:
- Model: Sonnet (planning doesn't need Opus)
- Max turns: 5-10 (investigate codebase if needed via Read/Grep, then produce plan)
- Tools: Read, Grep, Glob, WebSearch (read-only — pilot never writes code)
- System prompt: Custom pilot prompt (~500 tokens of otto-specific instructions). Note: `query()` still loads the full Claude Code system prompt underneath — the custom `system_prompt` parameter is additive, not a replacement. The real savings come from fewer `query()` calls, not smaller prompts.

**For trivial cases** (1 task, no dependencies): the planner's output is essentially `{batches: [{tasks: [{task_key: ..., strategy: "direct"}]}]}`. One turn, minimal reasoning. The overhead of a single `query()` call (~10-15s) replaces the current 3-5 minutes of pilot reasoning.

### Phase 2: EXECUTE

The Python orchestrator executes the plan. No LLM is involved in coordination — it's pure asyncio.

**Core rule: code in parallel, integrate serially.** Tasks within a batch code and verify in parallel worktrees. But merges happen one at a time through a serialized integrator, with rebase-before-merge to avoid `merge_diverged` failures (since parallel tasks branch from the same base, `--ff-only` would fail for the second task unless rebased).

```python
async def execute_plan(plan: ExecutionPlan, config, project_dir, tasks_file):
    telemetry = Telemetry(project_dir / "otto_logs" / "events.jsonl")  # write-only log
    context = PipelineContext(learnings=plan.learnings)
    sem = asyncio.Semaphore(config.get("max_parallel", 2))

    while plan.batches:
        batch = plan.batches.pop(0)

        # Start research agents for tasks that need pre-research
        for task_plan in batch.tasks:
            if task_plan.strategy == "research_first":
                asyncio.create_task(
                    research_and_publish(task_plan, context, config, telemetry)
                )

        # Code + verify in parallel (each in its own worktree)
        coding_results = await asyncio.gather(*[
            coding_loop(tp, context, config, project_dir, sem, telemetry)
            for tp in batch.tasks
        ], return_exceptions=True)

        # Merge serially (rebase-before-merge to handle parallel branches)
        for result in coding_results:
            if isinstance(result, Exception) or not result.success:
                context.add_failure(result)
                continue
            merge_ok = await asyncio.to_thread(
                rebase_and_merge, project_dir, result.worktree,
                result.task_key, config["default_branch"]
            )
            if merge_ok:
                context.add_success(result)
                telemetry.log(TaskMerged(result.task_key, result.commit_sha))
            else:
                context.add_failure(result, error="merge_diverged after rebase")

        # REPLAN at batch boundary — on BOTH success and failure.
        # Even successful batches carry learnings for remaining tasks.
        # Skip replan if no batches remain (nothing to replan for).
        if plan.batches:
            plan = await pilot_replan(context, plan, config, project_dir)
            # plan.batches now has updated remaining work; loop continues

    telemetry.log(AllDone())
```

### Serial Integration: Rebase-Before-Merge

```python
def rebase_and_merge(project_dir, worktree, task_key, default_branch):
    """Rebase task branch onto current default, then ff-only merge.

    Handles the case where parallel tasks A and B branched from the same base.
    After A merges, B's branch is behind. Rebase B onto the updated default
    before merging.
    """
    task_branch = f"otto/{task_key}"

    # Rebase task branch onto current default (picks up parallel peers' merges)
    rebase = subprocess.run(
        ["git", "rebase", default_branch, task_branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if rebase.returncode != 0:
        # Rebase conflict — abort and report
        subprocess.run(["git", "rebase", "--abort"], cwd=project_dir, capture_output=True)
        return False

    # Now ff-only merge is safe
    subprocess.run(["git", "checkout", default_branch], cwd=project_dir, capture_output=True)
    merge = subprocess.run(
        ["git", "merge", "--ff-only", task_branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    return merge.returncode == 0
```

### Phase 3: REPLAN

Invoked at **every batch boundary** (not just on failure). This preserves the planner's ability to do cross-task learning even when things go well.

Another `query()` call to the pilot with:
- All batch results (pass/fail/error details)
- Verification logs for failed tasks
- Research findings (if any)
- Cross-task learnings accumulated so far
- **Successful task summaries** (what was built, what patterns were used)

The pilot investigates (Read/Grep to understand failures) and produces a new ExecutionPlan for remaining tasks with updated hints, reordered batches, and new research queries.

- Model: Sonnet
- Max turns: 5-10 (focused investigation + produce plan)
- Tools: Read, Grep, Glob, WebSearch (read-only)

**Single-batch runs** (1 task, or all tasks in one batch): no replan call. Zero LLM overhead for the happy path beyond the initial PLAN.

**Multi-batch runs**: replan is called at every batch boundary (even on success) so the planner can inject cross-task learnings into remaining batches. This is lightweight — one `query()` call with accumulated context — but preserves the planner's ability to adapt based on what was learned.

---

## Agent Roles

All agents use `query()` from the Claude Agent SDK (subscription-based, no API keys required).

### Coding Agent

The workhorse. Implements features, writes tests, fixes bugs.

- **Model**: Configurable (default: model from otto.yaml, typically Opus or Sonnet)
- **Max turns**: 200
- **System prompt**: Focused coding prompt with spec compliance rules (~600 tokens, replaces the 50K Claude Code default)
- **Tools**: Full Claude Code suite (Read, Write, Edit, Bash, Grep, Glob)
- **Isolation**: Git worktree per task (parallel-safe)
- **Subagents**: `researcher` (Haiku, read-only) and `explorer` (Haiku, read-only) for within-task delegation
- **Settings**: `setting_sources=["user", "project"]` for CLAUDE.md, `env=os.environ` for PATH/API keys

After coding completes, the orchestrator builds a candidate commit and runs verification. QA runs inline within the coding_loop (gated by semaphore). The coding_loop returns a `TaskResult` to the orchestrator.

### QA Agent

Adversarial tester. Runs after verification passes.

- **Model**: Sonnet
- **Max turns**: 30
- **System prompt**: Adversarial QA prompt — find ways the implementation does NOT meet the spec (~300 tokens)
- **Tools**: Bash, Read (for testing via curl/API/browser)
- **MCP**: chrome-devtools (only when spec has visual items)
- **Timeout**: Configurable (default: `qa_timeout` from otto.yaml, 900s)

**Conditional QA**: The planner's plan can set `skip_qa: true` for tasks it deems simple (few spec items, no visual items, low effort). This saves 1-3 minutes per simple task. QA is always run for tasks that required retries or have visual spec items.

### Research Agent

Investigates technical topics. Runs in parallel with coding.

- **Model**: Haiku (cheap, fast — research doesn't need heavy reasoning)
- **Max turns**: 10
- **System prompt**: Research prompt — search web, read docs, report findings (~200 tokens)
- **Tools**: WebSearch, WebFetch, Read (for reading local docs/READMEs)

Research agents write findings to `PipelineContext.research`. The orchestrator injects them as hints into coding agent retries.

### Planner (Plan/Replan)

Strategic advisor. Consulted at decision points only — not a long-running session.

- **Model**: Sonnet
- **Max turns**: 5-10 (focused, not open-ended)
- **System prompt**: Planner prompt — task planning, failure analysis, cross-task learning (~500 tokens)
- **Tools**: Read, Grep, Glob, WebSearch (read-only — planner never modifies code)

Invoked once for PLAN, once per REPLAN at batch boundaries. Between invocations, all coordination is deterministic Python. The planner decides WHAT to do; the orchestrator decides HOW.

---

## Coordination & Telemetry (Separated Concerns)

**Key design decision (from Codex review):** Do NOT use the same mechanism for coordination and logging. JSONL is for telemetry (write-only, observable). In-memory asyncio primitives are for control flow (reliable, no lost events).

### Telemetry: JSONL Event Log (Write-Only)

For observability, debugging, and `otto status -w`. Write-only — never read for coordination.

```python
@dataclass
class TaskMerged:
    task_key: str
    commit_sha: str

@dataclass
class TaskFailed:
    task_key: str
    error: str
    attempts: int

@dataclass
class AgentToolCall:
    task_key: str
    tool_name: str
    summary: str

# ... other event types for logging

class Telemetry:
    """Write-only JSONL event log. NOT used for coordination."""

    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event) -> None:
        with open(self._path, "a") as f:
            f.write(json.dumps({"type": type(event).__name__, **asdict(event),
                                "ts": time.time()}) + "\n")
```

The display layer tails this file for live progress (same pattern as v3's `pilot_results.jsonl`). During migration, dual-write to both old format (`pilot_results.jsonl`, `live-state.json`) and new format (`events.jsonl`) so existing CLI commands (`otto status -w`, `otto show`, `otto logs`) continue working.

### Control Plane: In-Memory Asyncio Primitives

Agent coordination uses explicit asyncio objects — no pub/sub, no lost events.

```python
class PipelineContext:
    """Shared state for the pipeline. Passed to all agent loops."""

    def __init__(self, learnings: list[str] = None):
        self.learnings: list[str] = learnings or []
        self.research: dict[str, str] = {}          # task_key → findings
        self.results: list[TaskResult] = []          # accumulated results
        self.session_ids: dict[str, str] = {}        # task_key → session_id for resume
        self.costs: dict[str, float] = {}            # task_key → cost_usd
        self.interrupted: bool = False               # signal handler sets this
        self.pids: set[int] = set()                  # track query() subprocess PIDs

    def add_research(self, task_key: str, findings: str):
        self.research[task_key] = findings

    def get_research(self, task_key: str) -> str | None:
        return self.research.get(task_key)

    def add_success(self, result: TaskResult):
        self.results.append(result)

    def add_failure(self, result, error: str = ""):
        if isinstance(result, Exception):
            self.results.append(TaskResult(success=False, error=str(result)))
        else:
            result.error = error or result.error
            self.results.append(result)

@dataclass
class TaskResult:
    """Return value from coding_loop — plain data, no async primitives."""
    task_key: str
    success: bool
    commit_sha: str = ""
    worktree: str = ""
    cost_usd: float = 0.0
    error: str = ""
    qa_report: str = ""
    diff_summary: str = ""
    duration_s: float = 0.0
```

**Why this works better than EventChannel:**
- `coding_loop` returns a `TaskResult` directly — no pub/sub needed for the main flow.
- Research findings go into `PipelineContext.research` — a plain dict, no async queue.
- QA is called inline by `coding_loop` (or as a parallel task that returns a result) — no event routing.
- No race conditions from events emitted before subscribers register.
- No lost events. No ordering ambiguity.
- Telemetry is a separate concern — `telemetry.log()` calls don't affect control flow.

---

## Agent Loops

### coding_loop

Returns a `TaskResult` directly. No event bus for control flow. Telemetry is fire-and-forget logging.

```python
async def coding_loop(task_plan, context, config, project_dir, sem, telemetry):
    """Run coding agent with retry. Returns TaskResult."""
    task = load_task(task_plan.task_key)
    worktree = await asyncio.to_thread(setup_worktree, project_dir, task["key"])
    hint = task_plan.hint
    total_cost = 0.0
    task_start = time.monotonic()

    # Inject cross-task learnings
    if context.learnings:
        hint += "\n\nLearnings from prior tasks:\n" + "\n".join(context.learnings)

    try:
        for attempt in range(config.get("max_retries", 3) + 1):
            if context.interrupted:
                break

            # Check for research findings (may have arrived during previous attempt)
            research = context.get_research(task["key"])
            if research:
                hint += f"\n\nResearch findings:\n{research}"

            # Run coding agent (gated by semaphore)
            async with sem:
                session_id = context.session_ids.get(task["key"])
                success, cost, new_session_id = await run_coding_agent(
                    task, config, worktree, hint, session_id=session_id
                )
            total_cost += cost
            if new_session_id:
                context.session_ids[task["key"]] = new_session_id

            if not success:
                hint = f"Coding failed (attempt {attempt + 1}). Fix the issues."
                continue

            # Build candidate commit
            candidate_sha = await asyncio.to_thread(
                build_candidate_commit, worktree, task
            )

            # Run deterministic verification (no LLM — just run tests)
            verify_result = await asyncio.to_thread(
                run_verification, worktree, candidate_sha, config
            )
            telemetry.log(VerifyResult(task["key"], verify_result.passed))

            if not verify_result.passed:
                hint = f"Verification failed:\n{verify_result.failure_output}"
                await asyncio.to_thread(reset_to_base, worktree)
                continue

            # Run QA (if not skipped)
            qa_report = ""
            if not task_plan.skip_qa:
                async with sem:
                    qa_result = await run_qa_agent(
                        task["key"], candidate_sha, str(worktree), config
                    )
                total_cost += qa_result.get("cost_usd", 0)
                if not qa_result["passed"]:
                    hint = f"QA failed:\n{qa_result.get('error', '')}"
                    qa_report = qa_result.get("report", "")
                    await asyncio.to_thread(reset_to_base, worktree)
                    continue
                qa_report = qa_result.get("report", "")

            # Success — return result (merge happens in orchestrator, serially)
            diff_summary = await asyncio.to_thread(get_diff_summary, worktree)
            return TaskResult(
                task_key=task["key"], success=True,
                commit_sha=candidate_sha, worktree=str(worktree),
                cost_usd=total_cost, qa_report=qa_report,
                diff_summary=diff_summary,
                duration_s=time.monotonic() - task_start,
            )

        # All retries exhausted
        return TaskResult(
            task_key=task["key"], success=False,
            error="all retries exhausted", cost_usd=total_cost,
            duration_s=time.monotonic() - task_start,
        )
    finally:
        # Worktree cleanup on failure (successful worktrees cleaned after merge)
        pass  # orchestrator handles cleanup
```

### research_and_publish

Writes findings to `PipelineContext` — available for coding agent retries.

```python
async def research_and_publish(task_plan, context, config, telemetry):
    """Run research agent, store findings in shared context."""
    findings = await run_researcher(task_plan.research_query, config)
    context.add_research(task_plan.task_key, findings)
    telemetry.log(ResearchComplete(task_plan.task_key, findings))
```

---

## Execution Timeline Examples

### Simple case: 1 task, passes first try

```
t=0     PLAN (1 query call, Sonnet, ~10s)
t=10    coding_agent starts (query, Opus) — ~30s subprocess spawn
t=40    coding agent working (~60s)
t=100   candidate ready → verify (deterministic, ~5s)
t=105   verify passes → QA skipped (planner said skip_qa=true)
t=105   merge → done
Total: ~105s (vs 818s in v3 — 87% reduction)
```

No replan. No pilot reasoning beyond the initial plan.

### Multi-task: 3 tasks, 1 failure

```
t=0     PLAN (Sonnet, ~10s) → batches: [{A, B}, {C (depends on A)}]
        pilot also flags B for pre-research

t=10    EXECUTE batch 1:
        - coding_A starts (worktree_A, Opus)
        - coding_B starts (worktree_B, Opus)
        - researcher_B starts (Haiku) — parallel with coding

t=70    coding_A: candidate → verify → QA → pass → merge
t=90    coding_B: candidate → verify → FAIL
        research_B: findings ready (completed at t=50)

t=90    REPLAN (Sonnet, ~15s):
        "B failed. Research found: use express-rate-limit v7.
         A passed — project uses ESM modules.
         Replan: retry B with hint + research, then C."

t=105   EXECUTE retry:
        - coding_B starts (hint: research findings + error context + ESM learnings)

t=150   coding_B: candidate → verify → QA → pass → merge

t=150   EXECUTE batch 2:
        - coding_C starts (with learnings from A and B)

t=210   coding_C: candidate → verify → QA → pass → merge

Total: ~210s for 3 tasks (vs ~900s+ serial in v3)
```

### Full 5-task example with dependencies

```
Tasks:
  A: Build REST API          (no deps)
  B: Add logging             (no deps, needs research)
  C: Add auth middleware      (depends on A)
  D: Add rate limiting        (depends on A)
  E: Integration tests        (depends on C, D)
```

```
═══════════════ BATCH 1: [A, B] parallel ═══════════════════════
│
├─ research_B  ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
│              t=15─────t=45  → context.research["B"] (dict write)
│
├─ coding_A    ░░░░░░░░█████████████████████░░░░░░░░░░░░░░░░░░░
│              t=15───────────────────t=75 (code done)
│              ░░░░░░░░░░░░░░░░░░░░░░░░░████ verify t=75─80
│              ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░████████ QA t=80─110
│              → TaskResult(success) returned to orchestrator
│
├─ coding_B    ░░░░░░░░█████████████████████████░░░░░░░░░░░░░░░
│              t=15─────────────────────t=85 (code done)
│              ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░████ verify ✗
│              retry with hint + context.research["B"]
│              ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░██████████████
│              t=90───────────────────────────t=130 verify ✓
│              → TaskResult(success) returned to orchestrator
│
├─ asyncio.gather returns at t=130
├─ MERGE serially: A (ff-only), B (rebase + ff)    t=130─132
│
╠═ REPLAN (planner, Sonnet, ~15s)                   t=132─147
║  "A uses Express.js. B uses winston. ESM modules."
║  → updated hints for C, D
║
═══════════════ BATCH 2: [C, D] parallel ═══════════════════════
│
├─ coding_C    █████████████████████████████████████████████████
│              t=147───────code─────verify─────QA────t=245
│
├─ coding_D    ██████████████████████████████████░░░░░░░░░░░░░░
│              t=147───────code─────verify────t=210 (QA skipped)
│
├─ asyncio.gather returns at t=245
├─ MERGE serially: C, D (rebase)                    t=245─248
│
╠═ REPLAN (planner, ~15s)                           t=248─263
║
═══════════════ BATCH 3: [E] ═══════════════════════════════════
│
├─ coding_E    █████████████████████████████████████████████████
│              t=263───────code─────verify─────QA────t=365
│
├─ MERGE: E                                         t=365─366
│
▼ DONE  total: ~366s for 5 tasks (vs ~900s+ serial in v3)
```

### Compute-comms overlap

All communication is instantaneous (in-memory). No serialization overhead.

```
                COMPUTE (LLM + tools)          COMMS (data transfer)
                ══════════════════════         ═════════════════════

Planner         ████ plan()                    → ExecutionPlan (instant return)
                                                 │
Research_B      ████████                         │
                │                                → context.research["B"] = findings
                │                                  (dict write, ~0 latency)
Coding_A        ████████████████████████████      │
                │  (overlaps with research       │
                │   and coding_B — asyncio)      → TaskResult returned (~0 latency)
                │
Coding_B        ██████████████████████████████████
                │  retry reads context.research  ← dict read (~0 latency)
                │         ████████████████       → TaskResult returned
                │
Merge           ██ (serial: A then B)            → tasks.yaml updated
                │
Planner         ████ replan()                    ← context (all results + learnings)
                                                 → updated ExecutionPlan
Coding_C        ██████████████████████████████
Coding_D        ████████████████████████          (parallel — asyncio.gather)
                │
Merge           ██ (serial: C then D)
Planner         ████ replan()
Coding_E        ██████████████████████████████
Merge           █
```

**Key observations:**
- All comms are ~0 latency (dict writes, function returns). No bus, no serialization.
- Research overlaps with coding (separate asyncio tasks).
- QA overlaps across tasks within a batch (inline in each coding_loop coroutine).
- Batches are serial (dependency-driven). Within batches, tasks are parallel.
- Merges are serial (rebase-before-merge prevents ff-only conflicts).
- Planner is invoked at batch boundaries only (2-3 calls total, not 15-20 turns).

---

## Migration from v3

### What Changes

| Component | v3 | v4 |
|---|---|---|
| Orchestrator | Long-running LLM pilot session (100 turns) | Python asyncio loop + planner at batch boundaries |
| Pilot/Planner | Embedded MCP server, drives every step | Planner called 2-3 times total (plan + replans) |
| Coordination | Pilot makes sequential MCP calls | PipelineContext (in-memory shared state) + function returns |
| Coding agent | `query()` inside MCP tool | `query()` dispatched by Python orchestrator |
| QA agent | `query()` inside `run_task_with_qa()` | `query()` inline in coding_loop (per-candidate) |
| Research | Pilot dispatches via Agent tool (theoretical parallelism) | Independent `query()` call (real parallelism via asyncio) |
| Parallelism | Blocked (pilot waits on MCP) | Native (asyncio.gather + worktrees + semaphore) |
| Planner overhead (happy path) | 3-5 min (15-20 LLM turns) | ~10-15s (1 query call) |

### What Stays the Same

- `run_task()` core: branch management, candidate commits, verification tiers
- `verify.py`: Disposable worktree verification (tier 1/2/3)
- `testgen.py`: TDD test generation (if --tdd flag)
- `spec.py`: Spec generation
- `architect.py`: Architecture analysis
- `config.py`: Configuration loading
- `tasks.py`: Task CRUD with file locking
- `cli.py`: User-facing commands (add, run, status, retry, etc.)
- Git isolation model: worktrees for parallel, branches per task

### Files to Modify

- **`runner.py`**: Refactor, don't delete. Extract `coding_loop` and QA invocation as standalone async functions. Keep the per-task internals (branch management, candidate commits, session resume, cost tracking, progress emission) — v3 already has this battle-tested. Remove `run_task_with_qa()` wrapper (replaced by `coding_loop` + orchestrator coordination).
- **`cli.py`**: Gate v4 behind config. `otto run` checks `orchestrator` setting and dispatches to either `run_piloted` (v3) or `run_per` (v4). Existing CLI commands (`status`, `show`, `logs`) work with both via dual-write log strategy.
- **`display.py`**: Add v4 display mode that reads from `events.jsonl`. Keep v3 display for backward compat.

### New Files

- **`orchestrator.py`**: The PER orchestrator. `execute_plan()`, batch management, serial merge integrator (`rebase_and_merge`), signal handling, startup cleanup (`cleanup_orphaned_worktrees`).
- **`planner.py`**: ExecutionPlan/TaskPlan/Batch dataclasses, plan parsing/serialization, `plan()` and `replan()` functions that invoke `query()` for the planner LLM.
- **`telemetry.py`**: Write-only JSONL event log. Dual-writes to both new format (`events.jsonl`) and legacy formats (`pilot_results.jsonl`, `live-state.json`) during migration so existing CLI commands don't break.
- **`context.py`**: PipelineContext and TaskResult dataclasses. Shared state for the pipeline.

### Keep Existing (Rename Only)

- **`pilot.py`** → **`pilot_v3.py`**: Preserved intact for `--pilot` fallback. No modifications.

### Migration Strategy: Dual-Write Logs

The CLI is tightly coupled to v3 artifacts (`pilot_results.jsonl`, `pilot_debug.log`, `live-state.json`). During migration, the v4 telemetry layer dual-writes to both old and new formats:

```python
class Telemetry:
    def log(self, event):
        # New format
        self._write_jsonl("events.jsonl", event)
        # Legacy format (for otto status -w, otto show, otto logs)
        self._write_legacy_live_state(event)
        self._write_legacy_pilot_results(event)
```

This lets us ship v4 without touching the CLI display code. The legacy writers are removed once v3 is fully deprecated.

### Backward Compatibility

- `otto run` uses v4 by default (gated behind `orchestrator: "v4"` in otto.yaml).
- `otto run --pilot` falls back to v3 pilot (`pilot_v3.py`) for comparison/debugging.
- `otto.yaml` gains `orchestrator: "v4"` (default) vs `orchestrator: "v3"`. Existing configs without this key default to v4.
- All existing CLI commands work unchanged during transition via dual-write.

### Tracing (Pre-Implementation)

Before implementing v4, add real tracing to v3 to establish baseline measurements:
- `query()` spawn latency (time from call to first message)
- First-token latency
- Coding phase runtime
- Verify setup time (worktree creation + dep install)
- QA runtime
- Merge time

This gives us honest before/after numbers instead of estimates.

---

## Constraints & Non-Goals

### Constraints

- **Subscription only**: No Anthropic API keys. All LLM calls go through `query()` (Claude Agent SDK). Direct `anthropic` SDK calls are not available.
- **query() = subprocess**: Each `query()` spawns a Claude Code CLI subprocess with ~50K system prompt. Cannot be avoided with current SDK.
- **No subagent nesting**: AgentDefinition subagents can't spawn their own subagents.
- **query() is opaque mid-execution**: Can't inject messages into a running `query()` session. Communication happens before (prompt/hint) and after (result).

### Non-Goals for v4

- **Intra-task coding/QA overlap**: Not worth the complexity. Partial implementations aren't meaningfully testable. The coding agent already self-tests during implementation. Cross-task pipelining provides the real speedup.
- **Direct Anthropic API calls**: Requires API keys we don't have. May revisit if subscription model changes.
- **Agent-to-agent direct communication**: Agents communicate through the orchestrator (PipelineContext + function returns), not directly. Simpler, more observable, matches Temporal's pattern. Mid-execution messaging (via MCP tools/hooks) is technically possible but deferred to v5 — the ROI is low for v4's execution model.
- **Parallel TUI**: Rich per-task display panels. Deferred to v5 (see TODO.md).
- **Watch mode**: `otto watch` for auto-reimport. Separate feature.

---

## Verification Criteria

### Performance

- [ ] Fibonacci smoke test (1 trivial task): <120s (vs 818s in v3)
- [ ] 3-task multi-task run with no failures: <5 min wall time
- [ ] Happy path (all tasks pass first try): pilot makes exactly 1 `query()` call (PLAN only)
- [ ] Parallel tasks run concurrently (test with 2 tasks >3 min each — wall time should be significantly less than 2x)

### Correctness

- [ ] All existing smoke tests pass (fibonacci, bugfix, express-api, multi-task)
- [ ] Failed tasks trigger REPLAN with error context
- [ ] Research findings are available for retry hints
- [ ] Cross-task learnings propagate between batches
- [ ] Conditional QA works (simple tasks skip QA, complex tasks get full QA)
- [ ] Git state is clean after run (no orphaned branches, no dirty tree)
- [ ] Session resume works — retries reuse session_id, avoid full restart

### Robustness

- [ ] Ctrl+C during a 2-task parallel batch: all worktrees cleaned up within 10s, no orphaned subprocesses
- [ ] After forced kill (`kill -9`), next `otto run` recovers: detects orphaned worktrees, cleans them up, resets stuck "running" tasks to "pending"
- [ ] Malformed JSON from pilot plan triggers sensible fallback (sequential execution), not a crash
- [ ] Concurrent `query()` calls gated by semaphore — 3 parallel tasks don't trigger rate limit errors
- [ ] Synchronous git operations wrapped in `to_thread` — don't block asyncio event loop

### Observability

- [ ] `otto status -w` shows live progress from Telemetry JSONL
- [ ] `otto logs <task>` shows per-task agent output
- [ ] Agent tool calls (Read, Write, Bash) surfaced to display during coding phase
- [ ] Events JSONL is a complete audit trail (replayable)
- [ ] Phase timing displayed in run summary (plan, coding, verify, qa, merge per task)
- [ ] Cost tracking works (per-task and total)

### Backward Compatibility

- [ ] `otto run --pilot` falls back to v3 behavior (keep pilot.py as pilot_v3.py)
- [ ] `otto add`, `otto status`, `otto retry`, `otto diff`, `otto show` unchanged
- [ ] `tasks.yaml` format unchanged
- [ ] `otto.yaml` additions are backward-compatible (new keys with defaults)

---

## Review Findings & Resolutions

Design reviewed adversarially by both code-reviewer agent and Codex (GPT-5.4) against the v3 codebase. Codex endorsed PER as "the right direction" but identified critical issues. All findings and resolutions below.

### From Codex Review

**CRITICAL: Parallel merge is broken (ff-only fails for same-batch tasks).** Tasks branching from the same base can't both `--ff-only` merge. Resolution: "code in parallel, integrate serially" — added `rebase_and_merge` step in the orchestrator. See Phase 2 above.

**CRITICAL: EventChannel as control plane is unreliable.** Events emitted before waiters register are lost. Resolution: split into Telemetry (write-only JSONL log) + PipelineContext (in-memory shared state). See Coordination & Telemetry section above.

**HIGH: Failure-only replanning loses cross-task learning.** Resolution: replan at every batch boundary (both success and failure). See Phase 3 above.

**HIGH: Prompt size savings overstated.** v3 already uses custom system_prompt. `query()` still loads full Claude Code prompt underneath. Resolution: corrected claims throughout spec. The real win is fewer `query()` calls.

**HIGH: CLI tightly coupled to v3 log formats.** Resolution: dual-write telemetry layer during migration. See Migration Strategy above.

**MEDIUM: JSONL described as both "audit log" and "recovery journal."** Resolution: JSONL is telemetry only. Authoritative state is `tasks.yaml`. See Open Question #3.

**Codex design recommendations adopted:**
- "Code in parallel, integrate serially" as first-class rule
- Refactor existing per-task loop rather than deleting it
- Add real tracing before implementing v4
- Dual-write logs during migration
- Gate v4 behind config, don't flip default immediately

### From Code-Reviewer Agent

### Critical: Recursive execute_plan loses context

**Problem**: The original design called `execute_plan` recursively on replan, losing context and having no depth limit.

**Resolution**: Use an iterative loop (already reflected in Phase 2 code above).

### Critical: No cancellation / signal handling

**Problem**: Ctrl+C during concurrent `asyncio.gather` orphans `query()` subprocesses and leaves worktrees dirty.

**Resolution**: The orchestrator installs signal handlers that:
1. Set a cancellation flag checked by each `coding_loop` between phases (after coding, after verify, after QA).
2. Send SIGTERM to all running `query()` subprocesses (tracked via PID).
3. Clean up worktrees in a `finally` block.
4. Write partial results to Telemetry before exit.

```python
async def execute_plan(...):
    interrupted = False
    pids: set[int] = set()  # track query() subprocess PIDs

    def _on_signal(signum, frame):
        nonlocal interrupted
        interrupted = True
        for pid in pids:
            os.kill(pid, signal.SIGTERM)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        # ... main loop ...
        # Each coding_loop checks `interrupted` between phases
    finally:
        cleanup_all_worktrees(project_dir)
```

### Critical: No concurrency control for query() calls

**Problem**: Multiple concurrent `query()` calls may hit subscription rate limits.

**Resolution**: Gate all `query()` calls behind `asyncio.Semaphore(config.get("max_parallel", 2))`. Default to 2 (conservative). The semaphore is passed to all agent loops.

```python
sem = asyncio.Semaphore(config.get("max_parallel", 2))

async def run_coding_agent_throttled(task, config, worktree, hint):
    async with sem:
        return await run_coding_agent(task, config, worktree, hint)
```

### Critical: Worktree crash cleanup on startup

**Problem**: If the orchestrator crashes mid-run, worktrees are left behind.

**Resolution**: Add a startup cleanup phase that prunes orphaned worktrees:

```python
def cleanup_orphaned_worktrees(project_dir):
    """Called at the start of every run. Removes worktrees from dead runs."""
    worktrees_dir = project_dir / ".worktrees"
    if not worktrees_dir.exists():
        return
    for wt in worktrees_dir.iterdir():
        if wt.name.startswith("otto-"):
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                           cwd=project_dir, capture_output=True)
```

Also reset tasks stuck in "running" status to "pending" (already done in v3 pilot.py line 1046-1051).

### Important: Sync git operations block the event loop

**Problem**: `build_candidate_commit`, `run_verification`, `reset_to_base` are `subprocess.run` calls that block the asyncio event loop during parallel execution.

**Resolution**: Wrap blocking subprocess calls in `asyncio.to_thread()`:

```python
candidate_sha = await asyncio.to_thread(build_candidate_commit, worktree, task)
verify_result = await asyncio.to_thread(run_verification, worktree, candidate_sha, config)
```

### Important: QA runs inline, not as a separate watcher

**Problem** (original design): A single `qa_watcher` would serialize QA across tasks.

**Resolution**: QA runs inline within each `coding_loop` coroutine, gated by the same semaphore as coding. Since multiple coding_loops run via `asyncio.gather`, QA for different tasks overlaps naturally. When `skip_qa=True`, the QA step is simply skipped — no separate event routing needed.

### Important: Cost tracking mechanism

**Problem**: Not specified how costs flow through the pipeline.

**Resolution**: Each `run_coding_agent` / `run_qa_agent` / `run_researcher` extracts `total_cost_usd` from `ResultMessage` and returns it. The orchestrator accumulates costs in `PipelineContext` and writes them to `tasks.yaml` via `update_task`.

### Important: Session resume for retries

**Problem**: Each retry spawns a fresh `query()` call, paying full startup overhead.

**Resolution**: Store `session_id` from `ResultMessage` in `PipelineContext`. Pass it to retry calls via `agent_opts.resume = session_id`. This allows the coding agent to resume its context on retry instead of starting fresh.

### Important: Live observability of agent tool calls

**Problem**: v3's `on_progress` callback system surfaces agent tool calls (Read, Write, Bash) for display. v4 drops this.

**Resolution**: The `coding_loop` streams `query()` messages and logs significant tool calls to Telemetry:

```python
async for message in query(prompt=..., options=...):
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                telemetry.log(AgentToolCall(task_key, block.name, summary(block)))
```

The display layer tails the Telemetry JSONL for live progress output.

### Important: Fibonacci estimate correction

**Problem**: The ~75s estimate doesn't account for subprocess spawn overhead (~30s).

**Resolution**: Corrected estimate: **~105s** (10s plan + 30s spawn + 60s coding + 5s verify). Still a major improvement over 818s (87% reduction), but honest about the subprocess tax.

---

## Future Work (v5)

Deferred from v4 to keep scope manageable:

- **Mid-execution agent communication**: Agents can technically receive messages between tool calls (via MCP tools, hooks, or file-polling). This would enable research findings to reach a coding agent during its first attempt, not just on retry. The mechanism: give agents a `check_messages()` MCP tool or instruct them to periodically `Read` a known message file. Deferred because the ROI is low — agent runs are 60-120s, and the between-execution model (hints on start, results on finish) handles 90% of the value.

- **Parallel TUI**: Rich per-task display panels with `rich.Live`. Keypress toggles for compact/panel/focus modes. Requires multi-task progress tracking from Telemetry JSONL.

- **Crash resume from JSONL**: Parse `events.jsonl` on startup to detect partially-completed runs and resume from the last good state. Requires idempotent merge logic.

- **Intra-task coding/QA overlap**: QA starts testing partial implementations as the coding agent works. Technically possible but complexity outweighs benefit — partial implementations aren't meaningfully testable.


---

## Open Questions

1. **Pilot plan format**: Structured JSON with a `notes` field for free-form reasoning. If the pilot outputs malformed JSON, fall back to a default plan (sequential execution, no research, no skip_qa) with a warning. Don't crash.

2. **Rate limits**: Default `max_parallel` to 2. Characterize subscription concurrency limits empirically with the smoke test suite. Adjust default based on findings.

3. **Session resumption on crash**: The JSONL event log is telemetry only — NOT an authoritative journal. Authoritative state lives in `tasks.yaml` (task status, attempts, cost, session_id). On crash recovery, v4.0 reads `tasks.yaml` to find completed tasks (already merged) and resets "running" tasks to "pending". The JSONL log is useful for post-mortem debugging but not for replay. Crash resume from JSONL deferred to v4.1 if needed.
