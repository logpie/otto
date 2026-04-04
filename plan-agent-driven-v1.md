# Plan: Agent-Driven Build — V1 (v2 — post Codex review)

## Goal

Replace PER-driven build with agent-driven build. The coding agent has a continuous session, gets certification feedback (from certifier or human), and fixes in-session with full context.

Two variants to experiment with. They differ significantly — not just in who triggers certification, but in who controls the loop, what the agent knows, and what the orchestrator does.

### Variant comparison

| | Variant A (agentic) | Variant B (orchestrated) |
|---|---|---|
| **Who controls the loop** | Agent | Orchestrator |
| **Orchestrator role** | Session manager + tool provider | Loop driver + feedback injector |
| **Agent prompt** | "Build, certify, fix, repeat" | "Build this" → "Fix these issues" → ... |
| **Tools** | CC tools + certify() job API | CC tools only |
| **Agent knows about certification** | Yes — submits jobs explicitly | No — just receives "user feedback" |
| **Session lifecycle** | One continuous run, agent decides when to stop | Multiple resume() calls, orchestrator controls turns |
| **Certification timing** | Agent decides (could certify early, per-feature, etc.) | Orchestrator decides (after session ends) |
| **Certifier independence** | Weaker — agent knows it exists, could optimize for it | Stronger — agent can't distinguish certifier from human |
| **Paradigm** | Agent IS the loop | Orchestrator IS the loop (but agent keeps context) |

Variant A is the real paradigm shift — fully agentic. Variant B is an evolution of PER with session continuity.

## Key Design Decisions (from Codex review)

### Certifier physical isolation (not just conceptual)

"Builder-blind" requires physical isolation, not just separate code paths:
- **Certifier runs in a detached worktree** at the anchored candidate ref, never in the agent's workspace. AppRunner installs deps, starts app, runs journeys — all in the isolated worktree.
- **Certifier caches stored in orchestrator-owned storage** (`~/.otto/certifier-cache/`). All cache loaders/savers in the certifier stack updated to accept and honor an explicit `cache_dir` parameter (stories.py, baseline.py, etc.).
- **Cache isolation is path-based, not OS-level.** The agent runs with `bypassPermissions` and could technically access `~/.otto/`. True OS-level isolation (separate user, container) is a future hardening step. For V1, path relocation + agent prompt constraints ("work only in the project directory") provide practical isolation. The certifier cache path is NOT in the project tree, so the agent won't discover it via normal file exploration.
- This prevents: agent editing files while certifier runs, dep install contaminating agent's workspace, and incidental cache reads via codebase exploration. It does NOT prevent a deliberately adversarial agent — that requires OS-level sandboxing (Phase 2+).

### Session resume: build and test first

`run_agent_query()` currently has no `session_id` parameter. Session resume is provider-specific and Otto skips it for Codex. Before building either variant:
1. Extend `otto/agent.py` to accept `session_id` for resume
2. Test resume across providers (Claude SDK, Codex)
3. Build `otto/session.py` as a tested abstraction over start/resume/fallback
4. Prove: resume after 10-min gap returns same-context conversation

If resume fails for a provider, fallback: start new session with compressed state package (intent, architecture decisions, what was built, certifier findings). Mark the continuity breach in telemetry.

### Candidate snapshots: reuse existing git ops

Don't `git add -A`. Reuse `build_candidate_commit()` from `git_ops.py` which:
- Filters otto-owned files
- Creates proper candidate refs
- Doesn't stage unrelated files

Agent-driven builds run in a dedicated worktree/branch so `otto: candidate round N` commits don't pollute user-visible branch history.

### Explicit agent end states

"Session end = ready" is unreliable. Agent can stop for: uncertainty, tool error, local optimum, provider interruption. Require structured end states via `output_format`:

```python
output_schema = {
    "type": "object",
    "properties": {
        "status": {"enum": ["ready_for_review", "blocked", "needs_human_input"]},
        "summary": {"type": "string"},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary"],
}
```

Agent's structured output tells the orchestrator exactly what happened: ready for certification, blocked on something, or needs human input.

### Certification outcome mapping

Certifier results must be classified before becoming feedback:

| Outcome | Action |
|---------|--------|
| `passed` | Done (or offer human feedback in interactive mode) |
| `failed_actionable` | Format findings as feedback, resume agent session |
| `blocked` | Report to user, do NOT feed to agent as fixable bugs |
| `infra_error` | Retry certifier once with backoff, then report to user. NOT fed to agent. |

Note: `CertificationOutcome` enum needs INFRA_ERROR added (currently only PASSED/FAILED/BLOCKED). An infra error is when the certifier itself crashes — app won't start due to port conflict, story compilation fails, journey agent SDK error, etc.

BLOCKED and infra_error are NOT "user feedback" — the agent can't fix infrastructure problems.

### Human feedback classification

Human input after certification must be classified:
- **Within-spec bug report**: "the delete button doesn't work" → feed to agent, re-certify against same intent
- **New requirement**: "add tags to todos" → update intent/grounding, start new build cycle (or continue session with updated grounding)

For V1: treat all human input as within-spec. New requirements as a separate flow is a future enhancement.

### No-progress detection

Compare normalized root-cause fingerprints, not just finding counts:
- Hash: `(finding.category, finding.description[:50], finding.story_id)`
- Track which findings resolved vs persisted vs regressed (new findings after fix)
- Equal count can mean progress (different bugs). Lower count can hide regressions.
- Keep `passed_story_ids` tracking from current verification.py.

### Behind flag until parity

New path behind `--agent-driven` flag (or config `build_mode: agent_driven`). Default stays current PER until observability and recovery parity proven:
- Task counts, logs, run-history work correctly
- `otto show`, `otto status` work
- Crash recovery works
- Cost tracking accurate

## Shared Infrastructure

### 1. Session management (`otto/session.py`)

```python
@dataclass
class SessionCheckpoint:
    session_id: str
    base_sha: str              # commit at session start (before agent changes)
    round: int
    state: str                 # "building" | "certifying" | "certified" | "fixing"
    certifier_outcome: str | None  # "passed" | "failed" | "blocked" | "infra_error" | None
    candidate_sha: str | None  # latest candidate ref (None before first snapshot)
    intent: str
    last_summary: str          # agent's last status summary (from structured output)
    findings: list[dict] | None  # certifier findings (None if pre-certification)
    cost_so_far: float
    created_at: str

class AgentSession:
    """Manages coding agent session lifecycle: start, resume, checkpoint."""
    
    async def start(self, prompt: str, options: ClaudeAgentOptions) -> SessionResult:
        """Start a new agent session."""
        ...
    
    async def resume(self, feedback: str) -> SessionResult:
        """Resume session with feedback. Falls back to new session + state package."""
        try:
            return await run_agent_query(feedback, self.options, session_id=self.session_id)
        except ResumeFailedError:
            # Fallback: new session with compressed context
            state_prompt = self._build_state_package(feedback)
            return await run_agent_query(state_prompt, self.options)
    
    def checkpoint(self, candidate_sha: str, *, findings: list | None = None,
                   state: str = "building",
                   certifier_outcome: str | None = None) -> None:
        """Write durable checkpoint for crash recovery.
        
        state: "building" | "certifying" | "certified" | "fixing"
        certifier_outcome: "passed" | "failed" | "blocked" | "infra_error" | None
        findings: certifier findings (None if pre-certification checkpoint)
        """
        ...
    
    def _build_state_package(self, feedback: str) -> str:
        """Compressed context for fresh session when resume fails."""
        return f"""You were building a product and have been resumed after an interruption.

Intent: {self.intent}
What you built: {self.last_summary}
Current state: code is committed at {self.candidate_sha}
Previous certification feedback:
{feedback}

Continue fixing the issues above. Your code is still in the project directory."""
```

### 2. Certifier isolation (`otto/certifier/isolated.py`)

```python
def run_isolated_certifier(
    intent: str,
    candidate_sha: str,
    project_dir: Path,
    config: dict,
    cache_dir: Path | None = None,
) -> CertificationReport:
    """Run certifier in an isolated worktree at the candidate ref.
    
    - Creates a temporary worktree at candidate_sha
    - Runs certifier entirely within that worktree
    - Certifier caches stored in cache_dir (not project_dir)
    - Worktree cleaned up after certification
    - Agent's workspace is untouched
    """
    cache_dir = cache_dir or Path.home() / ".otto" / "certifier-cache"
    
    with _create_certifier_worktree(project_dir, candidate_sha) as wt_dir:
        report = run_unified_certifier(
            intent=intent,
            project_dir=wt_dir,
            config={**config, "certifier_cache_dir": cache_dir},
        )
    return report
```

### 3. Feedback formatting (`otto/feedback.py`)

```python
def format_certifier_as_feedback(report: CertificationReport) -> str | None:
    """Format certifier findings as agent feedback. Returns None for non-actionable outcomes."""
    if report.outcome == CertificationOutcome.PASSED:
        return None
    if report.outcome == CertificationOutcome.BLOCKED:
        return None  # NOT agent-fixable — report to user instead
    
    critical = report.critical_findings()
    if not critical:
        return None
    
    lines = ["A user tested your product and found these issues:\n"]
    for i, f in enumerate(critical, 1):
        lines.append(f"{i}. {f.description}")
        if f.diagnosis:
            lines.append(f"   What happened: {f.diagnosis}")
        if f.fix_suggestion:
            lines.append(f"   Suggested fix: {f.fix_suggestion}")
        lines.append("")
    
    warnings = report.break_findings()
    if warnings:
        lines.append("Quality warnings (edge cases found during testing):")
        for f in warnings:
            lines.append(f"- [{f.severity}] {f.description}")
        lines.append("")
    
    lines.append("Please fix these issues and let me know when you're done.")
    return "\n".join(lines)
```

### 4. Candidate snapshots (reuse `git_ops.py`)

```python
def _certify_with_retry(intent, candidate_sha, project_dir, config, max_retries=1):
    """Run certifier with one retry on infra errors."""
    for attempt in range(max_retries + 1):
        report = run_isolated_certifier(intent, candidate_sha, project_dir, config)
        if report.outcome != CertificationOutcome.INFRA_ERROR:
            return report
        if attempt < max_retries:
            time.sleep(5)  # backoff before retry
    return report  # still INFRA_ERROR after retries


def snapshot_candidate(project_dir: Path, round_num: int, base_sha: str) -> str:
    """Create an immutable candidate ref from the agent's current work.
    
    base_sha: the commit at session start (before agent made changes).
    Persisted in session checkpoint at session creation time.
    """
    from otto.git_ops import build_candidate_commit, _anchor_candidate_ref
    
    candidate_sha = build_candidate_commit(project_dir, base_sha, pre_existing_untracked=set())
    _anchor_candidate_ref(project_dir, f"build-round-{round_num}", round_num, candidate_sha)
    return candidate_sha
```

### 5. Guardrails

```python
@dataclass
class BuildGuardrails:
    max_rounds: int = 3
    max_session_cost: float = 10.0
    max_session_time: int = 3600
    no_progress_stops: bool = True
```

## Variant A: Agent Calls certify()

### Design

Fully agentic. Agent drives everything. Orchestrator provides tools and guardrails.

### certify() as job API (not blocking call)

A 5-10 min blocking tool call is a bad failure surface. Instead, job API semantics:

```python
# Agent calls:
job = certify_submit()      # returns immediately with job_id
# Agent can do other work (write docs, add tests, refactor)
result = certify_status(job_id)  # poll when ready (or agent just waits)
```

But for V1 simplicity: certify() blocks and returns. The agent waits. This is acceptable because:
- The agent has nothing better to do while waiting (it needs the feedback to continue)
- Job API adds complexity (poll loop, state management)
- We can upgrade to job API later if blocking is a problem

The certify() tool output is coarse — actionable findings only, not the full certifier schema. This limits what the agent can learn about the certifier's internals.

```python
# Tool output (coarse, not full schema):
{
    "status": "failed",  // "passed" | "failed" | "error"
    "issues": [
        {"what": "XSS in todo creation", "detail": "HTML stored without sanitization", "suggestion": "Escape HTML entities"},
    ],
    "warnings": ["No input length validation (10K char title accepted)"],
}
// On infra error: {"status": "error", "message": "Certification could not run: app failed to start"}
// Agent should stop and report — this is not a code bug to fix.
```

### Orchestrator

```python
async def build_variant_a(intent, project_dir, config, guardrails):
    certify_server = CertifyMCPServer(project_dir, intent, config, guardrails)
    
    options = ClaudeAgentOptions(
        system_prompt=VARIANT_A_PROMPT,
        cwd=str(project_dir),
        mcp_servers=[certify_server],
        permission_mode="bypassPermissions",
        output_format={"type": "json", "schema": END_STATE_SCHEMA},
    )
    
    prompt = f"Build this product:\n\n{intent}"
    session = AgentSession(intent=intent, options=options)
    result = await session.start(prompt, options)
    
    return _parse_build_result(result, session)
```

### Prompt

```
You are building a product from the intent below. You are an autonomous developer.

1. Read the intent carefully. Plan your approach.
2. Build the product — write code, write tests, make tests pass.
3. When ready, call certify() to get user feedback on your product.
4. Read the feedback. Fix issues. Call certify() again.
5. Repeat until it passes or you've addressed all issues.

certify() submits your current code for user testing and returns feedback.
Each call takes several minutes. Use it when you believe the product is ready.

certify() returns {status, issues, warnings}:
- status "passed": product works, you're done.
- status "failed": issues found, fix them and certify again.
- status "error": testing infrastructure failed, NOT a code bug. Stop and
  report the error. Do NOT attempt code fixes for "error" status.

When you're done (passed or addressed all issues), provide your final status.
```

## Variant B: Orchestrator Injects Feedback

### Design

Orchestrator drives the loop. Agent keeps context across rounds. Agent doesn't know certification exists.

### Orchestrator

```python
async def build_variant_b(intent, project_dir, config, guardrails, on_human_feedback=None):
    options = ClaudeAgentOptions(
        system_prompt=VARIANT_B_PROMPT,
        cwd=str(project_dir),
        permission_mode="bypassPermissions",
        output_format={"type": "json", "schema": END_STATE_SCHEMA},
    )
    
    session = AgentSession(intent=intent, options=options)
    build_prompt = f"Build this product:\n\n{intent}"
    result = await session.start(build_prompt, options)
    
    report = None  # may exit loop before any certification
    prev_fingerprints = set()
    
    for round_num in range(1, guardrails.max_rounds + 1):
        # Check agent's end state before certifying
        agent_status = _parse_end_state(result)
        if agent_status == "blocked":
            break  # agent is stuck, don't certify incomplete work
        if agent_status == "needs_human_input":
            if on_human_feedback:
                human = await on_human_feedback(None)  # no certifier report yet
                if human:
                    result = await session.resume(human)
                    continue
            break
        
        # Agent says ready_for_review — snapshot and certify
        # If structured output missing/malformed, treat as ready (fallback)
        candidate_sha = snapshot_candidate(project_dir, round_num, session.base_sha)
        
        # Checkpoint BEFORE certification (crash recovery: know we're mid-certify)
        session.checkpoint(candidate_sha, findings=None, state="certifying")
        
        # Certify in isolated worktree (with one infra retry)
        report = _certify_with_retry(intent, candidate_sha, project_dir, config)
        
        # Checkpoint AFTER certification (durable result with outcome)
        session.checkpoint(candidate_sha, findings=report.findings, state="certified",
                           certifier_outcome=report.outcome.value)
        
        # Check outcome — dispatch on all possible states
        if report.outcome == CertificationOutcome.PASSED:
            if on_human_feedback:
                human = await on_human_feedback(report)
                if human:
                    result = await session.resume(
                        f"Product passed testing. The user has additional feedback:\n{human}"
                    )
                    continue
            break
        
        if report.outcome in (CertificationOutcome.BLOCKED, CertificationOutcome.INFRA_ERROR):
            break  # not agent-fixable — report to user
        
        # Format actionable findings as feedback (FAILED outcome only)
        feedback = format_certifier_as_feedback(report)
        if not feedback:
            break  # no actionable findings
        
        # No-progress check
        current_fingerprints = _finding_fingerprints(report.findings)
        if round_num > 1 and current_fingerprints == prev_fingerprints:
            break
        prev_fingerprints = current_fingerprints
        
        # Human feedback (if interactive)
        if on_human_feedback:
            human = await on_human_feedback(report)
            if human:
                feedback += f"\n\nAdditional feedback from the user:\n{human}"
        
        # Resume session with feedback
        result = await session.resume(feedback)
    
    return _parse_build_result(result, session, report)  # report may be None (no cert attempted)
```

### Prompts

**Round 1:**
```
You are building a product. Build it, write tests, make them pass.
When you're done building, provide your final status.

Intent:
{intent}
```

**Round 2+ (injected by orchestrator):**
```
A user tested your product and found these issues:

1. XSS vulnerability in todo creation
   What happened: HTML tags stored without sanitization in POST /todos
   Suggested fix: Sanitize HTML entities before storing

2. Missing input validation
   What happened: Empty title accepted, creates blank todo
   Suggested fix: Return 400 when title is empty

Please fix these issues and let me know when you're done.
```

## Implementation Order

### Step 0: Prerequisites (must complete before either variant)
1. **`otto/session.py`**: session start/resume/checkpoint/fallback. Test resume across providers.
2. **`otto/certifier/isolated.py`**: run certifier in detached worktree at candidate ref. Move caches to orchestrator-owned storage.
3. **`otto/feedback.py`**: format findings as feedback, outcome classification.
4. Verify SDK `session_id` resume actually works with 10-min gap.

### Step 1: Variant B (simpler, lower risk)
- Build `build_variant_b()` in pipeline.py behind `--agent-driven` flag
- Structured end states via `output_format`
- No-progress detection with root-cause fingerprints
- CLI: `otto build --agent-driven "intent"`
- E2E test: todo API

### Step 2: Variant A (add certify() tool)
- Build `otto/certifier/mcp_tool.py` — MCP server wrapping isolated certifier
- Build `build_variant_a()` in pipeline.py
- CLI: `otto build --agent-driven --variant a "intent"`
- E2E test: todo API

### Step 3: Compare
- Run both variants + current PER on same projects
- Measure: pass rate, cost, time, rounds, fix quality, context utilization
- Chaos tests (see experiment plan)

## Experiment Plan

### Metrics
| Metric | How measured |
|--------|-------------|
| Pass rate | 5 runs per variant per project, count passes |
| Total cost | SDK cost tracking |
| Total time | Wall clock |
| Rounds to pass | Certification round count |
| Fix quality | Same bug fixed across runs? Agent re-explores? |
| Context utilization | Compare agent logs — does it re-read files it already wrote? |

### Projects
- Todo API (simple, Express + SQLite)
- Bookmark manager (medium, Express + SQLite + tags)
- Marketplace (complex, Next.js + Prisma + auth)

### Chaos tests
- Crash during certification → does checkpoint recovery work?
- Crash after snapshot before checkpoint → does it re-snapshot?
- 15-minute resume gap → does session continuity hold?
- Provider matrix: test with Claude SDK (primary) + Codex (if resume supported)
- Agent attempts to write to certifier cache directory → not in project tree, won't discover
- BLOCKED certifier outcome → not fed to agent as fixable?
- Human injects new requirement → V1: treated as within-spec, agent tries to add it. Future: reject and ask user to start new build cycle

### Decision criteria
- If variant A pass rate > variant B by >10%: A is default
- If variant B pass rate >= variant A and cost < A: B is default
- If both similar: B is default (stronger independence guarantee)
- If neither beats current PER: keep PER, session continuity was wrong hypothesis

## Files

- NEW: `otto/session.py` — session lifecycle
- NEW: `otto/certifier/isolated.py` — isolated certifier execution
- NEW: `otto/feedback.py` — findings → agent feedback
- NEW: `otto/certifier/mcp_tool.py` — certify() MCP server (variant A)
- MODIFY: `otto/pipeline.py` — add `build_agent_driven()` behind flag
- MODIFY: `otto/cli.py` — `--agent-driven`, `--interactive`, `--variant` flags
- MODIFY: `otto/agent.py` — session_id support in run_agent_query
- KEEP: `otto/certifier/__init__.py` — run_unified_certifier unchanged
- KEEP: `otto/verification.py` — legacy PER path
- KEEP: `otto/runner.py` — `otto run` unchanged

## Verify

- [ ] Session resume works after 10-min gap (tested per provider)
- [ ] Resume fallback: compressed state package when resume fails
- [ ] Certifier runs in isolated worktree at candidate SHA
- [ ] Certifier caches in orchestrator-owned storage, not project dir
- [ ] Certifier cache outside project tree, untouched in normal operation (OS-level isolation in Phase 2+)
- [ ] Candidate snapshots use build_candidate_commit (no git add -A)
- [ ] Agent-driven builds in dedicated worktree/branch (no polluting user history)
- [ ] Structured end states: ready_for_review, blocked, needs_human_input
- [ ] BLOCKED outcome NOT fed to agent as fixable bug
- [ ] Infra errors reported to user, not agent
- [ ] Human feedback treated as within-spec (V1)
- [ ] No-progress: root-cause fingerprints, not just count
- [ ] Behind --agent-driven flag until parity proven
- [ ] `otto run` unchanged
- [ ] `otto show`, `otto status` work with new path
- [ ] Crash recovery: checkpoint → resume from last round
- [ ] Cost tracking accurate across session resume
- [ ] Variant A: certify() output is coarse (no full certifier schema)
- [ ] Variant A: certify() blocks but is cancellable
- [ ] Variant B: feedback formatted as natural user feedback
- [ ] Variant B: session suspended during certification
- [ ] E2E: todo API with variant A
- [ ] E2E: todo API with variant B
- [ ] Comparison: A vs B vs PER on 3 projects × 5 runs
- [ ] Chaos: crash during certification, 15-min resume gap, poisoned cache attempt

## Plan Review

### Round 1 — Codex (14 issues)
- [CRITICAL] Session resume not implemented — fixed: build session.py first, test per provider, fallback to compressed state package
- [CRITICAL] Immutable snapshot is fake (certifier runs in live project_dir) — fixed: certifier runs in detached worktree at candidate ref
- [CRITICAL] Certifier mutates checkout (AppRunner installs deps) — fixed: all certifier work in isolated worktree
- [CRITICAL] Certifier caches poisonable by agent — fixed: caches in orchestrator-owned storage outside workspace
- [CRITICAL] git add -A regresses git hygiene — fixed: reuse build_candidate_commit, dedicated worktree/branch
- [IMPORTANT] Session end ≠ ready — fixed: structured end states via output_format
- [IMPORTANT] BLOCKED flattened into feedback — fixed: outcome classification, BLOCKED not fed to agent
- [IMPORTANT] Human feedback mixes bugs with scope changes — fixed: V1 treats all as within-spec, new requirements deferred
- [IMPORTANT] No-progress detection too weak — fixed: root-cause fingerprints
- [IMPORTANT] Existing system breakage — fixed: behind --agent-driven flag until parity
- [VARIANT A] 5-10 min blocking tool call — fixed: V1 blocks (simple), upgrade to job API later if needed
- [VARIANT A] Independence weaker than claimed — fixed: coarse output only, treat as experiment not default
- [VARIANT B] Resume failure — fixed: fallback to compressed state package, telemetry breach marker
- [IMPORTANT] Experiment plan insufficient — fixed: chaos tests, provider matrix, explicit decision criteria

### Round 2 — Codex (6 issues)
- [CRITICAL] Cache isolation is path-based not OS-level — fixed: acknowledged as V1 limitation, OS-level sandboxing deferred to Phase 2+. Path relocation + prompt constraints provide practical isolation.
- [CRITICAL] Cache loaders not wired to honor cache_dir — fixed: explicit cache_dir parameter threaded through certifier stack (stories.py, baseline.py loaders updated in implementation)
- [IMPORTANT] Variant B doesn't inspect agent end states — fixed: parse end state before certifying, gate on ready_for_review, handle blocked/needs_human_input
- [IMPORTANT] snapshot_candidate passes base_sha=None — fixed: persist base_sha at session start, pass through to build_candidate_commit
- [IMPORTANT] Crash recovery gap between snapshot and checkpoint — fixed: two-phase checkpoint (pre-certification "certifying" + post-certification "certified")
- [NOTE] Human feedback chaos test inconsistent with V1 scope — fixed: V1 treats new requirements as within-spec, documented explicitly

### Round 3 — Codex (5 issues)
- [IMPORTANT] Verify checklist claims "agent cannot read/write cache" but V1 is path-based only — fixed: criterion changed to "cache outside project tree, untouched in normal operation"
- [IMPORTANT] snapshot_candidate call site missing base_sha — fixed: use session.base_sha
- [IMPORTANT] Checkpoint API doesn't match usage (no state param) — fixed: added state enum + optional findings
- [IMPORTANT] report undefined if loop exits before certification — fixed: initialize report=None, _parse_build_result handles it
- [NOTE] "Session ended naturally" reintroduces ambiguity — fixed: ready_for_review is the gate, malformed/missing output = fallback to ready

### Round 4 — Codex (2 issues)
- [IMPORTANT] SessionCheckpoint missing fields used by resume/snapshot — fixed: added base_sha, state, last_summary to checkpoint schema
- [IMPORTANT] infra_error outcome not implemented in loop — fixed: explicit infra_error branch with one retry + backoff, then stop. INFRA_ERROR added to CertificationOutcome enum.

### Round 5 — Codex (2 issues)
- [IMPORTANT] Checkpoint state enum inconsistent ("ready" vs "building") — fixed: canonical set is building/certifying/certified/fixing everywhere
- [IMPORTANT] infra_error retry gated on round_num==1 and falls through wrong — fixed: extracted _certify_with_retry() helper, runs per-certification with inner retry loop, outcome dispatch is clean after

### Round 6 — Codex (2 issues)
- [IMPORTANT] Variant A certify() tool has no infra_error surface — fixed: tool returns {status: "error", message: ...} for infra failures. Agent told to stop and report, not fix.
- [IMPORTANT] Checkpoint collapses all outcomes into "certified" state — fixed: added certifier_outcome field to SessionCheckpoint (passed/failed/blocked/infra_error/None)

### Round 7 — Codex (2 issues)
- [IMPORTANT] checkpoint() API missing certifier_outcome param — fixed: added to signature, Variant B passes report.outcome.value on post-certification checkpoint
- [IMPORTANT] Variant A prompt doesn't teach agent about error status — fixed: prompt now documents all 3 statuses with explicit "do NOT fix code for error"
