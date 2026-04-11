# Otto Experiments & Work-in-Progress Index

Last updated: 2026-04-11

## Current State

Otto is 4,982 lines. Two commands: `build` and `certify`.
All legacy infrastructure (PER pipeline, task queue, QA agent, spec gen) removed.

## Shipped to main

| Feature | Date | Summary |
|---------|------|---------|
| v3 agent-driven build | 2026-04-06 | One agent drives build→certify→fix. Default mode. |
| Standalone `otto certify` | 2026-04-10 | Builder-blind verification on any project. |
| Incremental intent | 2026-04-10 | `otto build` on existing codebases. |
| PoW with screenshots + video | 2026-04-06 | agent-browser captures per-page screenshots + WebM video. |
| Build history | 2026-04-11 | `otto history`, cumulative intent.md. |
| Certifier finds real bugs | 2026-04-10 | 3/4 open-source projects had bugs. 8 bugs, 0 false positives. |
| Fair benchmark harness | 2026-04-06 | Fixed stories per intent. otto vs bare CC. |
| Prompts as files | 2026-04-11 | `otto/prompts/build.md`, `certifier.md`. |
| Error handling | 2026-04-11 | Graceful SDK crash/timeout recovery. |
| PER pipeline removal | 2026-04-11 | -26K lines. Recoverable from `i2p-pre-per-removal` tag. |
| Automated E2E tests | 2026-04-11 | 16 mock tests for v3 pipeline, 2.5s. |

### Earlier work (shipped before v3)

| Feature | Date | Summary |
|---------|------|---------|
| Agent-browser for QA | 2026-03-31 | Replaces chrome-devtools-mcp |
| LLM discovery agent | 2026-04-05 | Replaces if/else classifier |
| Tagged text verdict | 2026-04-05 | 2.6x fewer turns vs structured output |
| Subprocess-per-story | 2026-04-05 | APFS clone isolation, 2.5x speedup |

## Key findings

### Benchmark: otto vs bare CC
- **Greenfield**: otto $2.54 vs bare $1.70, both 7/7 PASS
- **Incremental**: otto $1.21 vs bare $0.61, both 10/10 PASS
- Otto adds cost when the agent gets it right (most of the time)
- Otto adds value when bugs exist (certifier catches them)

### Real-world validation
- 4 open-source Flask projects tested with `otto certify`
- 3/4 had real bugs: auth bypass, data isolation failure, missing validation
- 0 false positives across 23 story tests
- See `docs/real-world-validation-2026-04-10.md`

### Symphony analysis
- OpenAI's orchestration framework for Codex agents at scale
- Key insight: "the codebase IS the harness" — structured codebases make agents better
- Otto's differentiator: builder-blind certification catches bugs CI doesn't
- See `docs/symphony-analysis-and-next-steps.md`

## Recovery tags

| Tag | What it preserves |
|-----|-------------------|
| `i2p-pre-cleanup` | Before first dead code removal (old certifier pipeline) |
| `i2p-pre-per-removal` | Before PER pipeline removal (full orchestrator/runner/qa) |

## Historical docs (reference only)

These docs describe earlier versions of the architecture. They're accurate for their
time but don't reflect the current v3 system:

- `certifier-v2-*.md` — Specs for the multi-tier certifier (replaced by agentic certifier)
- `parallel-qa-findings.md` — Parallel QA experiment (QA agent removed)
- `plan-*.md` — Plans for worktree unification, QA unification (completed)
- `handoff-*.md`, `otto-review-*.md` — Handoff docs for reviewers
- `pressure-test-handover.md` — Pressure test methodology
