# Otto v5 — Implementation Plan, Draft 3 (the actual architecture)

Draft 2 was a retreat. This draft commits to the architecture we agreed on across the conversation: Lead-driven dynamic decomposition, child-task emission, real write-time locks, build/test agent split, autonomy by default, best-effort everywhere. The adversarial reviewer's correctness fixes are folded in. The implementation reviewer's wire-protocol gaps are answered, not avoided. Honest scope: 4–6 weeks.

---

## 0. North star

Otto converts intent into a stream of well-described product states. The user reads the stream when convenient, intervenes when they want, is never required to.

Three commitments:

1. **Autonomy is the default.** Supervised mode is a rare opt-in escape hatch for first runs / high-stakes work / debugging.
2. **Best-effort everywhere; advance always.** Every loop bounds retries, terminates in an honest verdict, and lets work continue. Hard blocks ONLY on (a) catastrophic infra failure (provider auth invalid, disk full) or (b) explicit user opt-in via supervised checkpoints.
3. **The agent decides decomposition; Otto provides the rails.** Otto is workspace + tools + verifier + queue + merge coordinator. Otto does NOT predict groups/owned_paths/contracts from intent text.

Anti-pattern from today's Otto, explicitly removed: spec_compile inventing a frozen multi-group decomposition before any code exists. We tested this — the finance-true-web run wasted 37 minutes on contradictions baked in by the spec compiler.

---

## 1. Architecture

### 1.1 Three persistent units, one transient unit

| Unit | Persistence | Definition |
|---|---|---|
| Project | Forever | Lives at `~/otto-projects/<name>`. Has a `main` branch. Accumulates code, history, accepted intent. |
| Queue | Forever, per-project | Tasks live here. Includes user-submitted tasks AND agent-emitted child tasks. |
| Project state | Forever, per-project | Honest health summary derived from recent verdicts + open task graph. Read-only surface, not a state machine with hard transitions. |
| **Task** | **Transient** | One run. PR-sized. Has worktree, Lead, verdict, proof packet. Merges to `main` when done; worktree retained for inspection but session is closed. |

A "task" is the unit of *change*. A "project" is the unit of *state*. A Lead operates at task scope, never at project scope. Cross-task coordination is Otto's queue + merge layer.

### 1.2 The Lead — the universal build primitive

Every task gets a Lead. There is one Lead implementation. Tier presets are knob bundles, not separate codepaths.

The Lead's tools:

| Tool | Origin | Purpose |
|---|---|---|
| Read, Write, Edit, Bash, Glob, Grep, TodoWrite | Claude Code defaults | Standard agent surface. |
| Agent / TeamCreate | Claude Code defaults | In-session subagents for tightly-coupled parallel work. |
| `otto.lock_paths(agent_id, globs[])` | New (subprocess CLI shim) | Register write-time locks for self / in-session subagents. |
| `otto.submit_subtask(intent, depends_on=[...], group_id=None)` | New (subprocess CLI shim) | Emit a new child task to the project's queue. Returns task_id immediately. |
| `otto.verify(claim_set)` | Wraps existing audit machinery | Run deterministic verification against the running product. Returns structured PASS/FAIL with evidence paths. |
| `otto.checkpoint(reason)` | New | Persist state for resumability. NON-BLOCKING in autopilot, regardless of mode. (Closes adversarial #9.) |
| `otto.request_review(reason, timeout_s=600)` | New, supervised-mode-only | Blocking request for user approval. Used only when `--mode supervised`. Aborts to `unverified` on timeout. |

The Lead's three decomposition choices, decided per-task by the Lead:

| Mode | When | Mechanism |
|---|---|---|
| **Inline** | Small task; tightly-coupled work; Lead can hold it all in context. | Lead does the work itself. No fan-out. |
| **In-session subagents** | Tightly-coupled parallel work where context-sharing matters. | Lead calls `Agent` / `TeamCreate`. Subagents share Lead's context. Their results return to Lead as text. |
| **Emit child tasks** | Genuinely independent work; child shouldn't share context. | Lead calls `otto.submit_subtask`. Each child gets its own worktree, own Lead, own PR, own verdict. |

The choice is the Lead's. Otto provides a heuristic in `lead.md` (see §1.4) but doesn't enforce. The plan deliberately exposes the same heuristic via `--tier` for users who want to override.

### 1.3 Build agent ≠ test agent (frozen behavior journeys)

Within each task, Otto runs THREE sessions:

1. **Spec compile** (existing `compile-spec.md`, repurposed). Produces a flat spec: features + behavior_journeys + (optionally) module manifest if Lead requested discovery. NO groups, NO owned_paths, NO shared_contracts unless emitted by a discovery pass. Behavior journeys are written in user-language, not implementation-language. A lint check rejects journeys containing `class=`, `id=`, `data-testid`, `getByRole(...)` syntax.

2. **Lead session** — calls build agent + uses tools. Build agent prompt explicitly forbids touching `tests/`, `*.test.*`, `*.spec.*`, browser journey files. May spawn helpers via in-session subagents or emit child tasks.

3. **Test agent session** — separate Claude session, runs after Lead commits. Reads `behavior_journeys.md` (FROZEN, read-only), observes the running product, writes selectors that find DOM elements satisfying journey steps. Test agent is forbidden from modifying journeys or app code.

`behavior_journeys.md` is the SINGLE GROUND TRUTH for what the product must do. Build agent reads it, builds against it. Test agent reads it, writes selectors against it. Audit reads it, runs the journeys against the running product.

This is the correctness move from adversarial #4. The test agent observing the running product cannot rewrite journeys to match what the build agent emitted (the tautology). If build emits `<a>Foo</a>` when journey says "click the Save button," the test agent's selector for "Save button" fails to find anything → test fails → audit fails. Correctly.

### 1.4 Lead heuristic for decomposition mode

Embedded in `lead.md` and enforced by simple pre-flight in `decomp_select.py`:

```
1. If intent + project state fits in <30K tokens:
     → INLINE (no fan-out, no subagents)
2. Else if intent enumerates ≥2 surfaces with disjoint owned_paths:
     → EMIT CHILD TASKS (one per surface, each independent)
3. Else if intent describes coupled parallel work (e.g., refactor multiple related files):
     → IN-SESSION SUBAGENTS (Agent/TeamCreate)
4. Else default to INLINE.
```

`--tier` overrides the heuristic:

| Flag | Effect |
|---|---|
| `--tier auto` (default) | Lead heuristic above. |
| `--tier solo` | INLINE forced. Agent/TeamCreate stripped from tools. `submit_subtask` blocked. Single agent, no fan-out. |
| `--tier lead` | Lead may use Agent/TeamCreate or submit_subtask. Discovery NOT triggered. (Default for normal-sized projects per user requirement: "don't lazy-default small projects to single agent.") |
| `--tier modular` | Lead REQUIRED to call discovery agent FIRST (writes ARCHITECTURE.md + manifest), then subtasks per module. |

Default for empty repo is `--tier auto`, which routes to `lead`. Trivial intents (<200 chars, no list markers) route to `solo`. Multi-surface intents (≥4 enumerated surfaces) route to `modular`.

### 1.5 Locks (write-time, PID-liveness)

`otto/locks.py` (new). Sqlite-backed per-worktree (file: `<worktree>/.otto/locks.sqlite`).

Schema:

```sql
CREATE TABLE locks (
  agent_id TEXT,           -- e.g., "lead", "lead.subagent.1", "task.<id>"
  glob TEXT,               -- e.g., "src/features/transactions/**"
  pid INTEGER,             -- process holding the lock
  parent_agent_id TEXT,    -- for hierarchy lookups
  task_id TEXT,            -- which task owns this
  acquired_at TEXT,        -- iso8601
  last_heartbeat_at TEXT   -- iso8601
);
CREATE INDEX idx_glob_lookup ON locks(glob);
CREATE INDEX idx_agent ON locks(agent_id);
```

Lock acquisition: `acquire(agent_id, globs, pid)`. Inserts a row per glob. Returns a handle.

Lock check: `check_write(agent_id, path)`. Walks all rows where `path` matches `glob` (Python-side fnmatch over candidates). If any row's agent_id is in the calling agent's parent chain → allow. Else deny.

Lock release: `release(handle)`. Deletes rows.

PID-liveness: a separate `otto/queue/lock_watcher.py` daemon polls every 30s. For each lock row, if PID is dead, delete the row. **No TTL.** Liveness is process-level, not tool-call-level. Closes adversarial #6.

SDK can_use_tool composition: today's `_otto_can_use_tool_safety` (otto/agent.py:262) only matches Bash. We compose into `_otto_can_use_tool_chain(agent_id)` which:

1. Calls `lock_check(agent_id, tool_name, tool_input)` — denies Write/Edit/Bash-with-redirect to paths outside the agent's locks.
2. Calls existing `_otto_can_use_tool_safety` for Bash safety.
3. Returns allow if both pass.

`make_agent_options(agent_type, agent_id=None, ...)` binds `agent_id` into the chain at construction time. Each subagent invocation gets its OWN options object with its OWN agent_id closed over. The SDK callback signature stays the same; the agent_id is captured in the closure. Closes implementation reviewer #1.3.

### 1.6 Subtask emission via Bash shim (no MCP server)

`otto.submit_subtask` is exposed as a CLI subcommand: `otto __subtask submit --intent "..." --depends-on <task_id> [--group-id <id>]`. Returns `{task_id, status: "queued"}` to stdout.

The Lead invokes this via the standard Bash tool. Otto's CLI subcommand dispatches to `otto/queue/subtask.py` which calls `enqueue_task` with the relevant fields.

This sidesteps the in-process MCP limitation (per `feedback_inprocess_mcp.md`). External MCP subprocess would also work but adds complexity. The Bash shim is the simplest reliable mechanism.

`otto/queue/enqueue.py` is extended with an idempotency check: `(parent_task_id, intent_hash)` is the key. Resuming a Lead that already submitted a subtask doesn't double-submit. Closes adversarial #5 partially.

### 1.7 Cross-task merge coordinator (the daemon question)

Today's `merge_queue.py` operates per-session. We extend the existing `otto/queue/runtime.py` watcher process to also own cross-task merging.

The watcher (already a long-running process per project) gains a `MergeCoordinator` component that:

1. Subscribes to task-completion events (already in `spec_state.jsonl`).
2. On task completion with verdict `pass | partial | unverified`:
   - Acquire fcntl lock on `main`.
   - `git rebase task-branch onto main`.
   - Clean rebase → land. Update main. Trigger project audit.
   - Conflict → resume task's Lead with conflict (uses existing checkpoint/resume machinery).
   - 3 conflict-resolution attempts exhausted → fresh "integration Lead" with full context.
   - Integration Lead also exhausted → mark task `merge_blocked`, emit notification, drop from merge queue, continue with other tasks.
   - Release fcntl lock.

This addresses implementation reviewer #1.6: the daemon is the existing watcher, extended. No new daemon process.

Project audit on main:

1. After every successful merge, the watcher dispatches `otto/audit.py` against the new main, using the project's accumulated `behavior_journeys.md` (the union of all merged tasks' journeys).
2. PASS → done.
3. FAIL → auto-revert the offending merge (last successful merge), file a fix task with the audit findings as input. Continue accepting new tasks.
4. Fix task fails → file another fix task with cumulative context. Cap at 3 cumulative attempts.
5. After 3 cumulative fix-task failures: surface notification "regression unfixable" in MC. Project remains operational. NO state machine, NO `quarantined` state, NO new task pause. (Closes adversarial #14.)

The merge coordinator is a single-writer to `main` (fcntl-serialized). Concurrent task completions queue up at the lock; FIFO. Closes implementation reviewer #1.6.

### 1.8 Parent-child verdict propagation

Adversarial #5: parent task can `pass` while emitted children are still building, breaking proof packet honesty.

Resolution: when a Lead emits subtasks via `submit_subtask`, the parent task's status moves to `partial+pending_children`. Parent's proof packet is rendered when children complete:

- All children `pass` → parent verdict promotes to `pass` (or stays `partial` if Lead itself returned partial).
- Any child `partial` or `unverified` → parent verdict at least `partial`.
- Any child `catastrophic` → parent verdict `partial` with notation.
- `merge_blocked` children don't block parent's proof rendering; they're listed in the proof packet's "deferred" section.

Parent task's worktree is held until children resolve. The watcher tracks the parent-child graph in `task_graph.json` per project. Children inherit a budget slice from the parent's remaining budget at emission time.

### 1.9 Discovery agent (used at Lead's discretion or `--tier modular`)

Lead may invoke discovery for projects where up-front architecture thinking helps. Discovery is:

- A separate Claude session with read-only tool access (no Write/Edit/Bash mutations).
- Reads intent + existing code (if any) + Lead's pre-thinking.
- Produces `ARCHITECTURE.md` (committed to project worktree on success) + `manifest.json` (lists modules with owned_paths + shared interfaces).
- Output runs through `manifest_check.py` (mechanical glob containment, see §1.10).
- On manifest_check rejection, discovery re-runs with structured failure feedback. Cap 2 retries; failure → discovery aborts, Lead falls back to inline mode.

Discovery is OPTIONAL. The Lead heuristic in §1.4 decides; user's `--tier modular` forces it.

### 1.10 Manifest_check (glob containment)

`otto/manifest_check.py` (new). Validates:

For every `module[A]` in manifest, for every `module[B != A]` in manifest, for every `path_glob` in `B.owned_paths`:
- If `path_glob` overlaps any `A.shared_contracts.paths` → reject with structured error UNLESS `path_glob` is in `A.shared_contracts.allowed_extension_paths`.

Glob overlap: uses pathspec library (already a dep). Realistic LOC: ~150 including edge cases.

Same validator runs in M1 against today's spec.json (see §3) — catching the finance-true-web class as a stop-the-bleeding measure if user wants it. (User said full design only; we'll wire this later but it's a small flap.)

### 1.11 Verdict vocabulary

Per-task verdicts (adversarial #14 fixes: dropped silent-degradation states):

| Verdict | Meaning |
|---|---|
| `pass` | All checks green. Merged to main. |
| `partial` | Built and merged; some declared features did not pass within retry budget. Honest list in proof packet. May coexist with `pending_children`. |
| `pending_children` | Parent task whose verdict awaits emitted child tasks. Status, not terminal verdict. |
| `unverified` | Built and committed; verifier itself failed/timed-out. Code unverified. |
| `merge_blocked` | Built fine, can't integrate after retry exhaust. Worktree preserved. Surface in MC. |
| `catastrophic` | Infrastructure failure (provider auth/credits, disk, etc.). |

Project health is a derived view (in MC dashboard), NOT a state machine:

- "Recent verdicts": last N task verdicts.
- "Open issues": count of `merge_blocked` + active `regression_unfixable_count`.
- "In-flight": count of running tasks.

No project state transitions. No quarantine. No auto-pause. The user reads the dashboard; they decide.

### 1.12 What's deleted from today's Otto

This is the real deletion list. All of it.

- `otto/spec_compile.py` group/contract synthesis (lines ~1280–1418 + the validators that follow). Group decomposition is dead. Spec is flat: features + behavior_journeys.
- `otto/spec_compile.py::_normalize_critical_shared_contract_scope` and friends.
- `otto/build.py` group orchestration (the `run_build` loop that fans out groups). Replaced by `lead.py`.
- `otto/build.py::detect_critical_shared_contract_violations` and other post-hoc scope detection. Replaced by write-time locks.
- `otto/repair_gates.py` — group-level repair gating. Replaced by Lead's own retry budget + best-effort verdict.
- Prompts: `compile-spec-brownfield*.md` (today's brownfield path replaced by "Lead reads existing code first" knob), `build-merge-repair.md` (replaced by Lead conflict resolution), `compile-spec-structured-output.md` (Lead doesn't need structured output of groups).

Deletion cascade: `tests/test_build.py` lines 33, 625, 641, 671 reference deleted symbols. Plus more in test_audit.py, test_merge_queue.py. These tests are deleted (not rewritten — they test the wrong architecture).

What stays:

- `otto/audit.py` (verifier; reads frozen journeys).
- `otto/checkpoint.py`, `otto/resume.py` (extended for parent-child task graphs).
- `otto/branching.py`, `otto/worktree.py`.
- `otto/queue/runtime.py` (extended with MergeCoordinator).
- `otto/queue/enqueue.py` (extended with idempotency keys).
- `otto/cli_run.py`, `otto/cli_build.py` (mostly intact; new flags).
- `otto/web/` (mostly intact; new fields).
- `otto/render.py` (extended for new verdict pills + parent-child task display).
- `otto/observability.py`, `otto/budget.py`, `otto/paths.py`.

---

## 2. Wire-protocol decisions (the implementation reviewer's gaps, answered)

### 2.1 Runner integration point

`otto/runner.py::run_pipeline` is rewritten. The new pipeline is:

```python
async def run_pipeline(project_dir, session_dir, intent, config, ...):
    # Phase 1: Compile flat spec
    if config["tier"] != "solo":
        spec = await compile_flat_spec(intent, project_dir, session_dir, config)
        # spec has: features, behavior_journeys, (optionally) seed manifest
        write_frozen_journeys(session_dir, spec.behavior_journeys)
    else:
        spec = None  # solo skips compile

    # Phase 2: Lead session
    lead_result = await run_lead(
        intent=intent,
        spec=spec,
        project_dir=project_dir,
        session_dir=session_dir,
        config=config,
    )
    # lead_result has: verdict (per-task, but pending_children if subtasks emitted),
    #                  cost, duration, agent_session_id, child_task_ids

    # If pending_children: register parent-child graph in task_graph.json,
    # exit; final verdict resolves when children complete.
    if lead_result.has_pending_children:
        return RunResult(verdict="pending_children", ...)

    # Phase 3: Test agent (if not solo)
    if config["tier"] != "solo" and not lead_result.is_pure_research:
        test_result = await run_test_agent(
            spec=spec,
            project_dir=project_dir,
            session_dir=session_dir,
            config=config,
        )

    # Phase 4: Per-task audit
    audit_result = await run_audit(
        spec=spec,
        project_dir=project_dir,
        session_dir=session_dir,
        config=config,
    )

    # Phase 5: Render proof packet
    proof = await render_proof_packet(
        lead_result, test_result, audit_result,
        session_dir=session_dir,
    )

    return RunResult(verdict=audit_result.verdict, ...)
```

Cross-task merge happens OUTSIDE this function, in the watcher (§1.7). Per-task `RunResult` includes verdict; watcher consumes the result and triggers cross-task merge.

Today's `_phase("seed")`, `_phase("merge")`, `_phase("repair")` are gone. Lead handles its own internal merges (in-session subagents return to Lead; their results are Lead's). Repair is replaced by best-effort retry within Lead + audit-driven fix-tasks.

### 2.2 Test agent runner

`otto/test_agent.py` (new):

```python
async def run_test_agent(spec, project_dir, session_dir, config) -> TestAgentResult:
    """Run after Lead commits. Read frozen journeys, observe running product, write tests."""
    options = make_agent_options(
        agent_type="test_agent",
        agent_id="test_agent",
        config=config,
    )
    options.system_prompt_path = "otto/prompts/test-agent.md"
    options.permission_mode = "default"  # not bypassPermissions; locked-down
    
    # Locks: test agent may write only to tests/**. Forbidden from app code.
    locks.acquire(agent_id="test_agent", globs=["tests/**"], pid=os.getpid())
    
    prompt = render_test_agent_prompt(spec, project_dir, session_dir)
    result = await run_agent_with_timeout(prompt, options, ...)
    
    locks.release_all(agent_id="test_agent")
    return TestAgentResult(...)
```

### 2.3 Lead implementation

`otto/lead.py` (new, ~400 LOC):

```python
async def run_lead(intent, spec, project_dir, session_dir, config) -> LeadResult:
    options = make_agent_options(
        agent_type="lead",
        agent_id="lead",
        config=config,
    )
    options.system_prompt_path = "otto/prompts/lead.md"
    
    # Lead has full toolset including subprocess access for otto.lock_paths and otto.submit_subtask
    locks.acquire(agent_id="lead", globs=["**"], pid=os.getpid())  # wide; lead delegates
    
    prompt = render_lead_prompt(intent, spec, project_dir, session_dir, config)
    result = await run_agent_with_timeout(
        prompt, options,
        on_subagent_dispatch=track_subagent_lock_inheritance,
    )
    
    # Read what Lead emitted
    lead_result = LeadResult(
        verdict=parse_lead_verdict(result),
        cost=result.cost,
        agent_session_id=result.session_id,
        emitted_subtask_ids=read_subtask_emissions(session_dir),
    )
    
    locks.release_all(agent_id="lead")
    return lead_result
```

When Lead spawns in-session subagents via Agent/TeamCreate, Otto intercepts via the `can_use_tool` chain to register a sub-lock for that subagent. The agent_id format is `lead.subagent.<n>` for in-session, `lead.task.<task_id>` for emitted children.

### 2.4 Subagent budget slicing

Closes adversarial #1 (#7.1 deferred). At Agent/TeamCreate dispatch time, Lead must pass `budget_usd_slice`. Otto's wrapper checks `remaining_budget >= budget_usd_slice * 1.2` (20% buffer). If insufficient, the Agent call is denied with a structured error: "Insufficient budget for subagent dispatch."

This is enforced inside `_otto_can_use_tool_chain` for the `Agent` and `TeamCreate` tools.

### 2.5 Manifest format

For now, manifest is the discovery agent's output. Schema:

```json
{
  "schema_version": 1,
  "modules": [
    {
      "id": "transactions",
      "owned_paths": ["src/features/transactions/**", "tests/features/transactions/**"],
      "depends_on": ["foundation"],
      "shared_contracts": [
        {
          "id": "store-interface",
          "paths": ["src/lib/store.ts"],
          "owner_id": "foundation",
          "allowed_extension_paths": ["src/lib/store.test.ts"]
        }
      ]
    }
  ],
  "shared_interfaces": ["src/types/**", "src/lib/store.ts"]
}
```

Discovery emits this; manifest_check validates it; Lead reads it and dispatches subtasks per module.

When Lead doesn't request discovery (default `--tier auto/solo/lead`), there's no manifest. Lock authority comes from runtime declarations.

### 2.6 Resume branching

Existing checkpoints have `command`, `phase`, `agent_session_id`. Schema migration:

- New: `tier`, `parent_task_id`, `pending_children_ids`, `cost_attempts`, `provider_history`.
- Old fields without new ones: resume code defaults `tier="lead"`, `cost_attempts=[]`, etc.
- Pre-v5 sessions resumed POST-v5: `phase` field tells us we're in the old multi-phase pipeline; resume falls through to a "legacy compatibility" path that runs the old multi-phase chain (preserved as `otto/legacy/run_pipeline_v4.py`). Old code is preserved-not-deleted for one release. Closes adversarial #11.

### 2.7 Provider fallback (task-level re-dispatch)

Adversarial #8: mid-session migration impossible. Implementation:

1. On provider-exhausted/auth-failed during a task: catch in `lead.py` or `test_agent.py` exception handler.
2. Mark task verdict `catastrophic` with `failure_reason="provider_exhausted"`, save partial work to worktree.
3. `otto/queue/runtime.py` watcher detects this verdict + reason. Checks `otto.yaml` for `fallback_provider` and `fallback_on` rules.
4. Re-enqueues task with fallback provider, sets `recovered_from=<original_task_id>`, includes original committed code as a baseline.
5. Cost accounting: `cost_attempts: [{"provider": "codex", "cost_usd": 0.13, "outcome": "exhausted", "duration_s": 240}, {"provider": "claude", "cost_usd": 2.10, "outcome": "pass", "duration_s": 600}]`. Total is sum across attempts.

If fallback also fails: terminal `catastrophic`. No infinite loop.

---

## 3. Implementation phases

Honest scope: 4-6 weeks. Five phases. Each phase is a coherent shippable increment.

### Phase 1 — Foundation (1 week, ~600 LOC)

The infrastructure that makes everything else possible.

- `otto/locks.py`: sqlite + PID-liveness + can_use_tool composition. (~250 LOC)
- `otto/queue/lock_watcher.py`: PID-liveness sweeper subprocess. (~80 LOC)
- `otto/queue/subtask.py`: CLI subcommand `otto __subtask submit` + `enqueue_task` with idempotency. (~120 LOC)
- `otto/queue/task_graph.py`: parent-child graph storage + verdict propagation logic. (~150 LOC)
- Tests: lock acquire/check/release; PID-death cleanup; subtask idempotency; parent-child propagation. (~200 LOC)

Outcome: rails are in place. Nothing user-visible changes yet.

### Phase 2 — Lead + flat spec + build/test split (1.5 weeks, ~800 LOC)

The Lead becomes the build primitive.

- `otto/spec_compile_flat.py`: extracted from spec_compile.py, drops group synthesis. (~300 LOC of refactor + new code)
- `otto/lead.py`: Lead runner. (~400 LOC)
- `otto/test_agent.py`: test agent runner. (~150 LOC)
- New prompts: `lead.md`, `test-agent.md`, `compile-spec-flat.md` with journey lint. (~300 LOC of content; ~20 LOC of integration)
- `otto/runner.py`: rewrite `run_pipeline` (Phase 2 of §2.1). (~250 LOC of changes; 400 LOC of deletes)
- Verdict vocabulary expansion (`AuditVerdict` enum). (~30 LOC)
- Tests: integration test for build/test split with finance-dashboard fixture. (~200 LOC)

Outcome: a single task runs through Lead → test agent → audit → render. NO in-session subagents yet (Lead does inline only). NO subtask emission yet. Single-task verdict works end-to-end.

### Phase 3 — In-session subagents + child tasks (1 week, ~500 LOC)

Lead can decompose.

- `otto/decomp_select.py`: Lead heuristic + tier-flag mapping. (~100 LOC)
- Lead prompt update: subagent dispatch + subtask emission. (~50 LOC of content)
- can_use_tool chain extended for Agent/TeamCreate budget pre-check. (~80 LOC)
- Subagent lock inheritance via tool wrapper. (~120 LOC)
- `--tier {auto, solo, lead, modular}` flag wiring through cli_run.py + RunPayload. (~80 LOC)
- Tests: Lead emits subtasks; parent-child verdict propagation; budget slicing denial. (~200 LOC)

Outcome: Lead decomposes per task. Child tasks queue and run. Cost accumulates correctly across parent-child graph.

### Phase 4 — Cross-task merge + project audit (1 week, ~600 LOC)

Tasks merge to project main coherently.

- `otto/queue/merge_coordinator.py`: extends watcher with single-writer cross-task merge. (~250 LOC)
- Project audit on main (post-merge): existing `audit.py` repointed to "project main as input"; uses cumulative `behavior_journeys.md`. (~100 LOC)
- Auto-revert + fix-task emission. (~150 LOC)
- Resume Lead for conflict resolution (uses existing checkpoint/resume; new entry path). (~80 LOC)
- Integration Lead fallback. (~100 LOC)
- Tests: 2 concurrent tasks merging; conflict resolution; regression auto-revert + fix-task. (~200 LOC)

Outcome: multi-task projects work coherently. Per-merge audit catches regressions.

### Phase 5 — Discovery + modular tier (1 week, ~500 LOC)

The big-project lever.

- `otto/discovery.py`: read-only discovery agent with manifest emission. (~200 LOC)
- `otto/manifest_check.py`: glob-containment validator. (~150 LOC)
- Lead prompt: discovery dispatch path. (~30 LOC of content)
- `--tier modular` codepath: forces discovery → manifest → child tasks per module. (~80 LOC)
- Tests: discovery on a multi-surface intent; manifest_check on overlap (the finance-true-web pattern). (~150 LOC)

Outcome: large projects (multi-surface, ≥4 distinct subsystems) decompose via discovery → modular subtasks. Browser-engine-class becomes possible (not yet realistic, but architecturally expressible).

### Phase 6 — Provider fallback + observability + cleanup (0.5–1 week, ~400 LOC)

Polish and ship.

- Provider fallback wrapper. (~80 LOC)
- Cost-attempts schema in summary.json. (~50 LOC)
- MC dashboard updates: parent-child task graph rendering + verdict pills + provider attempt display. (~200 LOC TS/CSS)
- Documentation: user-facing tier guide, verdict reference, supervised-mode doc.
- Bench: 5 reference projects (finance-dashboard, microblog, ops-dashboard, acme-expense, brownfield SAML add).
- Delete deprecated code (move to `otto/legacy/` for one release per §2.6).

Outcome: v5 ships.

---

## 4. Migration & deletion

### 4.1 Phased deletion

- Phase 2: deprecated code stays in place (gated by `--tier` runtime check). Old run paths still work.
- Phase 6: deprecated code moved to `otto/legacy/` (preserved for one release, used only by resume of pre-v5 checkpoints).
- v6: `otto/legacy/` removed. Resume of old checkpoints requires v5 client.

### 4.2 Test cascade

Old tests testing deleted machinery: deleted in Phase 2 alongside the code they test. New tests added per phase. Bench suite tests product-level behavior, not machinery internals.

### 4.3 MC compatibility

Phase 6 ships verdict pill updates + parent-child task graph display. Old verdicts (`pass | fail | blocked`) map to new vocabulary (`pass | partial | merge_blocked`). MC dashboard adds a "task graph" view next to existing task list.

---

## 5. Verdict vocabulary (final, with parent-child semantics)

```
pass               — all checks green; merged. Children all pass.
partial            — built and merged; some declared features missed within retry budget.
                     OR: parent task with at least one child verdict ≤ partial.
pending_children   — parent task whose children haven't all resolved. Not terminal.
unverified         — built and committed; verifier failed/timed-out.
merge_blocked      — couldn't integrate after retry exhaust. Worktree preserved.
catastrophic       — infrastructure failure (provider, disk, etc.).
```

Parent-child rules:
- Parent verdict = max(self_verdict, severity-max(child_verdicts)) where severity order: pass < partial < pending_children < unverified < merge_blocked < catastrophic.
- Parent's proof packet renders when all children resolve.
- Children's verdicts are listed in parent's proof packet.

Project-health view (NOT state machine):
- Recent verdicts (last 20).
- Open `merge_blocked` count.
- Open `regression_unfixable` count (from cumulative fix-task failures, see §1.7).
- In-flight tasks count.
- Total spend last 24h.

---

## 6. Open questions

Genuinely deferred; not blockers for any phase:

1. **Recursive subagent depth ≥ 3.** Phase 3 supports depth 1 (Lead's children may not themselves spawn). Depth 2 in Phase 5 (modular tier; module-Leads can spawn helpers). Depth 3+ unverified. Bench needed before claiming browser-engine support.
2. **Mid-run intent amendment.** v5 punts. User submits a follow-up task. v6 considers in-flight amendment.
3. **Multi-user concurrent submitters on same project.** Today's queue has fcntl on enqueue (we add this in Phase 1 if missing). Cross-task merge serializes via fcntl on main. Multi-MC concurrent should work; bench in Phase 6.
4. **Determinism.** Lead is non-deterministic. Verdict-level determinism: same intent, same model, same provider, same project state should produce same verdict distribution across N runs (target: 80% same verdict). Bench in Phase 6.
5. **Cumulative project intent (INTENT.md).** v5 doesn't auto-maintain. Each task's intent stands alone. v6 may add accumulation.
6. **Malware-reminder injection.** Build-test split doubles affected sessions. Mitigation: pin Lead and test-agent providers to Opus-4.6 by default (model in pbR exclusion set). User can opt out with `--allow-reminder-pollution`.

---

## 7. Risks & mitigations

### 7.1 Lead context overflow on medium tasks

Finance dashboard with 28 features may push 200K tokens.

**Mitigation:** Phase 3 ships `submit_subtask`. Lead heuristic in §1.4 detects high-feature-count intents and proactively emits child tasks. If Lead overflows mid-session despite heuristic, fall through to `unverified` verdict with whatever's committed. v6 may add automatic mid-session checkpoint-and-decompose.

### 7.2 Subagent runaway cost

Unbounded subagent fan-out burns budget.

**Mitigation:** budget pre-check in can_use_tool chain (Phase 3, §2.4). Refuses Agent/TeamCreate if remaining budget < requested slice * 1.2.

### 7.3 Test agent observing wrong product confirms wrong product

Adversarial #4. Closed by frozen behavior journeys (§1.3). Test agent writes selectors against journey, not against observed DOM-shape.

### 7.4 Lock cleanup under crash

Adversarial #6. Closed by PID-liveness watcher (§1.5). No TTL-based premature release.

### 7.5 Lead's decomposition heuristic mis-classifies

Lead picks wrong mode for a given task.

**Mitigation:** `--tier <explicit>` overrides. Telemetry on (intent, lead-chosen-mode, verdict) to tune heuristics over time.

### 7.6 Cross-task merge blocks under contention

Many concurrent tasks completing → merge_queue serializes → wall time bloats.

**Mitigation:** fcntl serialization is the right tradeoff for correctness over throughput. If real bench shows pathological wait times, Phase 6 may explore async merge with conflict-detection on commit.

### 7.7 v5 still doesn't deliver browser-engine

Honest. v5's modular tier is a foundation; depth-3 recursion + millions of LOC is genuinely v6+.

**Mitigation:** docs honestly state v5 scope. v6 plan informed by v5 telemetry on modular-tier behavior at <100 module count.

---

## 8. Definition of done for v5

1. Lead is the build primitive. spec_compile group synthesis is deleted (preserved in legacy/).
2. Lead can: do work inline, spawn in-session subagents (Agent/TeamCreate), emit child tasks (`submit_subtask`).
3. Build agent ≠ test agent; behavior journeys are frozen ground truth.
4. Locks are write-time, sqlite-backed, PID-liveness.
5. `--tier {auto, solo, lead, modular}` is in CLI and MC.
6. Verdict vocabulary `pass | partial | pending_children | unverified | merge_blocked | catastrophic` is in summary.json and MC.
7. Cross-task merge to main is operational; project audit on main runs post-merge; auto-revert on regression with fix-task emission.
8. Provider fallback (codex → claude task-level re-dispatch) works on a real instrumented codex-out-of-credits run.
9. Bench results on 5 reference projects:
   - finance-dashboard PASSES (real user-visible behavior; not just "doesn't fail like today").
   - microblog cost ≤ 1.5× today's.
   - ops-dashboard wall time ≤ 1.3× today's.
   - acme-expense regression-free (no `regression_unfixable` on baseline tasks).
   - brownfield SAML add: behavior preserved, new feature works.
10. Documentation: tier guide, verdict reference, supervised-mode doc, v6 punch list.

---

## 9. The one-paragraph version

Otto v5 is a 4-6 week, ~3500 LOC delta against today. It replaces today's spec_compile-driven multi-group decomposition with a Lead agent that decides decomposition per task at runtime — inline, in-session subagents, or child tasks emitted to the queue. Locks at write-time, not post-hoc. Build and test agents are split into separate Claude sessions reading frozen behavior journeys. Cross-task merge is coordinated by an extended watcher that runs project audit after each merge and emits auto-revert + fix-tasks on regression. Provider fallback is task-level re-dispatch, not mid-session migration. Tier is a knob preset (`auto | solo | lead | modular`), not a separate codepath. Best-effort everywhere; advance always; supervised mode is a rare opt-in. v5 explicitly does NOT deliver browser-engine scale, mid-run intent amendment, or full multi-user concurrent submission — those are v6.
