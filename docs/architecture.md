# Otto Architecture

## Overview

Otto is ~4,300 lines of Python. It builds, certifies, and improves software
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
      inline,    + sub-   edge     UX gaps    require
      ~30s       agents   cases               METRIC_MET
                 ~2min    ~5min    ~3min      ~2min
```

## Checkpoint & Resume

Both modes write `otto_logs/checkpoint.json`. Agent mode uses `session_id`
for SDK-level resume; split mode replays from the last completed round.

```
otto improve bugs --split -n 50
│
├─ Pre-write ──► checkpoint.json {status:in_progress, phase:initial_build}
├─ Initial build (if not resuming)
│
├─ Round 1 starts ──► checkpoint {phase:certify, current_round:0}   (last COMPLETED = 0)
│   ├─ Certify
│   ├─ Fix (on FAIL)
│   └─ Round complete ──► checkpoint {phase:round_complete, current_round:1}
│
├─ Round 2 starts ──► checkpoint {phase:certify, current_round:1}
│   ├─ Certify
│   ├─ [CRASH or Ctrl+C]
│   └─ checkpoint {status:paused, phase:certify, current_round:1}
│
└─ otto improve bugs --split --resume
    └─ resolve_resume reads checkpoint → start_round = current_round + 1 = 2
       └─ Replays round 2's certify phase (not round 3 — the crashed phase
          didn't complete, so we don't skip it).
```

**Key invariants:**
- `current_round` = last FULLY completed round (start with 0, advance only
  after the round's fix phase finishes).
- `phase` distinguishes where a crash happened. `phase=""` (old checkpoints)
  is treated as "unknown — don't skip the initial build."
- Checkpoint writes are atomic: `checkpoint.json.tmp` + `os.replace()` so a
  concurrent reader never sees a half-written file. Read path never touches
  `.tmp` — it belongs to an in-flight writer.
- Agent mode writes an `in_progress` checkpoint BEFORE calling the SDK so a
  crash mid-session still has a resumable marker (session_id may be empty
  until the agent returns).
- Inner `build_agentic_v3` calls from `run_certify_fix_loop` pass
  `manage_checkpoint=False` so they don't stomp the outer loop's checkpoint.
- Command attribution is fine-grained: checkpoints record
  `improve.bugs`/`.feature`/`.target` not just `improve`, so a mismatch
  warning fires if you resume under a different subcommand.
- `otto improve target --resume` inherits the goal from the checkpoint and
  hard-fails if the prior run wasn't `improve.target`.

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
| `pipeline.py` | 919 | `build_agentic_v3`, `run_certify_fix_loop`, PoW reports |
| `agent.py` | 596 | SDK abstraction, `run_agent_with_timeout`, provider switching |
| `cli_improve.py` | 380 | `improve` command group (bugs/feature/target), exit-code wiring |
| `certifier/__init__.py` | 350 | Standalone certifier, PoW generation, target-mode gate |
| `config.py` | 334 | Config loading, auto-detection, helpers |
| `cli.py` | 283 | `build`, `certify` commands |
| `journal.py` | 245 | Build journal: round tracking, current state |
| `markers.py` | 237 | Parse STORY_RESULT/VERDICT/METRIC_MET from agent output |
| `checkpoint.py` | 233 | Atomic checkpoint read/write/clear, `resolve_resume`, `ResumeState` |
| `memory.py` | 161 | Cross-run certifier memory (opt-in) |

## Error Handling

Centralized in `run_agent_with_timeout()`:
- **Timeout**: Derived from `RunBudget.for_call()` — the one timeout knob is
  `run_budget_seconds` (default 3600s), a wall-clock cap on the whole
  `otto build` / `otto certify` / `otto improve` invocation. Per-call timeouts
  shrink naturally as budget drains. `spec_timeout` (default 600s) is the
  only per-phase cap, applied inline in the spec agent call as
  `min(budget.remaining, spec_timeout)`. Orphan processes cleaned up.
- **Agent crash**: `AgentCallError` raised with preserved `session_id` from
  streaming state so `--resume` can continue the SDK conversation. Callers
  retry up to 2x for transient errors (not budget exhaustion).
- **No output**: No verdict markers → treated as FAIL.
- **KeyboardInterrupt**: Checkpoint written (status=paused, current phase
  recorded), re-raised.
- **Budget exhaustion**: Either pre-call (budget.exhausted()) or mid-call
  (AgentCallError from asyncio timeout) → `status=paused` checkpoint, exits
  non-zero. `otto build --resume` picks up from the recorded phase.

**Target-mode semantics:**
Target mode is invoked via `certifier_mode="target"` or
`config["_target"]`. The gate is strict: `result.passed is True` requires
BOTH story-level success AND `METRIC_MET: YES` from the certifier. A
target run where stories pass but the certifier omits `METRIC_MET:`
fails — in split mode with a fail-fast journal entry
(`"FAIL (certifier omitted METRIC_MET)"`) so the fix loop doesn't waste
a round on random guessing. Non-target modes (bugs/feature) never
consult `metric_met`.

`otto improve` exits non-zero on failure (matching `otto build`), so
CI wrappers can detect when the run didn't reach its goal.

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
