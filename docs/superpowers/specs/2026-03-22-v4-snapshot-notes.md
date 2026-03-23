# Otto v4 Snapshot Notes — Pre-v4.5 Baseline

**Tag**: `v4.0`
**Date**: 2026-03-22
**Commit**: `b4e1cdf`

---

## What v4 Is

Deterministic Plan-Execute-Replan (PER) orchestrator replacing v3's 100-turn LLM pilot.
- Python loop drives task execution — LLM only at plan/replan decision points
- Sequential batch execution (parallel was unsafe with shared checkout)
- `run_task_with_qa()` handles the full code→verify→QA→merge cycle per task
- v3 still available via `otto run --pilot`

## Key Files

| File | Purpose |
|------|---------|
| `otto/orchestrator.py` | PER loop, batch execution, signal handling |
| `otto/planner.py` | TaskPlan/Batch/ExecutionPlan, topo-sorted default_plan, LLM plan/replan |
| `otto/context.py` | PipelineContext shared state, TaskResult |
| `otto/telemetry.py` | JSONL events + v3 legacy dual-write |
| `otto/runner.py` | coding_loop, preflight_checks, rebase_and_merge, run_qa_agent |
| `otto/pilot_v3.py` | v3 fallback (renamed from pilot.py) |

## What Was Fixed (Audit Cleanup)

### Round 1 (original audit — C/S/T/M series)
- C3: test_hint telephone game removed
- C4: planner hints removed
- C5: replanner enriched with full error/QA/diff context
- S6: planner fallback logging
- S17: all max_turns removed, timeouts → 1hr circuit breaker
- T2-T11: spec count caps, forced workflows, "do NOT" prohibitions, pre-loading, "stay in your lane" — all removed
- M1, M3: prompt contradictions fixed

### Round 2 (N-series file-by-file audit)
- N4, N8, N11, N12, N14: truncations and prescribed workflows removed
- N17: TaskPlan.hint dead field removed
- N18: spec system prompt simplified (67→27 lines)
- N21, N22: retry prompts made factual (no diagnostic prescriptions)
- N25: TDD prohibitions removed
- N27: QA receives full diff instead of stat
- N29: orchestrator summary errors logged
- S14: temp file cleanup on error paths

### Bug Fixes Found in Real Runs
- CLAUDECODE env leak: cleared at CLI startup (caused QA agent crash exit -2)
- node_modules prompt bloat: 17K files (1MB) in git ls-files → filtered, then replaced with shallow project map, then removed entirely
- v4 display missing: TaskDisplay wired into coding_loop for inline progress
- QA display generic: labels replaced with actual tool call details

## Current Prompt State

### Spec agent
- No pre-loaded context — agent explores codebase via tools
- Behavioral specs only (no implementation details, no file names)
- Coverage depth: happy path, errors, negative cases, edge cases, retained behavior
- External reference handling (inspect sites, emit concrete criteria)

### Coding agent
- System: role + autonomy + completion check (12 lines)
- User: task prompt + spec items + learnings + task notes
- No file tree, no pre-loaded files, no prescribed workflow

### QA agent
- "Find bugs the test suite missed. Use whatever approach works."
- No prohibitions, no time target, no capability restrictions

## Known Issues / TODOs for v4.5

### Performance
- **Spec gen slow (~220s)**: Agent explores codebase from scratch each time. Project map for spec agent would save 150s. Deferred.
- **QA takes 7-8 min**: Runs full browser testing (Chrome DevTools). Thorough but doubles total time. No cap.
- **Total ~14min per complex task**: Coding 5min + QA 8min. Bare CC does same task in 5min.

### Architecture
- **Spec is a ceiling, not a floor**: Coding agent follows spec literally — won't add good ideas not in spec. Bare CC with no spec produced arguably better UX (richer explanations, source attribution).
- **Sequential execution only**: Parallel was removed due to shared-checkout safety. Worktree-based parallel needs redesign.
- **QA crash ≠ QA fail**: Infrastructure crashes (exit -2) treated same as "QA found bugs." Should distinguish.
- **No cost tracking per phase**: Can't correlate cost with quality or identify waste.

### Deferred Audit Items
- C1: Architect context (deferred — agent has full access, stale context misleads)
- C2: Redundant exploration across agents
- N1/N16: effort always "high"
- N3: Learnings lack provenance
- N9/N10: Architect output taxonomy rigid
- N13: Blackbox context Python-only
- N23: Subagent model hardcoded to haiku
- N28: Heuristic spec filter drops items
- N31-N34: Pilot v3 micromanagement (legacy path)
- S1-S12: Systemic design issues

### Process
- **Codex should audit all prompts**: Every system prompt and agent prompt in the project should be reviewed by Codex for over-specification, telephone games, arbitrary constraints, and missed opportunities. This caught real issues every time it was done (spec prompt, QA prompt, coding prompt). Should be a standard step before any release.

## Bare CC vs Otto Comparison (Weatherapp Task 31 — Radar)

| Metric | Otto v4 | Bare CC |
|--------|---------|---------|
| Time | 14m03s | 5m25s |
| Cost | $1.43 | ~$0.50 (est) |
| Tests written | 44 | 28 |
| Test pass | 807/807 | not verified |
| QA | 12/12 specs adversarially verified | none |
| Visual quality | Good — segmented progress bar, blue dot marker | Good — scrubber bar, crosshair, similar style |
| Extras | None (followed spec literally) | Credited formula source ("Environment Canada model") |

**Key insight**: For single creative tasks, bare CC is faster and produces comparable or better output. Otto's value is in multi-task coordination, regression prevention, and adversarial verification — not single-task quality.

## Git History (v4 commits)

```
b4e1cdf snapshot: v4 complete — pre-v4.5 baseline
e0f136f fix: QA display shows actual actions instead of generic labels
03b5fc4 fix: remove project map injection from prompts
97bfb59 fix: replace raw git ls-files with shallow project map
5be1bf4 fix: filter node_modules from file tree in prompts
1c39db7 fix: clear CLAUDECODE at CLI startup
ffb806c fix: spec prompt refined — Codex-written
23bacda fix: spec agent produces behavioral specs
615f2a4 test: add v4 display wiring tests
11e8669 fix: wire TaskDisplay into v4 coding_loop
73fd0d5 fix: audit round 2 — prescribed workflows, truncations, dead fields
56d18ff fix: audit cleanup — telephone games, arbitrary caps, agent over-specification
2190358 feat: otto v4 PER orchestrator — deterministic plan-execute-replan pipeline
```
