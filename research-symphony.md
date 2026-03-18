# OpenAI Symphony: Deep Research Analysis

## 1. What Is Symphony?

Symphony is an **open-source, long-running automation service** that turns project management tickets into autonomous coding agent runs. Released on **March 4-5, 2026** (repo created February 26, 2026), it is built in **Elixir** on the Erlang/BEAM runtime and licensed under **Apache 2.0**.

GitHub: [openai/symphony](https://github.com/openai/symphony) — 12,047 stars, 918 forks as of March 13, 2026.

**Core idea:** Instead of a developer prompting a coding agent and babysitting it, Symphony watches an issue tracker (currently Linear), picks up tickets, spawns isolated Codex agent sessions per ticket, and delivers verified pull requests — complete with CI status, code reviews, and walkthrough videos. Teams manage a kanban board; agents do the implementation.

Symphony is currently labeled **"engineering preview"** — suitable for testing in controlled environments, not production.

---

## 2. Architecture

### Six Abstraction Layers

1. **Policy Layer** — `WORKFLOW.md` (checked into the repo) defines agent behavior, prompt templates, and runtime settings. This is the control surface for teams.
2. **Configuration Layer** — Typed config parsing from YAML front matter in `WORKFLOW.md`, with environment variable indirection (`$VAR_NAME`) and sensible defaults.
3. **Coordination Layer** — `SymphonyElixir.Orchestrator`: single-authority in-memory state managing the poll-dispatch-reconcile loop, concurrency slots, retry scheduling, and claim tracking.
4. **Execution Layer** — `SymphonyElixir.AgentRunner` + `SymphonyElixir.WorkspaceManager`: per-issue filesystem workspaces, lifecycle hooks, and the Codex app-server subprocess protocol.
5. **Integration Layer** — Linear GraphQL API adapter (read-only from orchestrator side; agents write via tools).
6. **Observability Layer** — Structured logging with `issue_id`, `issue_identifier`, `session_id` correlation. Optional HTTP dashboard + JSON REST API.

### Why Elixir/BEAM?

- **Lightweight processes** (~2KB each) — can run hundreds of concurrent agent sessions.
- **OTP supervision trees** — automatic restart on agent failure; failures are isolated, not cascading.
- **Preemptive scheduling** — no agent starves others.
- **Built-in distributed computing** support for potential multi-node deployment.

This is a deliberate contrast to Python-based agent frameworks where one agent crash often takes down the system.

### Orchestrator State Machine

Five internal states (separate from Linear ticket states):

```
Unclaimed → Claimed → Running → (normal exit) → RetryQueued → Running ...
                                → (terminal state) → Released
```

- **Unclaimed**: Not running, no retry timer.
- **Claimed**: Reserved to prevent duplicate dispatch.
- **Running**: Active worker task in the `running` map.
- **RetryQueued**: Backoff timer active, no worker.
- **Released**: Claim removed (ticket went terminal, inactive, or disappeared).

### Poll-Dispatch-Reconcile Cycle

Every `polling.interval_ms` (default 30s):

1. **Reconcile** — Stall detection (kill if no Codex event within `stall_timeout_ms`, default 5 min) + tracker state refresh (terminal → stop + clean workspace; non-active → stop; still active → update snapshot).
2. **Preflight validation** — Config health check.
3. **Fetch candidates** — Query Linear for active-state issues.
4. **Eligibility filter** — Concurrency slots (global `max_concurrent_agents` default 10, plus per-state limits), blocker rules (Todo tickets with non-terminal blockers are ineligible), not already claimed/running.
5. **Sort** — Priority ascending (1-4, null last) → `created_at` oldest first → `identifier` lexicographic.
6. **Dispatch** — Spawn worker tasks while slots remain.
7. **Notify** — Update observability consumers.

---

## 3. Key Concepts

### Task Decomposition

Symphony does **not** decompose tasks internally at the orchestrator level. Each Linear ticket = one autonomous "implementation run." The decomposition happens at two levels:

- **Project planning level**: Humans (or AI planners) break work into Linear tickets. The WORKFLOW.md prompt can instruct agents on how to approach multi-step work within a single ticket.
- **Agent session level**: Within a run, the Codex agent uses multi-turn conversations (up to `max_turns`, default 20) to iteratively plan, code, test, and submit.

### Agent Communication

Agents do **not** communicate with each other directly. Each agent session is fully isolated:

- Separate filesystem workspace per issue.
- Separate Codex subprocess per issue.
- Separate conversation context per issue.
- No shared state between agents (avoiding "context pollution").

Coordination happens indirectly through:
- The **Linear board** (ticket state changes).
- **Git** (branches, PRs, merge conflicts detected by the `land` skill).
- The **orchestrator's concurrency control** (slot management prevents overload).

### Verification / Proof of Work

Before a task is considered complete, agents must provide verifiable deliverables:

- **CI status** — Tests must pass.
- **PR review feedback** — Code review analysis.
- **Complexity analysis** — Code quality metrics.
- **Walkthrough videos** — Demo recordings of changes.
- **Workpad comments** — A persistent Linear comment tracking progress, acceptance criteria, and validation results.

The WORKFLOW.md prompt enforces that ticket-provided test/validation sections are **non-negotiable gates** before moving to "Human Review" state.

### Workspace Isolation

Each issue gets `<workspace.root>/<sanitized_identifier>/`:

- Identifier sanitized to `[A-Za-z0-9._-]` (others replaced with `_`).
- Path containment enforced (no escaping workspace root).
- Workspaces **persist across runs** for the same issue (enabling continuation context).
- Four lifecycle hooks: `after_create` (fatal), `before_run` (fatal), `after_run` (logged), `before_remove` (logged).

### Retry and Backoff

- **Normal exit** (agent finished its turns): 1-second continuation retry to re-check tracker state.
- **Abnormal exit**: Exponential backoff: `delay = min(10000 * 2^(attempt-1), max_retry_backoff_ms)` (default max 300s / 5 min).
- **Stall detection**: If no Codex event within `stall_timeout_ms` (default 5 min), kill and retry with backoff.

### Dynamic Configuration Reload

`WORKFLOW.md` is watched for changes and **hot-reloaded without restart**. Changes apply to future dispatches, retries, and agent launches. In-flight sessions are not auto-restarted. Invalid reloads keep the last known good config.

---

## 4. Relationship to OpenAI Codex and Other Tools

### Codex App-Server Protocol

Symphony launches Codex as a subprocess in **app-server mode** — a bidirectional JSON-RPC protocol over stdio (line-delimited JSON messages on stdout).

The handshake sequence:
1. `initialize` request (client identity, capabilities) → wait for response.
2. `initialized` notification.
3. `thread/start` request (sandbox policy, working directory) → get `thread_id`.
4. `turn/start` request (rendered prompt, issue context) → get `turn_id`.

Session ID = `<thread_id>-<turn_id>`. The same `thread_id` is reused for continuation turns within one worker run.

The app-server stays alive across continuation turns — no re-launch between turns. This is the same protocol that powers the Codex CLI, VS Code extension, and web app (OpenAI tried and rejected MCP before arriving at this design).

### Default Codex Configuration (from WORKFLOW.md)

```
codex --config shell_environment_policy.inherit=all \
      --config model_reasoning_effort=xhigh \
      --model gpt-5.3-codex \
      app-server
```

- `approval_policy: never` — fully autonomous, no human approval prompts.
- `thread_sandbox: workspace-write` — agent can write within workspace.
- `shell_environment_policy.inherit=all` — inherit full shell environment.
- `model_reasoning_effort=xhigh` — maximum reasoning effort.

### Linear Integration

Symphony reads from Linear via GraphQL; it does **not write** from the orchestrator. The agent writes to Linear through the `linear_graphql` client-side tool injected into the Codex session. This tool:

- Executes raw GraphQL queries/mutations against Linear using Symphony's configured auth.
- One operation per tool call.
- Available only when `tracker.kind == "linear"`.

### Skills System

Skills are stored as `.codex/skills/<name>/SKILL.md` — procedural instruction sets in Markdown with YAML front matter. They drive Codex's tool calls (shell, gh CLI, git, GraphQL). Available skills:

| Skill | Purpose |
|-------|---------|
| **commit** | Stage changes, write conventional commit messages |
| **push** | Validate build, push branches, create/update PRs |
| **pull** | Fetch origin, fast-forward/merge, resolve conflicts |
| **land** | Watch PR until merged, handle CI failures and review feedback |
| **linear** | Raw GraphQL calls to Linear API |
| **debug** | Correlate logs by issue/session ID |

**Delegation hierarchy**: `land` is the top-level orchestrator skill; it delegates to `commit`, `push`, and `pull`. The `land` skill includes `land_watch.py`, a Python subprocess that concurrently monitors review feedback, CI checks, PR head changes, and merge conflicts (10s polling, 120s timeout).

---

## 5. Open Source Components

**Everything is open source** under Apache 2.0:

- **SPEC.md** — Complete, language-agnostic specification (~8000 words). Any team can implement Symphony in their preferred language using this spec.
- **Elixir reference implementation** — Working prototype under `elixir/`. Labeled experimental; the README recommends building your own hardened version from the spec for production.
- **WORKFLOW.md** — Default workflow configuration and prompt template.
- **Skills** — The full set of `.codex/skills/` Markdown files.

The repo explicitly encourages "instruct coding agents to build Symphony using the spec" as a valid getting-started path — a meta approach where you use AI to build the AI orchestrator.

---

## 6. Real-World Usage Examples and Results

### Demo Workflow

In OpenAI's demo, Symphony:
1. Monitors a Linear board continuously.
2. Picks up "Todo" tickets automatically.
3. Spawns isolated Codex agents per ticket.
4. Agents code, test, create PRs, generate CI reports and walkthrough videos.
5. Moves tickets to "Human Review" with proof of work attached.
6. On approval, the `land` skill merges the PR safely.

### Intended Use Cases

- **Feature implementation**: Parallel development of model/controller/service/test layers with dependency-aware sequencing.
- **Codebase migration**: Processing hundreds of files simultaneously with automated test verification.
- **Test generation**: Multiple agents analyzing different modules, producing tests in parallel.
- **Documentation sync**: Comparing code signatures with docs, flagging discrepancies.
- **Dependency upgrades / refactoring**: Repetitive maintenance tasks.

### Practical Considerations

- Requires **"harness engineering"** — codebases need good CI, tests, and clear task definitions for agents to work effectively.
- Currently only supports **Linear** as issue tracker and **OpenAI Codex** as the coding agent.
- The default config runs up to **10 concurrent agents** with **20 turns each**.
- Token consumption can be significant (the WORKFLOW.md uses `gpt-5.3-codex` with `reasoning_effort=xhigh`).

No public benchmarks or quantitative results have been published yet.

---

## 7. Comparison to Other Multi-Agent Systems

### vs. General-Purpose Agent Frameworks (AutoGen, CrewAI, LangGraph, OpenAI Agents SDK)

| Dimension | Symphony | AutoGen | CrewAI | LangGraph | OpenAI Agents SDK |
|-----------|----------|---------|--------|-----------|-------------------|
| **Language** | Elixir | Python | Python | Python | Python |
| **Domain** | Code implementation | General | General | General | General |
| **Concurrency** | BEAM processes | asyncio | threads | state graph | lightweight |
| **Fault tolerance** | OTP supervision trees | try/catch | try/catch | checkpoints | minimal |
| **Agent communication** | None (isolated) | Conversation | Shared context | State passing | Tool handoffs |
| **Task source** | Issue tracker (Linear) | Programmatic | Role-based crews | Graph definition | Programmatic |
| **Persistence** | In-memory + tracker | Varies | Varies | Checkpoint-based | Minimal |

**Key distinction**: Symphony is **not a general-purpose agent framework**. It is a **domain-specific orchestration service** for turning project tickets into code. It doesn't provide primitives for building arbitrary agent topologies — it provides a complete, opinionated pipeline: poll tracker → dispatch → workspace → run Codex → verify → merge.

### vs. Devin / Claude Code / Cursor

| Dimension | Symphony | Devin | Claude Code | Cursor |
|-----------|----------|-------|-------------|--------|
| **Interaction model** | Unattended, board-driven | Turnkey autonomous | Session-level, developer-initiated | IDE-integrated, developer-initiated |
| **Concurrency** | 10+ parallel agents | Single agent | Single agent (or manual multi-agent) | Single agent |
| **Customizability** | Fully open, spec-driven | Closed | Extensible via tools | Plugin ecosystem |
| **Issue tracker integration** | Native (Linear) | Some | Manual | Manual |
| **Self-hosting** | Required | SaaS | Local CLI | Local/cloud |

**Key distinction**: Claude Code and Cursor are **developer-in-the-loop** tools — you initiate, supervise, and iterate. Symphony operates at the **project level** — it continuously monitors and processes issues without human initiation. Devin is closer in ambition (fully autonomous) but is closed-source and less customizable.

### vs. OpenAI Swarm

Swarm (OpenAI's earlier experimental framework) was a lightweight multi-agent framework focused on agent handoffs. Symphony is a fundamentally different beast — it's a production service architecture, not an agent communication pattern. Symphony doesn't have agents talk to each other; it isolates them completely and coordinates through the issue tracker.

---

## Community Reception

**Hacker News** (item 47252045) reception was mixed:
- **Positive**: Interest in the Elixir/BEAM choice, the spec-driven approach enabling any-language implementations, and the novel "board-as-interface" paradigm.
- **Critical**: The spec was called "inscrutable agent slop" by one commenter who found it listed database fields without adequately describing the state machine. Documentation quality concerns.
- **Comparisons**: Compared to StrongDM's Attractor autonomous agent system.

**Overall**: Cautious optimism tempered by the engineering-preview status, Linear-only limitation, and documentation clarity concerns.

---

## Summary

Symphony represents a distinct approach to AI-assisted development: rather than making individual coding agents smarter, it builds the **infrastructure to deploy and manage fleets of them**. The key innovations are:

1. **Board-as-interface**: Manage AI work the same way you manage human work — through tickets.
2. **Workspace isolation**: Each agent gets a clean, contained environment.
3. **Proof-of-work verification**: Agents must demonstrate their work passes CI, review, and testing before handoff.
4. **Fault-tolerant orchestration**: BEAM/OTP provides industrial-grade process management.
5. **Policy-as-code**: `WORKFLOW.md` versions agent behavior with the codebase.
6. **Spec-driven design**: Complete language-agnostic specification enables re-implementation.

The biggest limitations today are the Linear-only tracker support, Codex-only agent runtime, engineering-preview maturity level, and the requirement for well-harnessed codebases (good CI, tests, clear task definitions).
