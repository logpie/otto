# Research: Agent-Driven Architecture — Beyond PER

## The Problem

PER treats agents as stateless workers. The orchestrator kills sessions, creates fix tasks, starts new sessions that must re-discover everything. The most valuable thing — the agent's understanding of what it built — is destroyed at every boundary.

## The Model

**The coding agent is the developer. The certifier is the user.**

A developer builds something, gives it to a user, the user tries it and reports bugs. The developer fixes the bugs with full context of what they built. The user doesn't know or care how the developer works — they just test the product.

```
Coding Agent (developer):
  - Continuous session, full context
  - Drives the loop: build → request certification → read feedback → fix → request again
  - Can dispatch subagents for parallel work
  - Cannot see or influence how the certifier tests

Certifier (user):
  - Builder-blind: doesn't know how the product was built
  - Tests an immutable snapshot (git ref), not the live workspace
  - Returns structured findings: what's broken, diagnosis, fix suggestions
  - Cannot be influenced by the coding agent — it's an independent environment
```

The key constraints:
1. **Certifier is immutable to the coding agent.** The agent gets findings back but cannot modify the certifier's environment, prompt, or behavior. This prevents the agent from gaming certification.
2. **Certifier tests a frozen snapshot.** The agent commits a candidate, the certifier tests that exact commit. No race between editing and testing.
3. **Session continuity.** The coding agent keeps its session across build→certify→fix cycles. No context loss.

## V1 Design

### Flow

```
otto build "intent"
  │
  ├─ Infrastructure: create project dir, init git, write intent.md
  │
  ├─ Coding Agent Session (continuous, resumable)
  │   │
  │   ├─ Build: read intent, explore, write code, write tests, make tests pass
  │   │
  │   ├─ Signal: "ready for certification" (commit candidate ref)
  │   │
  │   ├─ ← Infrastructure: run certifier against candidate ref (agent waits)
  │   ├─ ← Certifier findings injected into session as structured feedback
  │   │
  │   ├─ Fix: read findings, fix issues (full context — agent knows WHY it built things this way)
  │   │
  │   ├─ Signal: "ready for re-certification" (new candidate ref)
  │   │
  │   ├─ ← Certifier runs again (targeted: skip passed stories)
  │   ├─ ← Findings injected
  │   │
  │   ├─ Fix again (or done)
  │   │
  │   └─ Session ends when: certified OR max rounds OR no progress
  │
  └─ Result: passed/failed, findings, cost, duration
```

### What the coding agent sees

The agent gets a system prompt and tools. It doesn't know about otto's internals:

```
System: You are building a product from the intent below. Build it, make tests pass,
then call the certify() tool. You'll get feedback on what's broken. Fix and re-certify.

Tools available:
  - All standard CC tools (Bash, Read, Write, Edit, Grep, etc.)
  - certify()  → submits current code for product certification, returns findings
  - dispatch()  → spawn a subagent for parallel work (optional)

The certify() tool tests your product as a real user would. You cannot see or
influence how it tests. You can only read the findings and fix your code.
```

### What certify() does internally

```python
def certify_tool(project_dir, session_context):
    """Tool called by the coding agent. Runs certifier against committed code."""
    # 1. Commit agent's current work as a candidate ref
    candidate_sha = _commit_candidate(project_dir)
    
    # 2. Run certifier against that immutable snapshot
    #    (agent is blocked while this runs — 5-10 min)
    report = run_unified_certifier(
        intent=session_context.intent,
        project_dir=project_dir,
        candidate_ref=candidate_sha,
    )
    
    # 3. Return structured findings to agent
    return {
        "passed": report.passed,
        "findings": [
            {
                "description": f.description,
                "diagnosis": f.diagnosis,
                "fix_suggestion": f.fix_suggestion,
                "severity": f.severity,
            }
            for f in report.findings
        ],
        "summary": f"{len(report.critical_findings())} issues to fix",
    }
```

### What infrastructure owns (control plane)

- **Session lifecycle**: start, suspend during certification, resume with findings, checkpoint for crash recovery
- **Git operations**: candidate refs, worktrees for subagents, merge discipline
- **Certifier execution**: run in isolated environment, feed results back
- **Cost/time tracking**: transparent to agent
- **Logging/observability**: capture everything for debugging
- **Guardrails**: max certification rounds, max session time, cost budget

### What the agent owns (decision plane)

- What to build and how
- When to request certification (agent decides it's ready)
- What to fix and in what order
- Whether to dispatch subagents
- When it's done (or stuck)

### Session continuity

The agent's session persists across certify→fix cycles:

```
Turn 1-20:  Agent builds the product
Turn 21:    Agent calls certify()
            ... infrastructure runs certifier (5-10 min, agent waits) ...
Turn 22:    Agent receives findings: "XSS in POST /todos, missing input validation"
Turn 23-25: Agent fixes XSS, adds validation
Turn 26:    Agent calls certify() again
            ... certifier runs (targeted, 2-3 min) ...
Turn 27:    Agent receives: "all passed"
Turn 28:    Agent signals done
```

**Crash recovery**: After each certify() call, infrastructure writes a checkpoint:
```json
{
  "session_id": "sess_abc123",
  "candidate_ref": "abc1234",
  "certifier_report": {...},
  "round": 2,
  "intent": "...",
  "cost_so_far": 1.23
}
```
If the session crashes, resume from the checkpoint with the last certifier report as context.

### Multi-agent / parallel work

The main agent can dispatch subagents for parallel work:

```
Main agent:
  → "I need a backend API and a frontend. Let me dispatch two subagents."
  → dispatch("Build Express API with these routes: ...", worktree=true)
  → dispatch("Build React frontend with these pages: ...", worktree=true)
  → Both complete, main agent merges
  → Main agent calls certify()
  → Certifier finds integration issues
  → Main agent fixes (it has context of both — it designed the architecture)
```

The main agent is the architect. It keeps the big picture. Subagents are focused workers in isolated worktrees. Infrastructure handles worktree creation, merge mechanics. Main agent handles merge decisions and conflict resolution.

### What changes from current code

| Current | V1 |
|---------|-----|
| `pipeline.py` orchestrates build→certify→fix | Agent drives the loop, pipeline just manages session |
| `verification.py` creates fix tasks | Certifier findings injected into agent session |
| `runner.py` max_retries kill/restart loop | Agent iterates in-session, no kill/restart |
| `orchestrator.py` PER loop with planner | Agent plans (or uses planner tool), dispatches subagents |
| Fix tasks in tasks.yaml | No fix tasks — agent fixes in-session |
| Certifier called by verification.py | Certifier exposed as tool to agent |

### What stays the same

- **Certifier internals**: preflight + journey agents, builder-blind, immutable
- **Git operations**: worktrees, candidate refs, merge
- **Logging**: all agent tool calls captured, certifier reports persisted
- **`otto run`**: PER-based task execution (no certifier, retries still useful)
- **Cost tracking**: infrastructure tracks transparently

### Implementation approach

**Step 1: certify() as MCP tool**
- Wrap `run_unified_certifier()` as an MCP tool the coding agent can call
- Still use current session lifecycle (one build session, separate fix sessions)
- But certifier results go directly to agent as tool response, not via fix task text

**Step 2: Session continuity across certification**
- After build, don't kill the session
- Run certifier, inject results into same session
- Agent fixes in-place with full context
- Requires: SDK session resume or long-running session

**Step 3: Agent-as-orchestrator for monolithic**
- Agent drives the full loop: build → certify → fix → re-certify
- Infrastructure provides tools, guardrails, observability
- Pipeline becomes session manager

**Step 4: Subagent dispatch**
- Main agent dispatches parallel subagents via tool call
- Infrastructure manages worktrees, merges mechanical results
- Main agent resolves conflicts, handles integration

## Open Questions

1. **Certifier wall time**: 5-10 min for 7 stories. Agent blocked. Acceptable? Or should agent do something useful while waiting (e.g., write docs, add more tests)?
2. **Context window**: Long session with build + certifier feedback + fixes. Will it fit? Claude's context is large (200K) and compaction exists.
3. **SDK session resume**: Does it actually work reliably across a 10-minute gap?
4. **Cost model**: One long session vs multiple short sessions — which is cheaper? Hypothesis: long session is cheaper (no duplicated exploration).
5. **Failure modes**: What if the agent gets stuck in a fix loop? Max rounds + no-progress detection still needed as infrastructure guardrails.
