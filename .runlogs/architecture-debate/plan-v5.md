# Otto v5 — Hierarchical Lead Architecture

Final design draft. Anchored to one mental model (the company analogy) and grounded in
verified SDK and Otto code paths. Smoke tests cited where they exist; smoke tests
still needed are listed in §10.

---

## 0. North star

Otto turns intent into a working product, autonomously, with honest verdicts. The
product is built by a hierarchy of agents that mirrors how a company ships
software: strategic decomposition at the top, tactical decomposition closer to
the code, bottom-up integration with audit at every merge.

Three commitments:

1. **Autonomous by default.** Supervised mode is a rare opt-in.
2. **Best-effort everywhere.** Every loop bounds retries, terminates honestly,
   never hard-blocks except on catastrophic infra.
3. **Progressive decomposition.** No global plan up front. Each level plans for
   itself when it executes. The first level is treated specially — visible,
   reviewable — because strategic splits matter most.

---

## 1. Mental model — the company analogy

| Company | Otto |
|---|---|
| CEO writes vision | User submits intent |
| CEO+VP discussion: "what are the strategic areas?" | Root Lead's first action: emit ~3-7 VP-level child tasks (or `begin_inline` if atomic) |
| VP receives an area, breaks down within their org | Sub-Lead receives a semantic goal, recursively decomposes (or builds inline) |
| ICs ship PRs | Leaf Leads write code, run tests, commit |
| PRs merge bottom-up; reviewers verify "did this achieve the goal" | Each merge node runs an audit at its semantic level |
| Conflicts at the code level resolved by reviewer | Merge conflicts resolved at the parent node by its Lead |
| Quarterly review of the strategic split | First-level decomposition gets MC visibility and optional supervised review |

**Crucial property: contracts are semantic, not structural.** At every level, the
contract handed down is a sentence of user-visible behavior ("transactions can
be added, edited, deleted, filtered"), NOT a path or file ownership. How to
implement is the agent's choice. Conflicts at the code level are tolerated and
resolved at the right merge node, not pre-empted by ownership rules.

**Crucial property: planning is progressive.** The root plans only the first
split. Sub-Leads plan their own splits when they execute. No global plan.

---

## 2. The Lead primitive

There is exactly one primitive. It runs at every level of the hierarchy.

```python
async def run_lead(*, task_id: str, intent: str, project_dir: Path,
                   integration_branch: str | None) -> LeadResult:
    """
    Run a Lead session for one task.
    
    integration_branch:
      - None  -> root task; merges to project main when integration phase runs.
      - else  -> child task; merges to parent's integration branch.
    """
    server = create_otto_mcp_server(task_id, project_dir)
    options = ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code"},
        permission_mode="bypassPermissions",
        cwd=str(worktree_for(task_id)),
        mcp_servers={"otto": server},
        max_turns=200,
        max_budget_usd=task_budget,
        allowed_tools=[
            "Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite",
            "mcp__otto__submit_subtask",
            "mcp__otto__begin_inline",
            "mcp__otto__verify",
            "mcp__otto__checkpoint",
        ],
    )
    
    prompt = render_lead_prompt(
        intent=intent,
        project_state=read_project_state(project_dir),
        is_root=(integration_branch is None),
    )
    
    async for msg in query(prompt=prompt, options=options):
        record_message(msg, task_id)
    
    return collect_lead_result(task_id)
```

The prompt instructs the Lead:

```
You are a Lead. Your input:
  Intent: <semantic goal at YOUR level>
  Project state: <existing code in your worktree>

Step 1 — Decide.
  Read the intent. Is this ONE coherent unit of user-visible work, or MULTIPLE
  strategic areas?
  
  If ONE coherent unit (small, single feature, brownfield tweak):
    Call mcp__otto__begin_inline.
    Then build and test inline.
  
  If MULTIPLE strategic areas (large, multi-subsystem):
    For each area, call mcp__otto__submit_subtask with the area's
    SEMANTIC goal (a sentence of user-visible behavior).
    
    Examples of good semantic goals:
      - "Implement transaction CRUD (add/edit/delete/categorize)"
      - "Implement HTML and CSS parsers that produce a DOM-ready tree"
    
    Examples of bad goals (too implementation-heavy):
      - "Write src/transactions/Form.tsx and src/transactions/List.tsx"
      - "Own tests/browser/test_parser.py"
    
    Use 3-7 areas. Too few misses scope; too many fragments.

Step 2 — Execute.
  If you called begin_inline:
    Build and test inline. Use Read/Write/Edit/Bash freely. Run tests.
    Call mcp__otto__verify to run audit at your level. Iterate as needed.
    When satisfied, return your verdict.
  
  If you emitted subtasks:
    Your task is done at this stage. Return.
    Otto will spawn the children. When all return, Otto will spawn an integration
    Lead at this same task to merge children's branches and run integration audit.

Step 3 — Honesty.
  Return verdict honestly: pass, partial (some features missing), unverified
  (audit failed/timed out). NEVER claim pass without verify having run.
```

This is the entire primitive. Same code, same prompt template, every level.

### Why "begin_inline" is a marker tool

When the Lead chooses inline work, it must explicitly call `begin_inline` first.
This makes the decision observable — the system knows the Lead committed to
inline work at this level. Without this marker, an inline Lead and an
about-to-emit Lead look identical until one emits subtasks. The marker:

- Logs the decomposition decision to the task graph immediately.
- Lets Otto distinguish "Lead is still deciding" from "Lead is inline-building."
- Provides a hook for `--review-first-decomp` to know when to pause.

---

## 3. Bottom-up integration

When a Lead emits N subtasks, its OWN session ends. The Lead does not stay
alive while children run — that would burn context and require complex pause/
resume semantics. Instead:

```
Phase 1 (planning):    Lead session 1 runs, emits subtasks via submit_subtask, exits.
Phase 2 (execution):   Otto's queue spawns N child Leads (concurrent). Each runs
                       its own Phase 1 → 2 → 3 → 4. Each merges to parent's
                       integration branch when done.
Phase 3 (integration): When ALL children of this parent have merged or terminated,
                       Otto spawns a fresh "integration Lead" session for this
                       parent. The integration Lead reads:
                         - the parent's original intent
                         - the children's verdicts and proof summaries
                         - the integration branch (with all child work merged)
                       And does:
                         - mcp__otto__verify against parent's level journeys
                         - resolves any inter-child issues (test conflicts, etc.)
                         - returns parent's final verdict
Phase 4 (merge up):    Parent's integration branch merges to grandparent's
                       integration branch. Recurses.
                       Root's integration branch merges to main.
```

The integration Lead is the SAME Lead primitive, but invoked with a different
prompt template:

```
You are an integration Lead. Your task previously delegated to children. They
are done. Your job:

  1. Verify the children's combined work satisfies your task's intent.
  2. Resolve any cross-child issues you find (e.g., test conflicts, naming
     collisions, missing integration code).
  3. Run mcp__otto__verify to audit at your semantic level.
  4. Return your verdict: pass, partial, unverified.

Inputs:
  Original intent: ...
  Children's verdicts: [{id, intent, verdict, summary}, ...]
  Integration branch: <your branch with all children merged>
  Project state: <integrated code>
```

**Why a separate integration session, not session resumption?** Cleaner to
reason about and debug. Each Lead invocation is a discrete subprocess with a
discrete proof packet. If we used session resumption, parent's Phase 1 and
Phase 3 would share session_id but be separated by hours; Otto's checkpoint/
resume would have to model this. Separate sessions are simpler. The integration
Lead has all the inputs it needs from artifacts on disk; it doesn't need
context continuity with Phase 1.

---

## 4. The first-level review affordance

The root Lead's first action — its decomposition decision — is the most
strategically consequential. Otto treats it specially: visible, optionally
reviewable.

Three modes:

| Mode | What happens |
|---|---|
| `--mode autopilot` (default) | Root Lead emits subtasks via `submit_subtask`. Watcher logs them prominently in MC's "Decomposition" panel and dispatches them immediately. |
| `--review-first-decomp` (recommended for non-trivial intents) | Root Lead emits subtasks. Watcher holds dispatch in `pending_review` state. MC pops a modal: "Otto proposes these N tasks. Accept / Edit / Replace?". User accepts → dispatch. User edits → tasks updated, dispatch. User replaces → original tasks discarded, user-provided tasks dispatched. |
| `--tasks <file>` | Root Lead is skipped entirely. User-provided tasks become root's children directly. |

The pause is at the **watcher level**, not inside the Lead session. The root
Lead emits and exits cleanly; the watcher decides whether to dispatch
immediately or hold for review. This means **no SDK pause/resume mid-session
is required** — the SDK is well within its supported behavior.

`--review-first-decomp` only applies at the root level. Sub-Leads' decompositions
are autonomous (the user already approved the strategic split). This matches
the company analogy: VPs report a sprint plan but not every IC's PR plan.

---

## 5. Audit at every merge node

`mcp__otto__verify` runs the existing audit (`otto/audit.py`) but scoped to the
node's level. The verifier reads the running product (or built artifacts) and
returns structured pass/fail per behavior.

At each level, the audit checks behaviors APPROPRIATE TO THAT LEVEL:

- **Leaf node**: audit checks the behaviors implementing that leaf's goal.
  Example: "add transaction form accepts input, validates, persists."
- **Mid-level node**: audit checks integration of children's work.
  Example: "transaction added on dashboard appears in import-export listing."
- **Root**: audit checks whole-product user behaviors against the original intent.
  Example: "user can complete the full add-transaction-then-export-CSV flow."

The Lead at each level decides what to verify by reading the intent at that
level. There is NO frozen global behavior_journeys file inherited verbatim from
the root. Each Lead generates its own audit list when needed (or relies on the
verifier's intent-driven inference).

**Scope accountability comes from**: every level audits its own goal. If a leaf
silently cuts scope, its audit catches it. If a leaf passes but the integration
breaks, the parent's integration audit catches it. Multi-level audit is the
mechanism, not pre-frozen ownership.

**Self-attestation defense**: the Lead writes code AND audit-style tests. The
attack vector (build agent writes a brittle locator AND the test that uses it)
is closed by the verifier:
  - `mcp__otto__verify` runs the existing audit which independently re-derives
    behavior expectations from the intent.
  - Audit's expectations are not authored by the Lead in this task.
  - If Lead writes a test that passes but the audit's behavior check fails,
    the verdict reflects the audit's view, not the Lead's.

(In practice, audit's intent-derived checks may overlap with Lead-written
tests. Both run. If only the Lead's tests pass, audit reports `partial`.)

---

## 6. Otto runtime changes

The runtime is mostly extension, not rewrite.

### 6.1 Queue schema (`otto/queue/schema.py` + `enqueue.py`)

Add fields to `QueueTask`:

```python
parent_task_id: str | None    # None for user-submitted; set for agent-emitted
integration_branch: str | None # branch this task merges to; None means main
review_state: Literal["dispatched", "pending_review", "approved"] | None
```

`enqueue_task` accepts `parent_task_id` and `integration_branch`. The
`mcp__otto__submit_subtask` MCP tool wraps `enqueue_task` with these fields
populated from the calling Lead's identity.

### 6.2 Watcher (`otto/queue/runtime.py` + `runner.py`)

Existing watcher already spawns tasks via `subprocess.Popen(argv, ...)`
(`runner.py:1376`). Extensions:

1. **Skip dispatch for `pending_review` tasks** — watcher checks `review_state`
   before spawning. Tasks with `pending_review` are surfaced in MC and held.
2. **Track parent-child completion** — on task completion (existing
   `TaskNotificationMessage` lifecycle), check if the parent has any pending
   children. If all children complete, schedule an integration Lead for the
   parent.
3. **Spawn integration Lead** — same as user-submitted task spawn, but with
   `--integration-task-id <parent_id>` flag that selects the integration
   prompt template and reads child verdicts.

These are small additions, not a rewrite.

### 6.3 Merge queue (`otto/merge_queue.py`)

The merge_queue already supports `target_branch` (line 87:
`base_branch: str  # the integration target (typically "main")`). The
`resolve_integration_base_branch` mechanism (line 373) already exists. We
extend by:

1. Each parent task has an integration branch named
   `i2p/<parent_task_id>/integration` (off main, or off grandparent's
   integration).
2. Children's task `target_branch = parent_integration_branch`.
3. When all children of a parent have merged to its integration branch and the
   parent's integration audit returns pass/partial, the parent's integration
   branch merges up to grandparent's integration branch (or main, if root).

This is a configuration change to existing merge_queue logic, not new code.

### 6.4 Task graph (`otto/queue/task_graph.py`, NEW)

A small file `otto_logs/cross-sessions/task_graph.json` per project records
parent-child edges, status, and verdicts. Used by:

- The watcher to know when a parent is ready for integration.
- MC dashboard to render the tree.
- The integration Lead to read children's verdicts.

Schema:

```json
{
  "schema_version": 1,
  "tasks": {
    "<task_id>": {
      "parent_task_id": "<id or null>",
      "intent": "...",
      "decomposition": "inline | emit | pending",
      "verdict": "pass | partial | pending_children | unverified | failed | catastrophic",
      "integration_branch": "i2p/.../integration",
      "started_at": "...",
      "completed_at": "...",
      "cost_usd": 0.0,
      "child_task_ids": [...]
    }
  }
}
```

### 6.5 What dies in `spec_compile.py` and `build.py`

- `spec_compile.py` group/contract synthesis (~600 lines) — gone.
- `spec_compile.py` shared-contract normalizers (~200 lines) — gone.
- `build.py` group orchestration loop (~400 lines) — replaced by Lead.
- `build.py::detect_critical_shared_contract_violations` and friends (~600
  lines) — gone, no contracts to violate.
- `repair_gates.py` (~300 lines) — gone, repair is a fix-task in the queue.

### 6.6 What `spec_compile_flat.py` does (replaces full spec_compile)

For root tasks ONLY, `spec_compile_flat.py` produces a small artifact:

```json
{
  "schema_version": 1,
  "intent": "...",                        // verbatim user input
  "project_kind": "webapp | cli | ...",   // detected
  "behavior_journeys": [
    {"id": "...", "description": "user-language steps targeting features"}
  ]
}
```

That's it. NO groups, NO owned_paths, NO shared_contracts, NO features list.
The behavior_journeys are user-language ("the user clicks 'Add Transaction',
fills in $50 grocery, saves, sees it in the list") and serve as a
reference for the audit.

For sub-tasks, `spec_compile_flat.py` is NOT called. The sub-Lead reads its
intent directly from the task definition.

### 6.7 Pre-existing infrastructure that stays unchanged

- `otto/checkpoint.py`, `otto/resume.py` — task-level resume, unchanged.
- `otto/audit.py` — verifier core, unchanged.
- `otto/budget.py`, `otto/observability.py` — unchanged.
- `otto/branching.py`, `otto/worktree.py` — unchanged.
- `otto/web/` — extended for new tree-view UI, see §7.
- Brownfield path — unchanged. Brownfield root Lead reads existing code and
  decides decomposition like any other Lead.

---

## 7. UI changes

Mission Control changes are concrete and bounded.

### 7.1 Build form

Today: provider/mode selectors + intent textarea + advanced disclosure.

v5: same, plus:
- New checkbox: "Review root decomposition" (off by default; on for non-trivial intents detected by length/markers).
- New disclosure: "Provide tasks manually" — opens a multi-line input where the
  user can list tasks, one per line. If used, root spec_compile is skipped.

### 7.2 Run view

Today: list of stages (compile, build, merge, audit, render).

v5: replaced with a TREE VIEW.

```
[ROOT TASK: Build a personal finance dashboard]              verdict: pending_children
├─ [VP-1: Implement transaction CRUD]                        verdict: pass
│  ├─ [LEAF-1.1: Add transaction form]                       verdict: pass
│  ├─ [LEAF-1.2: Edit transaction]                           verdict: pass
│  └─ [LEAF-1.3: Delete + categorize]                        verdict: pass
├─ [VP-2: Implement charts (income/expenses/trends)]         verdict: running
├─ [VP-3: Implement search + filter UI]                      verdict: queued
├─ [VP-4: Implement CSV import/export]                       verdict: pending
└─ [INTEGRATION: root]                                       verdict: pending
```

Each node is a card with: id, intent (truncated), status pill, verdict pill,
cost. Click expands children. Double-click drills into the node's proof
packet, child summaries, audit findings.

Live updates via existing watcher messages: `TaskStartedMessage` →
"running"; `TaskNotificationMessage` → status change.

### 7.3 First-level review modal

When `--review-first-decomp` is on (or an MC checkbox), and the root Lead has
emitted subtasks:

```
┌─────────────────────────────────────────────────────────────┐
│ Review Decomposition                                         │
│                                                              │
│ Otto proposes these tasks for "Build a personal finance     │
│ dashboard":                                                  │
│                                                              │
│ [✓] 1. Implement transaction CRUD                  [Edit]   │
│ [✓] 2. Implement charts (balance/expenses/trends)  [Edit]   │
│ [✓] 3. Implement search + filter                   [Edit]   │
│ [✓] 4. Implement CSV import/export                 [Edit]   │
│                                                              │
│ [+ Add task] [Replace all with manual list]                 │
│                                                              │
│            [Discard run]    [Approve and dispatch]           │
└─────────────────────────────────────────────────────────────┘
```

User can uncheck (= cancel that task), edit (= modify intent), add new tasks,
or replace the whole list. Discard cancels the run cleanly.

### 7.4 Per-node drilldown

Click a leaf node card → drawer opens with:
- That node's intent and worktree path.
- The node's proof packet (rendered).
- Audit findings from its `verify` calls.
- Cost / duration.
- "Resume this task" button (uses existing checkpoint/resume).

Click a non-leaf node → drawer shows the same plus child summaries.

### 7.5 Project-level dashboard

Today: list of recent runs with a verdict pill each.

v5: same, but each row's verdict is the ROOT's verdict aggregating its tree.
Click into a run → tree view.

### 7.6 No changes to the project launcher, queue manager, or settings UI.

---

## 8. SDK + Otto code groundedness summary

| Capability | Status | Where verified |
|---|---|---|
| In-process MCP from main Lead | ✓ verified | `/tmp/sdk-smoke/smoke.py` |
| `mcp__otto__submit_subtask` returns task_id immediately | ✓ verified | `/tmp/sdk-smoke/test_queue_recursion.py` |
| Concurrent Lead `query()` calls don't race on shared MCP | ✓ verified | `/tmp/sdk-smoke/test_concurrency.py` |
| Idempotent submit by `(parent, intent_hash)` | ✓ verified | `/tmp/sdk-smoke/test_idempotent.py` |
| Queue-based recursion at depth 3+ | ✓ verified | `/tmp/sdk-smoke/test_queue_recursion.py` |
| Subagent cost in parent's ResultMessage | ✓ verified | `/tmp/sdk-smoke/test_subagent_cost.py` |
| `agent_id` populated in PreToolUse hook on subagent calls | ✓ verified | `/tmp/sdk-smoke/test_subagent_hook_id.py` |
| `subprocess.Popen(argv, ...)` for spawning Lead processes | ✓ in-use today | `otto/queue/runner.py:1376` |
| Merge queue supports non-main target_branch | ✓ in-use today | `otto/merge_queue.py:87` (`base_branch: str`) |
| Existing watcher tracks task lifecycle | ✓ in-use today | `otto/queue/runtime.py` + `runner.py` |
| Existing audit harness | ✓ in-use today | `otto/audit.py` |

What we're NOT relying on (verified does NOT work):
- Recursive in-session subagent dispatch (depth ≥ 2 broken in SDK 0.1.50).
- Mid-session pause for user input via SDK (we pause at watcher level instead).

---

## 9. Implementation phases

Honest scope: ~3 weeks, ~2200 LOC net.

### Phase 1 — Lead primitive + MCP server (1 week, ~700 LOC)

1. `otto/lead.py` — single Lead runner. ~300 LOC.
2. `otto/mcp_tools.py` — in-process MCP server with `submit_subtask`,
   `begin_inline`, `verify`, `checkpoint` tools. ~250 LOC.
3. `otto/queue/task_graph.py` — task graph storage. ~150 LOC.
4. New prompts: `lead.md` (planning + inline build), `lead-integration.md`
   (integration phase). ~250 LOC of content.
5. Tests: Phase 1 smoke suite (re-run all existing smoke tests under CI). ~50 LOC fixtures.

Deliverable: a Lead can run end-to-end on a small task (no decomposition); MCP
tools work; task graph records the task. NO multi-level yet.

### Phase 2 — Hierarchy + integration (1 week, ~700 LOC)

1. Queue schema extension: `parent_task_id`, `integration_branch`, `review_state`. ~100 LOC.
2. Watcher extension: track parent-child completion; spawn integration Leads. ~250 LOC.
3. Merge queue config: per-parent integration branches. ~150 LOC.
4. `spec_compile_flat.py` — minimal root-level intent extraction. ~150 LOC.
5. Integration prompt template + integration Lead handling. ~50 LOC.
6. Tests: small two-level tree fixture. ~100 LOC.

Deliverable: a root Lead can emit children; children run; parent's integration
Lead runs; verdicts aggregate. End-to-end on the finance dashboard fixture.

### Phase 3 — Review affordance + UI (0.5-1 week, ~400 LOC)

1. Watcher pause for `pending_review` state; webhook for MC. ~100 LOC.
2. MC tree view component + per-node drilldown. ~200 LOC TS/CSS.
3. First-level review modal. ~100 LOC TS/CSS.
4. `--review-first-decomp` flag plumbing. ~30 LOC.
5. `--tasks <file>` flag plumbing. ~30 LOC.
6. Build form additions in MC. ~50 LOC TS.

Deliverable: user can review root decomposition before dispatch; tree view
shows live status.

### Phase 4 — Provider fallback + cleanup (0.5 week, ~250 LOC)

1. Task-level provider fallback (codex 402 → claude). ~80 LOC.
2. `cost_attempts[]` schema in summary.json. ~40 LOC.
3. Move deprecated code to `otto/legacy/`. ~30 LOC of moves.
4. Bench against finance-dashboard, microblog, ops-dashboard, brownfield SAML. Documentation.
5. v6 punch list documented.

Deliverable: v5 ships.

### Cumulative LOC

- Net delta: roughly +2200 LOC of new code, -2000 LOC of deletion.
- Major net win: less code to maintain than today, because the new
  architecture is recursive (one primitive applied many times).

---

## 10. Smoke tests still needed (Phase 1)

These are written alongside Phase 1 implementation. Each ~30-50 LOC, ~30s
runtime.

1. **Per-parent integration branch merge** — verify merge_queue can target a
   non-main branch and that two children merging to the same integration
   branch produces a clean linear history (or fails cleanly on conflict).

2. **Watcher pause for `pending_review`** — write a task with
   `review_state="pending_review"`, start watcher, verify dispatch is held
   until state flips to "approved", then verify dispatch happens.

3. **Integration Lead reads child verdicts** — write a test fixture with two
   completed children + a parent in `pending_children` state. Spawn an
   integration Lead via the new entrypoint. Verify it reads child verdicts
   from task_graph.json and child proof packets, runs verify, returns a
   clean verdict.

4. **`begin_inline` marker propagates** — verify that calling `begin_inline`
   in the MCP server records the decision in task_graph.json and the
   watcher recognizes the task as committed-to-inline (won't try to
   integrate it).

5. **Root Lead pause for review** — full flow: root Lead emits 3 subtasks;
   watcher holds; MC modal would render (test the API surface, not the UI);
   approve via MC API; watcher dispatches.

If any of these fail unexpectedly, that's a phase-1 work item, not a v5
blocker — they're on the implementation critical path but not architecturally
unsettled.

---

## 11. What stays, what dies, what changes

### Dies (deleted in Phase 4 after Phase 2 ships)

- `otto/spec_compile.py` group/contract synthesis (~800 LOC).
- `otto/build.py` group orchestration (~600 LOC).
- `otto/build.py::detect_critical_shared_contract_violations` and friends (~600 LOC).
- `otto/repair_gates.py` (~300 LOC).
- Prompts: `compile-spec-brownfield*.md`, `build-merge-repair.md`, `compile-spec-structured-output.md`.

### Stays unchanged

- `otto/audit.py`, `otto/checkpoint.py`, `otto/resume.py`, `otto/budget.py`,
  `otto/observability.py`, `otto/branching.py`, `otto/worktree.py`,
  `otto/cli_*.py` (modulo new flags).

### Changes substantively

- `otto/queue/schema.py` + `enqueue.py`: new fields.
- `otto/queue/runtime.py` + `runner.py`: track parent-child completion;
  spawn integration Leads.
- `otto/merge_queue.py`: per-parent integration branches.
- `otto/web/`: tree view, first-level review modal, build form.

### Newly created

- `otto/lead.py`, `otto/mcp_tools.py`, `otto/queue/task_graph.py`,
  `otto/spec_compile_flat.py`, prompts.

---

## 12. Definition of done

1. Lead primitive in production. spec_compile group synthesis moved to legacy/.
2. Tree-shaped decomposition: root Lead emits VP-level children; sub-Leads
   recursively decompose or build inline. Verified at depth 3+ on a real
   project.
3. Integration Leads run at every non-leaf node; integration audit gates
   merges up the tree.
4. First-level review affordance works in MC: can accept, edit, or replace.
5. `--mode autopilot` (default) and `--review-first-decomp` (opt-in) and
   `--tasks <file>` (manual) all work.
6. Provider fallback works on a real codex-out-of-credits run.
7. Bench: finance-dashboard PASSES end-to-end; microblog cost ≤ 1.5×
   today's; brownfield SAML add preserves existing tests.
8. UI: tree view replaces stage-based run view; per-node drilldown opens
   proof packet; live updates.
9. All Phase 1 smoke tests in CI.
10. v6 punch list documented (recursive depth ≥ 4 caveats, cumulative INTENT.md,
    cross-task budget slicing details).

---

## 13. Best-effort invariants (the implementation checklist)

This section catalogs every layer that can fail. Each row specifies: what can
go wrong, what the layer does autonomously to recover or terminate, and what
the user sees. **No row terminates in "block" or "crash." Every row terminates
in a verdict the user can read and act on.**

This is the implementation checklist. If any code path violates one of these
invariants, the implementation is wrong.

### Lead session (Phase 1)

| Failure | Autonomous response | User-visible result |
|---|---|---|
| Tool call errors (Read, Edit, Bash) | Lead reads error in tool result, decides next step | Logged in messages.jsonl; Lead continues |
| `max_turns` reached | Lead session ends with whatever state was reached | Verdict = `partial`, proof packet shows progress |
| `max_budget_usd` reached | SDK terminates session at budget cap | Verdict = `partial`, `cost_usd` reflects spend |
| MCP tool failure (e.g., `submit_subtask` queue write fails) | Tool returns error to Lead; Lead handles inline (e.g., falls back to `begin_inline`) | Lead reasons over the error; continues |
| Lead crashes mid-session (uncaught exception in Python) | `lead.py` wraps `query()` in try/except; on crash, writes `summary.json` with verdict = `catastrophic` and `failure_reason` | Verdict = `catastrophic`, cause logged |
| Provider auth/credits fail | Wrapped at task level (Phase 4); task re-dispatched with fallback provider | Verdict = `pass` (eventually) with `cost_attempts[]` showing the swap |
| Verifier (`mcp__otto__verify`) fails or times out | Verifier returns structured failure; Lead reads it; verdict = `unverified` if no other signal | Verdict = `unverified`, audit findings empty |
| Lead's prompt makes Lead claim "pass" without calling verify | Render layer cross-checks: if Lead returned `pass` but no verify call recorded, downgrade to `unverified` | Verdict = `unverified` with note "verify not called" |

### Hierarchy (Phase 2)

| Failure | Autonomous response | User-visible result |
|---|---|---|
| Child task spawn fails (`subprocess.Popen` error) | Watcher marks child `catastrophic`, parent's integration runs without it | Tree shows that child as `catastrophic`; siblings continue |
| Child task crashes mid-run | Captured in child's summary.json (see Lead row above); parent's integration proceeds with the verdict it has | Parent integration runs over surviving children's work |
| Child task hangs past timeout (`task_timeout_s`, default 4200s) | Watcher kills child process; marks `unverified`; parent's integration proceeds | Tree shows hang; root verdict reflects |
| Child rebase conflict on integration branch — exhausted attempts | Child marked `merge_blocked`; not merged; siblings continue; integration runs without it | Tree shows `merge_blocked` child; user can drill in |
| Cyclic dependency in `submit_subtask(depends_on=[...])` | `enqueue_task` validates DAG (existing); rejects cycle with structured error to Lead | Lead reads error; revises emission |
| Integration branch creation fails (git error) | Watcher retries with fresh branch name; if all fail, parent's integration target falls back to the task's worktree branch directly | Logged warning; integration still runs |
| All children of a parent crash | Parent's integration Lead spawns anyway, with empty children list; reports verdict = `partial` or `catastrophic` | Tree shows all-children-failed; root sees `partial` |
| Parent integration Lead crashes | Same as Lead crash row above; verdict = `catastrophic` | Parent terminates; root continues to integrate parent's siblings if any |
| Watcher crashes / SIGTERM | Existing queue runtime handles graceful shutdown; in-flight tasks marked INTERRUPTED; resume on watcher restart | User sees pause; can `--resume` to continue |
| Tree budget cap hit (`--tree-budget-usd`) | Watcher refuses new dispatches; in-flight finish; integration runs over what's done | Verdict = `partial` with `budget_exceeded` notation |
| Disk full / out of inodes mid-run | All file writes fail loudly; watcher marks affected tasks `catastrophic` | Verdict = `catastrophic` with infra reason |

### UI (Phase 3)

| Failure | Autonomous response | User-visible result |
|---|---|---|
| Watcher unreachable from MC | MC shows last known state from disk + "watcher offline" badge | User sees stale-but-honest state |
| Tree view component error | React error boundary catches; falls back to flat list of tasks | List view; full proof-packet links still work |
| Review modal not approved (user closes browser) | Pending tasks remain `pending_review` in queue indefinitely; user can return any time and act | No data lost; resumes when user returns |
| Review modal user types invalid task content | Form validates; refuses submit; shows inline error | User edits; no run state mutated |
| Streaming task graph updates fall behind | UI polls on resume; reconciles from disk task_graph.json | UI catches up |

### Provider fallback (Phase 4)

| Failure | Autonomous response | User-visible result |
|---|---|---|
| Codex 402 (out of credits) | Task fails with `failure_reason="provider_exhausted"`; watcher detects + re-dispatches with `--provider claude` | Single visible task with `cost_attempts[]` showing both providers |
| Both providers exhausted | Task verdict = `catastrophic` with `failure_reason="all_providers_exhausted"` | Tree shows that branch as catastrophic; siblings continue |
| Mid-task rate limit (e.g., HTTP 429) | SDK retries with exponential backoff (built-in); if exceeded, task terminates with `unverified` and is re-dispatched on watcher's next eligibility check | User sees a brief stall, then continuation |
| Provider returns malformed response | Lead's wrapper catches parse error; treats as `unverified` for that turn; continues | Logged; if persistent, verdict = `unverified` |

### Cross-cutting invariants

These hold everywhere:

1. **Every layer terminates.** No infinite loops, no hangs without timeouts, no waits for external input outside `--mode supervised`. Even `--mode supervised` review states have user-configurable timeouts; expiry defaults to "auto-approve" with a logged note.
2. **Every termination produces a verdict.** Verdicts are 6 values: `pass | partial | unverified | merge_blocked | pending_children | catastrophic`. There is no terminal state without a verdict.
3. **Every crash is captured.** `lead.py`, the watcher, the merge_coordinator, and the MCP tool wrappers all have outer try/except that writes a `summary.json` with `verdict=catastrophic` + `failure_reason` before propagating. NO uncaught exceptions. NO silent task disappearance.
4. **Tree state always reflects ground truth on disk.** task_graph.json is the durable source of truth. UI reflects it. If tree state in memory differs from disk after a crash, disk wins on resume.
5. **No layer waits indefinitely on another.** Every cross-layer call has a timeout. Watcher waits for child completion bounded by `task_timeout_s`. Integration Lead waits for verify bounded by `judge_timeout_s`. UI polling waits bounded by network timeout.
6. **`--resume` works at any layer.** Killing watcher mid-run, killing a Lead mid-run, killing the merge_coordinator mid-run — all recoverable via Otto's existing checkpoint + resume mechanism.

### What "user gets at least something" means concretely

A worst-case run hits half the rows above. The user still gets:

- A proof packet rendered. Always.
- The tree view in MC showing what tried, what worked, what didn't.
- Whatever code committed to the integration branches before failures.
- A clear verdict per node (no "unknown" states).
- Cost spent itemized.

The user reads the proof packet. Decides: ship the partial, file follow-ups, or revert. They are NEVER blocked waiting for Otto. They are NEVER given a crashed run with no artifact.

### Phase 1 acceptance includes these invariants

Phase 1's smoke test suite must include at least:
- Lead with bad MCP tool call → verdict produced (not crash).
- Lead's `max_turns` hit → verdict produced.
- Lead crashes (raise) → wrapper catches, verdict = `catastrophic`.
- Verifier times out → verdict = `unverified`.

If any of those fail, Phase 1 doesn't ship.

### Phase 2 acceptance adds:

- Child crashes → parent's integration runs with surviving children.
- All children crash → parent terminates with `catastrophic`, root continues.
- Watcher SIGTERM → restart resumes cleanly.
- Tree budget exceeded → soft stop, no new dispatches.

### Phase 3 acceptance adds:

- Review modal abandoned → pending state persists, recoverable.
- Watcher offline → MC shows last known state, no error UI.

### Phase 4 acceptance adds:

- Codex 402 → claude fallback; cost_attempts populated.
- All providers exhausted → catastrophic, surfaced.

---

## 14. Architectural invariants (the design philosophy as code)

§13 captured "what happens when things fail." This section captures "what is
always true about how Otto operates" — the design philosophy as enforceable
contracts. If any invariant breaks during implementation, the implementation
is wrong, even if no test crashes.

### Decomposition / hierarchy

| Invariant | Enforced by | Tested by |
|---|---|---|
| Otto NEVER invents implementation structure (groups, owned_paths, shared_contracts) | spec_compile_flat output schema rejects these fields; CI test asserts the schema | Phase 1 schema test |
| Decomposition decisions are made by Leads at runtime, recorded in task_graph | Only `mcp__otto__submit_subtask` and `mcp__otto__begin_inline` emit decomposition signals; both originate from a Lead's session | Phase 2 integration test |
| Each Lead's contract from its parent is SEMANTIC (a goal sentence), not structural (paths/files) | Sub-task's intent is verbatim text; no `owned_paths` or similar fields exist on tasks | Phase 1 schema test |
| One Lead primitive runs at every level | Single `lead.py` file; all invocations route through it; only the prompt template varies (`lead.md` for planning/inline, `lead-integration.md` for integration phase) | Code review + bench across 3 different project shapes |
| The root's first decomposition is the only one with elevated visibility | `--review-first-decomp` only fires for tasks with `parent_task_id=None`; sub-Leads never pause for review | Phase 3 test |
| A child can only operate on its own worktree | OS-level: each task gets its own `cwd`; subprocess isolation; no cross-worktree paths in any tool | Phase 2 test attempts cross-worktree write, expects it to fail at OS level |
| Conflicts at the file level are tolerated and resolved at the parent merge node, never pre-empted | merge_queue rebase + conflict resolution runs at integration time only; no scope checker prevents writes upfront | Phase 2 fixture: two children write same path; merge resolves at parent |

### Verification (the truth gate)

| Invariant | Enforced by | Tested by |
|---|---|---|
| Audit is the ONLY pass gate. A Lead's self-claim of `pass` is not authoritative. | Render layer cross-checks: if Lead returned `pass` but no `mcp__otto__verify` call recorded, downgrade to `unverified` with reason | Phase 1 test: Lead claims pass without verify, render produces `unverified` |
| Behavior journeys are user-language only | spec_compile_flat post-validate runs a lint that rejects `class=`, `id=`, `data-testid`, `getByRole(...)` patterns and re-prompts the compiler | Phase 1 lint unit test |
| Audit runs against the running product, never source code | `mcp__otto__verify` invokes existing audit pipeline (browser/CLI/HTTP probes); Lead cannot pass synthetic evidence | Phase 1 test: try to fake-pass via source-only check, audit refuses |
| Audit at level N checks ONLY journeys appropriate to level N | Lead passes `feature_scope_ids` matching its inherited goals; root integration audits all journeys | Phase 2 test: leaf audit doesn't run cross-feature journeys |
| Build-agent and test-agent (if separated) never share write surface | When a Lead dispatches build + test as named subagents, their `AgentDefinition.tools` lists are disjoint; PreToolUse hook enforces | Phase 1 test: build agent attempts `tests/**` write, hook denies |
| Behavior journeys are frozen for the duration of a run | `behavior_journeys.md` is read-only after spec_compile_flat writes it; Lead and audit may read but not write | File permissions or hook check; Phase 1 test |

### Honesty (verdicts never lie)

| Invariant | Enforced by | Tested by |
|---|---|---|
| A parent's verdict is never more optimistic than its children's worst | `aggregate_verdict()` in render layer takes severity-max across child verdicts | Phase 2 unit test: 1 pass + 1 partial child → parent ≥ partial |
| Verdict vocabulary is finite and exhaustive: `pass | partial | unverified | merge_blocked | pending_children | catastrophic` | Type system: `Literal[...]` enforces no other strings | Phase 1 unit test |
| Cost is never under-reported | Per-attempt costs sum into `cost_attempts[]`; total includes all retries and provider switches | Phase 4 test with codex→claude fallback |
| Wall time includes all child time (cumulative for trees) | Aggregated at render from task_graph leaf timestamps | Phase 2 test |
| If a feature is missing or fails, the verdict reflects it | Audit reports per-journey pass/fail; render layer marks any missing journey as a `partial` reason | Phase 1 test: Lead intentionally cuts a feature, audit catches |
| The Lead's own claims appear in proof packet, but the verdict is computed from audit + render rules, not from Lead's claim | Render layer computes verdict from audit output + retry budgets, ignoring any pass/fail string in Lead's final text | Phase 1 test: Lead text says "pass," audit fails, verdict = `partial`/`unverified` |

### User control & transparency

| Invariant | Enforced by | Tested by |
|---|---|---|
| User intent is captured verbatim in summary.json and proof packet | Otto stores `intent.resolved_text` from input; never paraphrases | Existing observability code |
| User can interrupt at any time without data loss | Worktree commits + checkpoint state preserve all work-in-progress; SIGTERM is graceful | Existing watcher SIGTERM handler |
| Proof packet always renders, regardless of verdict | Render runs in `try/finally`; even on catastrophic, packet shows what existed | Phase 1 test: induce mid-run crash, verify packet renders |
| Proof packet shows: original intent, decomposition decisions, per-node verdicts, audit findings, costs | Render template includes all these sections; missing data shows "n/a" not omitted | Phase 4 acceptance — render packet for 5 reference projects, verify completeness |
| Supervised mode never blocks autopilot: it's strictly opt-in | `--mode supervised` is a CLI/MC flag; autopilot is the default; no code path adds review pauses without it | Code review |
| MC dashboard shows project state without requiring user action | Dashboard polls; never modal-blocks; tree view degrades to flat list on render error | Phase 3 acceptance |

### Observability

| Invariant | Enforced by | Tested by |
|---|---|---|
| Every Lead session writes messages.jsonl (full SDK trace) | Otto's existing observability layer; Lead session output piped to disk | Existing test |
| Every prompt the Lead receives is saved to disk | `save_rendered_prompt` (existing in observability.py) | Existing test |
| task_graph.json is durable on disk before any agent action proceeds | `submit_subtask` MCP tool writes graph + flushes before returning task_id; agent can't act on a task that's not yet recorded | Phase 1 test: kill agent after submit_subtask, verify graph state on disk |
| Every artifact (proof packet, summary, journey result) carries its task_id | Schema requires task_id field; render template includes it | Phase 1 schema test |
| Cost is tracked per-task and aggregated up the tree at every merge | `summary.json` per task includes own cost; integration Lead sums children + own | Phase 2 test |
| All decomposition decisions (begin_inline vs submit_subtask) are recorded with timestamps | task_graph.json `decomposition` field is set when MCP tools fire | Phase 1 test |

### Architecture / "Otto is mechanics, not architect"

| Invariant | Enforced by | Tested by |
|---|---|---|
| Otto never reads behavior journeys to decide what to build | Only Leads read behavior_journeys.md; only audit reads at verify time | Code review |
| Otto's queue, watcher, and merge_queue do not run any LLM calls themselves | Search for `query()` outside `lead.py`, `spec_compile_flat.py`, `audit.py`, `mcp_tools.py`; nothing else should call into the SDK | CI grep test |
| The Lead's prompt is the single point of intelligence per session | Otto orchestrates spawn/merge/audit; Lead's prompt is what decides | Code review |
| Decomposition heuristic is in the Lead's prompt, not in Otto's Python | `decomp_select.py` is removed; the inline-vs-decompose decision is text in `lead.md` | Code review (no decomp_select.py file exists) |

### Concurrency & isolation

| Invariant | Enforced by | Tested by |
|---|---|---|
| Concurrent siblings can write the same file | No write-time scope check; merge_queue resolves at integration | Phase 2 fixture, already covered in honesty section |
| `main` has exactly one writer at a time | merge_queue's existing fcntl serialization | Existing test |
| Each integration branch (per parent task) is also single-writer | merge_queue's `target_branch` per-task dispatch + fcntl | Phase 2 test: 4 concurrent children targeting same integration branch, no corruption |
| task_graph.json mutations are atomic | sqlite or fcntl + atomic file rename for JSON | Phase 1 concurrency test (similar to test_concurrency.py) |
| MCP tool calls are race-free across concurrent Leads | Verified empirically by `/tmp/sdk-smoke/test_concurrency.py` | CI re-runs that test |

### Resumability

| Invariant | Enforced by | Tested by |
|---|---|---|
| Killing watcher mid-run loses no completed work | Each task's outputs are committed to its worktree; queue state is durable | Existing test |
| Killing a Lead mid-run loses only that Lead's in-progress turn | Checkpoint after each tool call; resume picks up from last checkpoint | Existing test |
| Resume reconstructs task_graph from disk; no in-memory-only state | task_graph.json is the truth; in-memory cache is rebuilt on resume | Phase 2 test |
| A `merge_blocked` task can be resumed manually by user | Existing `--resume` flag works on any task_id | Phase 2 acceptance |

### Cost & time bounds

| Invariant | Enforced by | Tested by |
|---|---|---|
| Per-task budget is enforced by SDK (`max_budget_usd`) | Existing SDK feature | Verified in earlier smoke tests |
| Per-task wall time is bounded by `task_timeout_s` | Existing watcher kills hung tasks | Existing test |
| Tree-level budget cap (`--tree-budget-usd`) prevents runaway recursion | Watcher tracks cumulative cost across tree; refuses new dispatches when capped | Phase 2 test: synthetic infinite-emit Lead, watcher caps it |
| Cost ceilings cause soft-stops, not crashes | When budget hit, in-flight tasks finish; new ones don't dispatch; user notified | Phase 2 test |

### What we explicitly DON'T promise (anti-invariants)

These are NOT invariants. Don't add code that pretends they are.

1. **Determinism.** Same intent twice will not produce identical output. The Lead is stochastic.
2. **Strict cost ceilings under all conditions.** SDK's budget cap is checked between turns; one expensive turn can overshoot.
3. **No regressions on `main`.** Best-effort means honest reporting of regressions, not preventing all of them.
4. **Recovery from arbitrary system corruption.** Disk corrupted, sqlite unreadable → Otto can't fix; we mark catastrophic.
5. **Cross-project state.** Each project is independent. No memory transfer.
6. **Mid-run intent amendment.** v6.

---

## 15. The one-paragraph version

Otto v5 is the company analogy in code. One primitive — a Lead — runs at every
level of a hierarchy. The root Lead receives the user's intent, decides to
either build inline or emit ~3-7 strategic child tasks. Each child gets its
own Lead, which recursively decides the same. Leaves build code; merges
propagate bottom-up to per-parent integration branches; an integration Lead
runs at every merge node to audit the integration at its semantic level. The
SDK supports this via in-process MCP servers (verified at depth 3),
queue-based recursion (verified across concurrent and idempotent calls), and
existing Otto infrastructure (queue runtime, merge_queue with target_branch
support, audit harness). The first-level decomposition is treated specially
— visible in MC, optionally reviewable — because strategic splits matter
most. Sub-levels are autonomous. Net code change: ~+2200 LOC new, ~-2000 LOC
deleted; v4's frozen-decomposition machinery is gone. ~3 weeks.
