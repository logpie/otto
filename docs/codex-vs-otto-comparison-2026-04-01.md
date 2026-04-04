# Codex vs Otto Comparison — 2026-04-01

This note captures what we learned from running:
- bare Codex directly
- Otto using Codex as the provider

against the same representative scenarios.

## Scenarios Used

### 1. `multi-blog-engine`

Layered product build:
- data layer
- business/service layer
- CLI/persistence layer

### 2. `edge-conflicting-tasks`

Conflict-heavy utility module:
- same file
- overlapping edits
- merge conflicts
- batch QA retry path

## Main Finding

The comparison is not one-dimensional.

We now have evidence for all of the following:

1. Otto's harness adds real value on conflict-heavy integrated work.
2. Otto's harness can be too heavy on some layered product tasks.
3. Otto's internal QA/spec contract is stricter than the repaired benchmark verifier.

That means:
- some "bare passes, Otto fails" outcomes are not necessarily coding-quality differences
- they may instead be contract mismatches between Otto-generated specs and the benchmark verifier

## What We Saw

### `edge-conflicting-tasks`

Bare Codex:
- fast
- failed the benchmark verifier
- also failed Otto's own QA/spec checks when we ran Otto QA directly on the bare output

Otto + Codex:
- much slower
- handled:
  - parallel same-file work
  - merge conflicts
  - re-apply on updated main
  - batch QA
  - batch-QA retry
- passed the benchmark verifier

Interpretation:
- this is a strong case where Otto's harness appears genuinely valuable

### `multi-blog-engine`

Bare Codex:
- fast
- passed the repaired benchmark verifier
- failed Otto's own stricter QA/spec contract

Otto + Codex, decomposed:
- much slower
- passed the repaired benchmark verifier
- also failed Otto's stricter QA/spec contract in some forms before retry/fix

Otto + Codex, grouped:
- planner grouped the 3 tasks into one integrated unit
- integrated coding + merge worked
- batch QA found failures
- one grouped retry still did not fully converge
- Otto fell back to smaller work via rollback/replan

Interpretation:
- this is not clean evidence that Otto is "better" or "worse" than bare Codex
- it is evidence that:
  - decomposition changes model behavior
  - grouping changes model behavior
  - Otto QA/specs are imposing a different contract than the benchmark verifier

## Retry Policy Read

Current grouped retry behavior:
- one integrated coding pass
- if batch QA fails, retry the integrated unit once with targeted feedback
- if still failing, rollback and replan into smaller work

Assessment:
- not buggy
- coarse, but reasonable as a v1 policy

Why it makes sense:
- it preserves the integrated whole-system context for one retry
- it avoids immediately exploding back into many small tasks
- it still has an escape hatch when grouped retry does not converge

Why it may still need improvement:
- when only one or two sub-areas fail inside a large integrated unit, rerunning the whole unit may be too expensive
- future versions may need more selective fallback after the first integrated retry

## Fair Comparison Going Forward

We should stop treating a single verifier as the only truth source.

For a fair Otto vs bare Codex comparison, measure all three:

### A. External benchmark verifier

Question:
- does the output satisfy the benchmark project's own `verify.sh`?

This is the benchmark's official pass/fail signal.

### B. Otto QA/spec contract

Question:
- would Otto's own spec + QA layer accept the same output?

This measures Otto's internal quality bar, which is stricter in some cases.

### C. Human audit

Question:
- is the output actually acceptable to a reviewer?
- is the failure due to:
  - real implementation bug
  - benchmark verifier bug
  - Otto-spec overreach

Without this third check, we can misattribute outcomes.

## Recommended Comparison Matrix

For each scenario, record:

- bare Codex:
  - runtime
  - token usage
  - benchmark verifier pass/fail
  - Otto QA/spec pass/fail
  - human audit note

- Otto + Codex:
  - runtime
  - token usage
  - benchmark verifier pass/fail
  - Otto QA/spec pass/fail
  - human audit note

This creates a more honest picture than any single score.

## Current Bottom Line

Best current synthesis:

- Otto is clearly helpful on conflict-heavy integrated tasks.
- Otto is not clearly better on layered product tasks like `multi-blog-engine`.
- We should not conclude "Otto worse than bare Codex" from those layered tasks until:
  - verifier assumptions
  - Otto spec strictness
  - human audit
  are all separated cleanly.

## Semantic Grouping Follow-up

After the initial comparison, Otto was extended to support semantic grouping inside batches:
- planner emits `batches -> units -> task_keys`
- singleton unit = normal per-task execution
- multi-task unit = integrated execution

What we learned from real grouped runs:

### Full 3-task integrated unit was too coarse

On `multi-blog-engine`, grouping all 3 tasks into one integrated unit:
- succeeded mechanically
- but failed batch QA, then required rollback and replan
- looked too coarse for retry

### Pair grouping worked better

New planner policy capped integrated units at size 2.

Observed grouped plans:
- `multi-blog-engine` → `{task1, task2}` + `{task3}`
- `multi-expense-tracker` → `{task1, task2}` + `{task3}`

Observed outcomes:
- `multi-blog-engine` pair-grouped Otto run:
  - PASS
  - benchmark verifier PASS
  - runtime about `13m42s`
- `multi-expense-tracker` pair-grouped Otto run:
  - PASS
  - benchmark verifier PASS
  - runtime about `31m20s`
  - first batch QA failed, targeted retry repaired it, final QA passed

Current interpretation:
- semantic grouping appears promising
- but integrated units should stay small
- pair grouping currently looks like a better default than full 3-task chain grouping
