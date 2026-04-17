# Otto Architecture

## Overview

Otto is ~4,000 lines of Python. It builds, certifies, and improves software
products using LLM agents.

```
otto build "bookmark manager"          # build + certify + fix
otto certify                           # standalone verification
otto improve bugs                      # find and fix bugs
otto improve feature "search UX"       # suggest and implement improvements
otto improve target "latency < 100ms"  # optimize toward a metric
```

## System Diagram

```
┌─────────────────────────────────────────────────────┐
│                     CLI Layer                        │
│  cli.py (build, certify)  cli_improve.py (improve)  │
└──────────┬─────────────────────┬────────────────────┘
           │                     │
     ┌─────▼──────┐    ┌────────▼─────────┐
     │ Agent Mode │    │   Split Mode     │
     │ (default)  │    │   (--split)      │
     └─────┬──────┘    └────────┬─────────┘
           │                     │
     ┌─────▼──────────┐  ┌──────▼───────────────┐
     │ build_agentic  │  │ run_certify_fix_loop  │
     │ _v3()          │  │                       │
     │                │  │  ┌──► certify ──┐     │
     │  One agent     │  │  │              │     │
     │  session:      │  │  │  if fail:    │     │
     │  build/certify │  │  │   ┌──────┐   │     │
     │  /fix loop     │  │  └──┤ fix  ├───┘     │
     │                │  │     └──────┘          │
     └────────┬───────┘  └──────────┬────────────┘
              │                      │
        ┌─────▼──────────────────────▼─────┐
        │        Agent SDK Layer           │
        │  run_agent_with_timeout()        │
        │  • live logging                  │
        │  • timeout + orphan cleanup      │
        │  • retry on error                │
        │  • session_id for resume         │
        └─────────────┬───────────────────┘
                      │
        ┌─────────────▼───────────────────┐
        │     Claude Code / Codex CLI      │
        └──────────────────────────────────┘
```

## Build Flow (Agent Mode)

```
otto build "bookmark manager with tags"
│
├─ Load build.md prompt (explore → build → test → certify → fix → report)
├─ Pre-fill certifier prompt (certifier-thorough.md with {intent})
├─ Inject cross-run memory (if enabled)
│
└─ Single agent session ──────────────────────────────────────┐
     │                                                         │
     ├─ 1. Explore project, plan architecture                  │
     ├─ 2. Build code, write tests, commit                     │
     ├─ 3. Dispatch certifier subagent ───┐                    │
     │                                     │                    │
     │    Certifier (builder-blind):       │                    │
     │    ├─ Read project fresh            │                    │
     │    ├─ Install deps, start app       │                    │
     │    ├─ Test 5-10 user stories        │                    │
     │    └─ Report: PASS/FAIL per story   │                    │
     │                                     │                    │
     ├─ 4. Read findings ◄────────────────┘                    │
     ├─ 5. If FAIL: fix code, commit, re-dispatch certifier    │
     ├─ 6. Repeat until two consecutive PASSes                 │
     └─ 7. Report structured markers ─────────────────────────┘
                    │
                    ▼
        markers.py: parse STORY_RESULT, VERDICT, CERTIFY_ROUND
                    │
                    ▼
        Write: agent.log, proof-of-work.{json,html}, checkpoint.json
```

## Improve Flow (Split Mode)

```
otto improve bugs "error handling" --split -n 5
│
├─ Create improvement branch (improve/2026-04-17)
├─ Load config, set max_rounds=5
│
└─ Python-driven loop ──────────────────────────────────────┐
     │                                                       │
     │  ┌─── Round 1 ────────────────────────────────────┐   │
     │  │                                                 │   │
     │  │  Certify: fresh certifier agent session         │   │
     │  │  ├─ Load certifier-thorough.md                  │   │
     │  │  ├─ Test product, report findings               │   │
     │  │  └─ Parse results (markers.py)                  │   │
     │  │                                                 │   │
     │  │  If FAIL:                                       │   │
     │  │  Fix: fresh code agent session                  │   │
     │  │  ├─ Load code.md                                │   │
     │  │  ├─ Inject failures + previous attempts         │   │
     │  │  └─ Fix code, commit                            │   │
     │  │                                                 │   │
     │  │  Write checkpoint ──► checkpoint.json            │   │
     │  │  Write journal ──► build-journal.md              │   │
     │  │                                                 │   │
     │  └─────────────────────────────────────────────────┘   │
     │                                                       │
     │  Round 2, 3, ... (until PASS or max_rounds)           │
     └───────────────────────────────────────────────────────┘
```

## Improve Flow (Agent Mode — Default)

```
otto improve bugs "error handling" -n 5
│
├─ Create improvement branch
│
└─ Single agent session (improve.md prompt) ─────────────────┐
     │                                                        │
     ├─ 1. Explore project                                    │
     ├─ 2. Dispatch certifier subagent                        │
     ├─ 3. Read findings, fix, re-dispatch                    │
     ├─ 4. Repeat until two consecutive PASSes                │
     └─ 5. Report markers ───────────────────────────────────┘
                    │
     Agent IS the memory (single session, auto-compact)
```

## Certifier Modes

```
                    ┌─────────────┐
                    │   certify   │
                    └──────┬──────┘
                           │
         ┌─────────┬───────┼────────┬──────────┐
         ▼         ▼       ▼        ▼          ▼
     ┌───────┐ ┌───────┐ ┌──────┐ ┌────────┐ ┌───────┐
     │ fast  │ │ std   │ │ thor │ │ hill   │ │target │
     │       │ │       │ │ ough │ │ climb  │ │       │
     └───┬───┘ └───┬───┘ └──┬───┘ └───┬────┘ └───┬───┘
         │         │        │         │           │
      3-5 happy  full     adver-   suggest    measure
      paths,     verify   sarial,  features,  metric,
      inline,    + sub-   edge     UX gaps    compare
      ~30s       agents   cases               to target
                 ~2min    ~5min    ~3min      ~2min
```

## Checkpoint & Resume

```
otto improve bugs --split -n 50
│
├─ Round 1 ──► checkpoint.json {round:1, status:in_progress}
│   ├─ Certify ──► checkpoint.json {round:1, stories:...}
│   └─ Fix
│
├─ Round 2 ──► checkpoint.json {round:2, ...}
│   ├─ Certify
│   └─ Fix
│
├─ [CRASH or Ctrl+C]
│   └─ checkpoint.json {round:2, status:paused}
│
└─ otto improve bugs --split --resume
    └─ Reads checkpoint → "Resuming from round 3"
       └─ Round 3, 4, ... continues
```

Error retry: certifier/build retried up to 2x on failure before moving on.

## Prompts

All prompts are markdown files — edit without touching Python.

```
otto/prompts/
  build.md                  Agent-mode build (explore→build→certify→fix loop)
  improve.md                Agent-mode improve (certify→fix loop, no build step)
  code.md                   Code-only (split-mode fix agent, no cert knowledge)
  certifier.md              Standard verification (subagents, screenshots)
  certifier-fast.md         Happy path smoke test (inline, ~30s)
  certifier-thorough.md     Adversarial (edge cases, code review)
  certifier-hillclimb.md    Product improvements (missing features, UX)
  certifier-target.md       Metric measurement (METRIC_VALUE/METRIC_MET)
```

## Key Modules

| Module | Lines | Purpose |
|--------|------:|---------|
| `pipeline.py` | 880 | `build_agentic_v3`, `run_certify_fix_loop`, PoW reports |
| `agent.py` | 593 | SDK abstraction, `run_agent_with_timeout`, provider switching |
| `certifier/__init__.py` | 371 | Standalone certifier, PoW generation |
| `cli_improve.py` | 349 | `improve` command group (bugs/feature/target) |
| `config.py` | 334 | Config loading, auto-detection, helpers |
| `journal.py` | 245 | Build journal: round tracking, current state |
| `cli.py` | 243 | `build`, `certify` commands |
| `markers.py` | 238 | Parse STORY_RESULT/VERDICT/METRIC from agent output |
| `checkpoint.py` | 117 | Checkpoint read/write/clear for resume |
| `memory.py` | 137 | Cross-run certifier memory (opt-in) |

## Error Handling

Centralized in `run_agent_with_timeout()`:
- **Timeout**: Configurable via `certifier_timeout` (default 900s). Orphan processes cleaned up.
- **Agent crash**: `AgentCallError` raised. Callers retry up to 2x.
- **No output**: No verdict markers → treated as FAIL.
- **KeyboardInterrupt**: Checkpoint written (status=paused), re-raised.

## Observability

| Question | Where to look |
|----------|---------------|
| What was built/fixed? | `otto_logs/builds/<id>/agent.log` |
| Full agent trace? | `agent-raw.log` |
| Live tool calls? | `live.log` (timestamped) |
| Certifier results? | `otto_logs/certifier/*/proof-of-work.{json,html}` |
| Build history? | `otto history` or `run-history.jsonl` |
| Improve progress? | `build-journal.md`, `checkpoint.json` |
| Cost? | `checkpoint.json` → `total_cost` |

## Data Flow

```
User intent
    │
    ▼
┌─────────┐     ┌──────────┐     ┌──────────┐
│  Build  │────▶│ Certify  │────▶│  Parse   │
│  Agent  │     │  Agent   │     │ Markers  │
└─────────┘     └──────────┘     └────┬─────┘
                                      │
              ┌───────────────────────┤
              │                       │
        ┌─────▼─────┐          ┌─────▼─────┐
        │   Logs    │          │  Reports  │
        │           │          │           │
        │ agent.log │          │ PoW.html  │
        │ live.log  │          │ PoW.json  │
        │ history   │          │ journal   │
        └───────────┘          └───────────┘
```
