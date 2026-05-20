# Checkpoint/Resume v2 — Systematic Broken-State Recovery

> **Status:** Draft for Plan Gate (Codex adversarial review)
> **Branch:** cc-i2p-2
> **Origin:** iTracker Opus broken-state case (2026-05-20). 3 of 4 children
> merge_blocked via upstream bugs now fixed. Validation cost is currently
> ~$150 fresh; this plan reduces it to ~$30 via targeted retry, and
> establishes a structural foundation for future broken-state recoveries.

---

## Goal

Make checkpoint/resume systematically handle the **broken-state recovery
loop** — the cycle where (1) a run produces a partial/blocked state,
(2) we discover an otto-side bug, (3) we fix the bug, (4) we want to
resume from where the run left off, exercising the FIXED code at the
appropriate phase. Today this loop is partially supported (Phase 1.2-A
covers integration-only resume) but doesn't cover per-child rebuild or
mid-phase recovery — exactly the case the iTracker Opus run exposed.

## Locked invariants (from [[project_v5_one_hard_gate_redesign]])

1. **Resume must never silently change verdicts** — explicit reset is
   required (CLI command or config).
2. **Resume must never re-execute work that already completed
   successfully** — pass children stay pass, foundation merges stay.
3. **Resume must be observable** — `otto v5 status` and a new
   `plan-resume` must accurately predict what resume will do BEFORE the
   user spends money.
4. **--fresh always wins** — explicit fresh-run overrides any
   checkpoint regardless of state.

## Phases

### Phase 1 — `retry-children` (Tier 1A) — [REVISED post Codex R2]

**File:** `otto/cli_v5.py` (new subcommand) + `otto/v5_retry.py` (new
helper, extracted shared logic).

**Spec:**
```
otto v5 retry-children --task <id> [--task <id> ...]
                       [--cascade-dependents] [--continue]
                       [--dry-run]
```

**Behavior** — **strict all-or-nothing** transaction (Codex R3#2):
prevalidate the fully-expanded retry set first; only proceed if every
target passes; commit all reset+pending-rewrite under one stable lock;
on any per-task error during reset, ROLLBACK (restore archived
sessions, restore pending entries, release lock).

1. **Validation gate** (full set, BEFORE any mutation):
   - Each task exists in graph.
   - Each is a leaf: `decomposition != "emit"` AND no `child_task_ids`.
   - Each `task_role` != `foundation`.
   - Each branch `i2p/build/<task_id>` exists.
   - Worktree branch identity matches; worktree clean (or `--continue`).
   - No live otto/agent PIDs in any target's worktree/session.
   - If any check fails → exit 2 with all failures listed. **No state
     change yet.**

2. **Dependency closure (R2#4):**
   - Compute the closure: any other task whose `depends_on` includes
     a target AND whose verdict is currently `pass`/`partial`.
   - If non-empty AND `--cascade-dependents` not set → refuse with a
     clear listing of stale-dependent tasks.
   - If `--cascade-dependents` set → add those tasks to the retry set
     (recursive closure); re-run step 1 on the expanded set.

3. **Acquire stable lock (R3#1):**
   - Single canonical path: `otto_logs/.locks/retry-children.lock`
     (NOT timestamped — must serialize concurrent retries).
   - Held until step 7 completes or rollback completes.
   - Released atomically on success or failure.
   - **Revalidate under the lock (R5#3) — TOCTOU defense:** between
     validation gate (step 1) and mutation, an external otto run or
     agent could have started. Immediately after acquiring the lock,
     re-check: live PID liveness (no process in worktree/session),
     worktree branch identity, worktree dirty status. If any check
     fails post-lock → release lock, exit 2 with explicit "state
     changed between validation and lock acquisition" message.

4. **Archive prior sessions** (all tasks, under the lock):
   - Locate each child's session dir (via worktree symlink reverse).
   - Rename to `otto_logs/sessions/<sid>.archived-<timestamp>/`.
   - On any rename failure → ROLLBACK (revert any prior renames).

5. **Atomic retry-reset** [REVISED — Codex R2#1,#2; R4#1,#3]:
   - Use a NEW helper `clear_task_for_retry(project_dir, task_id)`
     placed in `otto/queue/task_graph.py` (where `clear_verdict_for_retry()`
     lives) so it has direct access to the graph store. It:
     - clears `verdict`, `completed_at`
     - clears `merge_blocked_reason`, `merge_blocked_structured_reason`,
       `merge_blocked_origin`
     - clears `failure_reason`, `annotation_origin`, `annotation_detail`,
       `annotation_cause`, `annotation_structured_reason`,
       `landed_with_annotation`
     - sets `retry_count = (existing or 0) + 1`
     - sets `retry_reason = "cli_retry_children"`
     - sets `retry_initiated_at = <timestamp>`
     - sets `review_state = None` (NOT `""` — R4#1; `None` is the
       legitimate "no review state" signal across both the graph store
       and the pending-entry consumers).
   - `verdict=None` IS the runnable signal in `take_ready()` (Codex R1).
   - **Rollback safety (R4#3):** snapshot the graph BEFORE any mutation
     (deep-copy the relevant tasks' entries). On any failure later in
     the transaction (steps 6, 7 — pending rewrite, etc.), restore the
     snapshot before releasing the lock. Order of mutations is
     therefore: archive sessions (step 4) → snapshot graph → mutate
     graph (step 5) → mutate pending (step 6); any failure after step
     5 restores from snapshot.

6. **Pending-entry rewrite, not append (R3#4) — [REVISED R4#1, R4#2]:**
   - Add a NEW helper `rewrite_pending_for_retry(project_dir, task_ids)`
     in `otto/queue/subtask.py` (where `read_pending()` lives) that:
     - Reads all entries from `v5_pending.jsonl`.
     - For each task_id in the retry set: if an entry exists, set
       `verdict=None`, `completed_at=None`,
       **`review_state="approved"`** (R4#1: `take_ready()` only accepts
       `"approved"` or `None`; an empty string `""` makes the entry
       invisible to the scheduler and breaks the whole retry path),
       `retry_count=N+1`, `retry_reason="cli_retry_children"`. If no
       entry exists, synthesize one with required fields
       (`integration_branch` from task entry's recorded value;
       `parent_session_dir` from latest session; `intent` from task;
       `review_state="approved"`).
     - The rewrite **canonicalizes to one active entry per task_id**:
       if multiple stale entries exist (e.g., previous cancelled +
       previous reviewed), only the latest is rewritten as active;
       earlier ones get a `superseded=True` field that
       `read_pending()` excludes from the runnable set.
     - Atomically writes back (tmp-file + rename) under the same retry
       lock from step 3.
     - DO NOT just append — that trips duplicate-task preflight (R2#3).
   - **Keep the duplicate-task preflight strict (R4#2).** The rewrite
     guarantees one active entry per task; `superseded=True` entries
     are excluded by `read_pending()`. Do NOT loosen the preflight to
     accept duplicates because they are retries — that risks
     double-dispatch on the same branch/worktree.

7. **Trigger the scheduler** (NOT direct `_run_child`):
   - Print clear next-step:
     `otto v5 run "<original intent>"` to dispatch the retries.
   - The watcher picks up the retry-replacement entries (whose
     `verdict=None` now matches `take_ready()`'s runnable check) and
     dispatches via canonical `_process_children` → preflight → deps
     → dispatch → integration handling.
   - Optional `--auto-run` flag: spawn the run automatically using the
     persisted root intent (saved on the task graph at decompose-time).

**Edge cases (REVISED):**
- Multiple children specified → all reset atomically (lock held across
  all); scheduler dispatches in parallel respecting `--max-parallel`.
- Non-existent task → exit 2 with clear error before any state change.
- Already `pass` child specified → refuse unless `--force`; `--force`
  with a `pass` child does the same reset + relies on cascade-dependents
  to invalidate dependents (refuse without --cascade-dependents if any
  downstream pass exists).
- Foundation child → hard refuse (see validation gate).
- Inline-decomposed child (no separate branch) → hard refuse with
  message ("inline children share parent's worktree; use --fresh on
  the parent").
- Mid-flight otto process detected → refuse, point at `otto v5 status`.

**Verify (system-level, each bulletted check is concrete) — [REVISED per Codex R3#5]:**
- Verify: after `retry-children --task v5-X --dry-run` on the iTracker
  Opus project, the dry-run output lists exactly the worktree(s) /
  session(s) that would be reset, lists the new task state
  (`verdict=None, retry_count=N+1`), and reports estimated cost.
- Verify: after `retry-children --task v5-X` (no dry-run), `otto v5 status`
  shows the child's state as **`retry_pending`** (verdict=None,
  retry_count>0) — NOT "unverified" (which is a settled-but-no-evidence
  state per Codex R1). The child's previous session dir is renamed
  `.archived-<ts>`. The pending-entry rewrite is durable in
  v5_pending.jsonl.
- **Status command update:** `otto v5 status` rendering logic
  recognizes `verdict=None + retry_count>0` and labels it
  `retry_pending` (distinct from `unverified`).
- Verify: a subsequent `otto v5 run "<original intent>"` re-builds ONLY
  the specified child(ren), skips compile + decompose + other children,
  and dispatches integration.
- Verify: cost of validating today's fixes via `retry-children` on the
  3 iTracker Opus blocked children is < $50 (vs. $150 for fresh).
- Verify: refusing to re-run a `pass` child without `--force` returns
  exit code 2 and a clear message.

**Risks / open questions:**
- **R1 (dependencies):** what if a child depends on a sibling that's
  also being retried? The dispatch loop should honor `depends_on` per
  the task graph.
- **R2 (foundation drift):** if foundation drifted (e.g., a contract
  was amended after the original child build), retried child may build
  against a different foundation. Mitigation: warn if foundation's HEAD
  has moved since the child branched off.
- **R3 (session archiving):** if the old session has live data otto
  hasn't read yet (unlikely but possible mid-run), archiving could lose
  it. Mitigation: refuse if any otto process is currently running.

**Codex review request (for this phase):**
- Are the edge cases complete? What about a child specified that was
  inline-decomposed (no separate branch)?
- Is the "refuse foundation" rule the right call, or should
  `retry-children --task v5-foundation` work + cascade to all dependents?
- The dispatcher reuse via `_run_child` — does that function assume
  parent-driven dispatch, and does it break under direct CLI invocation?

---

### Phase 2 — `plan-resume` (Tier 1B from proposal)

**File:** `otto/cli_v5.py`

**Spec:**
```
otto v5 plan-resume [--json]
```

**Behavior:** read-only simulation. Outputs:
- Phase resume would re-enter (compile / decompose / build / integration).
- For each child: action (skip = pass | merge | rebuild | error).
- Estimated cost remaining vs. tree-budget-usd.
- Estimated wall time.
- Specific concerns surfaced (e.g., "child X has merge_blocked verdict
  from foundation_contract_write_gate origin — current code routes that
  to LAND, so retry-children would land it").

This is the "look before you spend $$" command.

**Verify:**
- Verify: on the iTracker Opus project, `otto v5 plan-resume` outputs
  "would re-enter at integration phase," lists the 3 merge_blocked
  children with "stays merge_blocked unless reset-verdict or
  retry-children fires," estimates cost < $30.
- Verify: with `--json`, the output is structured JSON consumable by MC
  or scripts (single source-of-truth schema for plan-resume).
- Verify: plan-resume on a `pass`-verdict root says "not resumable,
  use --fresh" without crashing.

**Risks:**
- **R4 (cost estimate accuracy):** integration cost is hard to predict.
  Mitigation: report a range (low/high/p50) instead of a single number.

---

### Phase 3 — Checkpoint-as-data-structure (Tier 2D from proposal)

**File:** `otto/v5_checkpoint.py` (new) + `otto/v5_runner.py` integration

**Spec:** introduce `otto_logs/sessions/<sid>/checkpoint.json` with
explicit fields:
```json
{
  "schema_version": 1,
  "phase_reached": "integration|child_build|decompose|compile",
  "sub_phase": "smoke|repair|post_agent|...",
  "budget_spent_usd": 205.40,
  "budget_cap_usd": 200.0,
  "started_at": "...",
  "last_updated_at": "...",
  "resume_safe_from": "integration",  // hint to resume logic
  "children_state": [
    {"task_id": "v5-X", "verdict": "merge_blocked", "branch_head": "...",
     "session_dir": "..."}
  ]
}
```

Why now: the current resume relies on inferring state from graph.json
+ live branches. That's brittle and undocumented. A single checkpoint
file makes resume logic + diagnostic commands trivially aware of
"what's the truth?"

**Behavior:**
- Written incrementally during a run (after each phase boundary).
- Resume logic prefers `checkpoint.json` over inferred state; falls
  back to inference if checkpoint missing (backward compat).
- `otto v5 status` and `plan-resume` read this file directly.

**Verify:**
- Verify: a `otto v5 run` writes a checkpoint.json at every phase
  boundary (compile, decompose, each child completion, integration).
- Verify: killing the run mid-integration and re-running picks up
  exactly where it left off per the checkpoint.
- Verify: `otto v5 status` shows the same phase info as
  checkpoint.json (no second source of truth).
- Verify: a missing checkpoint.json still works (falls back to
  inference, never crashes).

**Risks:**
- **R5 (write atomicity):** checkpoint must be written atomically
  (tmp-file + rename) to avoid mid-write crash leaving garbage.
- **R6 (schema drift):** every otto v5 release must bump
  schema_version + handle older versions gracefully.
- **R7 (privacy):** checkpoint may contain intent text — be sure
  it's still scoped to the project dir (no leak via `otto_logs/`
  paths to agent prompts; see [[feedback_otto_owned_leakage]]).

**Codex review request (for this phase):**
- Is incremental write needed (every phase boundary), or is start +
  end sufficient?
- Should the checkpoint be opaque (otto-private) or documented for
  external tooling?
- How should it interact with the existing implicit-checkpoint
  (graph + branches)? Drop or keep both?

---

## iTracker validation strategy (after Phase 1 lands)

After Phase 1 ships:
1. `cd /Users/yuxuan/otto-projects/itracker-cci2p2-opus-022205`
2. `otto v5 status` → verify state matches expectations
3. `otto v5 retry-children --task v5-83da4b4ba629 --task v5-133534052888 --task v5-f353f8ea8602`
   - Re-runs each of the 3 broken children with TODAY'S FIXED CODE
   - Expected: each builds successfully because the schema-check fix
     prevents the false demotion, and L4041 routing means even if a
     gate fires, it LANDs instead of refusing
   - Estimated cost: ~$10-25 per child × 3 = ~$30-75
4. `otto v5 run "<intent>"` (no --fresh) → resume into integration
   - Expected: 5 of 5 children merge to main, integration journey
     check identifies remaining real UI bugs, verdict = `partial`
   - Estimated cost: ~$5-15 for integration phase
5. **Total validation cost: ~$35-90** vs. ~$150 for fresh re-run.
6. **Empirical proof points:**
   - The 3 children that were merge_blocked now land as `partial` or
     `pass` (proves schema fix + chokepoint routing)
   - The final root verdict is `partial` (NOT `merge_blocked`)
   - Product boots on http://127.0.0.1:<port>/api/health

**Verify (validation-specific):**
- Verify: after retry-children + resume, `otto history` shows the run
  ended with verdict `partial` (not `merge_blocked`).
- Verify: `git log main` on the iTracker project shows merges for ALL
  5 children (not just 2).
- Verify: at least one of the 3 previously-blocked children's verdict
  changes from `merge_blocked` to `pass`/`partial`/`unverified` (any of
  these is acceptable; the fix's intent is "land, don't refuse").

---

## Non-goals (out of scope for this plan)

- "Replay with code patches" (Tier 2E) — too large; defer to a separate
  plan after Phase 3 lands the data structure.
- Snapshot-and-fork (Tier 2F) — same reasoning.
- Compile-phase resume — compile is fast (~10min) and rarely the
  bottleneck; not worth the complexity.

## Ordering / dependencies

```
Phase 1 (retry-children) ──┐
                            ├──► iTracker validation
Phase 2 (plan-resume) ──────┤    (cheap, fast confirmation)
                            │
Phase 3 (checkpoint.json) ──┘    (foundation for future, not blocking)
```

Phase 1 + Phase 2 in parallel. Phase 3 follows (or in parallel as
opportunity allows).

## Implementation plan summary

- Each phase is a single PR-sized commit on `cc-i2p-2`.
- Each phase has its own regression test.
- Each phase includes a `--dry-run` mode for safety.
- Each phase ships independent of the others (no flag-day cutover).

---

## Plan Review

### Round 1 — Codex (read-only)

8 substantive issues, summary + dispositions below.

- **[CRITICAL/FIXED in plan]** Issue 3: `partial` verdict alone is NOT
  enough for upward merge. The predicate at `v5_runner.py:3740` requires
  `verdict=="partial" AND review_state=="reviewed_partial"`. My
  prior L4041 fix demoted verdict via chokepoint but doesn't set
  `review_state`, so the work demonstrably still does NOT reach main.
  This means commit `6a3f20d61` is necessary but not sufficient.
  → **Plan now includes Phase 0: extend the predicate or extend the
  chokepoint's LAND path so annotated partials are upward-mergeable.**
  This is a prerequisite to retry-children producing landed work; no
  point retrying if the result still bounces.

- **[FIXED in plan]** Issue 1: `unverified` is non-runnable per the
  scheduler (subtask.py:195, 257). Reset state must use real retry
  semantics via `clear_verdict_for_retry()` — `verdict=None`,
  `completed_at=None`, `retry_reason`, `retry_count`.
  → Plan now specifies `clear_verdict_for_retry()` as the reset
  primitive. `reset-verdict` CLI (already shipped) needs to be
  documented as a different tool (sets a verdict; does NOT make a task
  runnable). May need rename or add `--for-retry` flag.

- **[FIXED in plan]** Issue 2: `_run_child` is not CLI-safe. Requires
  `integration_branch`, `parent_session_dir`, `intent` from a
  pending-queue entry; missing `integration_branch` writes
  merge_blocked at v5_runner.py:6636. Bypasses `_process_children`
  preflight/dependency/foundation/review handling.
  → Plan now uses the v5_pending.jsonl scheduler path (enqueue +
  watcher) instead of calling `_run_child` directly. Reuses canonical
  dispatch.

- **[FIXED in plan]** Issue 4: dependency cascade. Retrying upstream
  while leaving downstream `pass` ships stale dependents.
  → Plan now requires retry sets to be closed under unsatisfied
  dependencies. `--cascade-dependents` flag (off by default; refuses
  upstream retries that would leave dependents stale).

- **[FIXED in plan]** Issue 5: foundation/non-leaf refusal scope.
  Pre-fix scope was just `foundation`; should also include `non-leaf
  decomposition=emit` and `any task with child_task_ids`.
  → Plan tightened.

- **[FIXED in plan]** Issue 6: worktree races. `.worktrees/<task>`
  reuse without checking dirty/live/branch-identity.
  → Plan now requires: project retry-lock, reject live otto/agent PIDs
  on worktree/session, verify branch == `i2p/build/<id>`, refuse dirty
  worktrees unless `--continue`.

- **[FIXED in plan]** Issue 7: `plan-resume` must NOT duplicate runner
  logic. `status` already duplicated `_resume_root_from_checkpoint`
  and now gives misleading advice (`reset-verdict --to unverified` →
  not runnable per Issue 1).
  → Plan now requires extracting a pure `ResumeplanResult` helper used
  by runner, status, plan-resume — single source of truth. Also fixes
  the misleading advice already in `status`.

- **[FIXED in plan]** Issue 8: Phase 3 checkpoint incrementality is
  needed (not start+end). Atomic write avoids torn JSON but NOT
  split-brain between graph + branches + checkpoint.
  → Plan reframed: checkpoint = materialized view over append-only
  events (`checkpoint.events.jsonl`), with graph generation, branch
  HEADs, checksum, and strict fallback warnings on inconsistency.

### Validation additions (from Codex)

- Temp-repo E2E proving selected child rebuilds while pass siblings
  are skipped.
- Dependency cascade tests (upstream retry refuses without --cascade).
- Foundation/non-leaf refusal tests.
- Dirty/live worktree refusal tests.
- JSON `plan-resume` parity with runner decisions.
- **iTracker validation: run ONE child first with spend cap before
  all three.** The `$35-90` estimate is a gated range, NOT an
  acceptance criterion — verify-repair / foundation-clean-boot /
  merge-repair paths could blow it.

### Round 2 — Codex
- [CRITICAL/FIXED] R2#1: `_verdict_satisfies_dependency` (in subtask.py)
  also strict — fixed via canonical helper in task_graph.py used by
  all callers.
- [FIXED] R2#2: `clear_verdict_for_retry()` leaves stale blocker
  metadata — fixed via new `clear_task_for_retry()` helper.
- [FIXED] R2#3: `v5_pending.jsonl` rewrite semantics — fixed via
  `rewrite_pending_for_retry()` atomic helper.
- [FIXED] R2#4: chokepoint writes `landed_with_annotation` to task
  metadata, not result — fixed by reading from task entry.
- [FIXED] R2#5: stale Phase 1 body text — rewrote canonical 7-step
  body.

### Round 3 — Codex
- [FIXED] R3#1: timestamped lock = no serialization — fixed to stable
  `otto_logs/.locks/retry-children.lock`.
- [FIXED] R3#2: transaction semantics ambiguity — rewrote as strict
  all-or-nothing with rollback.
- [FIXED] R3#3: canonical helper placement — `task_graph.py` (not a
  new module; already foundational layer).
- [FIXED] R3#4: pending-entry rewrite specifics — atomic tmp+rename,
  superseded marking, locked under same retry lock.
- [FIXED] R3#5: "unverified" vs "retry_pending" labeling.

### Round 4 — Codex
- [FIXED] R4#1: `review_state=""` invisible to scheduler — use
  `"approved"` for pending entries, `None` for graph.
- [FIXED] R4#2: don't weaken duplicate-task preflight — canonicalize
  to one active entry via `superseded=True` marker.
- [FIXED] R4#3: rollback must include graph snapshot.
- Cleanup: stale `task_graph.py` reference for
  `_verdict_satisfies_dependency` — corrected.

### Round 5 — Codex: APPROVED
- [FIXED] R5#1: one more stale `task_graph.py` reference — corrected.
- [FIXED] R5#2: widening `_verdict_satisfies_dependency` requires
  updating ALL callers (not just `take_ready()`), including
  `v5_runner._build_decomp_runtime_context()`.
- [FIXED] R5#3: TOCTOU window between validation and lock acquisition
  — added "revalidate under the lock" step.
- "These are bounded implementation notes, not design blockers. The
  core plan is now coherent. APPROVED."

### Status: APPROVED at R5 (5 rounds, convergent 8→5→5→3→0 issues)

---

## Phase 0 (added 2026-05-20, from Codex review) — Centralize the "is this task satisfactory" predicate so annotated partials are accepted everywhere

**Critical pre-requisite for retry-children**. Without this, retried
children still won't reach main even after chokepoint routing, AND
downstream tasks that depend on a now-LANDED partial will still see
it as unsatisfied.

[Codex R2 — three sites need the fix, not two:]

**Files (all currently strict, all need the same treatment):**
- `otto/v5_runner.py:_child_result_allows_upward_merge:3974` —
  whether THIS child's result can merge upward
- `otto/v5_runner.py:_task_entry_allows_upward_merge:3730` — whether
  the persisted task entry says it can merge upward
- `otto/queue/subtask.py:_verdict_satisfies_dependency:201` (used by
  `take_ready()`) — whether a depended-on task counts as satisfied
  (R2#1: without this, downstream tasks don't unblock even after we
  merge the annotated partial). [Corrected location per Codex R3#3
  and R4 cleanup — `subtask.py`, not `task_graph.py`.]

**Current predicate** (lines 3737-3740):
```python
verdict = str(entry.get("verdict") or "")
if verdict == "pass":
    return True
return verdict == "partial" and entry.get("review_state") == "reviewed_partial"
```

**Problem:** annotated partials (set via chokepoint LAND path) have
`landed_with_annotation=True` but NOT `review_state="reviewed_partial"`,
so they don't pass.

**Fix options:**
- A. Extend predicate to also accept `landed_with_annotation=True`.
- B. Change chokepoint LAND path to also set
  `review_state="reviewed_partial"`.
- C. Drop the `reviewed_partial` requirement entirely (broadest, most
  aligned with "always LAND").

**Recommendation:** **Option A** — extend predicate. Keeps the
distinction between "human-reviewed-and-accepted partial" vs.
"otto-annotated partial," but allows BOTH to merge upward. The campaign
invariant says LAND; what makes the work safe to merge is the chokepoint's
cause analysis (it already routed away from INFRA_CORRUPT), so the
upward-merge predicate should mirror that.

**Helper placement (Codex R3#3):** put the canonical helper in a
neutral module to avoid dependency direction problems. `subtask.py`
(where `_verdict_satisfies_dependency` actually lives —
`otto/queue/subtask.py:201`, not `task_graph.py` as I previously
miswrote) imports from `task_graph`; `v5_runner.py` imports from
`subtask`. So the canonical helper lives in
**`otto/queue/task_state.py`** (new neutral module) or
**`otto/queue/task_graph.py`** if a new module is overkill —
`task_graph` is already the most-foundational layer in the queue
package. Recommendation: extend `task_graph.py`. Both `subtask.py` and
`v5_runner.py` already import from it.

The helper takes the **full task entry** (dict), NOT a
`(verdict, review_state)` tuple — `_verdict_satisfies_dependency`'s
current signature lacks `landed_with_annotation` and the blocker
fields, so widening its input is part of the fix.

**Spec:** introduce ONE canonical helper, used by all four sites
(per Codex R2: avoid drift between predicates):

```python
# in otto/queue/task_graph.py
def _entry_is_satisfactory_terminal(entry: dict) -> bool:
    """Single source of truth: 'this task is in a satisfactory terminal
    state for downstream consumers' — both upward-merge and
    dependency-satisfaction.

    Locked invariant: annotated partials (via chokepoint LAND path)
    count as satisfactory, NOT just pass + human-reviewed-partial.
    """
    if entry.get("blocked_pending_contract_amendment") or entry.get("blocked_on_task_id"):
        return False
    verdict = str(entry.get("verdict") or "")
    if verdict == "merge_blocked":
        return False
    if entry.get("merge_blocked_structured_reason") or entry.get("merge_blocked_reason"):
        return False
    if verdict == "pass":
        return True
    if verdict != "partial":
        return False
    return (
        entry.get("review_state") == "reviewed_partial"
        or bool(entry.get("landed_with_annotation"))
    )
```

Used in:
- `_task_entry_allows_upward_merge` → drop the bespoke logic, call this.
- `_child_result_allows_upward_merge` → reads from the **task entry**
  (not just `result.verify_result`) because the chokepoint writes
  `landed_with_annotation` to task metadata, not result payload
  (R2#4). Existing code reads from entry already; just call the helper.
- `_verdict_satisfies_dependency` (in `otto/queue/subtask.py:201`)
  → call this same helper so `take_ready()` unblocks downstream tasks
  when upstream is annotated-partial. **This is the R2#1 fix.**
  **All callers of `_verdict_satisfies_dependency` must be updated**
  (R5#2): not just `take_ready()`, but also
  `v5_runner._build_decomp_runtime_context()` which currently calls it
  with a `(verdict, review_state)` tuple. The widened signature takes
  the full task entry; updating all callers is part of Phase 0.

**Verify (Phase 0) — REVISED to cover all three predicates:**
- Verify: unit test — a task entry with `verdict="partial"` +
  `landed_with_annotation=True` returns True from each of:
  `_entry_is_satisfactory_terminal`, `_task_entry_allows_upward_merge`,
  `_child_result_allows_upward_merge`, `_verdict_satisfies_dependency`.
- Verify: unit test — a task entry with `verdict="partial"` +
  no `landed_with_annotation` + no `review_state` returns False from
  all four (no regression on the existing strict path).
- Verify: unit test — `verdict="merge_blocked"` returns False from
  all four regardless of other flags (HONEST_TERMINAL is honored).
- Verify: scheduler unit test — `take_ready()` returns a downstream
  task whose `depends_on` upstream has `partial + landed_with_annotation=True`.
  Pre-Phase 0 this returns nothing; post-Phase 0 it returns the
  downstream task. **This is the R2#1 acceptance test.**
- Verify: source-level test — none of the three call sites (`_child_result_…`,
  `_task_entry_…`, `_verdict_satisfies_dependency`) duplicate the
  predicate logic; all delegate to `_entry_is_satisfactory_terminal`.
  (Ratchet against future drift, per the user's centralization concern.)
- Verify: after re-running iTracker (after Phase 1 ships) with
  Phase 0 in place, the 3 previously-blocked children's branches
  DO get merged into main (`git log main` shows their merges).

**Risks:**
- **R0a (over-permissive):** what if a real merge_blocked child's
  metadata sets `landed_with_annotation=True` accidentally? Mitigation:
  the chokepoint is the only writer of `landed_with_annotation`; verify
  via grep that no other code path sets it. If there are other writers,
  this fix is unsafe and we need Option B instead.
- **R0b (existing tests):** there may be tests that assert the strict
  predicate behavior. Need to find + update.

**Codex re-review item:** is this the right place to fix it, vs.
extending the chokepoint to set both `landed_with_annotation=True` AND
`review_state="reviewed_partial"` simultaneously (Option B)? Option B
might cause confusion downstream (why is review_state set without
review?). Asking for verification.

---

## Updated Phase ordering

```
Phase 0 (upward-merge predicate) ──┐
                                    ├──► retry-children produces landed work
Phase 1 (retry-children) ──────────┤    iTracker validation cheaply
                                    │
Phase 2 (plan-resume)              │    can ship in parallel with Phase 1
Phase 3 (checkpoint events)        │    foundation, follows Phase 1+2
```

**Phase 0 is blocking for Phase 1 to have meaningful validation.**

