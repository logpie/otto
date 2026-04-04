# Plan: Semantic Grouping Inside Batches

Date: 2026-04-01

Status: draft

## Goal

Reduce unnecessary fragmentation for tightly related tasks without losing Otto's harness benefits.

We want Otto to support:
- running some tasks independently in the same batch
- while running other tightly related tasks together as one integrated coding pass

Example:

```text
batch 1 = { {1}, {2}, {3,4} }
batch 2 = { {5}, {6} }
batch 3 = { {7,8} }
```

Where:
- `{1}` = one task, normal execution
- `{3,4}` = one integrated execution unit

This preserves parallelism across units while allowing semantic grouping where it helps.

## Why

Observed during real Codex validation:
- Some layered product tasks lose useful whole-system context when split into separate sequential task executions
- Example: data layer → service layer → CLI
- On some projects, a one-shot bare Codex implementation performed competitively or better than Otto's decomposed execution
- On conflict-heavy integrated work, Otto's harness still added clear value

Conclusion:
- Otto needs smarter grouping, not just "always separate" or "always integrated"

## Vocabulary

- `task`
  user-visible unit of intent
- `batch`
  the set of work Otto advances together before the next global decision point
- `unit`
  the execution granularity inside a batch
  - singleton unit: `["task-a"]`
  - integrated unit: `["task-c", "task-d"]`

Important:
- `batch` remains the top-level orchestrator concept
- `unit` is the minimum additional concept needed to express mixed execution within a batch

## Planner Output

Planner should continue to output strict JSON, but the batch schema changes from:

```json
{
  "batches": [
    {
      "tasks": [
        {"task_key": "a"},
        {"task_key": "b"}
      ]
    }
  ]
}
```

to:

```json
{
  "analysis": [...],
  "conflicts": [...],
  "batches": [
    {
      "units": [
        {"task_keys": ["a"]},
        {"task_keys": ["b"]},
        {"task_keys": ["c", "d"]}
      ]
    }
  ]
}
```

Rules:
- every task key must appear exactly once across all units
- unit order inside a batch is not semantically important
- unit size `1` means current behavior
- unit size `>1` means integrated execution

## Planner Responsibility

Planner owns semantic grouping.

It should decide:
- dependency/conflict structure
- batch assignment
- unit grouping inside each batch

Prompt guidance should explicitly teach:
- prefer grouping when tasks are tightly layered parts of one coherent feature slice
- do not split tasks just because they are dependent
- keep loosely related, contradictory, or conflict-heavy tasks separate

Examples of likely grouped units:
- data layer + service layer for one feature
- backend endpoint + small UI wrapper for the same exact slice
- service layer + CLI wrapper when both are narrowly coupled

Examples of likely separate units:
- unrelated features in the same repo
- same-file conflicts with competing semantics
- broad multi-domain work where integrated retries would be too coarse

## Orchestrator Responsibility

Orchestrator should not second-guess planner semantics.

It should only:
- validate structure
- execute units
- retry units
- merge units
- QA batches

Allowed orchestrator validation:
- unknown task keys
- empty units
- duplicate task coverage
- missing tasks
- malformed JSON/schema

Not allowed:
- heuristic semantic overrides like "this feels too large"

If planner output is structurally invalid:
- fallback to today's separate-task execution

## Execution Model

For each batch:

- units of size `1`
  - current task execution path
- units of size `>1`
  - one integrated coding run
  - one shared worktree
  - one test pass
  - one merge result

Parallelism:
- still allowed across units in the same batch
- no internal parallelism inside an integrated unit

So:
- batch parallelism remains
- integrated execution only changes the unit's internal coding path

## Retry Model

Retry should be unit-scoped.

If unit `["c", "d"]` fails:
- retry that same unit
- do not retry the whole batch

This is cleaner than integrated-at-batch-level design.

Initial v1 behavior:
- retry the same integrated unit with targeted feedback
- do not add fracture/split-on-retry yet

Later, if needed:
- allow a failing integrated unit to be split back into singleton units

## QA Model

Reuse current batch QA.

Batch QA already handles:
- combined specs
- per-task attribution
- retry feedback extraction
- proof/report generation

Needed adaptation:
- ensure integrated units still produce correct task-key/spec-id attribution
- keep per-task proof/report artifacts synced after batch QA and retries

No new QA architecture should be introduced for v1.

## Downstream Modules Likely Touched

### Planner / schema
- `otto/planner.py`
- `tests/test_planner.py`

### Orchestrator / execution
- `otto/orchestrator.py`
- `tests/test_orchestrator.py`

### Task result / context
- `otto/context.py`
- `tests/test_context.py`

### Coding path
- `otto/runner.py`
- `tests/test_runner.py`
- `tests/test_v45_pipeline.py`

### QA / proof attribution
- `otto/qa.py`
- `tests/test_qa.py`
- `tests/test_qa_proofs.py`

### Display / telemetry
- `otto/display.py`
- `otto/telemetry.py`

### Docs
- `docs/architecture.md`
- `docs/claude-impact-findings.md`

## Rollout Phases

### Phase 1
- Extend planner schema to emit `units`
- Keep every unit singleton by default
- Add parser/validation tests

### Phase 2
- Add orchestrator support for iterating over units
- Preserve existing behavior for singleton units

### Phase 3
- Add integrated coding path for multi-task units
- One combined prompt, one worktree, one test pass

### Phase 4
- Reuse batch QA with proper per-task attribution
- Verify proof/report artifacts remain correct

### Phase 5
- Unit-scoped retry for integrated units
- No fracture logic yet

### Phase 6
- Benchmark on:
  - layered project (`multi-blog-engine`)
  - conflict-heavy project (`edge-conflicting-tasks`)
  - at least one simpler multi-task project

## Success Criteria

- Integrated units reduce time and/or token usage on layered tasks
- Otto still outperforms or matches current reliability on conflict-heavy tasks
- Task-level proof/report artifacts remain correct after retries
- Planner output stays understandable and well-validated

## Open Questions

1. Should planner be allowed to create multi-task units across what would previously be separate batches, or only inside a same-stage batch?
2. Should there be a max unit size in v1, e.g. 2 or 3 tasks?
3. Should integrated units always be retried as a unit, or only once before splitting?
4. How much holistic context from sibling tasks should still be passed into singleton units?

## Recommendation

Proceed with the `batch -> units -> task_keys` design.

It is more expressive than whole-batch integration, preserves parallelism across units, and avoids inventing a separate top-level execution concept beyond `batch`.
