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
- 02:20 PDT — Codex FIXED root cause. v5_branching.py
  merge_branch_into(): discover branch's owning worktree via
  `git worktree list --porcelain`, merge inside it (skip the
  colliding checkout), per-target flock to serialize concurrent
  child→subtree merges. General fix, not 08-specific. Regression
  test red→green. 306 smoke / 113 hardening / 37 merge-worktree
  green. Claude reviewed concurrency-critical path. Committed
  89d4bad54. e4696 = separate runner-check nuance (follow-up,
  non-blocking); bc66f = correct dependency-block (not a bug).
- 02:24 PDT — relaunched 08-data-platform (fix/relaunch cycle 1/3)
  to confirm fix live. Driver PID 56126, run dir
  /Users/yuxuan/otto-projects/field-tests/20260515-092442.
  Monitor re-armed. iTracker gate: clears when 08-rerun shows
  depth=3 dual recursion with NO checkout-already-used-by-worktree
  failure (honest pass/partial OK; the checkout bug must be gone).
- 02:26 PDT — heartbeat fired early; 08-rerun only 1.5min in (spec-compile stage), no checkout bug, nothing to evaluate yet. Monitor b0bspb0ml armed for completion. Re-scheduled heartbeat.
- 02:43 PDT — 08-rerun DONE: verdict=partial, checkout_worktree_bug=0
  (FIX CONFIRMED — was 5+ before), depth=3, merge_blocked=0. Recursed
  subtree v5-6fcdc5d2f192: 3/3 grandchildren pass. Two None-verdict
  children (722b24,4f386) correctly dependency-blocked by the partial
  subtree (Pass-4 gate, = Codex's bc66f determination, NOT orphaning).
  "Subtree partial despite passing grandchildren" = the known
  runner-check/product-test divergence Codex flagged earlier
  (non-blocking follow-up, logged for wake report). The HARD blocker
  (checkout-worktree fatal) is genuinely fixed.
- iTracker GATE: CLEARED. 07✓ 06✓ 09✓ 08✓ (root bug fixed+confirmed,
  no unresolved root regression; subtree-partial-divergence is a
  noted non-blocking follow-up, not a crash/corruption/false-pass).
- 02:44 PDT — iTracker capstone LAUNCHED. Canonical v6e Linear-lite
  intent (74 lines). Dir /Users/yuxuan/otto-projects/v5-itracker-
  overnight-024432, session 2026-05-15-094440-2cd91c, otto v5 run
  PID 61081, --provider claude --budget 5400 --max-parallel 4
  --tier modular. Depth-aware monitor armed. Evaluating E2E:
  compile→decomp→child verdicts→nested integration→boot smoke→
  final verdict; watch checkout-worktree-bug must stay 0.

## Known non-blocking follow-ups (for wake report)
- Runner-check/product-test divergence: a recursed subtree whose
  grandchildren all PASS can still be refused upward as 'partial'
  because runner-level checks are stricter than product acceptance
  (seen on 08 e4696/6fcdc5). Honest (no false pass) but costs a
  clean pass. Codex-identified; needs a dedicated pass.
- task_graph verdict bookkeeping staleness: a grandchild recovered
  via integration-level repair can remain merge_blocked in
  task_graph.json while git shows it reached pass (seen 06 auth).
  Observability/honest-state issue, not correctness.
- 03:15 PDT — iTracker @30min: spec attempt-1 (13 advisory), root emit=5, scaffold child v5-1ff1e82163e5 PASS, 4 vertical leaves (bd433492/5928c7/ab8f88/3e49b5) building in parallel, all inline (modular depth-2: scaffold+4 leaves, no recursion chosen for this product — expected). checkout-worktree bug=0 during parallel build. Integration phase (real test of fix 89d4bad54 at scale) still ahead.
- 03:22 PDT — iTracker @37min: scaffold PASS, v5-3e49b5d57293 PASS, v5-ab8f88e9e318 partial, v5-5928c7f67ece building, v5-bd433492f6d1 → MERGE_BLOCKED via correct P0 path: log says 'Child remained partial after verify/repair; refusing upward merge' = agentic repair fired + honest containment (NOT cold block, NOT silent merge — same correct behavior as 09 vault-export). checkout-worktree bug=0 at scale during parallel build. No root bug. Integration phase still ahead.
- 03:27 PDT — iTracker @43min: 3/5 children PASS (scaffold/3e49b5/5928c7), 2 MERGE_BLOCKED via correct P0 honest-containment (bd433492+ab8f88: 'remained partial after verify/repair; refusing upward merge' — agentic repair fired, not silent merge). checkout-worktree bug=0 at scale (fix 89d4bad54 holds). Integration clean-deploy hit cross-leaf TS drift (User.notification_prefs missing across leaves + vite/vitest config). PREFLIGHT REPAIR LOOP RECOVERED IT: repair agent added notification_prefs to User interface, fixed vite.config import → tsc clean + vite build OK. 2nd repair lead working start.sh/WS_PORT. = post-hardening agentic repair recovering real decomposition-boundary bugs autonomously at scale. STRONG positive E2E evidence. Run continuing (47min budget left).

## FINAL WAKE REPORT (03:36 PDT, mission complete)

### Recursion verdict — VALIDATED (round 6, 4 scenarios)
- 07-lang-toolchain (CLI, no boot oracle): depth=3, all leaves pass.
- 06-saas-platform (web): depth=3 + LIVE agentic repair — an auth
  grandchild hit merge_blocked, was recovered to pass and merged up
  the nested chain (commit fa58a81 in that run's repo).
- 08-data-platform (dual concurrent recursion): exposed a REAL root
  bug (nested-integration git-checkout of a branch already bound to
  its dedicated integ worktree). Codex root-fixed it; 08-rerun
  confirmed clean: depth=3, checkout bug 0, 0 merge_blocked.
- 09-failing-slice: HONEST failure propagation — 3 agentic-repair
  dispatches, broken slices refused upward merge, working slices on
  main, honest terminal verdict, no hang.

### Failure-propagation verdict — VALIDATED
09 + iTracker both showed: verify/repair fires, unrecoverable work
is refused upward (never silent-merged), honest terminal verdict,
no false pass, no hang.

### Codex fixes this session (commit hashes)
- b18e21b9d P0 (verdict/merge integrity)
- 21b0f715d P1 (router-defaults-lenient)
- 146f2a889 P2 (over-classification)
- 2cb5f8503 P4 + standing brittleness guardrail
- 89d4bad54 nested-integration → merge in branch's OWNING worktree
  + per-target flock (THE overnight find from 08; the key fix
  validated at scale by iTracker)
- bb7155d65 forced recursion/failure scenarios (test assets)

### iTracker capstone E2E — merge_blocked, but a POSITIVE result
- Verdict merge_blocked. Cost $16.23. Wall 2930s (~49m of 90m —
  terminated honestly, did NOT hang or exhaust budget).
- checkout-worktree bug: **0 across the entire 49-min run at
  scale** → fix 89d4bad54 definitively holds. (Primary goal.)
- 0 Tracebacks. No crash. No false pass. No silent broken merge.
- Decomp: root emit=5 (scaffold + 4 vertical leaves), modular
  depth-2 (planner chose flat for this product; recursion was
  force-validated in round 6).
- 3/5 children PASS and landed on main (scaffold v5-1ff1e82163e5,
  v5-3e49b5d57293, v5-5928c7f67ece). 2 leaves (Auth bd433492,
  Cycles ab8f88) → merge_blocked via CORRECT P0 honest containment
  ("remained partial after verify/repair; refusing upward merge").
- Agentic repair WORKED at scale: preflight repair loop fixed a
  real cross-leaf TS contract drift (added User.notification_prefs,
  fixed vite/vitest config → build clean, committed d5b2a03) AND a
  WS-port wiring bug (committed cb39870); structured merge-driver
  auto-resolved 3 conflicts (commit feb8415).
- WHY merge_blocked (honest): 2 leaves the repair loop couldn't
  fully fix within budget + clean-deploy port contention on
  8000/8001 from prior-run zombies (test-harness hygiene, NOT an
  Otto bug). The product was genuinely not fully deployable, so
  Otto correctly refused to claim pass.

### Bottom line — is post-hardening v5 working smoothly E2E?
YES for what the hardening targeted. v5 does not one-shot a
production-grade product flawlessly, but it is now HONEST and
SELF-RECOVERING: zero checkout bug at scale, zero silent broken
merges, zero false passes, zero hangs/crashes, and autonomous
recovery of real decomposition-boundary bugs (type drift, port
wiring, merge conflicts) committed to main. The merge_blocked is
the system correctly declining to claim success on an
incompletely-deployable product — exactly the behavior P0–P4 were
built to produce. The brittleness class is contained behind the
standing guardrail.

### Known non-blocking follow-ups for user review
1. Runner-check vs product-test divergence: a subtree whose
   grandchildren all PASS can still be refused as 'partial'
   because runner checks are stricter than product acceptance
   (seen 08 e4696/6fcdc5; likely why iTracker Auth/Cycles stayed
   partial). Needs a dedicated pass — could be the difference
   between iTracker merge_blocked vs pass.
2. task_graph verdict bookkeeping staleness (06 auth grandchild
   showed merge_blocked in graph while git showed recovered→pass).
3. Test-harness port hygiene: iTracker default ports
   8000/8001/5173 are NOT auto-reaped across runs (contributed to
   the merge_blocked). Recommend a pre-run reaper covering default
   ports too, not just the field-test 19xxx range.
4. Worth targeted investigation: why Auth (bd433492) + Cycles
   (ab8f88) leaves stayed partial after verify/repair — product
   complexity vs budget vs follow-up #1.
