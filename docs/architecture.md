# Otto Architecture

## Overview

Otto is 4,982 lines of Python. Two commands: `build` and `certify`.

```
otto build "intent"     → one agent builds, certifies, fixes
otto certify "intent"   → standalone verification on any project
```

## Core Design

### One agent, one session

`build_agentic_v3()` launches a single SDK agent session. The agent drives everything:
build → test → dispatch certifier subagent → read findings → fix → re-dispatch.
No orchestrator. No task queue. The agent IS the orchestrator.

### Certifier as environment

The certifier is a subagent dispatched via the Agent tool. It's builder-blind —
reads the project fresh, doesn't know how it was built. It reports symptoms
("POST /toggle returns 500 without auth"), not fixes. The coding agent diagnoses
and fixes.

This is like running `pytest` — the coding agent calls it, reads the output, acts.

### Prompts are files

Prompts live in `otto/prompts/build.md` and `otto/prompts/certifier.md`.
Edit without touching Python. Loaded at runtime by `otto/prompts/__init__.py`.

## Code Map

```
otto build "intent"
  cli.py → build()
    pipeline.py → build_agentic_v3()
      prompts/__init__.py → load build.md
      agent.py → run_agent_query(prompt, capture_tool_output=True)
        SDK query() → single agent session
          Agent builds, tests, commits
          Agent dispatches certifier via Agent tool
            prompts/__init__.py → load certifier.md
            Certifier reads project, tests, returns verdict markers
          Agent reads verdict, fixes if FAIL, re-dispatches
      Parse STORY_RESULT/VERDICT from agent text
      Write: agent.log, agent-raw.log, checkpoint.json
      Write: proof-of-work.{json,html,md}, evidence/*.png, recording.webm
      Write: run-history.jsonl, intent.md
      Return BuildResult

otto certify "intent"
  cli.py → certify()
    certifier/__init__.py → run_agentic_certifier()
      prompts/__init__.py → load certifier.md
      agent.py → run_agent_query(prompt)
      Parse verdict, write PoW reports
      Return CertificationReport
```

## Files

| File | Lines | Purpose |
|------|------:|---------|
| `pipeline.py` | 467 | `build_agentic_v3`, result parsing, intent/history |
| `certifier/__init__.py` | 408 | `run_agentic_certifier`, PoW generation |
| `certifier/report.py` | 121 | Dataclasses: CertificationReport, Finding |
| `prompts/build.md` | 129 | Build prompt (editable) |
| `prompts/certifier.md` | 85 | Certifier prompt (editable) |
| `agent.py` | 496 | SDK wrapper: query, run_agent_query |
| `cli.py` | 265 | CLI: build, certify + display |
| `cli_logs.py` | 95 | CLI: history command |
| `cli_setup.py` | 89 | CLI: setup command |
| `cli_bench.py` | 267 | CLI: bench command |
| `config.py` | 410 | Config loading, otto.yaml creation |
| `display.py` | 1283 | Terminal display (partially legacy) |
| `observability.py` | 43 | Log writing utilities |
| `testing.py` | 432 | Test detection, subprocess env |
| `telemetry.py` | 266 | Usage telemetry |
| `theme.py` | 12 | Console styling |

## Error handling

- **SDK crash**: `run_agent_query` wrapped in try/except. Build returns error text.
  Certifier returns `CertificationOutcome.INFRA_ERROR`.
- **Timeout**: Configurable via `certifier_timeout` in otto.yaml (default 900s).
- **No output**: Agent produces no verdict markers → treated as FAIL.
- **KeyboardInterrupt**: Always re-raised (user abort).

## Retry with context

When a build fails and the user re-runs `otto build`:
1. Pipeline reads `run-history.jsonl` — last build failed?
2. Reads `proof-of-work.json` — what specific stories failed?
3. Injects findings into the prompt: "Previous build failed: S3 returned 500..."
4. Agent sees the failures, avoids repeating them.

## Observability

| Question | Where to look |
|----------|---------------|
| What was built? | `agent.log` → git commits |
| What did certification find? | `agent.log` → STORY_RESULT lines |
| Why did it fail? | `agent.log` → FAIL stories + DIAGNOSIS |
| Full agent trace? | `agent-raw.log` |
| Visual evidence? | `evidence/*.png`, `evidence/recording.webm` |
| Build history? | `otto history` or `run-history.jsonl` |
| Cost? | `checkpoint.json` |

## Real-world validation

Tested on 4 open-source Flask projects (code we didn't write):
- 3/4 had real bugs the certifier caught
- 8 total bugs found, 0 false positives
- Critical: auth bypass, data isolation failure (`user_id == user_id`)
- See `docs/real-world-validation-2026-04-10.md`
