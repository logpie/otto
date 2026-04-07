# Otto Architecture

## Overview

Otto builds products from natural language intent. One autonomous agent session drives the entire lifecycle: plan → build → test → certify → fix → re-certify.

```
otto build "REST API with auth, CRUD notes, search"
  │
  └─ SDK launches one coding agent session
       │
       ├─ Phase 1: BUILD
       │    Plan architecture → implement → write tests → self-review → commit
       │    (subagents for parallel features via Agent tool)
       │
       ├─ Phase 2: CERTIFY
       │    Dispatch certifier agent (builder-blind subagent)
       │    Certifier reads project fresh, tests as real user
       │    Returns: PASS/FAIL per story + evidence
       │
       ├─ Phase 3: FIX (if FAIL)
       │    Read certifier findings → fix code → run tests → commit
       │    Re-dispatch certifier → repeat until PASS
       │
       └─ Done: proof-of-work report + build summary
```

## Key Design Decisions

### Certifier as environment

The certifier is an environment signal — like a test suite. The coding agent dispatches it via the Agent tool and reads the result. The certifier reports **symptoms** ("check/circle buttons display raw HTML entities") not fixes ("change `<%= to `<%-`"). The coding agent diagnoses and fixes.

This is the core architectural insight: the coding agent treats certification like running `pytest` — dispatch, read failures, fix, re-run.

### Builder-blind certification

The certifier is a separate agent session (dispatched via Agent tool). It has zero knowledge of how the product was built — it reads the project files fresh, discovers the framework, installs deps, starts the app, and tests as a real user would. This prevents unconscious bias (testing what was built vs what should work).

### One session, no orchestrator

The coding agent drives everything — build, certify, fix loop. No external orchestrator manages the loop. This is simpler, cheaper (~50% vs split sessions), and the agent has full context across rounds.

### Subagent parallelism

Both the coding agent and certifier use the Agent tool for parallelism:
- **Coding**: dispatches subagents for independent features (auth module, CRUD API, etc.)
- **Certifier**: dispatches subagents for parallel story testing (first experience, CRUD lifecycle, edge cases, etc.)

The Agent tool creates fresh CC subprocesses — each subagent has isolated context.

### Auth discovery once

The certifier discovers auth once (register → login → capture token/cookie) and passes working auth commands to every test subagent. This prevents each subagent from fumbling auth from scratch (~20 turns saved per story).

## Code Map

### Default path: `otto build "intent"`

```
cli.py:build()
  └─ pipeline.py:build_agentic_v3()
       ├─ agent.py:run_agent_query(prompt, capture_tool_output=True)
       │    └─ SDK query() → single agent session
       │         ├─ Agent plans + builds + tests + commits
       │         ├─ Agent dispatches certifier subagent
       │         │    └─ certifier/__init__.py:CERTIFIER_AGENTIC_PROMPT
       │         │         ├─ Reads project, installs deps, starts app
       │         │         ├─ Dispatches test subagents (parallel stories)
       │         │         └─ Returns STORY_RESULT + VERDICT markers
       │         ├─ If FAIL: agent fixes + re-dispatches
       │         └─ Agent repeats markers in final message
       │
       ├─ Parse STORY_RESULT/VERDICT from agent text
       ├─ Write agent.log (structured) + agent-raw.log (full)
       ├─ Write proof-of-work.{json,html,md}
       └─ Return BuildResult
```

### Split path: `otto build --split`

```
pipeline.py:build_agentic_v2()
  ├─ Session 1: coding agent (AgentSession, persists via resume)
  ├─ Session 2: certifier agent (fresh each round)
  └─ Loop: build → certify → if fail: resume coding with findings → re-certify
```

### Orchestrated path: `otto build --orchestrated`

```
pipeline.py:build_product()
  └─ orchestrator.py:run_per()
       ├─ Planner → batches → parallel worktrees
       ├─ Per-task: coding agent → tests → QA
       └─ Merge → batch QA → retry/rollback
```

## File Reference

| File | Lines | Role |
|------|------:|------|
| `pipeline.py` | 854 | Build pipelines (v3, v2, orchestrated) |
| `certifier/__init__.py` | 440 | Agentic certifier + PoW generation |
| `certifier/report.py` | 121 | CertificationReport dataclasses |
| `agent.py` | 496 | Agent SDK wrapper |
| `session.py` | 280 | AgentSession for split mode |
| `cli.py` | 987 | CLI commands |
| `config.py` | 410 | Config + otto.yaml |
| `orchestrator.py` | 3469 | PER pipeline (--orchestrated) |
| `runner.py` | 2334 | Task runner (orchestrated) |
| `qa.py` | 2065 | QA agent (orchestrated) |

## Observability

### Build logs

```
otto_logs/builds/<build-id>/
  agent.log           Structured: timestamps, commits, certifier rounds, verdict
  agent-raw.log       Full unfiltered agent output (for debugging)
  checkpoint.json     Build metadata: cost, duration, stories, rounds
```

### Certifier reports

```
otto_logs/certifier/
  proof-of-work.json  Machine-readable: stories, evidence, round history
  proof-of-work.html  Styled HTML with collapsible evidence per story
  proof-of-work.md    Markdown summary
```

### Debugging

| Question | Where to look |
|----------|---------------|
| What was built? | `agent.log` → git commits |
| What did certification find? | `agent.log` → STORY_RESULT lines |
| Why did it fail? | `agent.log` → FAIL stories + DIAGNOSIS |
| What was fixed? | `agent.log` → git commits between rounds |
| Full agent trace? | `agent-raw.log` |
| Cost breakdown? | `checkpoint.json` |
