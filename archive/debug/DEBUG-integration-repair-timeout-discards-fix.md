# DEBUG: integration-smoke repair discards a COMPLETED fix on agent timeout

Created 2026-05-18 (rrifix-040010 run, PID 65752 — terminal merge_blocked,
$20.07, 4188.6s; fix-7 e2e-PROVEN ×3 + CHARTER-validated, BANKED).

## Symptom (terminal)

Fresh `otto v5 run --tier modular` (iTracker). compile accepted attempt-2
(fix-4 ✓). emitted=5. Foundation v5-0c706245543f PASS+merged b378b5d
(fix-6 ✓). 3 feature children clean-merged: v5-2a9bf79c556a 385571f,
v5-d1b084cb1174 4d22e50, v5-26d44ac9b0f2 4bc8326 — ZERO recurrence of
the prior decomp-boundary class (fix-7 ✓ e2e ×3; CHARTER emitted
backend/backend/models/__init__.py + schemas/__init__.py as
leaf-extensible auto-import packages, models/<feature>.py +
schemas/<feature>.py under leaf_extension_globs — exactly fix-7).
v5-96c002559354 merge_blocked (NEW auth-middleware boundary,
foundation_contract_write_blocked backend/backend/middleware/auth.py;
non-structural fix-8 candidate; a FEATURE merge_blocked still allows
root partial).

integration → root → `preflight clean_deploy_start_failed [block]`:
frontend `npm run build` (`tsc -b && vite build`) exit 2 —
`Conversion of type 'NotificationsSlice' to type 'Partial<RootStore>'
... Index signature for type 'string' is missing in type
'NotificationsSlice'` + `src/features/notifications/ThemeToggle.tsx(5,8):
TS6133 'React' declared but never read`. otto's DESIGNED bounded
integration-smoke repair agent ran → `Agent timed out after 1199s` →
`integration root: merge_blocked` → terminal `Verdict: merge_blocked`.
No proof-packet.

## Root cause (REPRODUCED from real artifacts, not narrative)

**The repair agent SUCCEEDED; otto discarded its work because the agent
hit its 1199s per-agent timeout mid-confirming-oracle.**

- `git -C $DST status --porcelain` → 20 files ` M` (UNCOMMITTED, in the
  $DST working tree, left by the repair agent):
  - `frontend/src/features/notifications/ThemeToggle.tsx`: `-import
    React, { useEffect, useState }` → `-import { useEffect, useState }`
    (fixes TS6133).
  - `frontend/src/features/notifications/store.ts` (33 lines changed):
    refactored the broken `set((state) => ({...}) as Partial<RootStore>)`
    callback-form casts to `const current = _get() as unknown as
    NotificationsSlice; set({...} as Partial<RootStore>)` (fixes the
    TS2352 NotificationsSlice→Partial<RootStore> conversion).
  - +17 files: unused-import TS6133 sweep across cycles/issues
    components+routes, backend models/cycles.py, comments router,
    webhooks, test files.
- **`tsc -b --force` in $DST/frontend (with the agent's uncommitted
  edits) → exit 0, ZERO errors.** The frontend build IS fixed. The fix
  is complete and correct.
- repair turn-1 messages.jsonl (294 events) tail: agent finishes edits
  → `python3 -m py_compile ... → OK` → `assistant TOOL Bash` running
  `otto.cli clean-verify --json --verify-scope subtree --repair-packet
  ...` (the agent re-running the acceptance oracle ITSELF to confirm) →
  `phase_end {"phase":"build","duration_s":1204.244}`. The 1199s
  per-agent timeout fired WHILE the confirming clean-verify oracle was
  running, before it returned.
- `repair_packet.json`: `latest_oracle_result` / `attempt_history[0]`
  are BOTH the PRE-repair failing clean-verify (`_written_at
  11:50:10Z`, the run that found the NotificationsSlice/ThemeToggle
  failures). There is NO post-repair oracle result — the timeout killed
  the confirming run, so otto never evaluated the agent's completed fix.

So: agent produced a complete + oracle-passing fix, started the
mandatory confirming oracle itself, the 1199s agent-turn timeout cut it
off mid-oracle, and otto's integration-smoke repair declared
merge_blocked from the stale PRE-repair oracle result — discarding a
successful repair (left uncommitted, unverified).

This is the EXACT `feedback_verify_terminal_cause` lesson ("user caught
me pinning merge_blocked on .otto/ which the agent fixed in 65s — real
cause was a separate 1199s child-build timeout"): the upstream
store-slice↔RootStore type gap is real, but the repair agent CAN and DID
fix it; the DECISIVE terminal cause is otto declaring merge_blocked on
agent-timeout WITHOUT running the acceptance oracle against the agent's
produced state. agent-finished ≠ repair-failed — the ORACLE decides.

## Two candidate root-fixes (patches-to-protocols — step back)

- **(A) timeout too short:** 1199s (~20min) per-agent budget for the
  integration-smoke repair is smaller than (multi-file integration
  type-composition + unused-import sweep) + the agent's own confirming
  clean-verify oracle re-run (clean install + build + journeys). Bumping
  a timeout is the patch#N reflex — only valid if a real shared-runner
  flat-budget class is confirmed (decision-tree (9)); risks masking, not
  curing.
- **(B) otto discards a completed repair on agent-timeout instead of
  letting the oracle judge it (consistent-by-construction, NOT
  gate-weakening):** when the repair agent's turn ends for ANY reason
  (incl. timeout), otto must commit the agent's produced state and run
  the acceptance oracle ONCE against it before declaring
  merge_blocked/pass. The oracle is the source of truth; "did the agent
  finish within its turn budget" is not. This RUNS the oracle (does not
  weaken it) and would have PASSED here (proven: tsc -b --force clean;
  the agent itself had just launched that exact clean-verify). Higher
  leverage; cures the class.

(B) is the genuine root cause + correct fix. Precedent:
`feedback_verify_terminal_cause`, `project_clean_deploy_saga`,
decision-tree (9)/(10), patches-to-protocols.

## Status
- [x] Prior run rrifix-040010 PID 65752 confirmed TERMINAL (PID gone,
      Verdict: merge_blocked $20.07 4188.6s). Safe for FRESH.
- [x] fix-7 e2e-PROVEN ×3 + CHARTER-validated — BANKED.
- [x] Terminal cause REPRODUCED from real artifacts (git status/diff;
      tsc -b --force exit 0 on repaired tree; repair turn-1 tail =
      agent finished + started confirming oracle when phase_end
      1204.244s fired; repair_packet only pre-repair oracle).
- [x] Read code path: run_oracle_repair_agent loop — `call_agent`
      returns normally on timeout (partial transcript, not raise);
      `latest_oracle` reloaded from packet = STALE pre-repair fail
      (agent's confirming clean-verify killed before persisting);
      `budget_exhausted_reason(include_turn_limit=False)` →
      `wall_clock_exhausted` at the post-agent check PREEMPTS the
      controller-oracle invocation that follows — so the produced state
      is NEVER oracle-judged. Confirmed via RED regression:
      merge_blocked, oracle_invocations=0, files_changed=[produced fix].
- [x] Decided (B): the ORACLE decides repair success, not the
      agent-turn wall clock. Implemented fix-8 (6f650ac91): module
      const `_TIME_TURN_EXHAUSTION`; nested `run_controller_oracle()`
      (factored from the inline 1656-block); nested
      `final_oracle_then_block()` runs ONE final acceptance oracle on
      the produced worktree before blocking when reason ∈
      time/turn-exhaustion, oracle budget remains, worktree dirty,
      latest_oracle not already passed; on genuine pass → accept
      (commit + composite gate), else block with truthful post-repair
      result. Applied at BOTH budget-block sites (pre- and post-agent).
      NOT gate-weakening (guard test proves a still-failing oracle still
      blocks). Regression tests/test_v5_repair_final_oracle_before_block.py
      2/2 (red→green honest); 59 repair-loop tests pass; the single
      test_v5_step5 failure is PRE-EXISTING (identical on baseline
      692897a16, fix stashed), unrelated; ruff clean.
- [ ] FRESH otto v5 run validating fix-8 e2e: repair agent that
      completes a fix but exhausts wall-clock before its own confirming
      oracle persists must now have otto run ONE final oracle on the
      produced state → accept → integrate → clean_deploy → non-cold
      journeys → root ≥partial + proof-packet (NOT a discarded fix /
      false merge_blocked).
