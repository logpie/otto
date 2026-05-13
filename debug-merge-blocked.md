# DEBUG: merge_blocked failures in v5 runs

## Bug statement

Recent v5 runs on complex products (itracker) end with root
verdict = `merge_blocked` due to sibling shared-file conflicts. User
challenged my prior analysis: "between these three experiments we have
commits that work well and reliably" — implying I was counting failures
wrong by treating any task=merge_blocked as a failed run.

Need to redo with the right metric: ROOT verdict, not per-task verdict.

## Phase 1: OBSERVE

### Root verdict distribution across ALL v5 runs (43 total)

```
pass               29 runs  ← 67%
pending_children    7 runs  (root planning OK, children still running on disk inspection)
catastrophic        3 runs  (crash before completion)
partial             3 runs
unverified          1 run
merge_blocked       0 runs  ← ZERO runs have root verdict = merge_blocked
```

### The crucial discrepancy

Today's run (`v5-itracker-postretry-093255`):
- Log final output: `Verdict: merge_blocked`
- Task graph root.verdict: `pass`

These come from two different sources:
- `task_graph.tasks["root"]["verdict"]` = what the integration agent
  set after its OWN session succeeded
- Log "Verdict:" line = aggregate computed via
  `aggregate_verdict()` in `task_graph.py`, which uses worst-wins
  severity: `catastrophic > merge_blocked > unverified > partial >
  pass`

When 2 children fail to merge upstream, root's STORED verdict stays
`pass` (integration agent succeeded on the partially-merged tree)
but the AGGREGATE rolls up to `merge_blocked`.

### Today's run is NOT an anomaly

Same pattern across many prior "successful" runs:

| Run | Root verdict | merge_blocked tasks |
|---|---|---|
| chat-decomp-012537 (5/10) | pass | 2 |
| chat-decomp2-031038 (5/10) | pass | 3 |
| chat-decomp4-164442 (5/10) | pass | 1 |
| finance-architect-224613 (5/10) | pass | 1 |
| chat-decomp5-174606 (5/11) | pass | 2 |
| finance-arbiter-190139 (5/11) | pass | 2 |
| batch-whiteboard-121938 (5/11) | pass | 3 |
| **itracker-postretry-093255 (5/13, today)** | **pass** | **2** |

The user is correct: these all "pass" at root level. The
merge_blocked-children pattern is chronic, not new.

### What changed in MY perception

I was reading the runtime LOG output (`Verdict: merge_blocked`) and
treating it as a hard failure. But that's the worst-wins aggregate,
not the same as "the run failed." Many historically-successful runs
displayed similar aggregate verdicts and still produced working
products.

### So what's the real bug then?

Two distinct questions, both worth examining:

1. **Observability bug:** the displayed `Verdict: merge_blocked`
   conflates two things — "integration session itself failed" vs
   "some children's work didn't merge upstream but integration ran
   on partial state." A user reading the log can't tell which.

2. **Lost work bug:** when 2 FE children get merge_blocked, their
   code IS lost from the integration branch. Whether this matters
   depends on whether their work was load-bearing for the final
   product. **Need to check what the integration result actually
   contained.**

### Empirical test of (2): is the work actually lost?

Examined the integration session's worktree
(`.worktrees/v5-f52d0dd6b028`):

- All 8 FE feature directories present: auth, cycles, inbox, issues,
  kanban, search, settings, workspace
- App.tsx contains routes for every feature: IssueList, KanbanBoard,
  CycleListPage, IssueDetail, SearchView, Inbox, all settings pages
- Integration verdict.json: all 13 journeys passed (with specific
  evidence — "merged from v5-30bfad0a52ef", etc.)

**(2) refuted.** The merge_blocked children's work was NOT lost. The
integration agent's Step 0b "recover merge_blocked siblings" path
(documented in `lead-integration.md`) ran: manually resolved
conflicts and got every child's work into the integrated branch.

### Conclusion of Phase 1

The "merge_blocked failure" I've been chasing all afternoon **isn't
a failure**. Today's run produced a fully functional product with
every child's work integrated. The displayed `Verdict:
merge_blocked` is misleading — it's the aggregate of stale per-task
verdicts, set *before* integration's recovery path overwrote them.

The actual bug is (1) — observability/aggregation, not lost work.

## Phase 2: HYPOTHESIZE (the real bug)

Now that the framing is corrected, the real question is: why does
the displayed verdict misrepresent the run's success?

### H1: `aggregate_verdict` reads stale per-task verdicts

- **Supports:** `task_graph.tasks["root"]["verdict"]` was `pass` but
  the displayed final line said `merge_blocked`. The two come from
  different sources. `aggregate_verdict` walks all task verdicts
  worst-wins; merge_blocked children dominate even though their
  work was later recovered.
- **Conflicts:** integration's recovery path SHOULD update those
  children's verdicts to `pass` after recovery. If aggregate is
  still seeing `merge_blocked`, something isn't propagating.
- **Test:** read the task_graph for today's run, look at the merge_blocked
  children. Are their verdicts still `merge_blocked` after integration
  recovery? Did the integration agent fail to call `set_verdict(child_tid,
  "pass")` after recovering them?

### H2: Integration's recovery is implicit (doesn't call set_verdict)

- **Supports:** Step 0b in `lead-integration.md` instructs the agent
  to merge the build_branch via git but says nothing about updating
  the child's verdict in the task graph.
- **Conflicts:** none yet.
- **Test:** grep `lead-integration.md` for "set_verdict" or similar
  mcp tool calls — does the prompt instruct the agent to update
  child task verdicts after recovery? If not, this is the bug.

### H3: The runtime print is using a different verdict source than task_graph

- **Supports:** the cli_v5 / v5_runner display logic might pull from
  a different source (e.g., V5RunResult.verdict in memory) that
  was computed before integration recovery.
- **Conflicts:** none yet.
- **Test:** read cli_v5.py for where "Verdict:" is printed; trace
  back to the variable's source.

**Ranked best test first**: H2 is the lightest test (read the prompt
file). If the prompt doesn't instruct recovery to update verdicts,
that's the structural gap — root cause confirmed.

## Phase 3: EXPERIMENT

### H1 confirmed

Read task_graph for today's run after completion:

```
v5-fc23c959c8cc: verdict=pass         (architect)
v5-adfa7af3ea72: verdict=pass         (backend)
v5-e9cdd347f08a: verdict=pass         (FE Auth)
v5-30bfad0a52ef: verdict=merge_blocked (FE Issues — recovered but verdict stuck)
v5-f52d0dd6b028: verdict=merge_blocked (FE Cycles — recovered but verdict stuck)
```

The merge_blocked children's work was recovered (their files are in
the integration worktree) but their verdicts were never updated.

### H2 confirmed

`lead-integration.md` Step 0b reads:

```
For each child with `verdict=merge_blocked`, the `recovery_hint`
field tells you which build branch holds the work.
1. `git merge <build_branch>` (named in `recovery_hint`).
2. If conflicts, resolve by hand — usually trivial.
3. Commit.
```

Three steps. None touches the child's verdict in task_graph. The
integration agent succeeds at git-level recovery but the system has
no mechanism to know that.

### H3 confirmed

`v5_runner.py:323`:

```python
result.verdict = aggregate_verdict(project_dir, ROOT_TASK_ID)
```

`aggregate_verdict` walks the whole tree with worst-wins severity.
Stale `merge_blocked` children at depth=1 dominate the parent's
`pass`, so the displayed final verdict is `merge_blocked` even
though the run was successful.

## Phase 4: ROOT CAUSE + FIX

### Root cause (one sentence)

When `lead-integration.md`'s Step 0b recovers merge_blocked siblings
via git merge, no one updates the recovered children's verdicts in
the task graph, so `aggregate_verdict` still reports `merge_blocked`
at the root and the displayed runtime verdict misrepresents
successful runs as failures.

### Proposed fix (option C — runner-side reconciliation)

Two competing fix shapes:

- **A. Prompt change.** Add to `lead-integration.md` Step 0b: "After
  successful recovery, call `mcp__otto__mark_child_recovered(tid)`."
  Requires a new MCP tool. Depends on agent compliance.
- **B. New MCP tool same as A.** Adds runtime surface area.
- **C. Deterministic runner reconciliation** (preferred). After the
  integration session completes successfully, the runner walks
  merge_blocked children, checks via `git merge-base --is-ancestor
  <child_branch> <integration_branch>` whether each was recovered,
  and updates the verdict to `pass` if so. No prompt change, no
  agent compliance dependency.

### Why this is the bug we actually need to fix

- Today's run "merge_blocked" verdict is misleading every user
  reading it. **I spent the entire afternoon misinterpreting it.**
- All the structural fixes I was proposing (owned_paths, fan-out cap)
  were targeting a problem that doesn't exist as I described it.
  Runs are succeeding; the system is just reporting them as failures.
- This is a ~30 LOC runner-side fix.

### What this means for our session arc

Several prior commits were aimed at preventing merge_blocked. They
still hold value — fewer auto-merge conflicts = less work for the
integration agent's recovery path. But the urgent priority I framed
(owned_paths, fan-out cap) was based on a false premise.
