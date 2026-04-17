# Plan: Checkpoint/Resume for Long-Running Otto

## Goal

Make `otto improve` (and `otto build`) survive crashes, support pause/resume,
and handle errors gracefully. Works for both agent mode and split mode.

## Design

### Checkpoint file

`otto_logs/checkpoint.json` — one active checkpoint per project.

```json
{
  "run_id": "improve-bugs-1776415285",
  "command": "improve",
  "mode": "bugs",
  "certifier_mode": "thorough",
  "prompt_mode": "improve",
  "branch": "improve/2026-04-17",
  "focus": "auth and permissions",
  "target": null,
  "max_rounds": 50,
  "status": "in_progress",
  "started_at": "2026-04-17T01:00:00Z",
  "session_id": "3d16bdce-...",
  "current_round": 5,
  "head_sha": "abc123",
  "total_cost": 12.50,
  "rounds": [
    {"round": 1, "stories_tested": 8, "stories_passed": 5, "cost": 2.50},
    {"round": 2, "stories_tested": 8, "stories_passed": 7, "cost": 2.80},
    ...
  ]
}
```

### Resume flow

```
$ otto improve bugs -n 50
  Found checkpoint: round 5/50, $12.50 spent
  Resume from round 5? [Y/n]       # or --resume to auto-resume

Agent mode:
  → pass resume=session_id to SDK
  → Claude Code reconnects, has full transcript from auto-compact
  → agent continues certify-fix loop from where it left off

Split mode:
  → start new certifier session at round 6
  → inject memory from rounds 1-5 into prompt
  → continue loop
```

### Error retry

Both modes, inside the certify-fix loop:

```python
MAX_RETRIES = 2

for attempt in range(MAX_RETRIES + 1):
    try:
        report = await run_agentic_certifier(...)
        break
    except AgentCallError as err:
        if attempt < MAX_RETRIES:
            logger.warning("Round %d attempt %d failed: %s. Retrying...",
                          round_num, attempt + 1, err.reason)
            continue
        logger.error("Round %d failed after %d attempts", round_num, MAX_RETRIES + 1)
        # Record failure in checkpoint, continue to next round
```

Same pattern for build agent calls in split mode.

### Pause (KeyboardInterrupt)

Currently KeyboardInterrupt re-raises and kills the process. Change to:
1. Catch KeyboardInterrupt in the loop
2. Write checkpoint with status="paused"
3. Print "Paused at round N. Run again with --resume to continue."
4. Exit cleanly (not crash)

### Agent mode specifics

- `run_agent_with_timeout` returns session_id (change return to include it)
- Checkpoint stores session_id
- On resume: `options.resume = session_id`
- The SDK reconnects to the session — Claude Code has the full transcript
- If the session expired (too old), fall back to fresh start with memory

### Split mode specifics

- Each round is a fresh session — no session_id to resume
- Checkpoint stores round number + per-round results
- On resume: skip to round N+1, inject memory from previous rounds
- The memory system we built is the cross-round context

### CLI changes

```python
@click.option("--resume", is_flag=True, help="Resume from last checkpoint")

# On start:
checkpoint = load_checkpoint(project_dir)
if checkpoint and checkpoint["status"] == "in_progress":
    if resume_flag or click.confirm(f"Resume from round {checkpoint['current_round']}?"):
        # Resume
    else:
        clear_checkpoint(project_dir)
        # Fresh start
```

## Files changed

| File | Change |
|------|--------|
| `otto/checkpoint.py` | NEW — read/write/clear checkpoint |
| `otto/agent.py` | `run_agent_with_timeout` returns session_id |
| `otto/pipeline.py` | `build_agentic_v3` writes checkpoint, supports resume |
| `otto/pipeline.py` | `run_certify_fix_loop` writes checkpoint, supports resume, retry |
| `otto/cli_improve.py` | `--resume` flag, checkpoint detection |
| `otto/cli.py` | `--resume` flag for build |

## Verification

1. Start `otto improve bugs -n 5` → let it run 2 rounds → Ctrl+C
   - Verify: checkpoint.json written with round=2, status=paused
2. Run `otto improve bugs --resume`
   - Agent mode: verify session_id passed to SDK, agent continues
   - Split mode: verify starts at round 3, memory injected
3. Simulate crash: start run, kill -9 the process
   - Verify: checkpoint.json has status=in_progress
   - Resume detects it, asks to continue
4. Error retry: mock certifier to fail once then succeed
   - Verify: retry happens, round completes
