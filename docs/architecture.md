# Otto Architecture

## Overview

Otto is ~3,500 lines of Python. Three commands: `build`, `certify`, `improve`.

```
otto build "intent"                    # one agent builds, certifies, fixes
otto certify                           # standalone verification on any project
otto improve bugs                      # find and fix bugs
otto improve feature "search UX"       # suggest and implement improvements
otto improve target "latency < 100ms"  # optimize toward a metric target
```

## Core Design

### Two primitives

1. **Code agent** — builds/fixes code. Uses `code.md` prompt (steps 1-6: explore, plan, build, test, self-review, commit). No certification knowledge.

2. **Certifier agent** — evaluates a product. Builder-blind: reads the project fresh, doesn't know how it was built. Reports symptoms, not fixes.

### Two orchestration modes

**Agent-driven (mono)** — `otto build`: one agent session drives everything. The build prompt (`build.md`) includes certification steps 7-9: dispatch certifier as subagent, read findings, fix, re-dispatch. Agent IS the orchestrator.

**System-driven (loop)** — `otto improve`: Python drives `certify → fix → certify → fix` loop. Each step is a fresh agent call. Loop terminates when certifier finds no issues (bugs/feature mode) or metric meets target (target mode).

### Prompts are files

```
otto/prompts/
  build.md                # Full build + certify loop (mono mode)
  code.md                 # Code-only (system-driven mode)
  certifier.md            # Standard verification
  certifier-thorough.md   # Adversarial bug hunting (improve bugs)
  certifier-hillclimb.md  # Feature suggestions (improve feature)
  certifier-target.md     # Metric measurement (improve target)
```

Edit without touching Python. Loaded at runtime by `otto/prompts/__init__.py`.

## Code Map

```
otto build "intent"
  cli.py → build()
    pipeline.py → build_agentic_v3()
      Load build.md + pre-fill certifier prompt
      agent.py → run_agent_with_timeout()
        Agent builds, tests, dispatches certifier, reads findings, fixes
      markers.py → parse_certifier_markers() → stories, verdict, rounds
      Write logs, PoW reports, run history
      Return BuildResult

otto certify
  cli.py → certify()
    certifier/__init__.py → run_agentic_certifier()
      Load certifier prompt, fill {intent}/{evidence_dir}
      agent.py → run_agent_with_timeout()
      markers.py → parse results
      Write PoW reports
      Return CertificationReport

otto improve bugs/feature/target
  cli_improve.py → improve group
    Create improvement branch
    pipeline.py → run_certify_fix_loop()
      Loop: certifier → parse results → if not done: code agent fixes → repeat
      Target mode: checks METRIC_MET instead of story pass/fail
      Journal: tracks rounds, findings, current state
    Display results, write report
```

## Files

| File | Lines | Purpose |
|------|------:|---------|
| `pipeline.py` | 751 | `build_agentic_v3`, `run_certify_fix_loop`, result parsing, PoW |
| `agent.py` | 591 | SDK wrapper, `run_agent_with_timeout`, `make_agent_options` |
| `certifier/__init__.py` | 350 | `run_agentic_certifier`, PoW generation |
| `config.py` | 334 | Config loading, `resolve_intent`, `get_timeout`, `get_max_rounds` |
| `cli_improve.py` | 282 | `improve` command group: bugs, feature, target |
| `journal.py` | 245 | Build journal: round tracking, current state |
| `markers.py` | 238 | Parse STORY_RESULT, VERDICT, METRIC_VALUE from agent output |
| `cli.py` | 227 | CLI: build, certify |
| `cli_setup.py` | 215 | CLI: setup (generates CLAUDE.md) |
| `cli_logs.py` | 103 | CLI: history command |
| `certifier/report.py` | 21 | `CertificationReport`, `CertificationOutcome` |
| `prompts/__init__.py` | 41 | Prompt loader |

## Error handling

Centralized in `run_agent_with_timeout()`:
- **Timeout**: Configurable via `certifier_timeout` in otto.yaml (default 900s). Orphan processes cleaned up.
- **Agent crash**: Raises `AgentCallError`. Build returns error text; certifier returns `INFRA_ERROR`.
- **No output**: No verdict markers → treated as FAIL.
- **KeyboardInterrupt**: Always re-raised.

## Retry with context

When a build fails and the user re-runs `otto build`:
1. Pipeline reads `run-history.jsonl` — last build failed?
2. Reads `proof-of-work.json` — what stories failed?
3. Injects findings into prompt: "Previous build failed: auth returned 500..."
4. Agent avoids repeating the same mistakes.

## Observability

| Question | Where to look |
|----------|---------------|
| What was built? | `otto_logs/builds/<id>/agent.log` → git commits |
| What did certification find? | `agent.log` → STORY_RESULT lines |
| Full agent trace? | `agent-raw.log` |
| Visual evidence? | `otto_logs/certifier/*/evidence/` |
| Build history? | `otto history` or `run-history.jsonl` |
| Cost? | `otto_logs/builds/<id>/checkpoint.json` |
| Improve rounds? | `build-journal.md` |
