# Otto Experiments & Work-in-Progress Index

Quick reference for all experiments, branches, and deferred work. Check here before starting new work to avoid duplication or re-discovering known dead ends.

Last updated: 2026-04-01

---

## Shipped to main

| Feature | Date | Key commits | Findings doc |
|---------|------|-------------|-------------|
| Worktree unification | 2026-03-28 | `plan-worktree-unification.md` | — |
| QA unification (run_qa) | 2026-03-30 | `plan-qa-unification.md` | — |
| Additive parallelism | 2026-03-30 | `7ebe88d` | A/B validated: 27% total speedup |
| QA speed + robustness | 2026-03-30 | `a2f9673` → `c021ab2` | 5 verdict bugs fixed, 12 tests added |
| Parallel QA | 2026-03-31 | `96e4ec4` | `docs/parallel-qa-findings.md` |
| Greenfield fixes | 2026-03-31 | `6bea835`, `6cc614d` | pytest exit 5, jest, npm placeholder |
| Agent-browser for QA | 2026-03-31 | `ecf9cd6` | Replaces chrome-devtools-mcp. 36% cheaper, parallel browser works |
| Subprocess-per-story parallelism | 2026-04-05 | `c783c87` | APFS clone isolation, 2.5x speedup, build-once-start-many |
| Tagged text verdict (drop structured output) | 2026-04-05 | `c37fb79` | 2.6x fewer turns, ~22s saved per story |
| LLM discovery agent | 2026-04-05 | `46592ff` | Replaces if/else classifier. Handles CLI, library, WS, any product |
| Repo-wide heuristic generalization | 2026-04-05 | `ba0cf74` | pnpm/yarn/bun/deno/tox/nox across config, testing, flaky, qa |
| Dead code removal | 2026-04-05 | `aacf4e8` | -924 lines: run_certifier_v2, product_qa.py |

---

## Active branches & worktrees

### `worktree-i2p` (worktree at `.claude/worktrees/i2p`)
**Intent-to-product / Certifier.** Outer loop around v4.5 inner loop. Journey-level execution with dual scoring (journey + step), proof-of-work reports, self-healing.
- **Status:** Active. LLM discovery agent shipped — certifier handles any product type.
- **Key changes (2026-04-05):**
  - `discover_project()` — LLM agent replaces if/else classifier for all product types
  - CLI support (argparse, Click, Cargo, Go), library support (import testing), WebSocket support
  - Parallel subprocess-per-story with build-once-start-many
  - Heuristic generalization across 7 files (config, testing, flaky, qa, runner, display, baseline)
  - Dead code removal: -924 lines (run_certifier_v2, product_qa.py)
  - E2E validated on 7 product types: CLI, library, HTTP API, WebSocket, data pipeline, web app
- **Key docs:** `docs/certifier-e2e-results-2026-03-31.md`, `docs/certifier-hidden-inputs-audit.md`
- **Memory:** `project_discovery_agent.md`, `project_agentic_architecture.md`
- **Next:** Squash commits, Codex implementation gate, merge to main.

### `worktree-gate-pilot` (worktree at `.claude/worktrees/gate-pilot`)
**Gate pilot.** LLM intelligence at batch decision boundaries. i2p v2 spec with outer loop.
- **Status:** Paused. Spec written, partial implementation.
- **Key doc:** `e167ca1` — i2p v2 spec
- **Revisit:** After i2p stabilizes on the i2p branch.

---

## Parked branches (no active worktree)

### `native-subagents`
**Native CC subagents for parallel execution.** Prepare/verify split + smoke test suite.
- **Status:** Parked. Was exploring CC Agent tool for parallelism.
- **Finding:** In-process SDK MCP breaks with Agent tool (see `feedback_inprocess_mcp.md`). External MCP subprocess required.
- **Revisit:** If SDK adds first-class subagent support.

### `display-polish`
**Display improvements.** Explicit retry display, colored line counts, animated spinner.
- **Status:** Parked. Cosmetic polish, low priority.
- **Revisit:** When doing a UX pass.

### `feat/bench-system`
**Benchmark system.** Vague-spec task suite, rubric generation, testgen cost tracking.
- **Status:** Parked. Bench infrastructure exists but not actively used.
- **Memory:** `project_bench_direction.md`, `project_feature_bench.md`
- **Revisit:** When measuring otto vs bare CC on new task types.

### `feat/bench-v3`
**SWE-bench integration.** Docker-based eval, first resolved SWE-bench instance.
- **Status:** Parked. Running on ubuntu with Docker.
- **Revisit:** When evaluating otto on real-world bugs.

### `fix/baseline-tolerance`
**Baseline test tolerance.** Tolerate pre-existing test failures in brownfield projects.
- **Status:** Parked. Partially superseded by greenfield fixes on main (exit code 5, jest, npm).
- **Revisit:** If brownfield projects with known-flaky tests are a priority.

### `pressure-test`
**Pressure test projects.** 6 complex projects (greenfield + brownfield), parallel + re-apply verified.
- **Status:** Parked. Projects exist at `bench/pressure/projects/`.
- **Key doc:** `docs/pressure-test-handover.md`
- **Revisit:** For regression testing after major changes.

---

## Experiments completed (results only, no branch)

### Prompt-driven QA subagents (2026-03-31)
Told QA agent to dispatch Agent tool for parallel spec verification.
- **Result:** Dead end. Agent dispatches serially, re-verifies everything. 2x slower, 2x cost.
- **Findings in:** `docs/parallel-qa-findings.md`

### Single-task spec-group splitting (2026-03-31)
Split one task's specs into groups, verified in parallel sessions.
- **Result:** Not worth it. 0-33% faster, 140-200% more expensive. Each group redundantly loads context.
- **Findings in:** `docs/parallel-qa-findings.md`

### Browser parallel QA with chrome-devtools-mcp (2026-03-31)
Per-task parallel QA with chrome-devtools MCP browser testing.
- **Result:** Failed — chrome-devtools-mcp is a singleton, 2/3 sessions silently failed.
- **Resolution:** Replaced with agent-browser CLI (shipped `ecf9cd6`). Agent-browser supports concurrent sessions natively via `--session` flag. All 3 parallel sessions now work with browser verification.

### QA verdict early-stop (2026-03-30)
Grace timeout (15s) after verdict capture to cut the session short.
- **Result:** Removed. Fragile (asyncio.TimeoutError injection), only saved ~10s, Codex recommended removal.
- **Shipped then reverted:** `e850f84` (add) → `c021ab2` (remove)

---

## Deferred TODOs (from `project_v4_todos.md`)

| Item | Priority | Notes |
|------|----------|-------|
| QA verdict MCP tool | Medium | Eliminates 40-70s verdict write/rewrite cycle. All QA modes benefit. |
| Loop detection | Low | Detect >=90% similar diff on consecutive retries. ~20 lines. |
| Spec gen scope creep | Low | Inferred requirements should default to [should] not [must]. |
| Spec agent project map | Medium | Saves ~150s per `otto add` by giving instant project orientation. |
| Merge conflict recovery | Medium | Rebase fails → retry on updated main. Enables robust multi-task. |

---

## Key handoff docs

| Doc | What |
|-----|------|
| `docs/architecture.md` | Full pipeline reference + debugging guide |
| `docs/parallel-qa-findings.md` | Parallel QA experiment results + next steps |
| `docs/pressure-test-handover.md` | Pressure test methodology + golden project set |
| `docs/codex-e2e-guide.md` | How to run otto e2e from Codex/fresh machine |
| `docs/coderabbit-review-2026-03-28.md` | Full codebase audit (24 source files, 13K LOC) |
| `docs/otto-review-handoff-2026-03-28.md` | Review handoff from 2026-03-28 session |
| `docs/next-improvements.md` | Improvement list from 2026-03-21 |
| `bench/ab-qa-test.sh` | A/B/C test runner for QA performance validation |
