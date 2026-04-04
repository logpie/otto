# Gate Pilot Stage 1: Smarter Replan — Implementation Plan

**Date**: 2026-03-29
**Spec**: `docs/superpowers/specs/2026-03-29-gate-pilot.md`
**Scope**: Stage 1 only — replace `replan()` with gate pilot at batch boundaries. No i2p, no gates, no interactive mode.
**Goal**: Retries get semantic guidance, upcoming tasks get routed context, doomed tasks get skipped.

---

## Current State

### What replan does today

`planner.py:863 replan()` is called after a batch with failures. It receives:
- `context.results` — task_key → TaskResult (success/fail, error, qa_report, diff_summary)
- `context.learnings` — flat list of Learning(text, source, kind)
- `remaining_plan` — ExecutionPlan with remaining batches
- `failed_keys`, `rolled_back_keys` — which tasks failed vs rolled back
- `pending_by_key` — full task dicts for remaining tasks

It builds a prompt with results summary + learnings + remaining tasks, and asks the LLM to re-batch them. Output: `{"batches": [...], "learnings": [...]}`.

**What it does well:** re-orders remaining tasks respecting dependencies on failed tasks.

**What it doesn't do:**
- Interpret WHY tasks failed (just passes error string)
- Decide retry strategy per task (same approach vs different)
- Route specific learnings to specific upcoming tasks
- Skip tasks whose dependencies are fundamentally broken
- Read detailed failure artifacts (verify logs, QA verdicts)

### Where replan is called

`orchestrator.py:1419-1454` — after batch results are tallied, if there are failures and remaining tasks.

### What artifacts exist on disk at replan time

```
otto_logs/
  ├── planner.log              ✓ original plan rationale
  ├── orchestrator.log         ✓ batch decisions
  ├── learnings.jsonl          ✓ persisted (load_learnings/persist_learnings)
  ├── {task_key}/
  │   ├── task-summary.json    ✓ per-phase cost + timing breakdown
  │   ├── attempt-N-agent.log  ✓ coding agent transcript
  │   ├── attempt-N-verify.log ✓ test output
  │   ├── qa-verdict.json      ✓ structured QA result
  │   ├── qa-report.md         ✓ QA reasoning text
  │   ├── spec-agent.log       ✓ spec agent transcript
  │   └── live-state.json      ✓ phase timeline
```

All the data the pilot needs is already on disk. No new artifacts required.

---

## What Changes

### New file: `otto/pilot.py`

Three functions:

#### 1. `assemble_pilot_context()`

Reads disk artifacts and builds a rich context string for the pilot. Richer than what replan gets today.

```python
def assemble_pilot_context(
    batch_results: list[TaskResult],
    remaining_plan: ExecutionPlan,
    context: PipelineContext,
    project_dir: Path,
    pending_by_key: dict[str, Any],
) -> str:
    """Build pilot context from disk artifacts."""
    sections = []

    # 1. Architecture context (if exists)
    arch = project_dir / "otto_arch" / "architecture.md"
    if arch.exists():
        sections.append(("Architecture", arch.read_text()[:3000]))

    # 2. Completed task results — richer than replan's one-line summaries
    for result in batch_results:
        task_dir = project_dir / "otto_logs" / result.task_key
        parts = [f"Task: {result.task_key}"]
        parts.append(f"Status: {'PASSED' if result.success else 'FAILED'}")
        parts.append(f"Cost: ${result.cost_usd:.2f}, Duration: {result.duration_s:.0f}s")

        if result.diff_summary:
            parts.append(f"Changes: {result.diff_summary}")

        if not result.success:
            # Include actual error output (not just error string)
            verify_logs = sorted(task_dir.glob("attempt-*-verify.log"))
            if verify_logs:
                last_log = verify_logs[-1].read_text()
                # Last 2000 chars — the actual error, not the whole log
                parts.append(f"Error output (last attempt):\n{last_log[-2000:]}")

            # Include QA verdict if available
            qa_verdict = task_dir / "qa-verdict.json"
            if qa_verdict.exists():
                parts.append(f"QA verdict:\n{qa_verdict.read_text()[:1500]}")

        if result.success:
            # For passed tasks: what files were touched (useful for routing)
            summary = task_dir / "task-summary.json"
            if summary.exists():
                parts.append(f"Summary:\n{summary.read_text()[:1000]}")

        sections.append((result.task_key, "\n".join(parts)))

    # 3. Cross-task learnings
    if context.learnings:
        learning_lines = [f"- [{l.source}] {l.text}" for l in context.learnings]
        sections.append(("Learnings so far", "\n".join(learning_lines)))

    # 4. Remaining tasks with prompts
    remaining_lines = []
    for batch in remaining_plan.batches:
        for tp in batch.tasks:
            task = pending_by_key.get(tp.task_key, {})
            remaining_lines.append(
                f"- {tp.task_key}: {str(task.get('prompt', ''))[:200]}"
            )
    sections.append(("Upcoming tasks", "\n".join(remaining_lines)))

    # Format
    return "\n\n".join(
        f"## {title}\n{body}" for title, body in sections
    )
```

#### 2. `invoke_pilot()`

Calls the planner model with the assembled context and a structured output prompt. Uses `_run_planner_prompt()` — same model as plan/replan (configurable via `planner_model` in otto.yaml).

```python
async def invoke_pilot(
    pilot_context: str,
    failed_keys: set[str],
    remaining_plan: ExecutionPlan,
    config: dict[str, Any],
    project_dir: Path,
) -> PilotDecision:
    """Invoke gate pilot at batch boundary. Returns structured decision."""

    prompt = BATCH_PILOT_PROMPT.format(
        context=pilot_context,
        failed_keys=", ".join(sorted(failed_keys)) or "none",
        remaining_keys=", ".join(
            tp.task_key
            for batch in remaining_plan.batches
            for tp in batch.tasks
        ),
    )

    raw_output, cost_usd, duration_s = await _run_planner_prompt(
        prompt, config, project_dir,
        model=_planner_model(config),
        effort=_planner_effort(config),
    )

    decision = parse_pilot_decision(raw_output)

    # Log the full decision
    _pilot_log(project_dir, f"pilot decision (${cost_usd:.3f}, {duration_s:.1f}s):",
               json.dumps(asdict(decision), indent=2))

    return decision
```

#### 3. `parse_pilot_decision()`

Parses the LLM's JSON output into a structured dataclass.

### Data structures (in `otto/pilot.py`)

```python
@dataclass
class RetryStrategy:
    action: str       # "retry" | "skip" | "retry_different"
    guidance: str     # specific advice for the retry ("use vitest not jest")
    reason: str       # why this strategy ("environment issue, not code bug")

@dataclass
class RoutedContext:
    target_task: str  # which upcoming task this is for
    context: str      # what to tell it

@dataclass
class PilotDecision:
    # Per-task retry strategies (for failed tasks being retried)
    retry_strategies: dict[str, RetryStrategy]
    # Tasks to skip entirely (broken dependency, can't succeed)
    skip_tasks: list[str]
    # Context to inject into specific upcoming tasks
    routed_context: list[RoutedContext]
    # Semantic failure analysis (for logging)
    failure_analysis: dict[str, str]
    # New learnings the pilot inferred from the failure pattern
    new_learnings: list[str]
    # Re-batching of remaining tasks (same as current replan output)
    batches: list[dict]  # same format as current replan
```

### Pilot prompt

```python
BATCH_PILOT_PROMPT = """You are otto's gate pilot. A batch of coding tasks just completed.
Analyze the results and make decisions about what to do next.

{context}

FAILED TASK KEYS: {failed_keys}
REMAINING TASK KEYS: {remaining_keys}

Your job:
1. FAILURE ANALYSIS: For each failed task, explain WHY it failed in one sentence.
   Read the error output carefully. Distinguish:
   - environment issue (wrong test framework, missing dependency, config problem)
   - code bug (logic error, wrong API usage, missing implementation)
   - spec ambiguity (task description unclear, multiple valid interpretations)
   - infrastructure error (timeout, OOM, network — retry same approach)

2. RETRY STRATEGIES: For each failed task that will be retried:
   - "retry" = same approach, the failure was transient (infrastructure)
   - "retry_different" = must change approach, include specific guidance
   - "skip" = can't succeed (dependency failed, fundamentally impossible)

3. ROUTED CONTEXT: What should specific upcoming tasks know?
   If a passed task discovered something relevant to an upcoming task, route it.
   Be specific: "task X should know that the project uses vitest, not jest"

4. SKIP TASKS: Which upcoming tasks should be skipped?
   Only skip if a task's core dependency failed AND can't be worked around.
   Don't skip aggressively — tasks may partially succeed even with failed deps.

5. BATCHING: Re-order remaining tasks (including retries of failed tasks).
   Same rules as before: respect dependencies, parallelize independent tasks.

Return JSON:
{{
  "failure_analysis": {{"task_key": "one sentence why it failed"}},
  "retry_strategies": {{
    "task_key": {{
      "action": "retry|retry_different|skip",
      "guidance": "specific advice for the retry agent",
      "reason": "why this strategy"
    }}
  }},
  "routed_context": [
    {{"target_task": "task_key", "context": "what to tell this task"}}
  ],
  "skip_tasks": ["task_key"],
  "new_learnings": ["any patterns inferred from the failures"],
  "batches": [
    {{"tasks": [{{"task_key": "key"}}]}}
  ]
}}
"""
```

### Modified: `otto/orchestrator.py`

Replace the replan block (lines 1419-1454) with pilot invocation:

```python
# BEFORE (current):
if not execution_plan.is_empty and batch_failed > 0 and not context.interrupted:
    console.print("  Replanning...", style="dim")
    replanned = await replan(context, remaining_plan, config, project_dir, ...)
    ...

# AFTER (with pilot):
if not execution_plan.is_empty and batch_failed > 0 and not context.interrupted:
    console.print("  Gate pilot analyzing...", style="dim")

    pilot_context = assemble_pilot_context(
        batch_results=batch_results,
        remaining_plan=execution_plan,
        context=context,
        project_dir=project_dir,
        pending_by_key=pending_by_key,
    )

    decision = await invoke_pilot(
        pilot_context=pilot_context,
        failed_keys=all_failed_keys,
        remaining_plan=execution_plan,
        config=config,
        project_dir=project_dir,
    )

    # Apply skip decisions
    for task_key in decision.skip_tasks:
        update_task(tasks_file, task_key, status="skipped", error="skipped by gate pilot")
        _orchestrator_log(project_dir, f"pilot: skip {task_key}")

    # Apply routed context as learnings targeted at specific tasks
    for rc in decision.routed_context:
        context.add_learning(
            text=f"[for {rc.target_task}] {rc.context}",
            source="gate_pilot",
            kind="observed",
        )

    # Apply retry strategies — store guidance for retry prompts
    for task_key, strategy in decision.retry_strategies.items():
        if strategy.action == "retry_different" and strategy.guidance:
            context.add_learning(
                text=f"[retry guidance for {task_key}] {strategy.guidance}",
                source="gate_pilot",
                kind="observed",
            )
        elif strategy.action == "skip":
            update_task(tasks_file, task_key, status="skipped",
                       error=f"pilot: {strategy.reason}")

    # Apply new learnings
    for learning in decision.new_learnings:
        context.add_learning(text=learning, source="gate_pilot", kind="inferred")

    # Apply re-batching (same validation as current replan)
    pilot_plan = parse_plan_json(json.dumps({"batches": decision.batches}))
    if pilot_plan and _plan_covers_pending(pilot_plan, remaining_pending):
        execution_plan = pilot_plan
        _orchestrator_log(project_dir, "pilot: rebatched",
                         f"new structure: {_summarize_batch_structure(pilot_plan)}")
    else:
        _orchestrator_log(project_dir, "pilot: rebatch rejected, keeping existing plan")

    persist_learnings(project_dir, context)
```

### Modified: `otto/runner.py` (retry prompt injection)

The pilot's routed context and retry strategies need to reach the coding agent. Today, retries get raw error feedback. The pilot's guidance should be appended.

Find where retry prompts are built (runner.py, the retry path in coding_loop) and inject pilot learnings that target the specific task:

```python
# When building retry prompt for task_key:
pilot_guidance = [
    l.text for l in context.observed_learnings
    if l.source == "gate_pilot" and task_key in l.text
]
if pilot_guidance:
    retry_sections.append(
        "GUIDANCE FROM PRIOR ANALYSIS:\n" + "\n".join(pilot_guidance)
    )
```

This is a small change — the retry prompt assembly already includes learnings. We just need to make sure pilot-sourced learnings with task-specific targeting get surfaced prominently.

---

## What Does NOT Change

| Component | Why unchanged |
|---|---|
| `plan()` / `default_plan()` | Initial planning is separate — pilot only runs after failures |
| `coding_loop()` | Per-task execution unchanged |
| `run_per()` outer loop | Same loop, just pilot replaces replan at the decision point |
| Spec agent | Unchanged |
| QA agent | Unchanged |
| Batch QA | Unchanged |
| `tasks.yaml` format | No schema changes |
| Telemetry | Existing events sufficient; add a `PilotDecision` event |

---

## Implementation Steps

### Step 1: Create `otto/pilot.py` skeleton

- `PilotDecision`, `RetryStrategy`, `RoutedContext` dataclasses
- `assemble_pilot_context()` — reads disk artifacts
- `invoke_pilot()` — calls planner model with structured output prompt
- `parse_pilot_decision()` — JSON → dataclass with validation
- `_pilot_log()` — append to `otto_logs/pilot.log`

**Verify:** unit test that `assemble_pilot_context()` produces expected output given mock otto_logs/ structure.

### Step 2: Wire pilot into orchestrator

- Replace replan call (orchestrator.py:1419-1454) with pilot invocation
- Apply pilot decisions: skip tasks, route context, store retry guidance, rebatch
- Add `PilotDecision` telemetry event
- Keep `replan()` as fallback if pilot parsing fails

**Verify:** run `otto run` on a multi-task project where one task fails. Check:
- `pilot.log` shows reasonable failure analysis
- Routed context appears in retry/subsequent task prompts
- Skipped tasks (if any) are marked in tasks.yaml
- Re-batching is at least as good as current replan

### Step 3: Inject pilot guidance into retry prompts

- In runner.py retry path, extract pilot-targeted learnings for the specific task
- Append as a "GUIDANCE FROM PRIOR ANALYSIS" section in the retry prompt
- Ensure it doesn't duplicate raw error feedback (which is already included)

**Verify:** run a task that fails due to an environment issue (e.g., wrong test framework). Check:
- Pilot identifies it as environment issue
- Retry prompt includes pilot's specific guidance
- Retry succeeds on changed approach (vs. current: retry with same raw error, may repeat mistake)

### Step 4: Test with pressure suite

- Run pressure test suite before/after pilot
- Compare: retry success rate, total cost, same-batch peer failures
- Check pilot.log across all runs — are decisions reasonable?
- Measure: does routed context reduce first-attempt failures for dependent tasks?

**Verify:** aggregate metrics. Pilot should not INCREASE cost or failure rate. Target: measurable improvement in retry success rate.

---

## Risks

1. **Pilot gives bad advice.** Mitigation: the orchestrator validates rebatching (same as current replan validation). For retry guidance, the worst case is unhelpful advice that the coding agent ignores — it's additive context, not a command.

2. **Pilot skips tasks that could succeed.** Mitigation: skip conservatively. The prompt says "don't skip aggressively." If a task is skipped wrongly, the user sees it in `otto status` and can re-run.

3. **Context too large.** For runs with many tasks, verify logs could be large. Mitigation: truncate to last 2000 chars per failed task. The pilot needs the error, not the full log.

4. **Cost increase.** One LLM call (planner model) per batch boundary with failures. Estimated $0.01-0.03 per invocation. For a 5-task run with 1 failure: +$0.02. Negligible vs. coding agent costs.

---

## Fallback

If pilot parsing fails or returns invalid output, fall back to current `replan()`. The pilot is an upgrade, not a replacement of the safety net:

```python
try:
    decision = await invoke_pilot(...)
    # apply decisions...
except Exception as e:
    _orchestrator_log(project_dir, f"pilot failed ({e}), falling back to replan")
    replanned = await replan(context, remaining_plan, config, project_dir, ...)
    # existing replan logic...
```

---

## Success Criteria

1. **Pilot decisions are reasonable** — read pilot.log across 10+ runs, decisions make sense
2. **Retry success rate improves** — tasks that failed once succeed on retry more often
3. **No cost regression** — total run cost does not increase (pilot cost offset by fewer wasted retries)
4. **Routed context is useful** — at least one case where pilot routes a discovery that prevents a peer failure
5. **No new failures** — pilot doesn't cause tasks to fail that would have passed without it
