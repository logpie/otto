# Otto Workflow Audit: Design Issues & Recommendations

**Date**: 2026-03-22
**Status**: Findings from deep audit of v3/v4 codebase. For implementation session to address.
**Scope**: Telephone games, redundant work, lost information, prompt bloat, conflicting instructions.

---

## How to Read This

Findings are prioritized by impact. Each has: the problem, where it is in code, why it matters, and a recommended fix. The implementation session should address Critical findings before shipping v4, and Moderate findings as cleanup.

---

## CRITICAL FINDINGS

### C1. Architect context never reaches the coding agent

**Files**: `architect.py:22-25`, `runner.py:_build_coding_prompt()` (~line 994-1083)

**Problem**: The architect agent spends ~15 turns producing role-specific context files:
```python
# architect.py
_ROLE_FILES = {
    "coding": ["conventions.md", "data-model.md", "interfaces.md", "task-decisions.md", "gotchas.md"],
    "pilot": ["codebase.md", "task-decisions.md", "file-plan.md"],
}
```

But `_build_coding_prompt()` in runner.py **never calls `load_design_context(project_dir, "coding")`**. The conventions, data model, interfaces, and gotchas — all produced specifically for the coding agent — are completely ignored.

Only testgen (`testgen.py:442-446`) loads design context. The coding agent, the primary consumer, doesn't get it.

**Impact**: The entire architect agent run (~15 turns, ~$0.50) is wasted for the coding agent's benefit. The coding agent re-discovers conventions and patterns that the architect already documented.

**Nuance**: The architect was designed for downstream agents (docstring: "shared conventions for downstream agents"), and `_ROLE_FILES` maps outputs to agent roles. But the question is: **does the coding agent actually need these files?** It has full codebase access and will discover conventions on its own.

The architect's `file-plan.md` IS consumed programmatically (dependency injection via `parse_file_plan()`) — that works and has proven value (eliminated merge conflicts). The coding-role files (`conventions.md`, `interfaces.md`, etc.) have never been tested as agent input.

**Fix — two options**:
- **Option A (test the hypothesis)**: Wire up `load_design_context(project_dir, "coding")` in `_build_coding_prompt()`. Measure if coding agent pass rate improves with architect context vs without.
- **Option B (simplify)**: Stop generating coding-role files. Keep only `file-plan.md` (conflict detection) and `codebase.md` (high-level overview). The architect is valuable for dependency injection; the rest may be solving a problem that doesn't exist.

**Priority**: Evaluate before fixing — don't assume wiring up is the right call. The coding agent may already discover everything it needs.

---

### C2. 4-5 agents independently explore the same codebase

**Files**: `spec.py:197`, `architect.py:64`, `testgen.py:316-413`, `runner.py:1017-1018`, `runner.py:~1954`

**Problem**: Each agent in the pipeline independently explores the project structure:

| Agent | How it explores | Approx turns |
|-------|----------------|-------------|
| Spec agent | "Explore the codebase as needed" (prompt instruction) | 3-5 |
| Architect agent | Reads main source files, maps dependencies | 10-15 |
| Testgen agent | `build_blackbox_context()` + agent exploration | 5-10 |
| Coding agent | `get_relevant_file_contents()` pre-load + agent reads more | 5-10 |
| QA agent | Full codebase access, re-reads everything | 5-10 |

Each agent runs `git ls-files`, reads source files, potentially runs CLI help commands. Total: **30-50 wasted turns per task** on redundant file I/O.

**Root cause**: Agents don't share exploration results. The architect's `otto_arch/` is supposed to prevent this redundancy, but the coding agent doesn't read it (Finding C1), and the spec agent runs BEFORE the architect.

**Fix**:
1. Fix C1 first (feed architect context to coding agent)
2. If architect has run, include `codebase.md` summary in spec agent and QA agent prompts instead of telling them to "explore"
3. Consider running architect ONCE as a prerequisite, then feeding its output to all downstream agents

**Priority**: High — saves 30-50 turns per task, which at subscription rates means faster runs and less rate limiting pressure.

---

### C3. `test_hint` is a telephone game

**Files**: `spec.py:86-96` (generation), `runner.py:1028-1032` (consumption)

**Problem**: The spec agent (LLM #1) generates test hints:
```
[verifiable] fibonacci(30) completes in <100ms | hint: time the function call
```

These hints are injected into the coding agent's prompt (LLM #2):
```python
# runner.py:1030-1032
hint_val = spec_test_hint(item)
hint_str = f"\n     Test hint: {hint_val}" if hint_val else ""
spec_lines.append(f"  {i+1}. [verifiable] {text}{hint_str}")
```

The coding agent has full codebase access, sees the implementation, and is better positioned to decide how to test. The spec agent saw the codebase superficially in ~10 turns.

**Impact**: Marginal token waste (~20-40 tokens per spec item), but more importantly it can **constrain** the coding agent's testing approach. "time the function call" might lead it to only benchmark the easy case.

**Fix**:
1. Remove `| hint:` parsing from `spec.py:_parse_spec_output()`
2. Remove `test_hint` from the spec agent's output format instructions in the system prompt
3. Remove `spec_test_hint()` from `tasks.py` and its usage in `runner.py:1030-1032`
4. Make spec items self-explanatory: if a spec item needs a hint, rewrite the item to be clearer

**Priority**: Easy cleanup. Remove the feature, simplify the format.

---

### C4. Planner hints are a weak telephone game (v4)

**Files**: `planner.py:199-235` (system prompt), `planner.py:269-271` (truncated input), `planner.py:124` (hint field in TaskPlan)

**Problem**: The v4 planner LLM sees truncated task prompts:
```python
# planner.py:270
task_lines.append(f"... {t.get('prompt', '')[:100]}")
```

From this 100-char view, it generates `hint` strings for each task. These hints are passed to the coding agent, which has:
- Full task prompt (not truncated)
- Full source files pre-loaded
- Architect context (if C1 is fixed)
- Spec items with detailed criteria
- Learnings from prior tasks

The coding agent has strictly more context. A hint from an LLM that saw a 100-char summary adds nothing.

**Impact**: One Sonnet `query()` call (~5 turns, ~10-15s startup) for minimal value. The `default_plan()` function already handles batching correctly via topological sort.

**Fix options**:
- **Option A (conservative)**: Keep the LLM planner for `strategy` and `skip_qa` decisions, but remove `hint` from the output format. The planner decides WHAT runs in parallel and what needs research — that's valuable. Hints are not.
- **Option B (aggressive)**: Drop the LLM planner entirely. Use `default_plan()` for batching. Add simple Python heuristics for `strategy` (keyword-based) and `skip_qa` (spec count < 3 and no visual items).

**Recommendation**: Option A for v4 launch. Evaluate whether the LLM planner's strategy/skip_qa decisions actually improve outcomes vs heuristics. If not, switch to Option B.

**Priority**: Medium — the planner cost is one call per run, not per task.

---

### C5. Replanner is starved of context (v4)

**Files**: `planner.py:312-386`

**Problem**: The `replan()` function sends the LLM:
```python
# planner.py:333-335
results_summary = []
for key, result in context.results.items():
    status = "PASSED" if result.success else f"FAILED: {result.error or 'unknown'}"
    results_summary.append(f"- {key[:8]}: {status}")
```

The LLM sees `abc123ef: FAILED: unknown` — an 8-char key and a generic status. It can't see:
- The actual error message or stack trace
- The verification output (which test failed and why)
- The diff (what code was written)
- The QA report (what adversarial testing found)
- The research findings (if any)

The v4 design spec says replan should receive "all batch results (pass/fail/error details), verification logs, research findings, cross-task learnings, successful task summaries." The implementation sends almost none of this.

**Impact**: The replanner can't make informed decisions. Its "replanning" is guesswork.

**Fix**: Feed rich context from `PipelineContext`:
```python
for key, result in context.results.items():
    if result.success:
        results_summary.append(f"- {key}: PASSED — {result.diff_summary[:200]}")
    else:
        results_summary.append(
            f"- {key}: FAILED\n"
            f"  Error: {result.error[:500]}\n"
            f"  QA: {result.qa_report[:300] if result.qa_report else 'N/A'}"
        )

# Include research findings
for key, findings in context.research.items():
    results_summary.append(f"- Research for {key}: {findings[:300]}")
```

**Priority**: High if keeping the LLM replanner. If switching to deterministic replanning (Option B from C4), this becomes moot.

---

## MODERATE FINDINGS

### M1. Conflicting instructions in coding prompt

**File**: `runner.py:1054-1061`

```
APPROACH — start writing code immediately:
1. You already have the source files above. Do NOT re-read them.
2. WRITE TESTS FIRST for each [verifiable] spec item.
```

"Start writing code immediately" followed by "write tests first" sends mixed signals. The intent is "don't explore, just start working" but the phrasing contradicts itself.

**Fix**: Reword to: "APPROACH: Begin implementation immediately — do not explore the codebase (source files are provided above). Write tests first for each [verifiable] spec item, then implement."

---

### M2. Source files pre-loaded wastefully for large projects

**Files**: `runner.py:1017-1020`, `testgen.py:179-253`

`get_relevant_file_contents()` reads up to 15 source files and injects their full content into the coding prompt. For large projects, this adds 10K-50K tokens. The coding agent has Read/Grep tools and often re-reads files after making changes anyway.

The prompt says "do NOT re-read these" but the agent must re-read after editing to verify changes.

**Fix**: For projects with > 5 source files, include file PATHS (not contents) and let the agent read what it needs. For small projects (< 5 files), keep inline inclusion (current behavior). The architect's `codebase.md` + `interfaces.md` can replace raw file dumps.

---

### M3. Spec compliance rules duplicated across agents

**Files**: `spec.py:120-188` (~70 lines), `runner.py:909-933` (~30 lines)

Both the spec agent and coding agent receive constraint preservation rules. The spec agent's rules ensure specs are well-written. The coding agent's rules ensure the implementation meets specs. But if the spec items are well-written (which is the spec agent's job), the coding agent doesn't need 30 lines of "don't weaken constraints" — it just needs to meet the spec items.

**Fix**: Remove the spec-dodging/constraint-weakening rules from the coding system prompt. The spec items themselves are the contract. Trust them.

---

### M4. QA system prompt removes capabilities that should be time-boxed

**Files**: `runner.py:952-991`

```
- Do NOT re-run existing tests (npm test, pytest, jest). That's already done.
- Do NOT build the project or start dev servers unless strictly necessary.
```

These constraints exist because QA agents previously wasted time. But they prevent the QA agent from discovering regressions or testing running web apps. The right fix is time-boxing (already done via `qa_timeout`), not removing capabilities.

**Fix**: Remove these prohibitions. Let the QA agent decide how to test, within its timeout.

---

### M5. Baseline tests run twice

**Files**: `pilot_v3.py:1018-1043` (startup baseline), verification tier 1 (per task)

The baseline test suite runs at startup to detect pre-existing failures. Then tier 1 verification runs the same test suite for each task. The second run catches regressions from the coding agent's changes — which is correct. But the startup baseline is redundant if tier 1 already runs.

**Fix**: Record the baseline result and pass it to verification so tier 1 can compare against it (distinguish pre-existing failures from new ones). Don't run the full suite twice.

---

### M6. `_subprocess_env()` duplicated

**Files**: `verify.py:13-36`, `testgen.py:580-591`

Testgen has its own copy with a comment: "Re-implemented here to avoid coupling." But runner.py already imports it from verify.py. They're in the same package — the coupling concern is misplaced.

**Fix**: Delete the duplicate in testgen.py. Import from verify.py.

---

### M7. QA findings not fed back to architect

**File**: `runner.py:~2562`

When QA fails and triggers a retry, the QA report is passed to the coding agent as `last_error`. But architectural insights from QA (e.g., "the data model doesn't support X") are never fed back to `otto_arch/gotchas.md`. The architect's `feed_reconciliation_learnings()` exists but is only called during reconciliation.

**Fix**: On QA failure, append relevant findings to `otto_arch/gotchas.md` so future tasks benefit.

---

### M8. Markdown task parsing uses full LLM agent unnecessarily

**File**: `spec.py:290-380`

`parse_markdown_tasks()` uses a full LLM agent (10 turns) to parse a markdown file into JSON tasks. For well-structured markdown (clear headings, numbered sections), this could be done deterministically with regex/parsing.

**Fix**: Try deterministic parsing first. Fall back to LLM only if the structure is ambiguous.

---

## DESIGN PRINCIPLE: Pass Environmental Feedback, Not Inferred Solutions

All four telephone games (C3, C4, P3, v3 pilot) share the same anti-pattern: **an upstream LLM interprets raw information and passes its interpretation downstream, instead of passing the raw information itself.**

```
WRONG (telephone game):
  error output → LLM interprets → "move func() inside lock" → coding agent follows blindly
                                   ↑ wrong inference, coding agent can't recover

RIGHT (environmental feedback):
  error output → pass verbatim → coding agent reads raw error → reasons from first principles
                                  ↑ coding agent has full context, makes better decision
```

**The rule**: When information flows between agents, pass the RAW ENVIRONMENTAL FEEDBACK (error messages, test output, stack traces, verification logs) — never an upstream agent's inferred solution. Let the downstream agent reason from the evidence.

This applies to:
- **Retry hints**: Pass the verification failure output, not "try a different approach." The coding agent reads the error and decides.
- **test_hint**: Don't tell the coding agent how to test. Give it the spec item. It figures out the test.
- **Planner hints**: Don't tell the coding agent what approach to use. Give it the task + spec + source files. It figures out the approach.
- **Cross-task learnings**: Pass factual observations ("project uses ESM modules", "middleware pattern is X") not prescriptive advice ("use ESM imports").

The exception: when an observation requires DOMAIN KNOWLEDGE the downstream agent might lack (e.g., "Telegram Bot API only accepts a fixed emoji whitelist" — non-obvious from the error message). In that case, the hint adds genuine information. But even then, describe the constraint, not the solution.

**Implementation for v4**:
- Remove `test_hint` from spec format (C3)
- Remove `hint` from TaskPlan or restrict to problem-description-only (C4, P3)
- Pass `TaskResult.error` (raw verification output) directly to retry prompts, not a planner's interpretation (C5)
- Cross-task learnings in PipelineContext should be factual observations, not advice

---

## ARCHITECTURAL OBSERVATIONS

### The v3 Pilot is a telephone game by design

The v3 pilot (now `pilot_v3.py`) is an LLM orchestrator that:
1. Calls `run_task_with_qa` via MCP
2. Reads the error output from the result
3. Generates a "hint" paraphrasing the error for the next retry

This is LLM #8 paraphrasing LLM #6's findings for LLM #5. The v4 orchestrator correctly replaces this with a deterministic Python loop that passes error context directly — no paraphrasing.

### The Spec Agent is the most valuable LLM call

The spec agent transforms vague user prompts into concrete, testable acceptance criteria. This is genuinely LLM-appropriate work — understanding intent, inferring edge cases, translating natural language into specifications. Its output is consumed by every downstream agent and provides real value. Don't cut this.

### The Architect Agent has an ROI problem (until C1 is fixed)

The architect produces valuable output. But the primary consumer (coding agent) doesn't read it. Fix C1 and the architect becomes the highest-ROI agent in the pipeline — its output prevents redundant exploration by every downstream agent.

---

## PRESSURE TEST FINDING (from bench session)

### P3. Hint system steers toward wrong solutions

**Evidence**: Pressure-tested cachetools stampede bug (22 golden real-repo projects, ground-truth validated). Otto's pilot generated the hint: "Move the func() call INSIDE the `with lock:` block." Both otto and bare CC applied this naive fix, which prevents stampede but **destroys all concurrency for different keys**.

The real fix uses `threading.Condition` + pending set for per-key coalescing — a fundamentally different approach the hint precluded.

**Fix for v4**: Hints should describe the PROBLEM ("threads stampede on cache miss, different keys should remain parallel"), not the SOLUTION ("move func inside lock"). Never suggest specific code changes in hints.

Full bench results and deep-dive analysis: `bench/pressure/reports/2026-03-22-deep-dive-findings.md`

---

## SYSTEMIC FINDINGS (Beyond Telephone Games)

These issues share a theme: the system's structure undermines its own goals.

### S1. Spec acts as information filter, not just translator

**Problem**: The spec agent transforms "build a bookmark manager with tags and search, similar to Pinboard" into 6 spec items. But "similar to Pinboard" — a design intent guiding UX/architecture choices — gets dropped. The spec items capture WHAT but lose WHY and LIKE WHAT.

The coding agent receives BOTH the original prompt AND the spec. If the spec is incomplete, should it follow the spec (contract) or the prompt (intent)? The system gives conflicting authority. In practice, the coding prompt says "meet ALL spec items" so the agent follows the spec and ignores dropped intent.

**Fix**: The spec agent should explicitly preserve design intent that isn't captured in spec items. Either: add a `design_context` field to the spec output ("similar to Pinboard: minimal UI, tag-first navigation, keyboard-driven"), or pass the original prompt to the coding agent WITH the spec (not instead of it) and instruct: "spec items are the contract, original prompt is context."

---

### S2. Premature decisions that can't be revised

**Problem**: The planner decides `skip_qa` and `strategy` BEFORE the task runs. But these depend on the outcome:
- `skip_qa`: A "simple" task might produce security-sensitive code (auth, crypto) that needs adversarial testing. Can't know until the code is written.
- `strategy: research_first`: The coding agent might discover mid-implementation that it needs research. No mechanism to request research dynamically.
- `effort: high`: Always set to "high" — the effort level is decided before seeing the task's actual difficulty.

These are decisions made with insufficient information. The planner has only the task description; the coding agent has the full codebase and implementation context.

**Fix**: Make these decisions LAZY:
- `skip_qa`: Decide AFTER verification passes, based on what was actually built (e.g., did the code touch auth/crypto files? → run QA regardless).
- Research: Let the coding agent request research via a mechanism (MCP tool, or fail with a "needs_research" error code that the orchestrator catches and dispatches research for retry).
- Effort: Let the coding agent escalate ("this is harder than expected, I need more turns").

---

### S3. Spec staleness between add and run

**Problem**: Specs are generated at `otto add` time. Tasks run at `otto run` time (could be hours or days later). If the codebase changes between add and run (user commits, another otto task merges, manual edits), the spec was generated against a different codebase than the one being modified.

The spec agent explored the codebase at add time and produced specs grounded in what it found. At run time, those files might be renamed, refactored, or deleted.

**Impact**: Mostly low for short add-to-run intervals. Could be significant if specs are generated days in advance or if a multi-task run reorders tasks (task 1 modifies code that task 2's spec assumed was unchanged).

**Fix**: Consider re-validating specs at run time (lightweight check: do the files/functions referenced in spec items still exist?). Or regenerate specs at run time instead of add time (controlled by a `--respec` flag, already in TODO.md).

---

### S4. No spec-to-test traceability

**Problem**: Spec says 6 items are `[verifiable]`. Coding agent writes tests. But nothing checks that EACH verifiable item has a corresponding test. The agent might test 4 of 6 items. Verification passes because existing tests pass — but 2 spec items have zero coverage. The system declares success with incomplete implementation.

**Impact**: High — this is a silent correctness gap. The spec-verify flywheel has a hole: "verify" proves tests pass, not that specs are met.

**Fix options**:
- **Lightweight**: Add a post-verification step that greps test files for keywords from each spec item. Heuristic but catches obvious gaps.
- **Structured**: Require the coding agent to output a spec-to-test mapping (which test covers which spec item) that the orchestrator validates.
- **Adversarial**: This is what QA is supposed to catch — QA tests spec items the test suite missed. But QA output is unstructured (see S5), so the gap isn't reliably detected.

---

### S5. QA output is unstructured — pass/fail is a lossy binary

**Problem**: The QA agent produces free-form text. The orchestrator parses it heuristically for pass/fail. Nuanced findings are lost:
- "Works for small inputs but fails at scale" → binary: fail
- "Functionally correct but has a minor UX issue" → binary: pass? fail? Depends on parsing.
- "Spec item #3 passes but #5 is ambiguous" → binary can't capture per-item results.

The QA agent's rich analysis is compressed into a boolean. Retry hints based on this boolean lose the detail.

**Fix**: Give the QA agent a structured output format:
```json
{
  "overall": "fail",
  "items": [
    {"spec_item": 1, "status": "pass", "evidence": "tested with curl, 200 OK"},
    {"spec_item": 3, "status": "fail", "evidence": "returns 500 on empty input", "severity": "high"}
  ]
}
```
The orchestrator can then pass the SPECIFIC failing items (with evidence) to the retry, not just "QA failed."

---

### S6. Silent fallback masking wasted LLM calls

**Problem**: The planner falls back to `default_plan()` on malformed JSON:
```python
result = parse_plan_json(raw_output)
if result and not result.is_empty:
    return result
# ... falls back silently
return default_plan(tasks)
```

No logging of fallback frequency. If the planner always produces malformed JSON, every planner `query()` call is pure waste — a subprocess spawned for nothing. Same risk for any LLM-output-to-structured-data pipeline.

**Fix**: Log every fallback with the raw output that failed parsing. Track fallback rate. If >30% of planner calls fall back, the planner prompt needs fixing or the LLM planner should be removed.

```python
result = parse_plan_json(raw_output)
if result and not result.is_empty:
    telemetry.log(PlannerSuccess(task_count=result.total_tasks))
    return result
telemetry.log(PlannerFallback(raw_output=raw_output[:500]))
return default_plan(tasks)
```

---

### S7. Verification proves tests pass, not that specs are met

**Problem**: The verification pipeline runs: tier 1 (existing tests) → tier 2 (generated tests) → tier 3 (custom verify command). If all pass, the task passes. But "tests pass" ≠ "specs are met":
- Tests might be weak (happy path only)
- Tests might be wrong (testing the wrong behavior)
- Tests might be incomplete (missing spec items — see S4)
- Tests might be tautological (testing that the code does what the code does, not what the spec says)

The spec → test → verify chain ASSUMES test quality but nothing enforces it. The mutation check in testgen.py partially addresses this but is optional (TDD mode only).

**Impact**: The core promise of otto — "verified implementation" — has a gap. Verification proves the coding agent's OWN tests pass, which is circular if the agent wrote bad tests.

**Fix**: This is fundamentally what QA is for — adversarial testing that doesn't trust the coding agent's tests. The fix is making QA more reliable (structured output, per-spec-item testing), not adding more automated verification layers. See S5.

---

### S8. Semantic merge conflicts pass silently

**Problem**: Tasks in the same batch run in separate worktrees, branch from the same base. The rebase-before-merge step catches textual conflicts. But SEMANTIC conflicts pass silently:
- Task A changes a function's return type from `str` to `dict`
- Task B adds code calling that function, expecting `str`
- No merge conflict (different files). Rebase succeeds. Code is broken.

The architect's `file-plan.md` catches FILE-level overlaps but not semantic conflicts across files.

**Impact**: Low frequency (requires parallel tasks touching related but different files), but high severity when it happens (broken code merged to main).

**Fix**: Run the test suite AFTER merging each task (not just in the worktree before merge). If tests fail after merge, the last-merged task broke something. This is already partially handled by the baseline check, but only on the next run, not within the current run.

Better: after all tasks in a batch merge, run an integration test before proceeding to the next batch. This catches cross-task semantic conflicts before they compound.

---

### S9. No cost-quality correlation

**Problem**: Otto tracks cost per task ($) and outcome (pass/fail). But doesn't correlate them. A task that costs $3 and passes first try looks the same as one costing $0.30. No mechanism to detect:
- Over-engineering (expensive coding for a simple task)
- Model mismatch (Opus used where Sonnet would suffice)
- Diminishing returns (retry 3 costs $1 but attempt 1 already had 90% of the work)

The planner's `effort` field exists but is almost always "high."

**Fix**: Track cost-per-attempt and success-per-attempt. After enough runs, build heuristics: "tasks with <3 spec items and no dependencies pass on first attempt 90% of the time — use Sonnet instead of Opus, skip QA." This is a v5+ optimization but the data collection should start in v4.

---

### S10. Agent drift over long sessions

**Problem**: The coding agent can run for 200 turns. Over long sessions, LLMs drift from system prompt instructions. Spec compliance rules, approach instructions, and formatting requirements from turn 1 may be forgotten by turn 150. Auto-compaction makes this worse — the compacted summary is a lossy compression of the original instructions.

**Impact**: Hard to measure directly. Manifests as: coding agent stops writing tests in later turns, starts exploring instead of coding, produces inconsistent code style.

**Fix options**:
- **Reduce max_turns**: Most tasks should complete in 50-100 turns. 200 is excessive and increases drift risk. If a task needs 200 turns, it's probably too complex and should be decomposed.
- **Periodic re-injection**: Every N turns, re-inject the key instructions (spec items, approach) as a user message. Expensive but effective.
- **Fail-fast on drift**: If the agent hasn't made changes in 20+ turns (exploring without coding), abort and retry with a more directive prompt.

---

### S11. Spec agent can't verify its own output

**Problem**: The spec agent generates spec items and the system trusts them. But specs can be:
- **Contradictory**: "Response time <100ms" AND "Must query 3 external APIs synchronously" — impossible together.
- **Ambiguous**: "Should handle edge cases properly" — what edge cases? What's "properly"?
- **Untestable despite [verifiable] tag**: "System is fast" marked as verifiable, but no concrete threshold.

The spec agent's compliance self-check (line 181-188 in spec.py) only checks that user constraints aren't softened. It doesn't check for internal consistency, ambiguity, or testability.

**Fix**: Add a spec validation step (can be deterministic):
- Check each `[verifiable]` item contains a measurable criterion (number, boolean, specific behavior)
- Check for contradictions (heuristic: items mentioning the same subject with conflicting requirements)
- Reject specs with too few items (<3) or too many (>12) — both are signs of bad decomposition

---

### S12. No observability into agent decision quality

**Problem**: Otto tracks WHAT agents do (tool calls, files modified, test results) but not WHY they make decisions. When a coding agent takes a wrong approach (e.g., the cachetools stampede fix), there's no way to understand why it chose that approach without reading the full agent transcript.

Post-mortem analysis requires reading `otto_logs/<key>/attempt-*-agent.log` — hundreds of lines of thinking + tool calls. No summarization, no decision highlighting, no "here's where it went wrong."

**Fix**: The coding agent's system prompt could instruct it to write a brief decision log to a known file before starting implementation:
```
## Approach Decision
- Considered: global lock, per-key lock, condition variable
- Chose: global lock because [reason]
- Risk: may reduce concurrency for different keys
```
This is lightweight (one Write call) and gives the orchestrator/user a quick way to validate the approach BEFORE the agent spends 50 turns implementing it.

---

## SUMMARY: Priority Order

| # | Finding | Type | Fix effort | Impact |
|---|---------|------|-----------|--------|
| **C1** | Architect context not fed to coding agent | Lost information | Evaluate | **High** — unlocks all architect value |
| **C2** | 4-5 agents explore same codebase redundantly | Redundant work | Medium | **High** — 30-50 wasted turns per task |
| **P3** | Hint system steers toward wrong solutions | Design flaw | Small | **High** — led to naive fix on cachetools |
| **S4** | No spec-to-test traceability | Correctness gap | Medium | **High** — silent incomplete implementation |
| **S7** | Verification proves tests pass, not specs met | Correctness gap | Medium | **High** — circular verification |
| **S5** | QA output unstructured — lossy binary | Information loss | Medium | **High** — retry hints lose detail |
| **C3** | test_hint telephone game | Telephone game | Small | **Medium** — simplifies format |
| **C4** | Planner hints telephone game | Telephone game | Small | **Medium** — remove hints |
| **C5** | Replanner starved of context | Implementation gap | Medium | **High** if keeping LLM replanner |
| **S1** | Spec drops design intent | Information loss | Small | **Medium** — coding agent loses context |
| **S2** | Premature planner decisions | Wrong timing | Medium | **Medium** — skip_qa decided too early |
| **S6** | Silent fallback masking waste | Observability gap | Small | **Medium** — hidden wasted LLM calls |
| **S8** | Semantic merge conflicts pass silently | Correctness gap | Medium | **Medium** — low freq, high severity |
| **S11** | Spec agent can't verify its own output | Quality gap | Medium | **Medium** — bad specs cascade |
| **S12** | No agent decision observability | Observability gap | Small | **Medium** — hard to debug wrong approaches |
| **S10** | Agent drift over long sessions | Quality degradation | Small | **Low-Medium** — reduce max_turns |
| **S3** | Spec staleness between add and run | Timing issue | Small | **Low** — short intervals mitigate |
| **S9** | No cost-quality correlation | Missing data | Small | **Low** — v5 optimization, start collecting |
| M1 | Conflicting prompt instructions | Prompt quality | Trivial | Low |
| M2 | Source file pre-loading wasteful | Token waste | Medium | Medium for large projects |
| M3 | Duplicated compliance rules | Prompt bloat | Small | Low |
| M4 | QA capabilities restricted | Over-specification | Small | Medium |
| M5 | Baseline tests run twice | Redundant work | Small | Low |
| M6 | `_subprocess_env()` duplicated | Code health | Trivial | None (runtime) |
| M7 | QA findings not fed to architect | Lost information | Small | Low-Medium |
| M8 | Markdown parsing over-engineered | Unnecessary LLM | Medium | Low |
| **S13** | Shell injection via test_command/verify_cmd | Security | Medium | **High** — shell=True with user input |
| S14 | Temp file leaks on error paths | Resource leak | Small | Low-Medium |
| S15 | Worktree cleanup gaps on crash paths | Resource leak | Medium | Medium |
| S16 | Broad exception swallowing in display code | Observability | Small | Low |
| **S17** | Artificial turn/time caps harm agent quality | Wrong abstraction | Small | **High** — caps abort productive agents |
| **T1** | Coding prompt is micromanagement (7 "Do NOT"s) | Agent distrust | Medium | **High** — constrains agent reasoning |
| **T2** | Spec capped at "5-8 criteria" | Artificial limit | Trivial | **Medium** — forces incomplete specs |
| **T3** | "No bikeshedding" removes valid spec concerns | Information suppression | Trivial | Low |
| **T4** | Spec agent forced into 5-step workflow | Micromanagement | Trivial | Low |
| **T5** | "Do NOT re-read" pre-loaded files | Actively harmful | Trivial | **Medium** — prevents verification |
| **T6** | QA told not to run tests/build/start servers | Capability removal | Small | **High** — defeats QA purpose |
| **T7** | Planner truncates prompts to 100 chars | Information destruction | Trivial | **Medium** — decisions on incomplete data |
| **T8** | Error messages truncated to 200 chars | Information destruction | Trivial | **Medium** — root cause lost |
| **T9** | Forced TDD workflow | Micromanagement | Trivial | Low-Medium |
| **T10** | System pre-selects files, forbids re-reading | Agent distrust + info destruction | Medium | **High** — wrong files, 10-50K wasted tokens, blocks verification |
| **T11** | "Stay in your lane" artificial boundaries | Agent distrust | Trivial | **Medium** — prevents early bug catching |
| **T12** | Heuristic spec filter silently drops agent output (cli.py:32-113) | Information destruction | Medium | **High** — 80 lines of regex overriding agent |
| **T13** | abort_task refuses based on hardcoded min-attempts, overrides agent (pilot_v3.py:594) | Agent override | Small | **High** — most direct trust violation |
| **T14** | QA agent gets diff stat, not actual diff (runner.py:1918) | Information withholding | Small | **Medium** — QA can't see what changed |
| **T15** | Test files excluded from coding agent context (testgen.py:215-221) | Information filtering | Small | **Medium** — agent can't learn test conventions |
| **T16** | CI=true injected into subprocess env without telling agents (verify.py:29) | Hidden interference | Trivial | **Medium** — agents debug different behavior |
| **T17** | Architect forced into rigid 7-file taxonomy with validation (architect.py:72-151) | Forced structure | Small | **Medium** — agent produces filler to pass validation |
| **T18** | Spec items without tags silently marked verifiable (spec.py:82-85) | System overrides agent | Trivial | Low-Medium |
| **T19** | Subagent models hardcoded to haiku (runner.py:1537-1548) | Hardcoded decision | Trivial | Low |

Full details of 37 additional findings: `docs/superpowers/specs/2026-03-22-otto-audit-new-findings.md`

---

## S17. Artificial turn caps and time limits should be removed

**Current state**: Every agent has a hardcoded `max_turns`:

| Agent | max_turns | max_task_time |
|---|---|---|
| Coding | 200 | 900s |
| Pilot (v3) | 100 | — |
| QA | 50 | 900s (qa_timeout) |
| Testgen | 15-20 | — |
| Architect | 15 | — |
| Spec | 10 | — |
| Planner | 5 | — |

**Problem from first principles**: These caps are arbitrary. A complex task needing 210 turns is aborted at 200. A spec needing 12 turns is rushed at 10. The caps don't distinguish "productively working" from "stuck."

Interactive Claude Code has NO max_turns. The agent runs until it's done. The human intervenes only if it's visibly stuck.

**Why stagnation detection is also wrong**: Heuristics like "no file writes in N turns" or "same file read 3 times" have too many false positives. The agent legitimately reads files for 10 turns before writing. Write → verify → re-read → fix is a normal cycle, not stagnation.

**What actually happens when an agent gets stuck**: It doesn't literally infinite loop. Modern LLMs try approach A, fail, try B, fail, try C, and eventually report "I couldn't solve this." `query()` returns a `ResultMessage` with the agent's conclusion. Natural termination.

**The principled answer:**

1. **Remove all turn caps** — they're meaningless arbitrary numbers that abort productive agents
2. **Remove time caps for normal operation** — with subscription, no cost concern; agent terminates naturally
3. **Keep `max_retries`** (already exists, default 3) — THIS is the real safety mechanism. If 3 full attempts can't solve it, the problem is too hard for one agent. Let the planner decompose or abort.
4. **One circuit breaker** — a generous timeout (30-60 min per task) purely as insurance against pathological edge cases (buggy SDK, process hangs). Not for normal operation. Should never trigger in practice.

**Why max_retries IS the stagnation detector**: Each retry lets the agent run to natural completion. If 3 full attempts fail, it's not a stagnation problem — it's a capability problem. The planner should decompose the task or change strategy. Retries with targeted hints (from environmental feedback, not inferred solutions) give the agent a fresh chance each time.

**Fix**:
- Remove `max_turns` from all `ClaudeAgentOptions` calls
- Set `max_task_time` to 3600s (1 hour) as a pure circuit breaker, not a normal operating limit
- Rely on `max_retries=3` as the primary failure mechanism
- For utility agents (spec, architect, planner): they naturally terminate when they've written their output file. No cap needed.

---

---

## DESIGN PRINCIPLE: Trust Agents — Give Problems, Not Instructions

The system should give agents the PROBLEM (goals, context, constraints) and TRUST them to figure out HOW. Don't micromanage, don't truncate information, don't prescribe workflows. Modern LLMs are capable of autonomous reasoning — the harness should enable that, not constrain it.

Violations of this principle found across the codebase:

### T1. Coding agent prompt is micromanagement, not goal-setting

**File**: `runner.py:1050-1086`

The coding prompt tells the agent exactly WHAT to do, in WHAT ORDER, and WHAT NOT to do:

```
APPROACH — implement the spec now (do not explore):
1. You already have the source files above. Do NOT re-read them.
2. WRITE TESTS FIRST for each [verifiable] spec item.
3. IMPLEMENT until all tests pass (green).
4. RUN ALL TESTS.
5. Re-read each spec item.
6. Write notes.

IMPORTANT — stay in your lane:
- Do NOT start dev servers, do NOT curl endpoints, do NOT do browser testing.
- Do NOT re-explore files already shown above.
- Do NOT invent unnecessary improvements, refactors, or extra features.
```

Count: 7 "Do NOT" instructions. The prompt is more about what the agent CAN'T do than what it should achieve.

**What it should say instead**: Give the goal, the spec, and the context. Trust the agent.

```
GOAL: Implement the feature described below. Meet every acceptance spec item.

ACCEPTANCE SPEC:
  1. [verifiable] fibonacci(30) completes in <100ms
  2. [verifiable] raises ValueError for negative input
  ...

CONTEXT: Source files are pre-loaded below. The project uses [framework].
Architect notes are in otto_arch/. A separate QA agent will test after you.

Do not create git commits (otto handles merging).
Write notes to otto_arch/task-notes/{key}.md about your approach and gotchas.
```

That's it. No forced TDD. No "do not explore." No "do not start dev servers." The agent decides its own approach. If it wants to explore more files, let it. If it wants to write implementation before tests, let it. If it needs to start a dev server to test, let it.

The only NECESSARY constraint is "do not create git commits" (otto handles git). Everything else is micromanagement.

---

### T2. Spec agent capped at "5-8 acceptance criteria"

**File**: `spec.py:139`

```
- 5-8 acceptance criteria. Hard constraints first, then supporting requirements.
```

Why 5-8? A complex task might need 15 criteria. A simple task might need 3. The agent should produce AS MANY AS NEEDED. The cap forces the agent to either skip edge cases (too few) or merge distinct requirements (ambiguous items).

**Fix**: Remove the count cap. Say "produce acceptance criteria that fully cover the task requirements — as many or as few as needed."

---

### T3. Spec agent told "no bikeshedding" — removes valid concerns

**File**: `spec.py:141`

```
- No bikeshedding (formatting, unit labels, value ranges unless user specified them).
```

"Unit labels" and "value ranges" are NOT bikeshedding — they're specification clarity. If the spec says "response time under 200" without units, is that 200ms or 200s? If the spec says "supports large inputs" without a range, how large? The agent should add these clarifications, not be told to skip them.

**Fix**: Remove this line. Let the agent decide what's important to specify.

---

### T4. Spec agent forced to follow 5-step workflow

**File**: `spec.py:185-191`

```
Steps:
1. EXPLORE: Read the relevant source files
2. EXTRACT: Identify every hard requirement
3. WRITE: Generate acceptance criteria
4. VERIFY: Re-read the task description
5. OUTPUT: Write the final numbered list to the file.
```

This prescribes HOW the agent should think. The agent might reason differently — and that's fine. Maybe it wants to write a draft spec first, then explore the codebase to validate, then revise. The forced order prevents adaptive reasoning.

**Fix**: Remove the step-by-step instructions. Just say: "Explore the codebase, understand the task, and write acceptance criteria to {spec_file}."

---

### T5. Coding agent told "Do NOT re-read" pre-loaded files

**File**: `runner.py:1054, 1059`

```
RELEVANT SOURCE FILES (pre-loaded — do NOT re-read these):
...
1. You already have the source files above. Do NOT re-read them.
```

The agent MUST re-read files after editing them to verify changes. Telling it not to re-read is actively harmful. The pre-loading is a convenience (saves early exploration turns), not a prohibition on future reads.

**Fix**: Change to "RELEVANT SOURCE FILES (pre-loaded for convenience):" — remove the prohibition.

---

### T6. QA agent told not to run tests, not to build, not to start servers

**File**: `runner.py:966-998`

```
Do NOT re-run existing tests (npm test, pytest, jest). That's already done.
Do NOT build the project or start dev servers unless strictly necessary.
Be FAST. Target 2-3 minutes total.
Do NOT rebuild the project. Do NOT re-run the full test suite.
```

Four prohibitions and a time target. The QA agent is an adversarial tester — its JOB is to find bugs the test suite missed. But it's told it can't run the test suite, can't build the project, can't start servers. That's like telling a security auditor "don't try to hack the system."

These constraints exist because QA agents previously wasted time. But the fix should be the time budget (`qa_timeout`, already exists at 900s — or removed per S17), not removing capabilities. Trust the agent to use its time wisely.

**Fix**: Remove all "Do NOT" prohibitions. Keep the goal: "Find bugs the test suite missed. You have [qa_timeout] to work. Prioritize high-impact findings."

---

### T7. Planner truncates task prompts to 100 chars

**File**: `planner.py:270`

```python
task_lines.append(f"... {t.get('prompt', '')[:100]}")
```

The planner sees only the first 100 characters of each task. A task prompt might be 500 chars with crucial context in the second half. The planner makes decisions (strategy, hints, skip_qa) based on incomplete information.

**Fix**: Pass full prompts. If context length is a concern, let the agent decide what's relevant — don't truncate for it.

---

### T8. Error messages truncated before reaching agents

**File**: `runner.py:744` (TaskFailed error), `runner.py:759` (exception error)

```python
telemetry.log(TaskFailed(..., error=error[:200]))
# ...
telemetry.log(TaskFailed(..., error=str(exc)[:200]))
```

Error messages truncated to 200 chars. A stack trace that explains the root cause might be 2000 chars. The first 200 chars might just be the exception class name and a generic message. The actual useful information (file, line, context) is in the truncated part.

Truncation for DISPLAY is fine (human readability). Truncation for AGENT CONSUMPTION or LOGGING is information destruction.

**Fix**: Log full errors to telemetry. Truncate only for display. When passing errors to retry hints or replanner, pass the full text.

---

### T9. Coding prompt forces TDD workflow

**File**: `runner.py:1061-1064`

```
2. WRITE TESTS FIRST for each [verifiable] spec item. Test the hardest case.
   Run them — they should FAIL (red). If they pass, your tests are too weak.
3. IMPLEMENT until all tests pass (green).
```

The coding agent is forced into strict TDD (red → green). This is a valid methodology but not always the best one. For some tasks, implementation-first is better (explore the problem, write code, then write tests to cover what you built). For refactoring tasks, you might want to write tests first to lock behavior, then refactor. The agent should choose.

**Fix**: Remove the forced TDD sequence. Say "implement the feature and write tests for all verifiable spec items" — let the agent decide the order.

---

### T10. System pre-selects source files and forbids agent from exploring

**Files**: `runner.py:1024-1026`, `runner.py:1054-1059`, `testgen.py:179-253`

**The full chain:**
1. System runs `git ls-files` → gets all tracked files
2. System filters by heuristics → skip tests, lock files, binary, >50KB
3. If >15 files, system builds AST symbol index → picks 15 "most relevant" via keyword matching
4. System reads all 15 files, dumps FULL CONTENT into prompt → 10K-50K tokens
5. Prompt says "do NOT re-read these" → agent forbidden from verifying its own edits

**Three violations of agent trust:**
- **System decides what's relevant**: The heuristic (`_find_relevant_files`) uses AST symbol matching. But the agent understands the task semantically — "add rate limiting" means middleware files, not whatever the symbol index matched. The system's guess may be wrong and the agent can't override.
- **Context pollution**: 15 files × ~500 lines = ~7500 lines in the prompt. For a task needing 2 files, the other 13 are noise diluting agent attention.
- **"Do NOT re-read" is actively harmful**: The agent MUST re-read files after editing to verify changes. This prohibition prevents basic software engineering practice.

**What interactive Claude Code does**: User says "add rate limiting." Agent uses Glob to find files, Read to examine them, decides its own exploration path. No pre-loading. No prohibition. Works great.

**Fix**: Replace pre-loaded file contents with a file tree overview. Let the agent read what it needs.

```python
# BEFORE (system decides, 10K-50K tokens of guessed content):
source_context = get_relevant_file_contents(effective_dir, task_hint=prompt)
prompt = f"RELEVANT SOURCE FILES (pre-loaded — do NOT re-read these):\n{source_context}"

# AFTER (agent decides, ~500 tokens of file tree):
file_tree = subprocess.run(["git", "ls-files"], cwd=effective_dir, ...).stdout
prompt = f"PROJECT FILES:\n{file_tree}"
# Agent reads what IT thinks is relevant
```

**Cost**: 5-10 extra turns for initial exploration (~30-60s).
**Gain**: Agent reads the RIGHT files, not a heuristic's guess. Better quality. Matches interactive CC behavior.

**Exception**: For tiny projects (<5 files), pre-loading the entire codebase is fine — it all fits in context. But remove "do NOT re-read" regardless.

---

### T11. Coding agent told to "stay in your lane"

**File**: `runner.py:1068-1074`

```
IMPORTANT — stay in your lane:
- Write code and unit/integration tests. Run them with the test runner.
- Do NOT start dev servers, do NOT curl endpoints, do NOT do browser testing.
  A separate QA agent handles live testing after you're done.
- Do NOT invent unnecessary improvements, refactors, or extra features.
  Implement EXACTLY what the spec asks for. Nothing more.
```

This creates artificial boundaries between the coding agent and QA agent. If the coding agent wants to test its API by curling it, that's GOOD — it catches bugs earlier. Telling it not to curl because "a separate QA agent handles that" is organizational overhead imposed on the agent.

"Do NOT invent unnecessary improvements" — the agent should use judgment. If it sees a bug while implementing, should it ignore it? If a small refactor makes the implementation cleaner, should it skip it?

**Fix**: Remove "stay in your lane." Let the agent decide what's necessary. The spec is the contract — as long as the spec is met, the agent's approach is its own business.

---

## THIRD PASS: Code-Level Findings

### S13. Shell injection via test_command and verify_cmd

**Files**: `verify.py:268-288`, `runner.py:141`

`test_command` (from otto.yaml or auto-detected) and `verify_cmd` (from tasks.yaml) are passed to `subprocess.run(..., shell=True)`. These are user-configurable strings.

```python
# verify.py — test_command interpolated into f-string, then shell=True
cmd = f"{test_command} {rel_path}"
result = _run_shell_command(cmd, workdir, timeout, env=env)

# runner.py — test_command run with shell=True directly
result = subprocess.run(test_command, shell=True, ...)
```

In practice, these come from the user's own config (not untrusted input), and otto runs with the user's permissions anyway. But it's still bad hygiene — an accidental `test_command: "pytest; rm -rf ."` would execute.

**Fix**: Use `shlex.split()` and pass as a list (no shell=True). Or validate commands before execution (no `;`, `|`, `$()`, backticks).

---

### S14. Temp file leaks on error paths

**Files**: `spec.py:117-118`, `spec.py:293-294`

Temp files created with `delete=False` are cleaned up on the happy path but leaked if the agent errors before reaching the cleanup line:

```python
with tempfile.NamedTemporaryFile(suffix=".txt", prefix="otto_spec_", delete=False) as temp_file:
    spec_file = Path(temp_file.name)
# ... if agent crashes here, spec_file is never deleted
```

**Fix**: Use try/finally for cleanup, or use a temp directory (cleaned up at the end of the function).

---

### S15. Worktree cleanup gaps on crash paths

**Files**: `runner.py:1368-1401`

Worktree setup/teardown uses `subprocess.run(capture_output=True)` without `check=True` for cleanup steps. If cleanup fails (locked file, permissions), the error is silently lost. The subsequent `shutil.rmtree(ignore_errors=True)` further masks failures.

**Fix**: Log cleanup failures to telemetry. Don't `ignore_errors` silently — at minimum warn.

---

### S16. Broad exception swallowing in display code

**Files**: `pilot_v3.py:725-752`

```python
def _safe_console_print(*args, **kwargs) -> None:
    try:
        console.print(*args, **kwargs)
    except Exception:
        pass
```

Multiple `_safe_*` wrappers catch ALL exceptions. These exist to prevent display failures from crashing the pilot, which is reasonable. But they also hide bugs in the display functions. At minimum, log swallowed exceptions to the debug log.
