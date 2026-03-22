# Otto v5+: Early Thinking — Agent Communication & the Road to Intent-to-Product

**Date**: 2026-03-21
**Status**: Early exploration — not a design, not committed. Revisit after v4 ships and we have real usage data.

---

## The Big Picture: Where Otto Is Going

Otto today is a task runner — you give it well-defined tasks with specs, it implements them. But the long-term vision is **intent-to-product**: a user describes what they want, and otto builds the entire thing.

```
Today:   "Add rate limiting to the API"          →  1 task, clear spec
Future:  "Build a bookmark manager with tags,    →  unknown tasks, emergent architecture,
          search, and Chrome extension"              cross-component integration
```

This changes what otto's communication model needs to support. This document explores the evolution from v4's orchestrated agents toward the richer coordination needed for intent-to-product.

---

## Context

Otto v4 uses a Plan-Execute-Replan (PER) architecture where agents communicate through the orchestrator at phase boundaries only. All communication is via function args/returns and PipelineContext (in-memory shared state). No mid-execution messaging.

This works well for Phase 3 (implementation) of the intent-to-product workflow. But intent-to-product has five phases, each with different autonomy and communication needs.

## What v4 Cannot Do

The one communication pattern v4 doesn't support: **mid-execution cross-domain insight transfer**.

When coding agents A and B run in the same batch, A might discover "this project uses ESM modules" at t=30s. In v4, B doesn't learn this until the replan at the batch boundary (~t=120s). If B fails at t=60s because it assumed CommonJS, that's a wasted attempt.

Claude Code's Agent Teams solve this with `SendMessage` — peer-to-peer messaging that lands in the recipient's context window between turns.

## Research: Claude Code Agent Teams Communication Model

Two coordination layers:
- **Shared task list** (file-locked JSON): WHO does WHAT, in WHAT ORDER. Handles coordination.
- **SendMessage** (peer-to-peer inboxes): Handles communication — discoveries, challenges, negotiations.

Communication patterns that require real-time messaging (not just shared state):

| Pattern | Description | Relevance to Otto |
|---|---|---|
| Cross-domain insight | "I found X that affects your work" | **High** — parallel coding agents in same codebase |
| Adversarial debate | Agents challenge each other's approaches | Low — QA handles this post-coding |
| Dynamic renegotiation | "This is harder than expected, reassign" | Medium — planner handles at batch boundary |
| Approval workflows | Review before merge | Low — verification tiers are deterministic |
| Broadcast interrupt | "Stop, design changed" | Low — signal handler covers this |

**Key finding**: For decomposable implementation tasks (otto's primary use case), shared state + batch-boundary replanning handles ~90% of the value. Mid-execution messaging primarily helps with cross-domain insights in same-batch parallel tasks.

**Cost of Claude Code teams**: ~200K tokens per teammate context window. Teams of 5 use ~800K-1.2M total. The community has identified limitations: point-to-point DMs create information silos, no shared channels with total ordering (GitHub Issue #30140).

## Decision Criteria: When to Invest in v5

Track this metric during v4 runs:

> **"How often does a task fail on attempt 1 with an error that was already solved by a peer task in the same batch?"**

- If >20% of same-batch first-attempt failures match this pattern → worth adding
- If <20% → batch-boundary replan is sufficient, skip v5 messaging

## Possible Approach: Shared Discovery Board

Lighter than Claude Code teams' full SendMessage. No inbox files, no peer addressing, no shutdown protocol. Just a shared board agents can optionally read/write.

```python
# MCP tools exposed to coding agents
@mcp.tool()
def check_peer_discoveries(task_key: str) -> str:
    """Check if peer agents have posted discoveries relevant to your work."""
    return json.dumps(context.discoveries)

@mcp.tool()
def post_discovery(task_key: str, discovery: str) -> str:
    """Share a codebase discovery with peer agents."""
    context.discoveries.append({"from": task_key, "text": discovery, "ts": time.time()})
    return "posted"
```

System prompt addition:
> "After exploring the codebase, call `post_discovery` with any surprising findings (frameworks, module systems, unusual patterns, gotchas). Before making major architectural decisions, call `check_peer_discoveries` for peer insights."

### Tradeoffs

| | Pro | Con |
|---|---|---|
| MCP discovery board | Simple, opt-in, no protocol overhead | Agent might forget to check; adds prompt complexity |
| File-based polling | Even simpler — agent reads a known file path | Must instruct agent to check periodically; unreliable |
| Claude Code teams (full) | Battle-tested, peer-to-peer | Heavy (200K tokens/teammate), requires rearchitecting around CC teams API |
| Hook-based injection | Deterministic — hook fires on ToolResult | Invasive; can't control timing |

### Implementation Considerations

- `query()` subprocess: MCP tools can be passed via `mcp_servers` config in `ClaudeAgentOptions`. The discovery board would run as a lightweight MCP server (similar to how v3's pilot MCP server worked, but much simpler — 2 tools instead of 9).
- Alternatively: the coding agent already has `Read` access. Writing discoveries to a file at a known path (`otto_logs/discoveries.jsonl`) and telling the agent to check it is the zero-infrastructure approach.
- The agent's willingness to actually call these tools depends on prompt engineering. May need to experiment with how strongly to instruct vs how often agents actually comply.

---

## Intent-to-Product: Five Phases, Different Autonomy Needs

Building a product from an intent isn't one phase — it's five, each requiring different levels of agent autonomy and communication:

```
Phase 1: EXPLORE        "What are we building?"
         → Research similar products, study technologies
         → Architect designs system, explores tradeoffs
         → HIGH AUTONOMY — open-ended, can't be pre-planned

Phase 2: DECOMPOSE      "What are the pieces?"
         → Break design into tasks with dependencies
         → Agents negotiate interfaces between components
         → "Backend proposes GET /bookmarks → {id, url, tags[]}"
         → "Frontend counters: need pagination fields"
         → MEDIUM AUTONOMY — structured but emergent

Phase 3: IMPLEMENT      "Build each piece"
         → Coding agents implement tasks in parallel worktrees
         → THIS IS WHAT OTTO v4 DOES WELL
         → LOW AUTONOMY — orchestrated, deterministic

Phase 4: INTEGRATE      "Make the pieces work together"
         → Agents discover conflicts at runtime
         → "Your API returns X but I expected Y"
         → Need to negotiate fixes across components
         → HIGH AUTONOMY — emergent problem-solving

Phase 5: POLISH         "Make it good"
         → QA, UX review, edge cases, documentation
         → Iterative, judgment-heavy
         → MEDIUM AUTONOMY — structured iteration
```

v4's PER model handles Phase 3. For intent-to-product, we need all five.

### Why CC Teams Has Rich Messaging

Claude Code teams have a team lead (LLM orchestrator) + autonomous teammates with peer-to-peer messaging. The messaging exists because:

1. **The LLM lead can't anticipate everything** — it's imperfect. When Teammate A discovers the database schema is different than expected, A needs to tell Teammate B directly, not wait for the lead to notice.
2. **Teammates have autonomy within scope** — they make their own implementation decisions. When those decisions affect peers, they need to communicate.
3. **The work is open-ended** — tasks emerge at runtime. The lead can't pre-plan all interactions.

This maps to Phases 1, 2, 4, 5 — the parts otto v4 doesn't handle.

For Phase 3 (implementation), CC Teams' messaging is overkill. The task list + orchestrator handles it. That's why otto v4 is right for its current scope.

---

## Capability Evolution Roadmap

The communication model should scale with otto's ambition. Each level adds capability on top of the last. **The PER backbone is never abandoned — it's extended.**

### Level 0: Function Args + Returns (v4 — now)

```
Orchestrator → agent:  prompt with hints
Agent → orchestrator:  TaskResult
Research → coding:     context.research dict (read on retry)
```

**Handles**: Phase 3 (implementation of well-defined tasks)
**Cannot handle**: Mid-execution insight sharing, interface negotiation, open-ended exploration

### Level 1: Shared Discovery Board (v5)

Add 2 MCP tools so agents can post/read discoveries during execution.

```
Coding_A → discovery board:  "project uses ESM modules"
Coding_B ← discovery board:  reads before major decisions
```

**Adds**: Cross-domain insight transfer within a batch (Phase 3 with interdependency)
**Decision criteria**: Ship only if >20% of same-batch first-attempt failures are peer-solvable (measure during v4)

### Level 2: Interface Negotiation Protocol (v6)

Agents propose, accept, or counter interface contracts between components.

```
Backend agent:   proposes  { endpoint: "GET /bookmarks", returns: {id, url, tags[]} }
Frontend agent:  counters  { need: "pagination", returns: {items[], total, page} }
Backend agent:   accepts revised contract
Both proceed with agreed interface
```

**Adds**: Structured negotiation for cross-component design (Phase 2 + Phase 4)
**Mechanism**: Could be MCP tools with structured schemas, or a negotiation phase in the orchestrator where agents take turns proposing/countering (deterministic sequencing, LLM content)

### Level 3: Role-Based Autonomous Teams (v7)

Specialized agents with domain expertise and peer-to-peer communication.

```
Architect agent:  designs system, reviews integration
Frontend agent:   implements UI, negotiates with backend
Backend agent:    implements API, negotiates with frontend
QA agent:         tests everything, challenges assumptions
DevOps agent:     infrastructure, deployment, CI/CD
```

**Adds**: Open-ended exploration (Phase 1), integration problem-solving (Phase 4), quality polish (Phase 5)
**Mechanism**: Closer to CC Teams — agents have persistent context, can message peers. But still with a deterministic orchestrator backbone that sequences phases and enforces quality gates.
**Key difference from CC Teams**: Otto's orchestrator manages phase transitions and quality gates deterministically. Agents are autonomous WITHIN a phase, not across the entire workflow.

### Level 4: Full Intent-to-Product (v8?)

All five phases, adaptive autonomy. The orchestrator adjusts communication level per phase:

```python
async def intent_to_product(intent: str, config):
    # Phase 1: HIGH autonomy — architect explores freely
    architecture = await run_architect(intent, config)

    # Phase 2: MEDIUM autonomy — decompose + negotiate interfaces
    plan = await planner_decompose(architecture, config)
    plan = await negotiate_interfaces(plan, config)

    # Phase 3: LOW autonomy — PER pipeline (v4 model)
    await execute_plan(plan, config, ...)

    # Phase 4: HIGH autonomy — integration agent resolves conflicts
    issues = await integration_test(config)
    if issues:
        await resolve_conflicts(issues, config)

    # Phase 5: MEDIUM autonomy — QA + polish iteration
    await qa_and_polish(config)
```

**The deterministic backbone is always there.** It's what makes otto safe to run unattended. Agent autonomy is injected WHERE NEEDED and kept minimal WHERE NOT NEEDED.

### Summary Table

| Version | Communication | Autonomy | Phases Covered |
|---|---|---|---|
| **v4** | Args + returns, PipelineContext | Low (orchestrated) | Phase 3 |
| **v5** | + shared discovery board | Low + peer awareness | Phase 3 (better) |
| **v6** | + interface negotiation | Medium (structured) | Phase 2, 3, 4 |
| **v7** | + role-based peer messaging | High (within phase) | Phase 1-5 |
| **v8** | + adaptive autonomy per phase | Varies by phase | Full intent-to-product |

Each version is usable on its own. Each builds on the last. The backbone evolves, it doesn't get replaced.

---

## Why Not Jump Straight to Level 3/4?

1. **We don't have the data yet.** v4 hasn't shipped. We don't know where the actual bottlenecks are. Premature autonomy adds complexity without proven benefit.

2. **Autonomy trades reliability for flexibility.** Otto's value proposition is "safe to run unattended." Every autonomy increase risks unpredictable behavior. Each level should be validated before adding the next.

3. **CC Teams' own limitations show the cost.** ~200K tokens per teammate, information silos from point-to-point DMs (GitHub #30140), no session resume. Rich communication is expensive and imperfect.

4. **The PER backbone is the foundation.** Even at Level 4, the orchestrator sequences phases, enforces quality gates, and manages state. Without it, you get the v3 pilot problem — an LLM trying to drive every step, slowly and expensively.

Ship v4. Measure. Let the data tell us which level is needed next.

---

---

## Strategic Moat: The Spec-Verify Flywheel

### The State of Intent-to-Product (March 2026)

No system reliably goes from intent to production-quality product autonomously. Every tool can generate impressive demos, but the gap between "demo that works" and "product that ships" is wide.

Key data points:
- Developers use AI in ~60% of work but can fully delegate only 0-20% of tasks
- AI-generated code has 1.7x more bugs, 75% more logic errors, 2.74x more security vulnerabilities
- Devin: 15% success on complex tasks. Excels at junior-dev scoped work, fails on ambiguous open-ended work.
- CC Agent Teams: Built a 100K-line C compiler (16 agents, $20K) — but with extensive human-designed scaffolding. Not autonomous.
- Bolt/v0/Lovable: Build simple CRUD apps. Fail on auth, state management, security. (Lovable CVE exposed 170+ production apps.)
- MetaGPT/ChatDev: Role-based agents with SOPs. 45% ModuleNotFound error rate in output.

Full research: `research-intent-to-product.md`

### Where Every System Fails

Code generation is solved — every LLM can write code. The bottleneck has shifted to:

1. **Specification amplification**: Turning "build a bookmark manager" into precise, verifiable requirements. Vibe coding tools skip this entirely. Devin infers it poorly. Only MetaGPT (SOPs) and otto (spec.py) attempt structured specification.

2. **Verification**: Knowing WHEN the output is correct without human review. Most tools generate and hope. Only otto has multi-tier verification (existing tests + generated tests + custom verify + adversarial QA).

3. **Self-correction**: When something fails (and it will), analyzing WHY and retrying intelligently. Devin "spent days pursuing impossible solutions." Otto's planner analyzes failures, dispatches research, crafts targeted hints.

### Otto's Moat: The Closed-Loop Spec-Verify Flywheel

Most tools work in an **open loop**: generate code → ship it → human debugs.

Otto works in a **closed loop** that self-corrects without human intervention:

```
          ┌─────────────────────────────────────────┐
          │                                         │
          ▼                                         │
  Intent → Spec → Architecture → Decompose          │
              │                      │               │
              │                      ▼               │
              │              Code (parallel)          │
              │                      │               │
              │                      ▼               │
              │              Verify (deterministic)   │
              │                      │               │
              │                 pass? ──yes──→ Merge  │
              │                      │               │
              │                      no              │
              │                      ▼               │
              │              Analyze failure          │
              │              Research solutions       │
              │              Update hints             │
              │                      │               │
              └──────────────────────┘               │
                                                     │
          Replan with learnings ←────────────────────┘
```

The flywheel compounds: each task's failures improve subsequent tasks. By task 5, the planner has learned: "this project uses ESM modules, the middleware pattern is X, the test framework expects Y." Each task gets easier.

### The Moat Is the Loop at Every Scale

Otto today applies the spec-verify loop at the task level. Intent-to-product needs it at every level:

| Level | Specification | Verification | Otto today | Otto future |
|---|---|---|---|---|
| **Task** | spec.py (verifiable criteria) | verify.py + QA agent | **Done (v4)** | Done |
| **Component** | Interface contracts | Integration tests | Partial (architect.py) | v6: interface negotiation |
| **Product** | Product spec from intent | E2E tests + user flows | Not started | v7-v8 |

The same closed loop — spec → implement → verify → analyze → retry — applied recursively at task, component, and product levels. That's the moat.

No other system has this:
- **Bolt/v0/Lovable**: No spec, no verification, no retry. Open loop.
- **Devin**: Has retry but no structured specs or multi-tier verification. Blind retry.
- **CC Teams**: Has coordination but no deterministic quality gates. Human verifies.
- **MetaGPT**: Has specs (SOPs) but weak verification. 45% error rate.
- **Cursor**: Has parallelism but no autonomous verification. User reviews.

### What Needs to Evolve

The moat components exist at the task level. To reach intent-to-product, they need to scale up:

1. **Product-level spec amplification** (v7-v8): Turn "build a bookmark manager" into a full PRD with component specs, API contracts, data models, and user flows. This is the hardest unsolved problem. Current spec.py handles task-level specs; product-level requires understanding user needs, market context, and technical constraints holistically.

2. **Component-level verification** (v6): Integration tests that verify components work together, not just individually. Interface contracts that are verified at both ends.

3. **Product-level verification** (v8): E2E tests, user flow tests, accessibility, security audit, performance benchmarks. The QA agent needs to think like a user, not just like a tester.

4. **Specification feedback** (all versions): When verification fails, the failure analysis should improve not just the implementation but the SPEC. "This spec item is ambiguous — clarify it" or "this spec item is impossible given the architecture — propose an alternative." Specs are not static.

---

## Other v5 Candidates

Not explored in depth, just noting:

- **Parallel TUI**: Rich per-task display panels for concurrent execution visibility
- **Crash resume**: Parse Telemetry JSONL to reconstruct pipeline state after crash
- **Direct API for lightweight agents**: If Anthropic adds subscription-compatible API access, use it for planner/research (bypass `query()` subprocess overhead)
- **Adaptive concurrency**: Auto-tune `max_parallel` based on observed rate limiting behavior
- **Multi-model coding**: Different models for different task difficulty levels (Haiku for trivial, Sonnet for medium, Opus for hard) — planner decides per-task
- **Agent middleware pipeline**: DeerFlow 2.0 uses an 11-stage middleware pipeline for cross-cutting concerns (sandbox, memory, context compression, concurrency). Overkill for otto v4 (~6 lines of inline infrastructure vs ~50 lines of abstraction), but reconsider if `coding_loop` grows past ~200 lines with many infrastructure concerns. See `research-deerflow.md`.

## References

- Claude Code Agent Teams docs: https://code.claude.com/docs/en/agent-teams
- Claude Code Subagents docs: https://code.claude.com/docs/en/sub-agents
- GitHub Issue #30140: Teams messaging limitations (shared channels request)
- Full research: `/Users/yuxuan/work/everyday_misc/research-claude-code-agent-teams.md`
- Full research: `/Users/yuxuan/work/everyday_misc/research-agent-orchestration-patterns.md`
- Full research: `/Users/yuxuan/work/everyday_misc/research-coding-agent-frameworks.md`
- Otto v4 spec: `docs/superpowers/specs/2026-03-21-otto-v4-design.md`
