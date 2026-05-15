# Plan-v5 Tabletop Simulation — Finance Dashboard

Walking plan-v5 through the *actual* finance-dashboard run that failed at 37
minutes on codex (session `2026-05-08-044149-c25608`). The point: surface
implementation gaps the high-level design glossed over, by tracing each step
concretely against real artifacts.

Honest goal: find what's wrong with plan-v5 BEFORE Phase 1 starts, not after.

---

## Setup

**Real intent (verbatim from input-provenance.json):**

> Build a personal finance dashboard web app. As a user I want to add income
> and expense transactions with category, amount, date, account, and note;
> edit and delete transactions; see monthly cash flow, category totals,
> account balances, and recent activity; filter and search transactions;
> create monthly budgets per category with overspend warnings; import/export
> transactions as CSV; persist data locally across refresh; and have a
> polished responsive UI with clear empty states and usable charts/tables.

**v4's actual failure pattern (from spec-state.jsonl):**
- 11 `group.attempt.failed`
- 6 `scope.critical` violations
- 4 `amendment.rejected`
- 3 `group.blocked`
- 4 of 5 groups never produced a successful merge
- Watcher killed at 37 min, run unfinished

**v4 root cause:** spec_compile generated `browser-quality-contract` owning
`tests/browser/**`, while every downstream group owned `tests/browser/test_<feature>.*`.
Every downstream group's first attempt to write its own browser test triggered
`scope.critical`, attempt failed, repeated 3× per group, group blocked.

---

## v5 trace, step by step

### T+0:00  User submits intent

CLI: `otto run --provider claude "Build a personal finance dashboard..."`

Or MC: form submission with intent text + `--mode autopilot` (default).

`otto/cli_run.py` enqueues a root task with:
```
parent_task_id: None
integration_branch: None    # null means "merges to main"
review_state: "approved"   # autopilot mode skips review
intent: <verbatim>
```

Watcher picks up the task. Spawns root Lead via `subprocess.Popen([..., "otto", "run", "--task-id", "<root>", ...])`.

### T+0:30  Root spec_compile_flat runs

A separate, lightweight call. Reads intent, emits:

```json
{
  "schema_version": 1,
  "intent": "Build a personal finance dashboard web app. ...",
  "project_kind": "webapp",
  "behavior_journeys": [
    {"id": "txn_crud", "description": "User adds an expense transaction with category, amount, date; sees it in the list; edits its category; deletes it. Refreshes browser; transaction state persists."},
    {"id": "monthly_cashflow", "description": "User adds 3 income and 2 expense transactions across two months. Dashboard shows current month's cash flow as income minus expense; shows 2 of the past months in trend view."},
    {"id": "category_totals", "description": "User adds 4 transactions across 3 categories. Category totals widget shows each category's sum, sorted descending."},
    {"id": "account_balances", "description": "User assigns transactions to two accounts. Balance widget shows per-account totals."},
    {"id": "filter_search", "description": "User adds 6 transactions. User filters to one category; only those show. User searches by description text; only matching show."},
    {"id": "budgets_overspend", "description": "User creates a $200 monthly budget for 'Food'. User adds Food transactions totaling $250. Budget widget shows overspend warning with the $50 overage."},
    {"id": "csv_export", "description": "User clicks Export. Browser downloads a CSV with transaction columns; rows match what's in the app."},
    {"id": "csv_import", "description": "User clicks Import; selects a CSV file with 3 rows; app shows them in the transaction list."},
    {"id": "responsive_ui", "description": "On mobile viewport (375px wide), app remains usable: forms accept input, lists scroll, no horizontal overflow."},
    {"id": "empty_states", "description": "Fresh app shows clear empty state: 'No transactions yet' on list; 'Add a budget to get started' on budgets; 'No data to chart' on cashflow chart. Each has a CTA or hint."}
  ]
}
```

That's it. **No groups, no owned_paths, no shared_contracts.** ~10 behavior journeys
in user-language. ~$0.05, ~30s. Saved at `otto_logs/sessions/<session_id>/spec/spec.json`.

**🔍 Gap surfaced #1:** The behavior journeys must be authored in user-language
(not implementation-language). Plan-v5 §6.6 says this; it needs a lint check
in `compile-spec-flat.md` to reject journeys with `class=`, `id=`, `getByRole(...)`-style
selectors. Otherwise the test agent inherits implementation-bias from the spec
compiler. **Action**: add an explicit lint pass in `spec_compile_flat.py` that
re-prompts the compiler if any journey contains DOM-implementation tokens.

### T+1:00  Root Lead session begins

Otto spawns the root Lead via `query()` with:
- `intent` = original user intent
- `project_state` = empty repo (greenfield)
- `is_root` = True

The Lead's `lead.md` prompt (from plan-v5 §2):

```
You are the root Lead. ...
Step 1 — Decide.
  Is this ONE coherent unit or MULTIPLE strategic areas?
  ...
```

The Lead reads. Reasons. Decides: this intent describes 4-5 distinct strategic
areas. Calls `mcp__otto__submit_subtask` four times:

```
submit_subtask(intent="Implement the foundation: app shell, routing, persistence layer, transaction data model, navbar.")
  → returns task_id="task_a1b2c3d4"

submit_subtask(intent="Implement transactions: add/edit/delete/categorize, list view, filter, search.")
  → returns task_id="task_e5f6g7h8"

submit_subtask(intent="Implement insights: monthly cashflow chart, category totals, account balances.")
  → returns task_id="task_i9j0k1l2"

submit_subtask(intent="Implement budgets: monthly budget per category, overspend warning.")
  → returns task_id="task_m3n4o5p6"

submit_subtask(intent="Implement CSV import/export of transactions.")
  → returns task_id="task_q7r8s9t0"
```

Then exits cleanly. Verdict: `pending_children`.

**🔍 Gap surfaced #2:** The Lead emits 5 subtasks but they have implicit
ordering. Foundation must complete before others (others import from
`src/lib/financeStore`). Plan-v5 says `submit_subtask(depends_on=[...])` exists,
but the Lead's prompt doesn't tell it to use deps. **Action**: the prompt needs
explicit guidance: "If your subtasks depend on each other (e.g., one provides a
shared store others consume), declare deps via `depends_on=[...]`. Generally:
foundation/scaffold tasks should be early, depended on by everything else."

The Lead ideally would emit:
```
submit_subtask(intent="...foundation...", depends_on=[])  → task_a1b2c3d4
submit_subtask(intent="...transactions...", depends_on=["task_a1b2c3d4"])
submit_subtask(intent="...insights...", depends_on=["task_a1b2c3d4"])
submit_subtask(intent="...budgets...", depends_on=["task_a1b2c3d4"])
submit_subtask(intent="...csv-io...", depends_on=["task_a1b2c3d4"])
```

**🔍 Gap surfaced #3:** Plan-v5's MCP `submit_subtask` tool needs to log the
parent_task_id automatically (the calling Lead's id) so the Lead's prompt
doesn't have to mention it. The tool should infer parent from the caller.

**🔍 Gap surfaced #4:** What if the Lead emits 12 micro-subtasks instead of 4-5
strategic ones? Or 1 big subtask? The prompt says "3-7 areas" but that's
guidance, not enforcement. Should we cap? Probably not at MCP level (over-cap
hurts large projects). But the audit at root level will catch silent scope cuts
either way. Acceptable.

### T+1:30  Foundation task spawned (task_a1b2c3d4)

Watcher reads queue. `task_a1b2c3d4` has `parent_task_id="root"` and `depends_on=[]`.
Eligible. Watcher spawns its Lead via `subprocess.Popen([..., "otto", "run", "--task-id", "task_a1b2c3d4", "--integration-branch", "i2p/root/integration"])`.

That spawned process:
1. Reads its own task entry: intent = "Implement the foundation..."
2. Sets up its Lead session.
3. Runs `lead.md` prompt at sub-Lead level (not root).
4. Lead decides this is one coherent unit. Calls `mcp__otto__begin_inline`.
5. Lead does the work inline: writes `package.json`, `tsconfig.json`, `vite.config.ts`,
   `src/App.tsx`, `src/lib/financeStore.ts`, `src/types/finance.ts`, `src/components/Navbar.tsx`,
   `src/pages/Home.tsx`. Runs `npm install`, `npm test`, `npm run build`.
6. Calls `mcp__otto__verify` with `feature_scope_ids=["empty_states", "responsive_ui"]`
   (the journeys this leaf is responsible for, plus any structural ones).
7. Verifier launches the dev server, runs the journeys with agent-browser. Returns:
   - `empty_states`: pass (Home renders "No transactions yet").
   - `responsive_ui`: pass.
8. Lead returns verdict: `pass`.

Foundation's commit is on its own worktree branch. `merge_queue` rebases it onto
`i2p/root/integration` (its target_branch). Clean rebase. Foundation merges to
root's integration branch.

**🔍 Gap surfaced #5:** The integration branch `i2p/root/integration` must be
created on demand the first time a child task targets it. Plan-v5 doesn't say
WHERE this happens — in the watcher? In `merge_queue.py`? Need to specify.
**Action**: when the watcher spawns the first child of a parent, it creates the
parent's integration branch from the parent's base (project main for root;
grandparent's integration branch for non-root). Idempotent operation.

**🔍 Gap surfaced #6:** Foundation needs to call `verify` against journeys
*it owns*. But who decides which journeys are "foundation's"? Plan-v5 §5 says
the Lead reads its intent and decides. But the journeys live in the root's
spec.json. The sub-Lead must:
  a. Read the root spec.json (which exists in root's session_dir).
  b. Decide which journeys map to its sub-intent.
  c. Pass those journey ids to `verify`.

This is implicit in plan-v5 but worth making explicit. **Action**: the
sub-Lead's prompt needs an explicit step: "Read the root spec at
`<root_session_dir>/spec/spec.json`. Identify behavior_journeys whose
descriptions overlap your sub-intent. Pass those journey ids to verify."
This is brittle if the Lead misjudges. Alternative: spec_compile_flat could
*assign* each journey to a putative module. But that's invented structure
again. Conservative: Lead reads + decides; integration audit at root catches
mismatches.

### T+12:00  Foundation done. Other 4 children dispatch.

Watcher sees `task_a1b2c3d4` complete with verdict=pass. The 4 dependent
tasks become eligible. Watcher spawns them concurrently (capped by `--concurrent`,
default 3 → 3 of 4 dispatched, 1 queued).

Each child's Lead reads its intent. Each calls `begin_inline`. Each:
- Reads root spec for its journeys.
- Reads foundation's code (already merged into integration branch; child's
  worktree was created off integration branch, so it has foundation's files).
- Builds inline.
- Tests.
- Calls `verify` with its scoped journey ids.
- Returns verdict.

**THIS IS WHERE V4 DIED.** v4's transactions-ledger task tried to write
`tests/browser/test_transactions.*`, hit `scope.critical` from
`browser-quality-contract`, retried 3 times, blocked.

**v5 doesn't have this problem because there are no shared_contracts.**
The child's Lead writes `tests/transactions.test.ts` (or wherever it puts
tests) freely. No scope checker runs. The test file is committed normally.
If two children happen to write to the same path, the merge_queue rebase
detects the conflict at integration time, not at write time. The parent
(root in this case) resolves at integration.

**🔍 Gap surfaced #7:** What does "the parent resolves at integration" actually
look like? Plan-v5 §3 says the integration Lead resolves cross-child issues.
But how:
  - The integration Lead is spawned AFTER all children have committed to the
    integration branch.
  - If two children wrote to the same file (say both wrote to `tests/setup.ts`),
    the second child's `git rebase` onto the integration branch would have
    conflicted. Where did it get resolved?
  - In v4, the build agent at the slice level resolved it. v5 needs to specify.
  
**Action**: the merge_queue currently has conflict-resolution logic — we
inherit that. When a child's rebase conflicts, the merge_queue prompts the
child's Lead (resumed via session_id or fresh) with the conflict and asks for
resolution. Plan-v5 should clarify this is unchanged from today.

If conflict resolution at the child level fails (3 attempts), the child is
marked `merge_blocked`. Other children continue. Parent's integration phase
proceeds with whatever children DID merge. Best-effort.

### T+22:00  All children done. Root integration phase fires.

Watcher sees all 5 children of root resolved. Each has its own verdict:
- foundation: pass
- transactions-ledger: pass
- insights-dashboard: pass
- budgets: partial (overspend warning works but the per-category month-bucketing has a bug)
- csv-io: pass

(All 5 passing in v5 because the Class-1 bug from v4 is gone. The hypothetical
"partial" on budgets is just realistic.)

Watcher spawns root's integration Lead via `subprocess.Popen([..., "otto", "run", "--task-id", "root", "--integration"])`.

The integration Lead's prompt (`lead-integration.md`):

```
You are the integration Lead for task root.
Original intent: <root intent>
Children's verdicts:
  - foundation: pass
  - transactions-ledger: pass
  - insights-dashboard: pass
  - budgets: partial (per-category month-bucketing edge case)
  - csv-io: pass

Children's diffs are merged into i2p/root/integration. The integrated worktree
is at <path>.

Your job:
  1. Run mcp__otto__verify against the FULL behavior_journeys list (all 10).
  2. If integration journeys fail (e.g., transactions don't appear in the
     monthly cashflow chart even though both children passed individually),
     EITHER fix the integration glue yourself, OR file a fix-task for the
     responsible child.
  3. Resolve any inter-child issues you find (test conflicts, naming
     collisions, missing integration code).
  4. Return verdict: pass | partial | unverified.
```

The integration Lead runs `verify` with no scope filter. The verifier launches
the dev server, runs all 10 journeys against the running app.

**Likely outcomes:**
- 7 journeys pass.
- `budgets_overspend` partial (consistent with budgets's verdict).
- `csv_export` and `csv_import` pass (csv-io worked).
- Maybe 1 integration journey fails: e.g., `monthly_cashflow` shows imported
  CSV transactions but doesn't include them in budget calculations because
  csv-io's path didn't update budget aggregations. This is an integration bug
  invisible to per-child verify.

The integration Lead either fixes the integration glue (small Edit) OR files a
fix-task for csv-io or budgets to handle. Returns verdict.

**🔍 Gap surfaced #8:** Plan-v5's integration Lead can EITHER fix glue itself
OR file a fix-task. The decision criteria is unspecified. **Action**: prompt
should be: "If the fix is small (≤50 LOC), do it inline. If the fix
substantively re-implements feature behavior, file a fix-task targeting the
responsible child."

**🔍 Gap surfaced #9:** When the integration Lead files a fix-task, what does
the fix-task look like? Is it a new top-level child of root, or a child of the
already-completed child? Plan-v5 doesn't say. **Action**: fix-tasks should be
SIBLINGS of the original child (children of the parent that triggered the
integration), not nested under the original child. This keeps the tree
shallow and the verdict propagation simple.

### T+24:00  Root verdict resolves

Integration Lead returns. Root's verdict aggregates from:
- foundation: pass
- transactions-ledger: pass
- insights-dashboard: pass
- budgets: partial
- csv-io: pass
- (root) integration: pass (after fix)

Root verdict: **partial** (budgets's edge case bumps the aggregate, even
though everything else and integration passed).

`merge_queue` rebases `i2p/root/integration` onto `main`. Clean. Final commit
on main. Proof packet rendered showing the tree.

User sees in MC:
```
ROOT: partial
├─ foundation: pass
├─ transactions-ledger: pass
├─ insights-dashboard: pass
├─ budgets: partial (per-category month-bucketing edge case)
├─ csv-io: pass
└─ integration: pass
```

User clicks budgets to see exactly what's edge-cased. Decides: ship anyway, or
file a follow-up "fix budgets month-bucketing" task. They are not blocked.

---

## What v5 fixes (vs v4's actual failure)

| v4 failure mode | What killed it in v4 | v5 status |
|---|---|---|
| `scope.critical` on browser-quality-contract | spec_compile invented contradictory ownership | **Fixed** — no contracts to violate |
| `amendment.rejected` x4 | Children couldn't widen scope to write their own tests | **Fixed** — no scope, no amendments needed |
| `group.attempt.failed` x11 | Doomed retries on contradictory contracts | **Fixed** — no doomed retries; first attempt succeeds for paths children own |
| 4 of 5 groups never produced merge | Cascading scope blocks | **Fixed** — children merge to integration branch independently |
| 37 minutes wasted, watcher SIGTERM | Doomed work + over-aggressive blocking | **Fixed** — work isn't doomed; integration handles cross-child issues |

---

## Other failures it does NOT fix automatically

1. **Self-attestation locator collision (finance-dash-claude):** A Lead writing
   both the brand link `Finance Dashboard` and the test `getByRole('link',
   name: 'Dashboard')` non-strict gets caught at audit time, not at write time.
   The verifier runs the journey, the journey fails on the strict-mode error,
   the verdict is `unverified`. Honest result, but the Lead may have already
   committed code with the bug. The user sees the failure in the proof packet
   and decides to repair.
   
   **Could v5 do better?** Yes, by making the test-writing capacity (or
   integration-audit capacity) literally a different agent session that
   doesn't share blind spots with the build agent. Plan-v5 keeps the build/
   test split optional — the Lead can dispatch a separate test-agent
   subagent if it wants. But it's not enforced. **Action**: lead.md prompt
   should encourage build/test separation when journeys involve UI selectors.

2. **Slow on small projects:** A 30-line CSV-to-JSON CLI runs through:
   spec_compile_flat (1 LLM call) → root Lead (`begin_inline`, builds
   inline, verify) → render. ~2-3 min, ~$0.20. Faster than today's i2p
   on the same project. Not faster than the radical-minimal v5 we discussed
   (no spec_compile at all), but spec_compile_flat is so light it's
   tolerable. **No action needed.**

3. **Cumulative INTENT.md across multiple tasks on the same project:**
   Out of scope for v5; documented in §6.

4. **Budget runaway on deep recursion:** Plan-v5 has `max_budget_usd` per
   task, but tree-level budget (the sum of budget across all descendants)
   isn't capped. A pathological Lead emitting 100 children each with
   $5 budget could spend $500. **Action**: add a `--tree-budget-usd` cap
   that the watcher enforces by refusing new dispatches when the cumulative
   cost across the tree exceeds the cap. ~30 LOC.

---

## Implementation gaps surfaced (final list)

In order of severity:

1. **Spec-compile journey lint** — reject implementation-language journeys.
   ~30 LOC in `spec_compile_flat.py` post-validate.
2. **`submit_subtask` auto-infers parent_task_id** — don't make the Lead
   pass it. ~5 LOC in MCP tool.
3. **Lead prompt explicit on `depends_on`** — guide the Lead to use deps
   when subtasks have ordering. Prompt content change in `lead.md`.
4. **Integration branch creation on demand** — watcher creates parent's
   integration branch when first child is spawned. ~30 LOC in watcher.
5. **Sub-Lead reads root spec for journey assignment** — explicit step in
   `lead.md`. Brittle; the integration audit at parent catches mismatches.
6. **Conflict resolution at child rebase** — already exists in merge_queue;
   plan-v5 just needs to say so. Documentation only.
7. **Integration Lead's small-vs-big-fix decision criteria** — prompt content.
8. **Fix-tasks are siblings of the original child, not nested** — schema/
   semantic decision, ~10 LOC in fix-task emission.
9. **`--tree-budget-usd` cap** — watcher enforcement. ~30 LOC.

Total surface area added by these gaps: ~150 LOC + several prompt revisions.
None are architecturally hard; all are local fixes to plan-v5.

---

## Verdict on the architecture

**Plan-v5 substantively fixes the v4 finance-dashboard failure.** The
specific bug (contradictory `shared_contracts` ownership) is gone because
there are no `shared_contracts` to be wrong about. The architecture's logic
flows naturally from intent → strategic decomposition → recursive Lead →
bottom-up integration → audit at every merge. The implementation is bounded
and grounded in code that exists today (`merge_queue`'s `target_branch`,
`audit.py`'s `feature_scope_ids`, queue's subprocess spawn pattern, in-process
MCP verified at depth 3+).

**The 9 gaps above are real but small.** Each is local: a prompt revision, a
~10-30 LOC fix in a specific module. None require redesign. They should be
fixed alongside Phase 1 implementation.

**Recommendation:** start Phase 1 with the 9 gaps explicitly listed in the
Phase 1 work items.

---

## Compressed walkthrough for two other shapes

### Trivial: "Convert CSV to JSON CLI"

```
T+0:   spec_compile_flat → 2 journeys (basic conversion, malformed input)
T+0:30 Root Lead: reads intent, calls begin_inline (one coherent task)
T+0:30 Lead writes csv2json.py + tests, runs pytest, calls verify
T+1:00 verify runs: pass
T+1:30 done. Root verdict: pass.
```

~$0.20, 2 minutes. **No decomposition overhead.** Lead correctly chooses
inline because the intent is one coherent unit.

### Brownfield: "Add SAML SSO to existing Django app"

```
T+0:   spec_compile_flat → ~3 journeys (SAML login flow, password login still works, IdP metadata loads)
T+0:30 Root Lead: reads existing code first (brownfield knob in prompt).
       Surveys auth/, settings.py, urls.py.
       Decides: this is one coherent change. Calls begin_inline.
T+0:30 Lead adds auth/saml.py, registers urls, adds tests.
T+5:00 Lead runs existing test suite (regression check) + new tests + verify.
T+6:00 done. Verdict: pass.
```

**No decomposition needed.** The Lead correctly inlines because brownfield
modifications usually don't warrant strategic splits. If the user wanted to
"replace auth entirely with multi-tenant SAML+OAuth+API-keys," that would
warrant decomposition; this targeted change doesn't.

### Browser engine: "Build a minimal browser engine"

```
T+0:   spec_compile_flat → ~10 journeys (parse HTML, render text, click link, run alert(), load remote page, etc.)
T+0:30 Root Lead: reads intent. Recognizes 6+ subsystems. Emits 6 children:
         - parser (HTML+CSS)         → depends_on=[]
         - dom                       → depends_on=[parser]
         - layout                    → depends_on=[dom]
         - paint                     → depends_on=[layout]
         - js-runtime                → depends_on=[dom]
         - networking                → depends_on=[]
         - gui-shell                 → depends_on=[paint, networking]
T+30 min:    parser children spawn (recursively decomposes into html-parser + css-parser)
T+8 hours:   leaves done; integration Leads at each subsystem
T+10 hours:  root integration Lead runs all 10 journeys end-to-end
T+10.5h:     verdict: partial (5/10 passing — first iteration of a browser is humbling)
```

**Recursive decomposition works.** Each level uses the Lead primitive. Wall
time is dominated by actual work, not by orchestration overhead. The
architecture scales.

---

## Summary

The tabletop confirms plan-v5's architecture is sound, surfaces 9 small
implementation gaps that should be fixed alongside Phase 1, and demonstrates
the design works across trivial, medium-greenfield, brownfield, and
deep-decomposition scenarios. The original v4 finance-dashboard bug is
structurally impossible in v5 (no contracts to violate). The scope-accountability
property from v4 is preserved via per-level audit. The progressive-decomposition
property from the company analogy is realized via the recursive Lead
primitive.

Recommend: start Phase 1 with the 9 gaps explicitly listed as Phase 1 work.
