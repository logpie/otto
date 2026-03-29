# Plan: Unify Per-Task and Batch QA into One Code Path

**Goal:** Eliminate the duplicate QA prompt/function. Per-task QA becomes `batch_qa(tasks=[single_task])`.

**Why:** Two separate prompts (QA_SYSTEM_PROMPT_V45 + _build_batch_qa_prompt) means every prompt improvement must be applied twice. The BREAK phase was missing from batch QA for this exact reason.

## Current State

```
Per-task:  run_qa_agent_v45() → builds prompt from QA_SYSTEM_PROMPT_V45 → _run_qa_prompt()
Batch:     run_batch_qa_agent() → _build_batch_qa_prompt() → _run_qa_prompt() → _finalize_batch_qa_result()
```

Both call `_run_qa_prompt()` for the SDK session. The divergence is in prompt construction and post-processing.

## Proposed State

```
All QA:  run_qa() → _build_qa_prompt(tasks=[...]) → _run_qa_prompt() → _finalize_qa_result()
```

One function, one prompt builder, one post-processor. Per-task is `run_qa(tasks=[task], ...)`.

## Prompt Structure

```
BASE (shared):
  - VERIFY + BREAK instructions
  - Proof format requirements
  - Browser guidance (screenshots, kill servers)
  - Verdict JSON schema
  - Regression check instruction

BATCH ADDITIONS (when len(tasks) > 1):
  - "Every verdict item must include task_key for attribution"
  - "Generate cross-task integration tests"
  - "Return exactly one must_items entry for every task/spec pair"
  - Cross-task integration findings in verdict schema

PER-TASK SIMPLIFICATIONS (when len(tasks) == 1):
  - task_key is implicit (only one task)
  - No integration tests needed
  - Simpler verdict (no task_key per item required, but accepted)
```

## Step-by-step

### Step 1: Create unified `_build_qa_prompt()`

New function that accepts `tasks: list[dict]` (each with key, prompt, spec).
- Shared base: VERIFY + BREAK + proof format + browser + verdict schema
- If `len(tasks) > 1`: add task_key attribution, integration tests, coverage requirement
- If `len(tasks) == 1`: simpler framing, no integration section

### Step 2: Create unified `run_qa()`

New function signature:
```python
async def run_qa(
    tasks: list[dict],      # [{key, prompt, spec}] — 1 for per-task, N for batch
    config: dict,
    project_dir: Path,
    diff: str,
    *,
    on_progress=None,
    log_dir: Path | None = None,
    retry_focus: list[str] | None = None,    # per-task: prev failed criteria
    retried_task_keys: set[str] | None = None,  # batch: retried task keys
) -> dict:
```

Internally:
1. Build prompt via `_build_qa_prompt(tasks, ...)`
2. Call `_run_qa_prompt()`
3. Post-process via `_finalize_qa_result(tasks, qa_result)`

### Step 3: Unify post-processing

`_finalize_qa_result()`:
- Always: check must_passed, check regressions, check test_suite_passed
- If `len(tasks) > 1`: coverage matrix validation, integration findings, failed_task_keys attribution
- If `len(tasks) == 1`: skip coverage matrix (trivial), skip integration (N/A)

Per-task currently DOESN'T check regressions or test_suite_passed — this is a gap that unification fixes for free.

### Step 4: Update callers

In runner.py (`_run_qa`):
- Replace `run_qa_agent_v45(task, spec, ...)` with `run_qa(tasks=[{key, prompt, spec}], ...)`

In orchestrator.py (`_run_batch_qa`):
- Replace `run_batch_qa_agent(tasks_with_specs, ...)` / `run_targeted_batch_qa_agent(...)` with `run_qa(tasks=tasks_with_specs, ...)`

### Step 5: Delete dead code

- `QA_SYSTEM_PROMPT_V45` — replaced by base section in `_build_qa_prompt`
- `run_qa_agent_v45()` — replaced by `run_qa()`
- `run_batch_qa_agent()` — replaced by `run_qa()`
- `run_targeted_batch_qa_agent()` — replaced by `run_qa(retried_task_keys=...)`
- `_build_batch_qa_prompt()` — replaced by `_build_qa_prompt()`

## What stays separate

- `_run_qa_prompt()` — the SDK call layer. Unchanged.
- `_build_qa_mcp_servers()` — browser MCP config. Unchanged.
- `determine_qa_tier()` — simplified to always return 1. Could be removed entirely later.
- Proof artifact writing — `_write_proof_artifacts()` stays as-is.

## Codex Review Findings (Round 1)

1. **Missed caller:** `runner._handle_no_changes` also calls `run_qa_agent_v45`. Must migrate.
2. **Focus model:** Per-task has `prev_failed` (text criteria) + `focus_items` (spec items). Batch has `retried_task_keys`. Normalize into one structured focus input keyed by task/spec identity.
3. **Return shape:** Batch callers consume `failed_task_keys`, `test_suite_passed`, `integration_findings`, task_key attribution in must_items, proof fan-out to per-task log dirs. These outputs must remain stable.
4. **Single-task completeness:** Current per-task QA uses `expected_must_count` in `_run_qa_prompt()`. Must preserve this or replace with equivalent validator in unified finalizer.
5. **task_key in single-task verdicts:** Don't require from agent, but finalizer injects the sole task_key for consistency.

## Risks

1. Normalizing the input format: per-task passes (task, spec, original_prompt, diff), batch passes (tasks_with_specs, diff). Use `tasks: list[{key, prompt, spec}]` — per-task constructs 1-item list.
2. Focus/retry unification: replace `prev_failed`, `focus_items`, `retried_task_keys` with one `focus: list[{task_key, criteria?, items?}]` input.
3. Proof artifact fan-out: batch QA writes references to per-task log dirs. Must preserve.
4. `_handle_no_changes` path: separate per-task QA caller, must be migrated.
