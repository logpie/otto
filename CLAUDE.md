# Otto — Project Instructions

## Debugging & Log Analysis

When debugging otto runs, ALWAYS read real logs. Never guess.

### Quick diagnosis
```bash
otto history                          # Build history with results
cat otto_logs/run-history.jsonl       # Machine-readable build log
```

### Build logs (otto_logs/builds/<build-id>/)
| File | What it tells you |
|------|-------------------|
| `agent.log` | Structured summary: git commits, certifier rounds, STORY_RESULT lines, verdict |
| `agent-raw.log` | Full unfiltered agent output (for deep debugging) |
| `checkpoint.json` | Build metadata: cost, duration, stories tested/passed, mode |

### Certifier logs (otto_logs/certifier/)
| File | What it tells you |
|------|-------------------|
| `proof-of-work.html` | Styled report with embedded screenshots + video link |
| `proof-of-work.json` | Machine-readable: stories, evidence, round history |
| `proof-of-work.md` | Markdown summary |
| `certifier-agent.log` | Full certifier agent output (standalone certify only) |
| `evidence/*.png` | Screenshots of web app pages |
| `evidence/recording.webm` | Video of browser walkthrough |

### Improve logs
| File | What it tells you |
|------|-------------------|
| `build-journal.md` | Round-by-round index: action, result, cost |
| `current-state.md` | Latest certifier findings (handoff for fix agent) |
| `improvement-report.md` | Final summary with branch info and merge instructions |

### Other logs
| File | What it tells you |
|------|-------------------|
| `run-history.jsonl` | One line per build: intent, cost, time, stories, passed |
| `intent.md` | Cumulative log of all build intents with timestamps |

### Common debugging patterns

**"Why did the build fail?"**
→ Read `agent.log` — look for STORY_RESULT lines with FAIL + DIAGNOSIS.

**"What did the certifier test?"**
→ Read `proof-of-work.json` — per-story pass/fail with evidence.

**"Did the fix loop trigger?"**
→ Read `agent.log` — look for CERTIFY_ROUND: 1 (FAIL) → CERTIFY_ROUND: 2 (PASS).
→ Check `proof-of-work.json` → `certify_rounds` and `round_history`.

**"How much did it cost?"**
→ `checkpoint.json` → `cost_usd`. Or `otto history`.

**"What was the visual evidence?"**
→ Open `proof-of-work.html` in a browser — screenshots embedded, video linked.

## Key Principles

- `system_prompt` must use `{"type": "preset", "preset": "claude_code"}` — NEVER `None`.
- Trust the agent — give full data or skip entirely. Never truncate/cap.
- Prompts live in `otto/prompts/*.md` — edit without touching Python code.
- The certifier reports symptoms, not fixes. The coding agent diagnoses.
- `otto_logs/` paths must NEVER leak into agent prompts or git commits.
