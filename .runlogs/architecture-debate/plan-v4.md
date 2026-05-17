# Otto v5 — Implementation Plan, Draft 4 (SDK-grounded)

Plan-v3 was the right architecture but reinvented mechanisms the SDK provides. After running four smoke tests against `claude_agent_sdk` 0.1.50, this draft uses SDK natives. The architecture is unchanged from v3; the implementation gets meaningfully simpler.

Smoke-test scripts: `/tmp/sdk-smoke/{smoke.py, test_depth2_mcp.py, test_deny_interrupt.py, test_subagent_cost.py, test_subagent_hook_id.py}`. Re-run after SDK upgrades.

---

## 0. Verified SDK primitives

These are the building blocks plan-v4 uses. Each verified empirically.

| Primitive | Use in v5 |
|---|---|
| `create_sdk_mcp_server(name, tools)` + `@tool` decorator | In-process MCP server holding Otto's custom tools (`submit_subtask`, `lock_paths`, `verify`, `checkpoint`). Direct Python function access, no IPC. |
| `ClaudeAgentOptions.mcp_servers={"otto": server}` | Register the in-process MCP. Reachable from main agent + subagents via the parent's control channel. |
| `ClaudeAgentOptions.agents={"build": AgentDefinition(...), "test": AgentDefinition(...)}` | Define build agent and test agent as named subagents. SDK manages their lifecycle. |
| `AgentDefinition.tools=[...]` + `AgentDefinition.mcpServers=[...]` | Per-subagent tool ACL. Build agent's `tools` excludes test files; test agent's excludes app code. |
| `HookMatcher` + `PreToolUse` callback | Hook fires before Write/Edit/Bash. Receives input dict including `agent_id` (subagent attribution) per `_SubagentContextMixin`. |
| Hook return `{"decision": "block", "reason": "..."}` | Deny a tool call; agent sees the denial in its tool result and reasons about it. |
| `TaskStartedMessage` / `TaskProgressMessage` / `TaskNotificationMessage` | Subagent lifecycle stream. Use to track child sessions, attribution, completion. |
| `ResultMessage.total_cost_usd` | Cumulative cost (parent + subagents). Use for budget enforcement. |
| `ClaudeAgentOptions.max_budget_usd` | Native budget cap. Use as the per-task ceiling. |
| `ClaudeAgentOptions.max_turns` | Per-call turn cap. Use to bound runaway subagents. |
| `ClaudeAgentOptions.resume: str` + `fork_session: bool` | Session resumption. Use for crash recovery within a task. |
| `ClaudeAgentOptions.output_format` (JSON Schema) | Structured spec compile output. |

---

## 1. Architecture (unchanged from v3)

Otto = workspace + tools + verifier + queue + merge coordinator. The Lead is the universal build primitive. Decomposition is per-task, decided by the Lead at runtime.

### 1.1 Persistent vs transient units

- **Project** (persistent): `~/otto-projects/<name>`, `main` branch, accumulates code/intent/queue.
- **Queue** (persistent, per-project): user-submitted + agent-emitted tasks.
- **Project state** (persistent, derived view): recent verdicts + open task graph. NOT a state machine.
- **Task** (transient): one Lead session, one worktree, one PR-sized change.

### 1.2 The Lead session, concretely

A task's Lead runs as one `query()` call with this options shape:

```python
options = ClaudeAgentOptions(
    system_prompt={"type": "preset", "preset": "claude_code"},
    permission_mode="bypassPermissions",
    cwd=str(worktree_path),
    max_turns=200,
    max_budget_usd=task_budget,

    # In-process MCP holding Otto's custom tools.
    mcp_servers={"otto": create_otto_mcp_server(session_dir, project_dir)},

    # Build/test agents as named subagents.
    agents={
        "build": AgentDefinition(
            description="Build app code (no tests).",
            prompt=BUILD_PROMPT,
            tools=[
                "Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite",
                "mcp__otto__lock_paths",
                "mcp__otto__submit_subtask",
                "mcp__otto__checkpoint",
                "Task",  # may dispatch helpers
            ],
            mcpServers=["otto"],
        ),
        "test": AgentDefinition(
            description="Write tests + selectors against running product.",
            prompt=TEST_PROMPT,
            tools=[
                "Read", "Write", "Edit", "Bash",  # Write blocked outside tests/** by hook
                "mcp__otto__verify",
                "mcp__otto__checkpoint",
            ],
            mcpServers=["otto"],
        ),
    },

    # PreToolUse hook chain: lock_check + bash_safety.
    hooks={
        "PreToolUse": [
            HookMatcher(matcher="Write|Edit|Bash", hooks=[lock_check_hook]),
            HookMatcher(matcher="Bash", hooks=[bash_safety_hook]),
        ],
    },

    # Lead orchestration only — actual building is delegated to "build" subagent.
    allowed_tools=["Task", "mcp__otto__lock_paths", "mcp__otto__submit_subtask",
                   "mcp__otto__verify", "mcp__otto__checkpoint", "TodoWrite"],
)
```

The Lead's prompt instructs it to:
1. Read intent + project state.
2. Decide decomposition (inline / in-session subagents / emit child tasks).
3. Dispatch the `build` subagent (or skip to direct work in solo tier).
4. After build commits, dispatch the `test` subagent.
5. Call `mcp__otto__verify` to run the audit.
6. Output a final result block with verdict.

### 1.3 Build/test split via AgentDefinitions

Build and test are SDK subagents, not separate `query()` calls. The Lead dispatches them via Task. The SDK enforces tool ACLs per-AgentDefinition. `mcp_servers=["otto"]` on each definition makes the in-process MCP reachable from the subagent (verified by smoke).

The frozen `behavior_journeys.md` is enforced by:
- `compile-spec-flat.md` lints journey selectors (no `class=`, `id=`, `getByRole`-with-non-exact, etc.) before writing.
- `behavior_journeys.md` is on the lock list as read-only for build and test agents.
- `verify` MCP tool reads it directly (cannot be modified).

### 1.4 Locks via PreToolUse hook + in-process state

`otto/locks.py` is a Python module with module-level state per Lead session:

```python
# Per-session, in-process. No sqlite.
class LockRegistry:
    """In-process lock state for one Lead session."""
    def __init__(self):
        self._locks: dict[str, list[str]] = {}  # agent_id -> globs
        self._parent_chain: dict[str, str] = {}  # agent_id -> parent_agent_id

    def acquire(self, agent_id: str, globs: list[str], parent_agent_id: str | None = None):
        self._locks[agent_id] = list(globs)
        if parent_agent_id:
            self._parent_chain[agent_id] = parent_agent_id

    def can_write(self, agent_id: str | None, path: str) -> bool:
        # agent_id None = main thread; main can write anything.
        if agent_id is None:
            return True
        # Walk parent chain; if any ancestor's lock matches, allow.
        cur = agent_id
        while cur:
            for glob in self._locks.get(cur, []):
                if fnmatch.fnmatch(path, glob):
                    return True
            cur = self._parent_chain.get(cur)
        return False

    def release(self, agent_id: str):
        self._locks.pop(agent_id, None)
        self._parent_chain.pop(agent_id, None)
```

The PreToolUse hook reads `agent_id` from the input dict (verified populated for subagents per smoke test 4):

```python
async def lock_check_hook(input_data, tool_use_id, context) -> dict:
    tool = input_data.get("tool_name")
    if tool not in ("Write", "Edit"):
        return {}  # only check writes
    agent_id = input_data.get("agent_id")  # None if main thread
    path = (input_data.get("tool_input") or {}).get("file_path", "")
    if not registry.can_write(agent_id, path):
        return {
            "decision": "block",
            "reason": f"Write to {path!r} denied: outside agent {agent_id}'s lock.",
        }
    return {}
```

For Bash with redirects (`> path`, `>> path`, `tee path`), parse the command and check redirected paths the same way.

Locks live for the duration of the Lead session. When the session ends, the LockRegistry is GC'd. NO sqlite, NO PID watcher, NO TTL. Cross-task locks (preventing two Leads writing the same file in different worktrees) is a non-issue because each task has its own worktree.

### 1.5 The `otto` MCP server (custom tools)

`otto/mcp_tools.py` (new):

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

def create_otto_mcp_server(session_dir, project_dir, lock_registry):
    """Build the in-process MCP server for one Lead session."""

    @tool("lock_paths",
          "Register write-time locks for self or a named subagent. "
          "Subsequent Write/Edit calls outside locked paths will be denied.",
          {
              "agent_id": str,        # "self", "build", "test", or specific subagent name
              "globs": list[str],     # paths the agent may write
              "parent_agent_id": str  # "lead" for top-level; "build" if Lead's helper, etc.
          })
    async def lock_paths(args):
        lock_registry.acquire(
            agent_id=args["agent_id"],
            globs=args["globs"],
            parent_agent_id=args.get("parent_agent_id"),
        )
        return {"content": [{"type": "text", "text": "ok"}]}

    @tool("submit_subtask",
          "Emit a child task to the project's queue. Returns task_id immediately. "
          "Parent task's verdict will wait on child closure.",
          {
              "intent": str,
              "depends_on": list[str],  # other task_ids this depends on
              "group_id": str,          # optional grouping (transactional merges)
          })
    async def submit_subtask(args):
        from otto.queue.subtask import enqueue_subtask
        task_id = enqueue_subtask(
            project_dir=project_dir,
            parent_session_dir=session_dir,
            intent=args["intent"],
            depends_on=args.get("depends_on", []),
            group_id=args.get("group_id"),
        )
        return {"content": [{"type": "text", "text": json.dumps({"task_id": task_id})}]}

    @tool("verify",
          "Run the deterministic verifier (browser/CLI/HTTP probes) against the running product "
          "using frozen behavior_journeys.md. Returns structured PASS/FAIL with evidence paths.",
          {"feature_ids": list[str]})  # optional narrowing
    async def verify(args):
        from otto.audit import run_verifier_against_session
        result = await run_verifier_against_session(
            session_dir=session_dir,
            feature_ids=args.get("feature_ids"),
        )
        return {"content": [{"type": "text", "text": json.dumps(result.to_dict())}]}

    @tool("checkpoint",
          "Persist current state for resumability. Non-blocking in autopilot.",
          {"reason": str})
    async def checkpoint(args):
        from otto.checkpoint import write_checkpoint
        write_checkpoint(session_dir=session_dir, reason=args["reason"])
        return {"content": [{"type": "text", "text": "checkpointed"}]}

    return create_sdk_mcp_server("otto", "1.0.0", tools=[
        lock_paths, submit_subtask, verify, checkpoint,
    ])
```

These are real Python functions calling Otto's existing modules. No subprocess, no IPC.

### 1.6 Cross-task merge (unchanged from v3)

The watcher (`otto/queue/runtime.py`) extends with `MergeCoordinator`:

- Owns single-writer fcntl on `main`.
- On task completion, attempts `git rebase task-branch onto main`.
- Clean → land + trigger project audit.
- Conflict → resume task's Lead via `query(prompt=conflict_prompt, options=options.with(resume=session_id))` — SDK native session resume.
- Retry exhausted → mark task `merge_blocked`, drop, continue.

Project audit on `main`:
- After merge, run audit against accumulated `behavior_journeys.md`.
- PASS → done.
- FAIL → auto-revert the offending merge, file fix-task with audit findings.
- 3 fix-tasks fail → notification, project remains operational, no quarantine.

### 1.7 Subagent dispatch tracking

The watcher consumes `TaskStartedMessage` / `TaskNotificationMessage` from the Lead's stream:

```python
async for msg in query(prompt=lead_prompt, options=options):
    if isinstance(msg, TaskStartedMessage):
        # Subagent dispatched. Record in task_graph.json.
        task_graph.record_dispatch(parent_id=session_id, child_id=msg.task_id, task_type=msg.task_type)
    elif isinstance(msg, TaskNotificationMessage):
        # Subagent finished. Record verdict + cost.
        task_graph.record_completion(child_id=msg.task_id, status=msg.status, usage=msg.usage)
    elif isinstance(msg, AssistantMessage):
        ...
    elif isinstance(msg, ResultMessage):
        # Final cumulative cost/usage for the entire task (including subagents).
        task_result.cost_usd = msg.total_cost_usd
        task_result.usage = msg.usage
```

Parent-child verdict propagation reads `task_graph.json` (built from these messages) at proof-render time.

### 1.8 Cost ceilings (using SDK natives)

Per-task budget = `options.max_budget_usd` (cumulative across pilot + subagents, verified by smoke test 3). When the SDK exceeds this, it terminates the session. No custom budget pre-check needed.

Per-subagent: `AgentDefinition.tools` doesn't include `Task`, OR set per-call `max_turns` lower for leaf subagents. Recursion depth is naturally bounded by removing `Task` from leaf agent definitions.

### 1.9 Provider fallback

Plan-v3's task-level re-dispatch unchanged. Codex out-of-credits is detected at the task-result level (verdict `catastrophic` with `failure_reason="provider_exhausted"`); watcher re-enqueues with claude.

The Claude SDK has `fallback_model: str | None` for within-provider fallback (e.g., sonnet → haiku), but cross-provider migration is impossible mid-session.

### 1.10 What's deleted from today's Otto

(Same as v3.)

- `otto/spec_compile.py` group/contract synthesis (~400 lines, lines 1280–1418).
- `otto/spec_compile.py::_normalize_critical_shared_contract_scope` (~200 lines).
- `otto/build.py` group orchestration loop (~400 lines).
- `otto/build.py::detect_critical_shared_contract_violations` and post-hoc scope detection (~600 lines).
- `otto/repair_gates.py` (~300 lines).
- Prompts: `compile-spec-brownfield*.md`, `build-merge-repair.md`, `compile-spec-structured-output.md`.

Test cascade: `tests/test_build.py` (lines 33, 625, 641, 671), `tests/test_audit.py`, `tests/test_merge_queue.py` — deleted (testing wrong architecture) or rewritten in Phase 2.

Phase 6 moves deprecated code to `otto/legacy/` for one release before final deletion (preserves resume of pre-v5 checkpoints).

---

## 2. Verdict vocabulary (unchanged from v3)

| Verdict | Meaning |
|---|---|
| `pass` | All checks green; merged. Children all pass. |
| `partial` | Built and merged; some declared features missed within retry budget. |
| `pending_children` | Parent task whose children haven't all resolved. Not terminal. |
| `unverified` | Built and committed; verifier failed/timed-out. |
| `merge_blocked` | Couldn't integrate after retry exhaust. Worktree preserved. |
| `catastrophic` | Infrastructure failure (provider, disk, etc.). |

NO `quarantined`, NO `regression_unfixable` task verdict, NO project state machine. Project health is a derived view (recent verdicts + open issues + in-flight count) in MC.

---

## 3. Implementation phases (revised LOC with SDK natives)

### Phase 1 — Foundation + verification suite (1 week, ~500 LOC)

**Goal:** rails in place, no behavior change.

1. `otto/locks.py`: in-process LockRegistry (Python dict-based, no sqlite). (~100 LOC)
2. `otto/mcp_tools.py`: in-process MCP server with the four custom tools (`lock_paths`, `submit_subtask`, `verify`, `checkpoint`). Each calls existing Otto modules. (~250 LOC)
3. `otto/queue/subtask.py`: `enqueue_subtask(project_dir, parent_session_dir, intent, depends_on, group_id)`. (~100 LOC)
4. `otto/queue/task_graph.py`: parent-child graph storage via `task_graph.json`, fed by `TaskStartedMessage` / `TaskNotificationMessage` events. (~150 LOC)
5. **Phase-1 smoke test suite** (formalized): re-run all five smoke tests (smoke + 4 grounding tests) as part of the Otto test suite. Fail CI if SDK version changes the contract. (~200 LOC including fixtures)

Deliverable: foundation merged; SDK contract pinned via tests; no production behavior change yet.

### Phase 2 — Lead + flat spec + build/test split (1.5 weeks, ~600 LOC)

**Goal:** finance-dashboard runs through a Lead session with build/test split.

1. `otto/spec_compile_flat.py`: extracted from spec_compile.py, drops group synthesis. Outputs flat spec (features + behavior_journeys). Lints journeys for user-language. (~250 LOC of refactor + new code)
2. `otto/lead.py`: Lead runner. Sets up MCP server, AgentDefinitions, hooks. Streams messages, captures TaskStartedMessage/TaskNotificationMessage, builds task_graph. (~250 LOC)
3. New prompts: `lead.md`, `build-agent.md`, `test-agent.md`, `compile-spec-flat.md` with journey lint. (~300 LOC of content)
4. `otto/runner.py`: rewrite `run_pipeline` for the new 5-phase shape (compile → lead → audit → render). Old multi-phase chain preserved as `otto/legacy/run_pipeline_v4.py`. (~200 LOC of changes)
5. Verdict enum extension. (~30 LOC)
6. Integration test: finance-dashboard fixture end-to-end. (~150 LOC)

Deliverable: single tasks run through new pipeline. No fan-out yet (Lead does inline only). Build/test split active. Behavior journeys frozen.

### Phase 3 — Lead-emitted child tasks + parent-child verdict (1 week, ~400 LOC)

**Goal:** Lead can decompose by emitting subtasks.

1. `otto/decomp_select.py`: deterministic Lead heuristic + tier-flag mapping. (~100 LOC)
2. `otto/queue/runtime.py`: extend watcher to consume agent-emitted submissions, schedule with declared deps. (~120 LOC)
3. Parent-child verdict propagation: on parent's `pending_children`, hold proof-packet rendering until all children resolve; aggregate worst-case verdict. (~150 LOC)
4. `--tier {auto, solo, lead, modular}` flag wired through `cli_run.py` + `RunPayload`. (~80 LOC)
5. Tests: Lead emits subtasks; parent waits; verdict aggregation. (~100 LOC)

Deliverable: medium-complex tasks decompose into focused subtasks. Big tasks ("build a browser") become natively expressible.

### Phase 4 — Cross-task merge + project audit (1 week, ~600 LOC)

**Goal:** multi-task projects merge coherently.

1. `otto/queue/merge_coordinator.py`: extends watcher with single-writer cross-task merge to `main`. (~250 LOC)
2. Project audit on `main` (post-merge): existing `audit.py` repointed; cumulative `behavior_journeys.md` accumulated across tasks. (~120 LOC)
3. Auto-revert + fix-task emission. Cap at 3 fix-tasks per regression. (~150 LOC)
4. Resume Lead for conflict resolution via SDK `resume: session_id`. (~80 LOC)
5. Tests: 2 concurrent tasks merging; conflict resolution; regression auto-revert + fix-task. (~150 LOC)

Deliverable: project's `main` advances coherently across multiple tasks. Regressions self-heal up to retry budget.

### Phase 5 — Discovery + modular tier (1 week, ~400 LOC)

**Goal:** big-project lever.

1. `otto/discovery.py`: read-only discovery agent (its own AgentDefinition with no Write/Edit tools). Emits `ARCHITECTURE.md` + `manifest.json`. (~200 LOC)
2. `otto/manifest_check.py`: glob-containment validator for the manifest's owned_paths vs shared_contracts. (~100 LOC)
3. Lead heuristic in `decomp_select.py`: when intent enumerates ≥4 surfaces, dispatch discovery before subtask emission. (~50 LOC of changes)
4. Tests: discovery on a multi-surface intent; manifest_check rejects the finance-true-web overlap pattern. (~100 LOC)

Deliverable: large projects (multi-surface) decompose via discovery → modular subtasks. Browser-engine class becomes architecturally expressible (not yet realistic at depth 3+).

### Phase 6 — Provider fallback + observability + cleanup (0.5–1 week, ~300 LOC)

**Goal:** ship.

1. Provider fallback wrapper (catch provider-exhausted, re-dispatch via fallback). (~80 LOC)
2. `cost_attempts[]` schema in summary.json. (~40 LOC)
3. MC dashboard updates: parent-child task graph view + verdict pills + provider attempt display. (~150 LOC TS/CSS)
4. Documentation: tier guide, verdict reference, supervised-mode doc, v6 punch list.
5. Bench: 5 reference projects (finance-dashboard, microblog, ops-dashboard, acme-expense, brownfield SAML).
6. Move deprecated code to `otto/legacy/`.

Deliverable: v5 ships.

**Total: 4-5 weeks, ~2800 LOC.** ~600 LOC less than plan-v3 because we use SDK natives for MCP, agents, hooks, budget, lifecycle events.

---

## 4. Open questions (with the verified vs unverified split)

### Verified (no further action)

- Subagent text + cost propagate to parent ✓
- `agent_id` populated in subagent hook calls ✓
- In-process MCP reachable from depth-1 subagents ✓
- PreToolUse deny hook intercepts ✓
- Subagent lifecycle visible via `TaskStartedMessage` family ✓

### Unverified — Phase 1 smoke tests

These each get a 30-line smoke test in Phase 1's CI suite:

1. **Depth-2 dispatch**: subagent dispatches another subagent via Task — does in-process MCP still reach? Test 1 was inconclusive; isolate.
2. **`PermissionResultDeny(interrupt=True)`** vs `interrupt=False` — does the agent halt or continue with denial? Affects whether locks abort the task or just deny tools.
3. **`hookSpecificOutput.permissionDecision`** field shape — eliminate the `NoneType.items()` SDK error from test 2.
4. **Subagent failure mid-execution**: subagent raises or hits max_turns — does cost still accumulate? Does TaskNotificationMessage carry an error status?
5. **`AgentDefinition` per-agent `mcpServers`** with non-overlapping sets — verify a subagent can't reach an MCP server it didn't declare.

These don't block Phase 1 implementation; they're written alongside the foundation code.

### Genuinely deferred to v6

- Browser-engine scale (millions of LOC, depth-3+ recursion).
- Mid-run intent amendment.
- Multi-user concurrent submitters on same project.
- Cumulative project INTENT.md.

---

## 5. Risks (from v3, refreshed)

### 5.1 Lead context overflow on medium tasks

Same mitigation: `submit_subtask` available from Phase 3; Lead heuristic detects context pressure.

### 5.2 SDK changes break our contract

The smoke test suite (Phase 1) is the canary. Re-runs in CI on every SDK upgrade. Failures stop a release.

### 5.3 Subagent text return is autonomous (workers may rephrase or summarize)

Verified empirically. Mitigation: don't rely on subagent free-form text for verdict signaling. Use `TaskNotificationMessage.status` + `cost_usd` + cumulative ResultMessage.

### 5.4 In-process locks lose state on session restart

Resume of a Lead session reconstructs locks from the agent's prior tool calls (replayed from messages.jsonl). Only locks the agent declared via `lock_paths` need to be reconstructed; SDK-native session resume covers the rest.

### 5.5 Hook return shape SDK quirks

Use `SyncHookJSONOutput` exactly as documented. Don't return ad-hoc dicts. Phase 1 smoke test 3 above pins this.

---

## 6. Definition of done for v5

(Same as v3 §8, with "uses SDK natives" added as a quality bar.)

1. Lead is the build primitive. `spec_compile.py` group synthesis is in `otto/legacy/`.
2. Lead can: do work inline, spawn in-session subagents (`Task` tool with `agents` dict), emit child tasks (`mcp__otto__submit_subtask`).
3. Build agent ≠ test agent (separate `AgentDefinition`s); `behavior_journeys.md` is read-only ground truth across both.
4. Locks via PreToolUse hook + in-process Python state (no sqlite).
5. `--tier {auto, solo, lead, modular}` in CLI and MC.
6. Verdict vocabulary `pass | partial | pending_children | unverified | merge_blocked | catastrophic` in summary.json and MC.
7. Cross-task merge to `main` operational; project audit post-merge with auto-revert + fix-task emission.
8. Provider fallback (codex → claude task-level re-dispatch) verified on a real instrumented run.
9. All Phase 1 smoke tests in CI. Pass on SDK 0.1.50.
10. Bench results on 5 reference projects: finance-dashboard PASSES (real user-visible behavior); cost ≤ 1.5× today's on passing projects; wall time ≤ 1.3× today's.
11. Documentation: tier guide, verdict reference, v6 punch list.

---

## 7. The one-paragraph version

Otto v5 is a 4-5 week, ~2800 LOC delta against today, using SDK 0.1.50 natives throughout. The Lead is the universal build primitive (one `query()` per task with `agents` dict for build/test subagents). Otto's custom tools live in an in-process MCP server (`create_sdk_mcp_server`) — `submit_subtask`, `lock_paths`, `verify`, `checkpoint`. Locks are an in-process Python dict checked by a PreToolUse hook that reads `agent_id` from `_SubagentContextMixin`. Build/test agents are `AgentDefinition`s with their own tool ACLs and shared MCP server reference. Frozen `behavior_journeys.md` is the single ground truth across build, test, and audit. Cross-task merge is coordinated by an extended watcher with single-writer fcntl on `main`. Provider fallback is task-level re-dispatch. Best-effort everywhere; advance always; supervised mode is rare opt-in. v5 explicitly does NOT deliver browser-engine scale, mid-run intent amendment, or multi-user concurrent submission — those are v6.
