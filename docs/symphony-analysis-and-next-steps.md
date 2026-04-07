# Symphony Analysis & Otto Next Steps

Date: 2026-04-07

## Part 1: Symphony — What It Is

Symphony is OpenAI's **orchestration framework** for autonomous coding agents. Released March 2026, Apache 2.0, written in Elixir/OTP.

Core idea: turn Linear tickets into isolated, autonomous implementation runs. Teams manage *work*, not agents.

```
Linear board → Symphony polls → spawns Codex agent per issue → agent works in
isolated workspace → proof of work (CI passes, PR review, walkthrough) → merge PR
```

Symphony is NOT a coding agent. It's the infrastructure that runs coding agents at scale. The agent is Codex. Symphony provides: workspace isolation, retry with exponential backoff, concurrency management (BEAM supervision trees), and a proof-of-work gate before merge.

### Key Concepts

**Harness engineering** — the discipline of designing infrastructure/constraints/feedback loops around AI agents. Four functions: constrain, inform, verify, correct. The insight: "constraining the solution space makes agents more productive, not less."

**Proof of work** — agents must demonstrate success through: CI status, PR diff, complexity analysis, walkthrough video. No proof → no merge.

**Implementation runs** — isolated, autonomous executions. One issue → one workspace → one agent → one PR. Runs are independent and fault-isolated (BEAM supervision trees handle crashes without system-wide impact).

**Context engineering** — never dump the whole repo into context. Carefully construct only relevant information per task. "Anything it can't access in-context doesn't exist."

### Results (OpenAI Internal)

- 1M+ lines of code in 5 months, zero human-written lines
- 3 engineers driving Codex, ~1,500 PRs merged
- 3.5 PRs per engineer per day
- ~1/10th traditional development time

---

## Part 2: Otto vs Symphony — Honest Comparison

| Dimension | Symphony | Otto (i2p) |
|-----------|----------|------------|
| **Scope** | Issue → PR (task-level) | Intent → working product |
| **Input** | Linear tickets (small, scoped) | Natural language (whole product) |
| **Agent** | Codex (subprocess, sandboxed) | Claude Code (SDK, one session) |
| **Verification** | CI + PR review + walkthrough | Independent certifier agent |
| **Scale** | 100s concurrent (BEAM) | Single build |
| **Codebase** | Existing large (1M LOC) | Primarily greenfield |
| **Continuous** | Polls board forever | One-shot command |
| **Self-hosted** | Yes (Elixir/OTP) | Yes (Python) |

### Where Symphony Is Stronger

1. **Scale** — BEAM handles 100s of concurrent runs. Otto is single-threaded.
2. **Existing codebases** — Symphony is built for incremental work on large repos. Otto is greenfield-focused.
3. **CI integration** — CI as verification is battle-tested and deterministic. Otto's certifier is probabilistic.
4. **Continuous operation** — Symphony runs always-on. Otto is manual.
5. **Harness engineering** — systematic approach to making codebases agent-friendly.

### Where Otto Is Stronger

1. **Product-level certification** — certifier tests as a real user (curl, CLI, browser). Catches bugs that unit tests miss (e.g., HTML escaping found by agent-browser).
2. **Builder-blind verification** — certifier has zero knowledge of how the product was built. Symphony's CI was written by the same agent — potential for bias.
3. **Agent-driven autonomy** — the coding agent drives the certify→fix loop itself. Symphony's retry is orchestrator-driven with backoff timers.
4. **Zero setup** — `otto build "intent"` on an empty repo. Symphony requires existing codebase, Linear board, CI pipeline.
5. **Any product type** — CLI, API, library, web app, hybrid. Symphony assumes web services.

### Where Neither Is Strong

1. **Incremental features on existing codebases** — Symphony handles this but requires Linear tickets. Otto doesn't do it at all.
2. **Multi-service architectures** — neither handles frontend + backend + database as coordinated services.
3. **Human feedback loops** — Symphony has PR review. Otto has no structured human input.

---

## Part 3: What Otto Should Learn

### 3.1 Incremental intent (Symphony's core use case)

Symphony's value is incremental work on existing codebases — the 90% real-world case. Otto only does greenfield. This is the biggest gap.

**What it looks like:**
```
# Existing project with auth, CRUD, etc.
otto build "add full-text search to the notes API"
```

The agent reads the existing codebase, understands the architecture, adds the feature, runs existing tests, certifies the addition works without breaking existing functionality.

**Why it's hard:** The agent needs to understand existing code, not just build from scratch. The certifier needs to verify the new feature AND regression-test existing features.

### 3.2 Run existing tests (harness engineering insight)

Symphony requires CI to pass. Otto ignores existing tests — the certifier tests from scratch every time.

**The fix:** Before certification, run the project's existing test suite. If it fails, the product has regressions regardless of what the certifier finds. This is free verification — the tests already exist.

```
Coding agent builds → run existing tests → if fail: fix → certify new features
```

### 3.3 Codebase as context (context engineering)

Symphony emphasizes: don't dump the whole repo. Construct relevant context carefully.

Otto's v3 agent just gets the intent. For existing codebases, it needs to understand the architecture, conventions, and existing code. CLAUDE.md is a start. But the agent should also:
- Read the project's README, AGENTS.md
- Understand the test suite structure
- Know the dependency graph
- Respect existing patterns

### 3.4 Proof of work (complement certifier with CI)

Symphony's proof of work: CI passes + PR review + walkthrough video.
Otto's proof of work: certifier stories PASS + PoW HTML report.

**Combine both:** Run existing CI/tests AND certifier stories. CI catches regressions. Certifier catches product-level issues that unit tests miss. Together they're stronger than either alone.

---

## Part 4: Otto Next Steps — Prioritized

### P0: Incremental intent on existing codebases

**Why first:** This is where real-world value is. Nobody builds products from scratch every time. The benchmark showed bare CC matches otto on greenfield — otto's value must come from something else.

**Design:**
```
otto build "add search to notes" (in existing project)
  │
  └─ Coding agent:
       1. Read existing codebase (README, CLAUDE.md, key files)
       2. Understand architecture + conventions
       3. Plan the addition (what to change, what to keep)
       4. Implement the feature
       5. Run EXISTING test suite → fix regressions
       6. Write NEW tests for the feature
       7. Certify: new feature works + existing features not broken
```

**Key difference from greenfield:** Steps 1-3 and 5 don't exist in greenfield. The agent needs to be a good citizen in an existing codebase.

### P1: Run existing tests as pre-certification

**Why:** Free verification. If the project has 500 passing tests and the agent breaks 3, that's caught immediately — no LLM needed. This is Symphony's strongest insight: CI is deterministic and cheap.

**Design:**
```
After build, before certify:
  detect test command (npm test, pytest, etc.)
  run it
  if fails: agent fixes regressions, re-runs
  then: certifier tests new features
```

This makes the certifier more focused — it only needs to test new/changed behavior, not regression-test everything.

### P2: Harness engineering support

**Why:** The better the codebase is structured, the better the agent performs. Symphony proved this: LangChain went from 52.8% to 66.5% on benchmarks by only improving the harness.

**Design:**
- Read CLAUDE.md / AGENTS.md for project conventions
- Auto-detect project structure (monorepo? microservice? single app?)
- Pass relevant architecture context to the agent
- For otto build on existing projects: auto-generate a project summary

### P3: Benchmark on existing codebases

**Why:** Our current benchmark is greenfield-only. We need to measure otto's value on the incremental case — "add feature X to project Y" where Y has 10K+ lines.

**Design:**
- Take 5-10 real open source projects
- Define feature additions for each
- Run otto vs bare CC
- Measure: feature completeness, regressions introduced, cost, time

---

## Part 5: What NOT to Do

1. **Don't build a Linear integration** — Symphony's board polling is nice but not where otto's value is. Otto is a build tool, not a project manager.
2. **Don't rewrite in Elixir** — BEAM concurrency is elegant but Python is fine for otto's scale. Concurrent builds can use subprocess isolation.
3. **Don't copy Symphony's orchestrator pattern** — Otto's agent-driven approach (certifier as environment) is architecturally better for product-level work. Symphony's orchestrator-driven retry is right for task-level work.
4. **Don't add walkthrough video generation** — Symphony's walkthrough videos are cool but the certifier's PoW report serves the same purpose with less overhead.

---

## Summary

Symphony and otto solve different problems at different scales. Symphony orchestrates agents on existing codebases for task-level work. Otto builds and certifies products from intent.

The gap is incremental intent — adding features to existing codebases. That's where otto should go next, borrowing Symphony's insight that existing tests + structured codebase context are the highest-leverage improvements.

The certifier remains otto's differentiator. Symphony verifies via CI (deterministic but narrow). Otto verifies via builder-blind agent testing (broader but stochastic). The right answer is both.
