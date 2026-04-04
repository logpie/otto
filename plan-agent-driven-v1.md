# Plan: Agent-Driven Build — V1

## Goal

Replace PER-driven build with agent-driven build. The coding agent has a continuous session, gets certification feedback (from certifier or human), and fixes in-session with full context.

Two variants to experiment with:

- **Variant A (agentic):** Agent calls `certify()` as a tool. Fully agentic — agent decides when to request feedback.
- **Variant B (orchestrated):** Orchestrator runs certifier and injects feedback into agent session. Agent doesn't know certification exists.

Both variants share the same infrastructure: session continuity, candidate snapshots, human feedback injection.

## Shared Infrastructure

### 1. Session continuity

The coding agent's session persists across build→feedback→fix cycles.

**SDK mechanism:** `session_id` is already tracked in runner.py. The SDK supports resuming a session by passing `session_id` to a new `query()` call. The agent sees it as a continuation of the same conversation.

```python
# First turn: agent builds
session_id, result, _ = await run_agent_query(build_prompt, options)

# Later: inject feedback into same session
feedback_prompt = "User tested your product. Issues found:\n" + findings_text
session_id, result, _ = await run_agent_query(
    feedback_prompt, options, session_id=session_id
)
# Agent sees this as a follow-up message in the same conversation
```

**Checkpoint after each round:** Write durable state so crashes are recoverable.
```json
{
  "session_id": "sess_abc123",
  "candidate_sha": "abc1234",
  "round": 2,
  "intent": "...",
  "last_findings": [...],
  "cost_so_far": 1.23
}
```

### 2. Candidate snapshots

Before certification, commit the agent's work as an immutable ref:

```python
def _snapshot_candidate(project_dir: Path, round_num: int) -> str:
    """Commit current state as a candidate ref for certification."""
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", f"otto: candidate round {round_num}"],
        cwd=project_dir, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_dir,
        capture_output=True, text=True,
    ).stdout.strip()
    return sha
```

The certifier tests this exact SHA. The agent can keep editing after signaling readiness (in variant A), but the certifier tests the snapshot.

### 3. Human feedback injection

The orchestrator can inject human feedback alongside certifier feedback:

```python
async def build_with_feedback(intent, project_dir, config, on_human_feedback=None):
    """
    on_human_feedback: async callable that returns human input (or None to skip).
    Called after each certification round. Blocks until human responds or times out.
    """
    ...
    after_certify:
        # Certifier feedback
        messages.append(format_certifier_feedback(report))
        
        # Human feedback (if interactive mode)
        if on_human_feedback:
            human_input = await on_human_feedback(report)
            if human_input:
                messages.append(f"Additional feedback from the user:\n{human_input}")
    ...
```

CLI integration:
```
otto build "todo app"                    # fully autonomous
otto build "todo app" --interactive      # pause for human input after each round
```

### 4. Guardrails (infrastructure-owned)

```python
@dataclass
class BuildGuardrails:
    max_rounds: int = 3           # max certify→fix cycles
    max_session_cost: float = 10.0  # cost budget for the coding session
    max_session_time: int = 3600   # 1 hour wall clock
    no_progress_stops: bool = True  # stop if same failures across rounds
```

These are infrastructure concerns. The agent doesn't see them — it just gets told "session ending" if a guardrail triggers.

## Variant A: Agent Calls certify()

### Design

The agent has `certify()` as a tool. It decides when to call it.

```
System prompt:
  You are building a product. Build it, write tests, make them pass.
  When ready, call certify() to submit for user testing. You'll receive
  structured feedback on what works and what doesn't. Fix issues and
  certify again. Repeat until all issues are resolved.

Tools:
  - Standard CC tools (Bash, Read, Write, Edit, etc.)
  - certify(): Submit current code for product certification.
    Returns: {passed, findings: [{description, diagnosis, fix_suggestion, severity}]}
```

### certify() implementation

```python
# MCP tool or injected tool definition
async def certify_tool(project_dir: Path, intent: str, config: dict) -> dict:
    """Called by the coding agent when it wants certification."""
    # 1. Snapshot current work
    candidate_sha = _snapshot_candidate(project_dir, round_num)
    
    # 2. Run certifier (blocks 5-10 min)
    report = run_unified_certifier(
        intent=intent,
        project_dir=project_dir,
        config=config,
    )
    
    # 3. Return structured findings
    return {
        "passed": report.passed,
        "round": round_num,
        "findings": [
            {
                "description": f.description,
                "diagnosis": f.diagnosis, 
                "fix_suggestion": f.fix_suggestion,
                "severity": f.severity,
            }
            for f in report.findings
        ],
    }
```

### Session flow

```python
async def build_variant_a(intent, project_dir, config, guardrails):
    """Agent-driven build with certify() tool."""
    
    # Inject certify as a tool via MCP or tool definition
    certify_server = CertifyMCPServer(project_dir, intent, config, guardrails)
    
    options = ClaudeAgentOptions(
        system_prompt=AGENT_DRIVEN_PROMPT,
        cwd=str(project_dir),
        mcp_servers=[certify_server],
        permission_mode="bypassPermissions",
    )
    
    prompt = f"Build this product:\n\n{intent}"
    
    # One session — agent builds, calls certify(), fixes, calls certify(), etc.
    # Session ends when: agent stops (certified), or guardrail triggers
    session_id, result, _ = await run_agent_query(prompt, options)
    
    return result
```

### Pros
- Fully agentic — agent decides strategy (certify early, certify after each feature, etc.)
- Simple — one session, one prompt, agent does everything
- Agent can call certify() strategically ("let me test just the API before building the frontend")

### Cons
- Agent blocked 5-10 min per certify() call (context window occupied but idle)
- Agent knows certification exists — could game it
- Agent might call it too often (expensive) or too rarely (waste build time)
- 5-10 min tool call creates timeout/crash edge cases

## Variant B: Orchestrator Injects Feedback

### Design

The agent doesn't know about certification. It just builds. The orchestrator decides when to certify and feeds results back as "user feedback."

```
System prompt:
  You are building a product. Build it, write tests, make them pass.
  When you believe the product is complete, say "The product is ready 
  for review" and stop making changes. You may receive user feedback
  with issues to fix — address them and signal ready again.

Tools:
  - Standard CC tools only (Bash, Read, Write, Edit, etc.)
  - No certify() tool
```

### Session flow

```python
async def build_variant_b(intent, project_dir, config, guardrails, on_human_feedback=None):
    """Orchestrator-driven build with feedback injection."""
    
    options = ClaudeAgentOptions(
        system_prompt=ORCHESTRATED_PROMPT,
        cwd=str(project_dir),
        permission_mode="bypassPermissions",
    )
    
    # Round 1: Build
    build_prompt = f"Build this product:\n\n{intent}"
    session_id, result, _ = await run_agent_query(build_prompt, options)
    
    for round_num in range(1, guardrails.max_rounds + 1):
        # Snapshot and certify
        candidate_sha = _snapshot_candidate(project_dir, round_num)
        report = run_unified_certifier(intent=intent, project_dir=project_dir, config=config)
        
        if report.passed:
            # Optional: let human add more requirements
            if on_human_feedback:
                human = await on_human_feedback(report)
                if human:
                    # Continue session with human feedback
                    session_id, result, _ = await run_agent_query(
                        f"Product passed testing. The user has additional feedback:\n{human}",
                        options, session_id=session_id,
                    )
                    continue  # re-certify after human-requested changes
            break
        
        # Format findings as user feedback
        feedback = _format_as_user_feedback(report)
        
        # Optional: human feedback too
        if on_human_feedback:
            human = await on_human_feedback(report)
            if human:
                feedback += f"\n\nAdditional feedback from the user:\n{human}"
        
        # No progress check
        if round_num > 1 and _no_progress(prev_findings, report.findings):
            break
        prev_findings = report.findings
        
        # Resume session with feedback
        session_id, result, _ = await run_agent_query(
            feedback, options, session_id=session_id,
        )
    
    return BuildResult(...)


def _format_as_user_feedback(report):
    """Format certifier findings as natural user feedback."""
    lines = ["A user tested your product and found these issues:\n"]
    for i, f in enumerate(report.critical_findings(), 1):
        lines.append(f"{i}. {f.description}")
        if f.diagnosis:
            lines.append(f"   What happened: {f.diagnosis}")
        if f.fix_suggestion:
            lines.append(f"   Suggested fix: {f.fix_suggestion}")
        lines.append("")
    
    if report.break_findings():
        lines.append("Quality warnings (edge cases):")
        for f in report.break_findings():
            lines.append(f"- [{f.severity}] {f.description}")
        lines.append("")
    
    lines.append("Please fix these issues. When done, say 'ready for review'.")
    return "\n".join(lines)
```

### Pros
- Certifier fully blind — agent can't game it
- Session suspended during certification (no idle context burn)
- Infrastructure controls timing, cost, rounds
- Human feedback is natural — same injection mechanism
- Simpler agent prompt (no tool to explain)

### Cons
- Agent can't request early feedback
- "Ready for review" detection is fuzzy (need to detect when agent is done)
- Less agentic — orchestrator decides the feedback loop

## Detecting "Agent is Done"

Both variants need to know when the agent has finished building. Options:

**Structured output:** Agent's final message must include a JSON signal:
```json
{"status": "ready_for_review", "summary": "Built todo API with CRUD endpoints"}
```

**Natural language detection:** Check if agent's last message contains phrases like "the product is complete", "ready for review", "done building". Fragile.

**ResultMessage inspection:** The SDK's ResultMessage has `subtype`. When the agent stops calling tools and produces a final text response, it's done. This is the natural signal — the agent has nothing more to do.

**Simplest for V1:** Use SDK's natural session end. When `run_agent_query()` returns, the agent is done. No explicit signal needed. The agent builds until it's satisfied, then the session ends naturally.

## What to Build

### Common (both variants)
- `otto/session.py` — session management: start, resume, checkpoint, guardrails
- `otto/feedback.py` — format certifier findings as agent feedback
- Update `otto/pipeline.py` — new `build_agent_driven()` alongside existing `build_product()`
- Update `otto/cli.py` — `--interactive` flag, `--variant a|b` for experimentation

### Variant A specific
- `otto/certifier/mcp_tool.py` — MCP server wrapping certify()
- Agent prompt with certify() tool description

### Variant B specific  
- "Ready for review" detection (or just: session ends = ready)
- Feedback injection via session resume

### Shared with existing code
- `run_unified_certifier()` — unchanged, called by both variants
- Git operations — candidate snapshots, worktrees
- Logging/observability — track rounds, cost, findings per round
- `otto run` — unchanged, PER-based

## Experiment Plan

Build both variants. Run same intent on same project. Compare:

| Metric | Variant A | Variant B | Current PER |
|--------|-----------|-----------|-------------|
| Pass rate (out of 5 runs) | ? | ? | ? |
| Total cost | ? | ? | ? |
| Total time | ? | ? | ? |
| Rounds to pass | ? | ? | ? |
| Fix quality (same bug fixed?) | ? | ? | ? |
| Context utilization (agent re-explores?) | ? | ? | ? |

Test on: todo API (simple), bookmark manager (medium), marketplace (complex).

## Files to Create/Modify

- NEW: `otto/session.py` — session lifecycle management
- NEW: `otto/feedback.py` — certifier findings → agent feedback formatting
- NEW: `otto/certifier/mcp_tool.py` — certify() MCP server (variant A)
- MODIFY: `otto/pipeline.py` — add `build_agent_driven()` 
- MODIFY: `otto/cli.py` — `--interactive`, `--variant` flags
- KEEP: `otto/certifier/__init__.py` — `run_unified_certifier()` unchanged
- KEEP: `otto/verification.py` — legacy path, `otto run` still uses it
- KEEP: `otto/runner.py` — `otto run` still uses PER

## Verify

- [ ] Variant A: agent calls certify(), gets findings, fixes in-session
- [ ] Variant B: orchestrator injects findings, agent fixes in-session
- [ ] Session continuity: agent has full context of what it built when fixing
- [ ] Candidate snapshot: certifier tests immutable ref, not live workspace
- [ ] Human feedback: --interactive pauses for human input after each round
- [ ] Guardrails: max rounds, cost budget, no-progress detection
- [ ] Checkpoint/recovery: crash during certification → resume from last checkpoint
- [ ] Certifier independence: agent cannot influence certifier behavior
- [ ] `otto run` unchanged — PER still works
- [ ] Existing tests pass
- [ ] E2E: todo API with variant A
- [ ] E2E: todo API with variant B
- [ ] Comparison: variant A vs B vs current PER on same project
