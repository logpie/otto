# Otto — Project Instructions

## Debugging & Log Analysis

When debugging otto runs, ALWAYS read real logs. Never guess. Check in this order:

### Quick diagnosis
```bash
otto status                           # Current task states
cat otto_logs/run-history.jsonl       # Cost, time, pass/fail per run
```

### Per-task logs (otto_logs/{task_key}/)
| File | What it tells you |
|------|-------------------|
| `live-state.json` | Full phase timeline with timestamps, cost, status. Preserved after completion. |
| `task-summary.json` | Per-phase cost + timing breakdown, retry reasons. **Start here for cost/timing questions.** |
| `attempt-N-agent.log` | What the coding agent did — tool calls, file reads/writes. |
| `attempt-N-verify.log` | Full test suite output for that attempt. |
| `spec-agent.log` | What the spec agent read and generated. |
| `qa-agent.log` | What the QA agent ran — Bash commands + output. **Check this when QA passes but verify.sh fails.** |
| `qa-tier.log` | QA decision log — task context, attempt info. |
| `qa-report.md` | QA agent's reasoning text. |
| `qa-verdict.json` | Structured verdict with per-item pass/fail + evidence. |
| `qa-proofs/proof-report.md` | Human-readable proof per [must] item. |
| `qa-proofs/regression-check.sh` | Re-runnable verification commands. |
| `cost-warning.log` | Warnings when parallel task reports $0 cost (SDK concurrency bug). |

### Run-level logs (otto_logs/)
| File | What it tells you |
|------|-------------------|
| `planner.log` | Task analysis, relationship classification, conflicts, batch structure, cost. **Check when tasks are batched wrong or dropped.** Single LLM call (no more two-phase shortlist). |
| `orchestrator.log` | Batch decisions, merge attempts, parallel worktree lifecycle, replan triggers, rollback. **Check for parallel/merge issues.** |
| `v4_events.jsonl` | Phase-level timeline with timestamps + cost. Grep for `phase_completed` events. **Reconstruct full run timeline.** |
| `run-history.jsonl` | One line per run: tasks, cost, time, failures. |

### Common debugging patterns

**"Why did this task cost so much?"**
→ Read `task-summary.json` — check `phase_costs` breakdown (coding vs qa vs spec).

**"Why did QA pass when the code is wrong?"**
→ Read `qa-agent.log` — did QA actually run behavioral tests or just read code?
→ Read `qa-agent.log` — did QA actually test behaviorally or just read code?

**"Why was this task serialized instead of parallelized?"**
→ Read `planner.log` — check relationship analysis and conflict detection.

**"Why did the merge conflict retry take so long?"**
→ Read `orchestrator.log` — look for "merge retry" entries with error type and feedback path.
→ Read the retry attempt's `attempt-N-agent.log` — did the agent use the diff feedback or re-explore from scratch?
→ Compare attempt-1 (original) vs attempt-2 (retry) — retry should be faster if feedback worked.

**"What happened during a parallel run?"**
→ Read `orchestrator.log` — shows semaphore acquire/release, worktree creation, dep install timing, coding_loop results per task.

**"Why did batch QA fail?"**
→ Read `otto_logs/batch-qa/qa-proofs/proof-report.md` — per-item results.
→ Check `v4_events.jsonl` for `phase_completed` events around the batch QA time.

## Key Principles

- Otto-owned paths (`otto_logs/`, `tasks.yaml`, `.otto-worktrees/`) must NEVER leak into agent prompts, git commits, or diffs. Filter via `_is_otto_owned()`.
- `system_prompt` must use `{"type": "preset", "preset": "claude_code"}` — NEVER `None` (blanks CC defaults), NEVER invented fields like `"append"`.
- Trust the agent — give full data or skip entirely. Never truncate/cap what you pass to an agent.
- Before blaming otto for a pressure test failure, verify the ground truth (`verify.sh`) is correct.
