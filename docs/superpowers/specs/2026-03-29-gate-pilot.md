# Otto: Gate Pilot — LLM Intelligence at Decision Points

**Date**: 2026-03-29
**Status**: Design
**Builds on**: v4.5 pipeline (implemented), intent-to-product spec (design)
**Context**: Evolved from brainstorming session comparing CC agent teams, daemon orchestrators, and shared discovery boards. The gate pilot is the middle ground between v3's always-on LLM pilot (too much) and v4's replan-only LLM (too little).

---

## The Problem

v4.5's orchestrator makes two kinds of decisions:

**Mechanical decisions** (deterministic Python handles well):
- Batch ordering, worktree creation, merge, retry counting, signal handling

**Semantic decisions** (need LLM judgment):
- Why did this task fail? (environment issue vs code bug vs spec ambiguity)
- What should the retry strategy be? (same approach vs different angle vs skip)
- Which learnings from task A are relevant to task B?
- After a gate: did the natural-language verify condition actually pass?
- Is this failure a glue bug, architecture mismatch, or design flaw?

Today, the replan step handles semantic decisions — but barely. It gets pass/fail status and re-batches. It doesn't interpret failures, route context, or decide repair strategies.

For intent-to-product, the semantic decisions multiply: gate verification against natural-language conditions, failure triage across three severity levels, architecture revision, re-spec of affected tasks. The replan step can't scale to this.

---

## The Gate Pilot

An LLM session invoked at **decision boundaries** — not always-on, not a daemon, not watching execution in real-time. Stateless: reconstructs context from files on disk every invocation.

### When it activates

```
                    ┌─────────────────────────────────┐
                    │   DETERMINISTIC ORCHESTRATOR     │
                    │   (Python — runs everything)     │
                    └──────────┬──────────────────────┘
                               │
            ┌──────────────────┼──────────────────────┐
            ▼                  ▼                       ▼
     BATCH BOUNDARY      GATE BOUNDARY          PRODUCT QA
     (between batches     (between gates         (after all
      within a gate)       in i2p mode)           gates pass)
            │                  │                       │
            ▼                  ▼                       ▼
    ┌───────────────┐  ┌───────────────┐  ┌────────────────┐
    │  GATE PILOT   │  │  GATE PILOT   │  │   GATE PILOT   │
    │  (planner     │  │  (planner     │  │   (planner     │
    │   model)      │  │   model)      │  │    model)      │
    │               │  │               │  │                │
    │ • Interpret   │  │ • Verify gate │  │ • Interpret    │
    │   failures    │  │   condition   │  │   journey      │
    │ • Route       │  │ • Classify    │  │   results      │
    │   learnings   │  │   failures    │  │ • Classify     │
    │ • Decide      │  │ • Decide      │  │   failures     │
    │   retry       │  │   repair      │  │ • Recommend    │
    │   strategy    │  │   strategy    │  │   fixes        │
    └───────┬───────┘  └───────┬───────┘  └───────┬────────┘
            │                  │                   │
            ▼                  ▼                   ▼
    Structured decision  Structured decision  Structured decision
    back to orchestrator back to orchestrator back to orchestrator
```

### What it does NOT do

- Drive execution (orchestrator does that)
- Spawn or manage agents (orchestrator does that)
- Make trivial decisions (retry count, merge, worktree lifecycle)
- Run continuously between decision points
- Maintain persistent state (reads files each invocation)
- Interpret per-task results FOR the coding agent (no telephone game — QA verdicts, merge diffs, and error logs go directly to the coding agent, not through the pilot. The coding agent interprets its own context. The pilot only makes system-level decisions that span across tasks.)

---

## Context Assembly (How the Pilot Knows What Happened)

The pilot is stateless. Every invocation, the orchestrator assembles context from files already on disk:

```python
def assemble_pilot_context(
    gate: Gate | None,
    task_results: list[TaskResult],
    project_dir: Path,
    mode: str,  # "batch_boundary" | "gate_boundary" | "product_qa"
) -> str:
    """Build the gate pilot's context from disk artifacts."""
    sections = []

    # 1. Original plan rationale
    planner_log = project_dir / "otto_logs" / "planner.log"
    if planner_log.exists():
        sections.append(("Plan rationale", planner_log.read_text()[-2000:]))

    # 2. Architecture (if exists — i2p or user-provided)
    arch = project_dir / "otto_arch" / "architecture.md"
    if arch.exists():
        sections.append(("Architecture", arch.read_text()))

    # 3. Task results from this batch/gate
    for tr in task_results:
        task_dir = project_dir / "otto_logs" / tr.task_key
        parts = [f"Task: {tr.task_key} — {'PASSED' if tr.passed else 'FAILED'}"]

        summary = task_dir / "task-summary.json"
        if summary.exists():
            parts.append(summary.read_text())

        if not tr.passed:
            # Include last attempt's verify log (the actual error)
            verify_logs = sorted(task_dir.glob("attempt-*-verify.log"))
            if verify_logs:
                parts.append(f"Error output:\n{verify_logs[-1].read_text()[-3000:]}")

            # Include QA verdict if QA ran
            qa_verdict = task_dir / "qa-verdict.json"
            if qa_verdict.exists():
                parts.append(f"QA verdict:\n{qa_verdict.read_text()}")

        sections.append((tr.task_key, "\n".join(parts)))

    # 4. Cross-task learnings accumulated so far
    learnings = project_dir / "otto_logs" / "learnings.jsonl"
    if learnings.exists():
        sections.append(("Learnings", learnings.read_text()[-2000:]))

    # 5. User messages (from otto chat, if any)
    control = project_dir / "otto_logs" / "control.jsonl"
    if control.exists():
        unread = _get_unread_commands(control)
        if unread:
            sections.append(("User guidance", "\n".join(unread)))

    # 6. Gate verify condition (gate_boundary mode only)
    if gate and mode == "gate_boundary":
        sections.append(("Gate verify condition", gate.verify))

    # 7. Product spec + user journeys (product_qa mode only)
    if mode == "product_qa":
        spec = project_dir / "otto_arch" / "product-spec.md"
        if spec.exists():
            sections.append(("Product spec", spec.read_text()))

    return _format_sections(sections)
```

### Prerequisites: files that must be persisted (not yet done)

These are items from `next-improvements.md` that the gate pilot depends on:

| File | Current state | Needed for pilot |
|---|---|---|
| `learnings.jsonl` | In-memory only | Must persist to disk |
| `attempt-N` structured records | Partial (logs exist, no structured summary) | Need `task-summary.json` per task (exists) |
| `retry-context-{n}.md` | Not persisted | Nice-to-have, not blocking |

`learnings.jsonl` persistence is the only hard prerequisite. The rest already exists.

---

## Structured Output (What the Pilot Returns)

The pilot doesn't take actions. It returns structured decisions the orchestrator executes.

### At batch boundaries (within a gate)

```python
@dataclass
class BatchDecision:
    """Gate pilot's decision at a batch boundary."""

    # Per-task retry strategy
    retry_strategies: dict[str, RetryStrategy]
    # Tasks to skip (dependency on failed assumption)
    skip_tasks: list[str]
    # Learnings to inject into specific upcoming tasks
    routed_context: dict[str, list[str]]  # task_key → context lines
    # Semantic failure notes (for logging/debugging)
    failure_analysis: dict[str, str]  # task_key → why it failed
    # Revised batch ordering (if needed)
    next_batch: list[str]  # task keys for next batch

@dataclass
class RetryStrategy:
    action: str  # "retry_same" | "retry_different" | "skip" | "abort"
    guidance: str  # e.g., "project uses vitest not jest — use vitest"
    reason: str   # e.g., "environment issue, not a code bug"
```

### At gate boundaries (between gates, i2p mode)

```python
@dataclass
class GateDecision:
    """Gate pilot's decision at a gate boundary."""

    # Did the gate's verify condition pass?
    gate_passed: bool
    gate_evidence: str  # what the pilot checked and found

    # Regression results (prior gate proofs)
    regressions: list[RegressionFailure]

    # If failed: failure classification per issue
    failures: list[ClassifiedFailure]

    # Repair plan
    fix_tasks: list[str]  # new task prompts to create
    architecture_updates: str | None  # changes to architecture.md
    needs_replan: bool  # true = design_flaw, re-invoke product planner
    replan_context: str | None  # what to tell the planner

@dataclass
class ClassifiedFailure:
    description: str
    type: str  # "glue_bug" | "architecture_mismatch" | "design_flaw"
    affected_tasks: list[str]
    recommended_fix: str

@dataclass
class RegressionFailure:
    proof_script: str
    gate_origin: str  # which gate this proof came from
    error_output: str
```

### At product QA (after all gates, i2p mode)

Same as `GateDecision` but with journey-level results:

```python
@dataclass
class ProductQADecision:
    product_passed: bool
    journey_results: list[JourneyResult]
    failures: list[ClassifiedFailure]  # same classification
    fix_tasks: list[str]
    architecture_updates: str | None
    needs_replan: bool
    replan_context: str | None

@dataclass
class JourneyResult:
    name: str  # "Save & Search"
    passed: bool
    steps: list[JourneyStep]
    failure_description: str | None
```

---

## Orchestrator Integration

The orchestrator calls the pilot and acts on its structured output. The pilot never touches files or state directly.

### At batch boundaries

```python
# In orchestrator.py, after a batch completes with failures
async def handle_batch_results(batch_results, remaining_tasks, context, config):
    failed = [r for r in batch_results if not r.passed]

    if not failed:
        return  # all passed, proceed

    # Invoke gate pilot
    pilot_context = assemble_pilot_context(
        gate=current_gate,
        task_results=batch_results,
        project_dir=config["project_dir"],
        mode="batch_boundary",
    )
    decision: BatchDecision = await invoke_pilot(pilot_context, mode="batch")

    # Act on structured output
    for task_key in decision.skip_tasks:
        update_task(tasks_file, task_key, status="skipped")
        _orchestrator_log(project_dir, f"pilot: skip {task_key} — {decision.failure_analysis.get(task_key)}")

    for task_key, strategy in decision.retry_strategies.items():
        if strategy.action == "retry_different":
            # Inject guidance into retry prompt
            context.add_learning(Learning(
                text=strategy.guidance,
                source="gate_pilot",
                provenance="inferred",
            ))
        elif strategy.action == "abort":
            update_task(tasks_file, task_key, status="failed")

    for task_key, lines in decision.routed_context.items():
        for line in lines:
            context.add_learning(Learning(
                text=f"[from pilot, for {task_key}] {line}",
                source="gate_pilot",
                provenance="observed",
            ))

    # Log all decisions for debugging
    _orchestrator_log(project_dir, f"pilot decision: {json.dumps(asdict(decision), indent=2)}")
```

### At gate boundaries (i2p mode)

```python
# In the gated execution loop
async def run_gated(gates, config, tasks_file, project_dir):
    prior_proofs = []

    for gate in gates:
        write_gate_tasks(tasks_file, gate.tasks)
        await run_per(config, tasks_file, project_dir)

        # Invoke gate pilot for verification
        pilot_context = assemble_pilot_context(
            gate=gate,
            task_results=get_gate_results(gate, project_dir),
            project_dir=project_dir,
            mode="gate_boundary",
        )
        decision: GateDecision = await invoke_pilot(pilot_context, mode="gate")

        if not decision.gate_passed:
            for round in range(3):  # circuit breaker
                # Apply repairs
                if decision.architecture_updates:
                    arch_path = project_dir / "otto_arch" / "architecture.md"
                    arch_path.write_text(decision.architecture_updates)

                if decision.needs_replan:
                    await reinvoke_product_planner(
                        project_dir, config, decision.replan_context
                    )
                    break  # restart from revised plan

                for fix_prompt in decision.fix_tasks:
                    add_task(tasks_file, prompt=fix_prompt)

                await run_per(config, tasks_file, project_dir)

                # Re-verify
                decision = await invoke_pilot(
                    assemble_pilot_context(gate=gate, ...),
                    mode="gate",
                )
                if decision.gate_passed:
                    break

        prior_proofs.extend(decision.proof_scripts)
```

---

## The Pilot LLM Call

One LLM call per invocation via `_run_planner_prompt()` — same model as the planner (configurable via `planner_model` in otto.yaml, defaults to user's CC default). Cheap (~$0.01-0.03), fast (~5-15s).

```python
async def invoke_pilot(context: str, mode: str, config: dict, project_dir: Path) -> BatchDecision | GateDecision | ProductQADecision:
    """Invoke the gate pilot. Returns structured decision."""

    prompt = PILOT_PROMPTS[mode].format(context=context)

    raw_output, cost_usd, duration_s = await _run_planner_prompt(
        prompt, config, project_dir,
        model=_planner_model(config),
        effort=_planner_effort(config),
    )

    return parse_decision(raw_output, mode)
```

### System prompt

```
You are otto's gate pilot — an LLM that makes semantic decisions at execution
boundaries. You do NOT execute anything. You analyze what happened and return
structured decisions.

Your job:
1. Interpret task results — WHY did things pass or fail (not just that they did)
2. Classify failures — glue_bug (simple fix), architecture_mismatch (contract wrong),
   design_flaw (fundamental approach wrong)
3. Route context — what did passing tasks discover that failing/upcoming tasks need?
4. Decide strategy — retry same approach, try different approach, skip, or abort

Principles:
- Try the cheapest fix first (glue_bug before architecture_mismatch before design_flaw)
- Don't guess — if the error logs don't tell you why, say "unclear"
- Be specific in guidance — "use vitest not jest" beats "try a different test framework"
- Skip tasks whose dependencies are fundamentally broken (don't waste retries)
- Route discoveries proactively — if task A found something task B needs, say so

Return your decision as structured JSON matching the schema provided.
```

### Mode-specific prompts

**Batch boundary:**
```
A batch of tasks just completed. Analyze the results and decide what to do next.

{context}

Return a BatchDecision:
- retry_strategies: for each failed task, what should the retry do differently?
- skip_tasks: which upcoming tasks should be skipped (broken dependency)?
- routed_context: what should specific upcoming tasks know?
- failure_analysis: why did each task fail (one sentence)?
- next_batch: task ordering for the next batch
```

**Gate boundary:**
```
A verification gate just completed. Determine if the gate condition is met
and classify any failures.

{context}

Gate verify condition: "{gate.verify}"

Return a GateDecision:
- gate_passed: does the codebase satisfy the verify condition?
- failures: classify each issue (glue_bug / architecture_mismatch / design_flaw)
- fix_tasks: prompts for fix tasks (if glue_bug or architecture_mismatch)
- architecture_updates: revised architecture.md content (if architecture was wrong)
- needs_replan: true only if the fundamental approach is wrong
```

---

## Incremental Rollout

The gate pilot can be introduced in stages. Each stage is independently useful.

### Stage 1: Smarter replan (v4.5, no i2p needed)

Replace the current `replan()` call with a gate pilot invocation at batch boundaries.

**What changes:**
- `orchestrator.py`: after batch failure, call `invoke_pilot()` instead of `replan()`
- New file: `otto/pilot.py` — context assembly + pilot invocation + decision parsing
- `learnings.jsonl` persisted to disk (prerequisite)

**What improves immediately:**
- Retries get semantic guidance ("use vitest not jest") instead of raw errors
- Upcoming tasks get routed context from completed tasks
- Doomed tasks get skipped instead of wasting retries
- Failure analysis logged for debugging

**Cost:** one LLM call (planner model) per batch boundary (~$0.01-0.03). Already paying for replan.

### Stage 2: Gate verification (requires i2p)

Add gate boundary invocations with verification conditions and failure classification.

**What changes:**
- Gate pilot invoked between gates (not just batches)
- Failure triage: glue_bug → architecture_mismatch → design_flaw
- Fix task generation from pilot output
- Architecture revision from pilot output

**Depends on:** i2p spec (product planner produces gated task graph)

### Stage 3: Product QA integration (requires i2p)

Gate pilot interprets product QA results and recommends repairs.

**What changes:**
- Pilot invoked after product QA with journey results
- Same failure classification applied to journey-level failures
- Repair loop: fix → re-verify → fix (max 3 rounds)

### Stage 4: Interactive mode (otto chat)

Gate pilot's decisions presented to user instead of auto-executed.

**What changes:** See "Interactive Mode" section below.

---

## Interactive Mode (otto chat)

`otto chat` is an interactive interface to the same system. The gate pilot's decisions are presented to the user instead of auto-executed.

### Architecture

```
┌──────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  otto chat   │     │    control.jsonl      │     │  orchestrator   │
│  (CC session │────▶│  (file on disk)       │◀────│  (background)   │
│   with user) │     │                       │     │                 │
│              │◀────│  v4_events.jsonl      │────▶│  reads control  │
│  reads logs  │     │  otto_logs/*          │     │  at boundaries  │
└──────────────┘     └──────────────────────┘     └─────────────────┘
```

No sockets, no IPC, no message bus. File-based communication.

### Background execution

```python
# otto/cli.py
@cli.command()
def run_background():
    """Run otto in background, write PID for lifecycle management."""
    pid_file = project_dir / "otto_logs" / "otto.pid"
    pid_file.write_text(str(os.getpid()))

    try:
        asyncio.run(run_per(config, tasks_file, project_dir))
    finally:
        pid_file.unlink(missing_ok=True)
```

```bash
otto run --background
# or
otto run &
```

### Control channel

```python
# otto_logs/control.jsonl — chat writes, orchestrator reads

# Chat writes:
{"ts": "2026-03-29T10:00:00Z", "id": "c1", "action": "skip_task", "task_key": "chrome-extension", "reason": "not needed for v1"}
{"ts": "2026-03-29T10:01:00Z", "id": "c2", "action": "inject_context", "target": "task-4", "message": "API prefix is /v1/ not /api/"}
{"ts": "2026-03-29T10:02:00Z", "id": "c3", "action": "pause_after_gate", "gate": "core_features"}
{"ts": "2026-03-29T10:03:00Z", "id": "c4", "action": "update_architecture", "section": "API Design", "content": "All endpoints under /v1/ prefix"}
{"ts": "2026-03-29T10:04:00Z", "id": "c5", "action": "resume"}
{"ts": "2026-03-29T10:05:00Z", "id": "c6", "action": "abort", "reason": "wrong direction, starting over"}

# Orchestrator reads at boundaries:
{"ts": "2026-03-29T10:00:05Z", "id": "c1", "ack": true, "applied_at": "batch_boundary"}
```

### Orchestrator reads control channel

```python
# In orchestrator.py, at every boundary
def process_control_commands(project_dir: Path, tasks_file: Path, context: PipelineContext) -> str | None:
    """Process pending user commands. Returns 'pause' or 'abort' or None."""
    control = project_dir / "otto_logs" / "control.jsonl"
    if not control.exists():
        return None

    for cmd in read_unacked_commands(control):
        if cmd["action"] == "skip_task":
            update_task(tasks_file, cmd["task_key"], status="skipped")
            ack_command(control, cmd["id"])

        elif cmd["action"] == "inject_context":
            context.add_learning(Learning(
                text=cmd["message"],
                source="user",
                provenance="observed",
            ))
            ack_command(control, cmd["id"])

        elif cmd["action"] == "pause_after_gate":
            ack_command(control, cmd["id"])
            return "pause"

        elif cmd["action"] == "update_architecture":
            arch = project_dir / "otto_arch" / "architecture.md"
            # Update specific section or append
            _update_arch_section(arch, cmd["section"], cmd["content"])
            ack_command(control, cmd["id"])

        elif cmd["action"] == "abort":
            ack_command(control, cmd["id"])
            return "abort"

        elif cmd["action"] == "resume":
            ack_command(control, cmd["id"])

    return None
```

### Chat session

`otto chat` launches a Claude Code session with otto-aware context:

```python
# otto/cli.py
@cli.command()
def chat():
    """Interactive chat with otto — monitor, steer, and query execution."""
    # Ensure execution is running or has run
    if not (project_dir / "otto_logs").exists():
        console.print("No otto run found. Start one with: otto run --background")
        return

    system_prompt = f"""
You are otto's interactive interface. The user can ask you about the current
otto run and give you commands to steer execution.

## What you can READ (use Read/Grep/Glob tools):
- otto_logs/v4_events.jsonl — event stream (grep for latest events)
- otto_logs/orchestrator.log — orchestrator decisions
- otto_logs/planner.log — original plan rationale
- otto_logs/{{task_key}}/ — per-task logs (agent.log, verify.log, qa-verdict.json)
- tasks.yaml — current task list with status
- otto_arch/ — product spec, architecture (if i2p mode)

## What you can WRITE:
- otto_logs/control.jsonl — append commands to steer execution
  Actions: skip_task, inject_context, pause_after_gate, update_architecture, resume, abort

## How to answer questions:
- "what's happening?" → read v4_events.jsonl tail + tasks.yaml status
- "why did X fail?" → read otto_logs/X/attempt-N-verify.log + qa-verdict.json
- "how much has it cost?" → read otto_logs/run-history.jsonl or task-summary.json files
- "what's the plan?" → read otto_logs/planner.log + tasks.yaml

## How to steer execution:
- Write a JSON line to otto_logs/control.jsonl with the command
- Commands take effect at the next batch/gate boundary (not immediately)
- Tell the user when the command will take effect

## Important:
- Execution runs in background. You are not driving it.
- Your commands are advisory — the orchestrator applies them at boundaries.
- If execution is not running, say so. Don't pretend things are happening.
- Read actual files before answering. Don't guess from memory.
"""

    # Launch interactive CC session
    subprocess.run([
        "claude",
        "--system-prompt", system_prompt,
        "--cwd", str(project_dir),
    ])
```

### What otto chat looks like

```
$ otto chat

You: what's happening?

Otto: Run started 3 minutes ago. Currently executing gate 1 (foundation).
      - Task 1 (project setup): PASSED ✓ (45s, $0.08)
      - Task 2 (CRUD API): RUNNING (2m in)
      - Task 3 (data model): WAITING (depends on task 1, will start after 2)

You: skip the chrome extension task

Otto: Written skip command for task 7 (chrome-extension) to control.jsonl.
      This will take effect at the next batch boundary. Gate 3 will now
      only have task 8 (import/export).

You: why did task 4 fail earlier?

Otto: Task 4 (search) failed on attempt 1. From the verify log:

      FAILED: test_search_returns_results
      > sqlite3.OperationalError: no such module: fts5

      The test environment doesn't have SQLite compiled with FTS5 support.
      The gate pilot classified this as an environment issue and injected
      guidance for attempt 2: "Check for FTS5 availability at startup,
      fall back to LIKE queries if unavailable."

      Attempt 2 is running now.

You: change the API prefix from /api/ to /v1/

Otto: Updated architecture.md with /v1/ prefix. Written inject_context
      command for all unstarted tasks that reference API endpoints.
      Tasks already merged (2, 3) still use /api/ — I've created a fix
      task prompt that the orchestrator will pick up: "Update all API
      route prefixes from /api/ to /v1/ to match revised architecture."
```

---

## Relationship to Other Specs

### How the gate pilot fits into i2p execution

```
otto build "bookmark manager"
  │
  ▼
PRODUCT PLANNER (one query() call)
  → product-spec.md, architecture.md, tasks.yaml with gates
  │
  ▼
FOR EACH GATE:
  │
  ├─ ORCHESTRATOR runs gate's tasks via run_per()
  │    │
  │    ├─ Per task: coding agent → clean test → QA (v4.5, unchanged)
  │    │
  │    └─ BATCH BOUNDARY: ──▶ GATE PILOT (batch mode)
  │         "task A failed because X, route Y to task B, skip task C"
  │         orchestrator applies decisions, continues
  │
  └─ GATE BOUNDARY: ──▶ GATE PILOT (gate mode)
       "verify condition met? classify failures, recommend repairs"
       orchestrator applies: fix tasks / arch update / replan
  │
  ▼
ALL GATES PASSED
  │
  ▼
PRODUCT QA: ──▶ GATE PILOT (product_qa mode)
  "journey results: 2/3 passed, search broken, classify + recommend"
  orchestrator applies fixes, re-verifies (max 3 rounds)
  │
  ▼
DONE — or report failures
```

### What this replaces

| Current component | Replaced by | Notes |
|---|---|---|
| `replan()` in planner.py | Gate pilot (batch mode) | Same position, richer input/output |
| Manual failure interpretation | Gate pilot failure analysis | Logged to orchestrator.log |
| Flat `last_error` in retries | Routed context from pilot | Semantic guidance per task |

### What this does NOT replace

| Component | Why it stays |
|---|---|
| `plan()` / `default_plan()` | Initial planning is a separate concern |
| `coding_loop()` | Per-task execution unchanged |
| `run_per()` | Orchestrator loop unchanged |
| QA agent | Per-task spec verification unchanged |
| Spec agent | Per-task spec generation unchanged |

---

## Implementation Plan

### Stage 1: Smarter replan (1-2 days)

**Prerequisites:**
- Persist `learnings.jsonl` to disk (currently in-memory)

**New files:**
- `otto/pilot.py` — context assembly, pilot invocation, decision parsing

**Modified files:**
- `otto/orchestrator.py` — call pilot at batch boundaries instead of replan
- `otto/context.py` — persist learnings to JSONL on disk

**Verification:**
- Run pressure test suite, compare retry success rate before/after
- Check orchestrator.log for pilot decisions — are they reasonable?
- Measure: does routed context reduce same-batch peer failures?

### Stage 2: Gate verification (requires i2p, 2-3 days)

**New in pilot.py:**
- Gate boundary invocation mode
- Failure classification (glue/mismatch/flaw)
- GateDecision output parsing

**Modified files:**
- `otto/orchestrator.py` — gate verification loop with pilot
- `otto/cli.py` — `otto build` command

**Verification:**
- Run `otto build` on 5 product descriptions
- Check: does the pilot correctly identify when gates pass/fail?
- Check: are failure classifications reasonable?

### Stage 3: Interactive mode (1-2 days)

**New files:**
- `otto/control.py` — control channel read/write/ack

**Modified files:**
- `otto/orchestrator.py` — check control.jsonl at boundaries
- `otto/cli.py` — `otto chat` command, `otto run --background`

**Verification:**
- Run otto in background, use otto chat to skip a task mid-run
- Verify command is acked and applied at next boundary
- Verify chat can read logs and answer questions about current state

---

## Cost

| Invocation | Model | Estimated cost | Frequency |
|---|---|---|---|
| Batch boundary | planner_model (configurable) | $0.01-0.03 | Per batch with failures |
| Gate boundary | planner_model (configurable) | $0.02-0.05 | Per gate (i2p only) |
| Product QA | planner_model (configurable) | $0.03-0.05 | Once per run (i2p only) |

For a typical v4.5 run (3-5 tasks, 1-2 batch boundaries): +$0.02-0.06 total.
For an i2p run (8-12 tasks, 3 gates): +$0.10-0.20 total.

Negligible compared to coding agent costs ($0.30-2.00 per task).

---

## Open Questions

1. **Pilot hallucination.** The pilot classifies failures and recommends strategies. What if it's wrong? The orchestrator should validate: e.g., if pilot says "skip task X" but X has no failed dependencies, ignore the skip. How much validation is needed?

2. **Context window pressure.** For large runs with many tasks, the assembled context could be large. Should the orchestrator summarize old task results or only include the current batch + recent history?

3. **Pilot vs full agent.** The current design uses `max_turns=1` (single reasoning step, no tools). Should the pilot be able to read additional files if it needs more context? That makes it a mini-agent instead of a pure reasoning call.

4. **Replan compatibility.** Stage 1 replaces replan with the pilot. Should replan be kept as a fallback? Or is the pilot strictly better?

---

## References

- i2p spec: `docs/superpowers/specs/2026-03-26-otto-intent-to-product.md`
- v4.5 pipeline: `docs/superpowers/specs/2026-03-22-otto-v4.5-pipeline-redesign.md`
- v5 early thinking: `docs/superpowers/specs/2026-03-21-otto-v5-early-thinking.md`
- Next improvements: `docs/next-improvements.md`
- v3 pilot (cautionary tale): `docs/superpowers/plans/2026-03-18-otto-v3-design.md`
- CC agent teams research: `~/work/everyday_misc/research-claude-code-agent-teams.md`
