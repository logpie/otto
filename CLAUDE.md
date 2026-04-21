# Otto — Project Instructions

## Debugging & Log Analysis

When debugging otto runs, ALWAYS read real logs. Never guess.

### Quick diagnosis
```bash
otto history                                          # Build history with results
cat otto_logs/cross-sessions/history.jsonl            # Machine-readable history
readlink otto_logs/latest                             # Most recent session
readlink otto_logs/paused                             # Resumable session (if any)
```

### Per-session layout (`otto_logs/sessions/<session-id>/`)

Every `otto build | certify | improve` invocation creates one session dir.
Session id format: `<yyyy-mm-dd>-<HHMMSS>-<6hex>`.

| File / dir | What it tells you |
|------|-------------------|
| `summary.json` | Post-run: verdict, cost, duration, stories, status |
| `checkpoint.json` | Resume state — exists only while running/paused |
| `intent.txt` | Archival copy of the intent at session start |
| `spec/spec.md` | Approved spec (spec-gate); versioned `spec-v1.md…` |
| `spec/agent.log` | Spec agent trace |
| `build/live.log` | Streamed tool/thinking activity (timestamped) |
| `build/agent.log` | Post-run structured summary: commits, certifier rounds, verdict |
| `build/agent-raw.log` | Full unfiltered agent output (streamed) |
| `certify/proof-of-work.html` | Styled report with screenshots + video link |
| `certify/proof-of-work.json` | Machine-readable: stories, evidence, round history |
| `certify/proof-of-work.md` | Markdown summary |
| `certify/evidence/*.png` | Browser screenshots |
| `certify/evidence/recording.webm` | Browser walkthrough video |
| `improve/session-report.md` | Final `otto improve` summary + merge instructions |
| `improve/build-journal.md` | Round-by-round index: action, result, cost |
| `improve/current-state.md` | Latest certifier findings (handoff to fix agent) |
| `improve/rounds/<round-id>/` | Per-round evidence: certifier findings, builder summary |

### Cross-session indexes (`otto_logs/cross-sessions/`)

| File | What it tells you |
|------|-------------------|
| `history.jsonl` | One line per completed session (intent, cost, time, passed) |
| `certifier-memory.jsonl` | One line per cert run (for cross-run memory) |

### Runtime-input files (project root)

| File | Role |
|------|------|
| `intent.md` | Canonical product description (git-tracked, appended per build) |
| `otto.yaml` | Project config |
| `CLAUDE.md` | Agent instructions |

### Legacy layout (pre-restructure)

Older projects may still have `otto_logs/runs/`, `otto_logs/builds/`,
`otto_logs/certifier/`, `otto_logs/checkpoint.json`,
`otto_logs/run-history.jsonl`, `otto_logs/certifier-memory.jsonl`. Otto
reads these as fallbacks — `otto history` merges old + new
chronologically, `--resume` detects legacy checkpoints. No migration
command; clean up by `rm`-ing legacy subdirs if desired.

### Common debugging patterns

**"Why did the build fail?"**
→ Read `otto_logs/latest/build/agent.log` — look for STORY_RESULT lines.

**"What did the certifier test?"**
→ Read `otto_logs/latest/certify/proof-of-work.json`.

**"Did the fix loop trigger?"**
→ `build/agent.log` — CERTIFY_ROUND: 1 (FAIL) → CERTIFY_ROUND: 2 (PASS).
→ `certify/proof-of-work.json` → `certify_rounds` and `round_history`.

**"How much did it cost?"**
→ `summary.json` → `cost_usd`. Or `otto history`.

**"Live tail during a run?"**
→ `tail -f otto_logs/latest/build/live.log` (until Phase 6 replaces it
with `narrative.log`).

## Key Principles

- `system_prompt` must use `{"type": "preset", "preset": "claude_code"}` — NEVER `None`.
- Trust the agent — give full data or skip entirely. Never truncate/cap.
- Prompts live in `otto/prompts/*.md` — edit without touching Python code.
- The certifier reports symptoms, not fixes. The coding agent diagnoses.
- `otto_logs/` paths must NEVER leak into agent prompts or git commits.
- All path construction goes through `otto/paths.py` — no hardcoded
  `"otto_logs/..."` literals elsewhere.
