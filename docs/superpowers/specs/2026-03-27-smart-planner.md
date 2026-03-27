# Smart Planner — Task Relationship Analysis

## Problem

The planner is a dumb batch grouper. It either:
- Serializes everything (old behavior — defeats `max_parallel`)
- Parallelizes everything (P3 fix — misses real conflicts)

Neither is right. The planner needs to understand task SEMANTICS to make good decisions. When two tasks both say "rewrite calculateWindChill," that's a conflict the planner should catch before wasting $4 and 20 minutes on doomed parallel coding.

## Current State

```python
# P3 fix: skip LLM planner entirely when max_parallel > 1
if max_parallel > 1 and not has_explicit_deps:
    return default_plan(tasks)  # all in one batch, no analysis
```

This puts everything in one batch. No conflict detection, no semantic analysis. Git merge handles additive overlaps, but destructive conflicts (same function rewritten differently) cause expensive retry cascades.

## Design

### Planner Responsibilities (new)

```
Input:  N task prompts + project context
Output: execution plan + conflict warnings

1. CLASSIFY each task pair:
   ├─ INDEPENDENT — different files/functions → parallel OK
   ├─ ADDITIVE — same file, different parts → parallel OK (merge handles it)
   ├─ DEPENDENT — task B needs task A's output → serialize (B after A)
   └─ CONTRADICTORY — both modify same thing incompatibly → FLAG to user

2. GROUP into batches respecting dependencies
3. FLAG contradictions with clear explanation
```

### Three-Way Classification

| Relationship | Example | Action |
|---|---|---|
| **Independent** | "Add search" + "Add dark mode" | Parallel batch |
| **Additive** | "Add function to utils.ts" + "Add another function to utils.ts" | Parallel batch (merge handles it) |
| **Dependent** | "Add user auth" + "Add user profile page" | Serialize (profile after auth) |
| **Contradictory** | "Rewrite calculateWindChill with formula A" + "Rewrite calculateWindChill with formula B" | Flag to user, don't execute both |

### Planner Model

Use the **default CC model** (same as coding agent), not hardcoded Sonnet. The planner needs real comprehension of task prompts to detect semantic conflicts.

```python
# Old: model="sonnet", effort="low"
# New: model=config.get("model") or None (CC default), effort="medium"
```

Cost: ~$0.05-0.15 per plan (up from ~$0.01). Worth it to avoid $4 wasted on contradictory tasks.

### Planner Prompt

```
You are planning the execution of {N} coding tasks for an autonomous pipeline.

Tasks:
{task prompts with keys}

Project structure:
{key files list from project}

Your job:
1. Analyze relationships between tasks
2. Classify each pair: INDEPENDENT, ADDITIVE, DEPENDENT, or CONTRADICTORY
3. Produce an execution plan

Rules:
- INDEPENDENT tasks (different files/components) → same batch (parallel)
- ADDITIVE tasks (same file, different functions/sections) → same batch (parallel).
  The pipeline handles merge conflicts automatically.
- DEPENDENT tasks (B needs A's output) → B in later batch than A
- CONTRADICTORY tasks (both modify same function/component incompatibly) →
  report the conflict. Do NOT plan contradictory tasks.

Output JSON:
{
  "analysis": [
    {"task_a": "key1", "task_b": "key2", "relationship": "independent|additive|dependent|contradictory",
     "reason": "brief explanation"}
  ],
  "conflicts": [
    {"tasks": ["key1", "key2"], "description": "Both rewrite calculateWindChill with different formulas",
     "suggestion": "Combine into one task or choose one approach"}
  ],
  "batches": [
    {"tasks": [{"task_key": "key1"}, {"task_key": "key2"}]}
  ]
}
```

### Conflict Handling

When the planner reports contradictions:

```
Otto detects conflicting tasks:
  ⚠ Tasks #1 and #2 both rewrite calculateWindChill with incompatible goals.
    #1: North American wind chill formula
    #2: Australian Apparent Temperature formula (+ rename)

  Suggestion: Combine into one task or choose one approach.

  Skipping conflicting tasks. Run otto retry after resolving.
```

Contradictory tasks are marked `conflict` status (new) and skipped. Non-conflicting tasks proceed normally. User resolves conflicts manually (`otto drop` one, or `otto retry` with combined prompt).

### Fallback

If the LLM planner fails (parse error, timeout, etc.), fall back to `default_plan()` which puts everything in one batch. Same as today — the merge/reapply pipeline handles conflicts mechanically.

### Single Task

Single task → skip planner entirely (no relationships to analyze). Same as today.

### Integration with Batch QA

The planner's analysis feeds into batch QA:
- Planner identifies which tasks are ADDITIVE (same file) → batch QA pays extra attention to cross-task interactions in those files
- Planner identifies DEPENDENT pairs → batch QA verifies the dependency chain works end-to-end

```
Batch QA combined spec now includes:
  ## Cross-Task Relationships (from planner)
  - Tasks #1 and #2: ADDITIVE on weather-utils.ts — verify both functions coexist
  - Tasks #3 depends on #1 — verify #3 uses #1's output correctly
```

## Implementation

### Phase 1: Upgrade Planner

**File: `otto/planner.py`**

1. Remove the P3 "skip planner" shortcut
2. Use default model instead of hardcoded Sonnet
3. New prompt with three-way classification
4. Parse `analysis`, `conflicts`, `batches` from response
5. Return conflicts alongside the execution plan

```python
@dataclass
class PlanResult:
    plan: ExecutionPlan
    conflicts: list[dict]  # [{tasks: [key1, key2], description: str, suggestion: str}]

async def plan(tasks, config, project_dir) -> PlanResult:
```

**File: `otto/orchestrator.py`**

1. Handle conflicts from planner:
   - Mark conflicting tasks as `conflict` status
   - Skip them in execution
   - Report to user
2. Pass planner analysis to batch QA (cross-task relationship context)

**File: `otto/tasks.py`**

Add `conflict` status.

### Phase 2: Conflict Status + Display

**File: `otto/display.py`**

Show conflict status: yellow icon, "conflict — incompatible with task #N" label.

**File: `otto/cli.py`**

- `otto status`: show conflicts with explanation
- `otto show <id>`: show conflict details + suggestion

### Verify

1. Two independent tasks → parallel (same as today)
2. Two additive tasks (same file, different functions) → parallel (same as today)
3. Two dependent tasks → serialized into separate batches
4. Two contradictory tasks → flagged, skipped, user notified
5. Mixed: 4 tasks, 2 independent + 1 dependent + 1 contradictory → correct handling
6. Single task → planner skipped
7. Planner failure → fallback to default_plan()
8. Existing tests pass

## Decisions

- **Separate `planner_model` config** — not tied to coding model. Defaults to Sonnet (good enough for relationship analysis, cheap). User can upgrade.
- **effort="medium"** — balance between cost and quality
- **Contradictions skip, dependents blocked** — conflict propagates through dependency chain
- **`conflict` status is user-resolvable** — drop, retry, or combine
- **Planner analysis feeds batch QA** — cross-task context for integration testing
- **Fallback is SERIAL, not parallel** — safer than default_plan() which parallelizes everything
- **Per-task spec gen unchanged** — planner ensures compatibility before specs are generated
- **Extend ExecutionPlan, don't replace** — add `conflicts` and `analysis` fields to existing dataclass, don't break replan/caller API
- **`uncertain` classification** — when planner isn't sure, serialize (conservative)
- **Bounded project context** — file list from `git ls-files` capped at 200 entries, no file contents

## Plan Review

### Round 1 — Codex (10 issues)
- [CRITICAL] Conflict tasks omitted from batches → coverage validation fails → falls back to parallel — fixed: validate planned + conflicted = pending (not planned = pending)
- [CRITICAL] Conflict skipping doesn't propagate to dependents — fixed: compute blocked closure, mark downstream tasks blocked
- [IMPORTANT] PlanResult breaks planner API contract — fixed: extend ExecutionPlan with conflicts/analysis fields, change plan() and replan() together
- [IMPORTANT] conflict status not wired through state machine — fixed: full state machine integration (startup, retry, status, show, summary, exit accounting)
- [IMPORTANT] Batch QA integration point doesn't exist yet — fixed: land batch QA pipeline first (already in progress on batch-qa branch), then add planner context
- [IMPORTANT] Fallback to default_plan() defeats the feature — fixed: fallback is serial (all tasks in separate 1-task batches), not parallel
- [MEDIUM] Pairwise labels insufficient for complex graphs — fixed: add `uncertain` classification, Python resolves graph, uncertain → serialize
- [MEDIUM] Reusing coding model is wrong config boundary — fixed: separate `planner_model` and `planner_effort` config
- [MEDIUM] Project context underspecified — fixed: `git ls-files` capped at 200 entries, no file contents
- [MEDIUM] Verification too shallow — fixed: added comprehensive test scenarios for conflict propagation, state machine, fallback, large-N

### Round 2 — Codex (5 issues)
- [IMPORTANT] remaining_after() loses analysis/conflicts — fixed: remaining_after() preserves conflict metadata, drops completed edges from analysis
- [IMPORTANT] Conflict state stale after queue mutations — fixed: recompute planner-derived states after add/retry/drop/revert and at startup
- [IMPORTANT] Serial fallback must respect depends_on — fixed: fallback is topo sort + split into 1-task batches (not naive file order)
- [MEDIUM] O(n²) pairwise doesn't scale — fixed: Python prefilters candidate overlaps (grep task prompts for shared file/function names), only ask LLM about suspicious pairs
- [MEDIUM] File list too weak for semantic grounding — fixed: when prompts mention specific functions/files, grep the codebase for those identifiers as grounding. Low-grounding → `uncertain` → serialize

### Round 3 — Codex (3 issues)
- [IMPORTANT] Recompute must be atomic with mutation — fixed: single locked `mutate_and_recompute()` in tasks.py. CRUD + planner-state refresh in one transaction.
- [IMPORTANT] Lexical prefilter causes false negatives — fixed: use cheap first-pass model call (Haiku) over all tasks to shortlist suspicious pairs, not lexical grep alone. Reserve expensive analysis for candidates.
- [MEDIUM] uncertain → serialize defeats max_parallel — fixed: `uncertain` only triggers serialization when there IS overlap evidence (shared file/component mentioned). No overlap evidence → parallel (benefit of doubt). Measure serialization rate on bench projects.

### Round 4 — Codex (2 issues)
- [IMPORTANT] Two-stage shortlist misses implicit dependencies — fixed: first pass scores BOTH overlap/conflict risk AND dependency risk. Final batching pass can introduce dependencies for non-shortlisted pairs.
- [MEDIUM] Persisted conflict state stale from manual edits — fixed: treat persisted conflict metadata as cache. Recompute on `otto status` and `otto plan` by checking task fingerprints (hash of prompt+id). Stale → cleared.

### Round 5 — Codex (1 issue)
- [IMPORTANT] Cache fingerprint incomplete — fixed: fingerprint includes ALL planner-relevant inputs: prompt, depends_on, feedback. Not just prompt+id.

### Round 6 — Codex (1 issue)
- [IMPORTANT] Status in fingerprint causes self-invalidation — fixed: fingerprint only includes source-of-truth inputs (prompt, depends_on, feedback), NOT planner-derived outputs (conflict, blocked). Derived statuses excluded from fingerprint.
