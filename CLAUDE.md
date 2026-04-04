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
| `qa-agent.log` | What the QA agent ran — ALL tool calls (Bash, Write, Read, Grep, etc.) + timestamps. **Check this when QA passes but verify.sh fails.** |
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
| `events.jsonl` | Phase-level timeline with timestamps + cost. Grep for `phase_completed` events. **Reconstruct full run timeline.** |
| `run-history.jsonl` | One line per run: tasks, cost, time, failures. |

### Common debugging patterns

**"Why did this task cost so much?"**
→ Read `task-summary.json` — check `phase_costs` breakdown (coding vs qa vs spec).

**"Why did QA pass when the code is wrong?"**
→ Read `qa-agent.log` — did QA actually run behavioral tests or just read code?
→ Check tool call timestamps — did QA spend time testing or just writing the verdict?
→ Check "extras" field in verdict — did BREAK phase find anything?

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
→ Check `events.jsonl` for `phase_completed` events around the batch QA time.

## Logging Standards

Every log file otto writes must follow these rules. We learned them the hard way — each rule exists because its absence caused a real debugging failure.

### 1. Timestamp everything
Every log write must include wall-clock time. Without timestamps, you can't correlate events across files or diagnose "where did the 367 seconds go?"

- JSON files: include `_written_at` or `_updated_at` field
- Text/markdown files: `_write_log_safe()` auto-adds `<!-- generated: timestamp -->` header
- Append logs (orchestrator, planner, qa-agent): each entry block has a timestamp
- Tool call logs: elapsed time from phase start (`[  5.2s] ● Bash  grep ...`)

### 2. Never overwrite — append or number
Log files that get written multiple times across retries must preserve ALL attempts:

- **Append mode** for accumulating logs: `qa-agent.log`, `qa-tier.log`, `spec-agent.log`, `orchestrator.log`, `planner.log`
- **Numbered files** for per-attempt artifacts: `attempt-N-agent.log`, `attempt-N-verify.log`, `attempt-N-qa-report.md`, `attempt-N-qa-verdict.json`, `attempt-N-proof-report.md`
- **"Latest" + copies**: `qa-report.md` is always the latest (tools read this), plus `attempt-N-qa-report.md` for history

Why: we found that QA retry logs were being overwritten, making it impossible to see WHY the first attempt failed. The proof that caught a real bug was gone by the time we looked.

### 3. Log the right level of detail
- **Tool calls**: log tool name, key input (truncated to ~120 chars), output (truncated to ~200 chars), elapsed time
- **Phase transitions**: log phase name, status (running/done/fail), elapsed time, cost
- **Decisions**: log WHY, not just WHAT. QA tier log says "risk patterns: none matched, spa detection: no" — the reasoning, not just "tier: 1"
- **Errors**: log the full error, not "command failed." Include stderr, exit code, which file/function

### 4. SDK session metrics
Every agent session (coding, QA, spec, planner) should log:
- SDK init time (time to first message)
- Total wall clock time
- Turn count (number of messages exchanged)
- Cost

We added this to QA and it immediately showed that SDK init was 0.4s (not the bottleneck) and that the time was in LLM output generation. Without these metrics, we guessed wrong about MCP server startup being slow.

### 5. Log honestly
If dep install fails, don't log "deps installed." If a verdict file is malformed, log the parse error. We found `_install_deps` logging success unconditionally while the install actually failed, and verdict parse errors being silently swallowed.

### 6. Proof artifacts must survive retries
Proof reports (`proof-report.md`, `must-N.md`) are the human-auditable evidence that QA worked. If QA retries, the first attempt's proof shows WHY it failed — that's often more valuable than the passing proof. Always write per-attempt copies.

## Key Principles

- Otto-owned paths (`otto_logs/`, `tasks.yaml`, `.otto-worktrees/`) must NEVER leak into agent prompts, git commits, or diffs. Filter via `_is_otto_owned()`.
- `system_prompt` must use `{"type": "preset", "preset": "claude_code"}` — NEVER `None` (blanks CC defaults), NEVER invented fields like `"append"`.
- Trust the agent — give full data or skip entirely. Never truncate/cap what you pass to an agent.
- Before blaming otto for a pressure test failure, verify the ground truth (`verify.sh`) is correct.
