# Autonomous Overnight Plan — Recursion E2E → iTracker Capstone

_Owner: Claude (autonomous while user sleeps). Started 2026-05-15 ~01:45 PDT._

## Mission

1. Keep the round-6 debug E2E experiments running to completion.
2. When a root issue is found, dispatch **Codex** (workspace-write) to fix
   it, commit, relaunch the affected scenario.
3. Once experiments validate clean, dispatch the **iTracker** (Linear-lite)
   capstone E2E and verify it works smoothly end-to-end.
4. Have a clear written report ready at user wake.

## State at start

- Branch `cc-i2p-2`. Commits through `bb7155d65` (P0–P4 hardening +
  guardrail + forced scenarios).
- Round 6 driver PID 14732, run dir
  `/Users/yuxuan/otto-projects/field-tests/20260515-081853`.
- Monitor `beod5gka6` (depth-aware). `--parallel 2` → wave1 = 06+07,
  wave2 = 08+09.
- 07-lang-toolchain DONE: **depth=3, emit_tasks=2, verdict=partial**,
  all leaves pass. Recursion proven on CLI/no-boot-oracle case.
- 06-saas-platform running; has a `merge_blocked` child + a port_busy
  block — forensics pending (did it get verify/repair+oracle before
  blocking, or block cold = potential P0 regression).

## Per-scenario evaluation (run on each monitor DONE event)

For 06/07/08 (recursion): PASS if
- max task-graph depth >= 3 AND >=1 non-root task `decomposition==emit`
  with grandchildren;
- no `merge_blocked` at ROOT, no lost branch, no `unverified` leaf;
- (web scenarios) boot smoke HTTP 200.

For 09 (failure propagation): PASS if HONEST failure —
- the impossible export slice is NOT verdict `pass` and NOT merged to main;
- the reporting slice did not run/merge as if its dep succeeded;
- working slices (core API, UI) DID land on main;
- terminal verdict (no hang/loop), overall honest non-pass.

If a scenario shows a **root bug** (cold merge_blocked without repair
attempt; lost branch; cross-subtree branch leakage; false `pass`;
silent merge of broken work; infinite loop; traceback):
1. Capture concrete evidence (session dir, the failing event, file:line).
2. Dispatch Codex `mcp__codex__codex` sandbox=workspace-write,
   approval-policy=never: diagnose + fix root cause + regression test
   (red→green) + smoke tier green. Codex fixes Codex/Claude-found bugs.
3. Claude reviews the high-risk part of the diff against source.
4. Commit (worktree-safe: verify pwd+branch first).
5. Relaunch ONLY the affected scenario via run_field_tests.py
   `--scenario <name>` (driver argparse append fix is in).
6. Re-arm monitor, continue.

Do NOT fix Codex/Claude-found bugs myself — always Codex (CLAUDE.md).
Do NOT delete logs/evidence. Time-based budgets only, never USD.

## Gate: experiments "working" → dispatch iTracker

Round-6 DRIVER_DONE. Declare WORKING if:
- 07 passed (done ✓) AND >=1 of {06,08} hit depth>=3 clean
  (so recursion is proven on >=2 independent shapes incl. a web one);
- 09 demonstrated honest failure propagation (or its bug was
  Codex-fixed and the rerun did);
- no unresolved root regression outstanding.

If WORKING → launch iTracker. If NOT working after Codex fixes +
reruns (cap 3 fix/relaunch cycles per scenario), stop, write the
blocker analysis, do NOT launch iTracker (don't burn a 90-min run on
broken foundations) — leave it for user decision.

## iTracker capstone launch

- Canonical intent: copy from
  `/Users/yuxuan/otto-projects/v5-itracker-v6e-153729/intent.md`
  (full Linear-lite spec — workspaces/teams/issues/cycles/comments/
  auth/WebSocket/webhooks/keyboard UX).
- Fresh project dir:
  `/Users/yuxuan/otto-projects/v5-itracker-overnight-<HHMMSS>`,
  `git init`, write `intent.md`, minimal `otto.yaml`
  (`default_branch: main`, `provider: claude`,
  `run_budget_seconds: 5400`, `max_turns_per_call: 200`,
  `test_command: null`).
- Launch (background, nohup):
  `OTTO_ALLOW_REAL_COST=1 .venv/bin/otto v5 run "<intent>"
  --provider claude --budget 5400 --max-parallel 4 --tier modular`
  (modular = the architecture-first shape we hardened; iTracker is
  genuinely multi-subsystem so it should decompose deep — this is the
  real test of the post-hardening recursion/merge/verdict path on a
  production-grade product).
- Arm depth-aware monitor on its run dir. Evaluate E2E:
  spec compile → decomposition shape → child verdicts → integration →
  boot smoke (it has an HTTP surface) → final verdict; capture cost,
  wall, depth, any merge_blocked/lost-branch/unverified, any agentic
  repair that fired. Inspect proof/summary artifacts.

## Loop mechanism

- Monitor events drive per-scenario wakes.
- ScheduleWakeup heartbeat (~30 min) is the safety net: on each wake
  re-check driver liveness, re-arm any expired monitor, advance the
  gate logic, and re-schedule. Continue until: iTracker E2E evaluated
  OR hard blocker requiring user.
- Append a running log to this file (timestamped) so the trail
  survives context compaction.

## Running log

- 01:45 PDT — plan written. Round 6 wave1 in flight.
- 01:40 PDT — 07-lang-toolchain DONE: depth=3, emit=2, partial, all
  leaves pass. Recursion proven (CLI/no-boot-oracle).
- 01:42 PDT — 06-saas-platform DONE: depth=3, emit=2, partial.
  FORENSICS: backend-platform child recursed → 4 service grandchildren.
  Auth grandchild hit merge_blocked → git shows commit fa58a81
  "recover auth merge_blocked child" → became pass → merged up to main.
  **AGENTIC REPAIR CONFIRMED LIVE on a depth-3 tree.** Root partial
  caused by cross-run port zombies (my prior rounds), not an Otto bug.
  No silent broken merge, no lost branch, honest verdict. NOT a root
  bug — recursion+repair both validated. Candidate low-sev Codex item:
  task_graph.json still shows that grandchild=merge_blocked while git
  shows it recovered to pass (verdict bookkeeping staleness — honest-
  state principle; note, don't block iTracker on it).
- 01:42 PDT — killed 6 confirmed Otto field-test port zombies
  (worktree backends, otto-clean uvicorn/http.server) on 19000-19499.
  Range now clean so wave2 + iTracker don't inherit false port_busy.
- 01:42 PDT — wave2 live: 08-data-platform (4 children, 1 pass,
  dual-recursion test) + 09-failing-slice (4 children, failure-
  propagation test). Driver PID 14732 healthy @24min.
- Decision so far: recursion WORKING (07+06 depth=3, repair live).
  Awaiting 08 (dual-recursion) + 09 (honest failure) to clear the
  iTracker gate. No root bug requiring Codex yet.
- 01:57 PDT — 09-failing-slice DONE: verdict=merge_blocked, depth=2.
  **FAILURE-PROPAGATION VALIDATED (PASS).** 4 children:
  * core API (v5-1c8dd7211a9b): pass → landed on main ✓
  * UI (v5-1d97c1f8d821): partial; agentic repair fired (commit
    2e6f2a1 "start.sh now actually launches backend+frontend") →
    merged to main ✓
  * vault-export (v5-d460751c0577): verify/repair AGENT fired,
    remained partial, P0 gate "refusing upward merge" →
    merge_blocked. NOT silently merged ✓✓
  * reporting (v5-6173a6718b9c): merge conflict → conflict-repair
    AGENT dispatched ("Now I understand the conflict. Let me
    analyze") → still merge_blocked, NOT silently merged ✓
  Root: honest terminal merge_blocked (no false pass, no hang).
  Working slices on main; broken slices contained; 3 distinct
  agentic-repair dispatches observed (verify/repair, start.sh fix,
  conflict resolver). NOT a root bug.
  Nuance (not a bug): planner wired reporting→UI rather than
  reporting→export, so the exact "dependent-of-impossible-slice
  stays waiting" edge wasn't exercised as scripted; but containment
  + honest verdict + repair are thoroughly validated.
- iTracker GATE STATUS: 07✓ 06✓ 09✓ → gate already satisfied.
  Holding for 08 (dual-recursion) to finish + round-6 DRIVER_DONE
  before launching iTracker (avoid resource contention with 08).
- 02:09 PDT — Round 6 DRIVER_DONE. 08-data-platform DONE:
  depth=3, emit=3 (DUAL RECURSION structurally happened ✓),
  verdict=merge_blocked. Forensics found a REAL ROOT BUG (not
  honest containment):
  `merge_child_into_integration(<grandchild>) failed: checkout
  i2p/integ/v5-212ea51688a9 failed: fatal: '<branch>' is already
  used by worktree at '.worktrees/integ-v5-212ea51688a9'`.
  Nested-integration merge helper git-checkouts the subtree integ
  branch while it is legitimately bound to its own dedicated integ
  worktree → git one-branch-one-worktree violation. All 3
  grandchildren of v5-212ea51688a9 failed this way; the other
  subtree v5-e4696c23651d had 3/3 passing grandchildren but ALSO
  ended "Subtree integration remained 'partial'; refusing
  propagation" (likely same root cause at subtree→root level).
  Orphan v5-bc66f4349b3c (decomp=unknown verdict=None) likely a
  downstream symptom. This WOULD sink the iTracker capstone (deep
  nesting). iTracker gate BLOCKED on this fix.
- 02:10 PDT — dispatching Codex (workspace-write) to root-cause
  fix the nested-integration worktree/checkout bug + regression
  test + smoke; then re-run 08 to confirm before iTracker.
  ScheduleWakeup heartbeat continues.
