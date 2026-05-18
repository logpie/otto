# DEBUG: fix8-orcl-052425 terminal analysis (do NOT commit)

Created 2026-05-18. Run: fix8-orcl-052425, PID 91936 (dead),
DST /Users/yuxuan/otto-projects/v5-itracker-fix8-orcl-052425.
Verdict: merge_blocked, cost $14.03, duration 6232.1s (~104min).

## Terminal cause (verified from REAL artifacts, not agent musings)

Last repair event = `type:"repair_escalated"`:
`reason: oracle_budget_exhausted`, `oracle_invocations: 7` (budget=3),
`final_oracle_result`= the **13:57:57Z** clean-verify (npm build ✓,
start.sh ✓, **ui_journeys FAILED** all 4: register_and_create_issue,
mention_triggers_inbox, cycle_velocity_burndown, saved_view_filter).

Exact code path (otto/v5_preflight_repair.py):
- repair budget: agent_turns=1, oracle_invocations=3, wall_clock_s=4800,
  provider_max_turns=200.
- 7 `oracle_run` events 13:10:20→13:57:57, all `source:'cli'`,
  passed:false — these are the AGENT's own `otto.cli clean-verify`
  self-invocations. `reconcile_replayed_usage` replays them into
  `oracle_invocations` → 7.
- Agent turn-1 ended `error_max_turns` (hit provider_max_turns=200) at
  14:08:12Z, ~3591s, $9.70.
- Post-agent line 1706 `budget_exhausted_reason(include_turn_limit=False)`
  → None (wall 3600<4800; idle/cost/churn null; turn-limit excluded).
- Line 1713 `oracle_invocations(7) >= budget(3)` → TRUE →
  `block_with_escalation("oracle_budget_exhausted")`.
- `final_oracle_then_block` (fix-8) was **never reached**; blocked on the
  stale 13:57:57Z oracle.

## Classification: decision-tree PATH 3 (honest fail) — NOT a fix-8 bug

Produced tree genuinely fails: 13:57:57Z oracle (agent's own, after 6
prior failing self-oracles) = all 4 journeys fail. Only post-13:57:57
change = ONE uncommitted line in RegisterPage.tsx:
`+ console.log('[verification_token]', result.verification_token)` — a
speculative debug log, NOT a fix, added while the agent was confusedly
grepping otto's source for the oracle's `__CONSOLE_TOKEN__` extraction,
then hit error_max_turns mid-confusion.

`Verdict: merge_blocked` is the CORRECT honest verdict. No completed
passing fix was discarded → the fix-8 false-discard scenario never
occurred → fix-8 neither validated nor refuted this run.

## Banked validations (strong, this run)

- fix-6 e2e✓: foundation v5-434e00a913e4 pass+merged 944401a/01e6de8.
- fix-7 e2e✓ + CHARTER-validated: shared_registry_files
  backend/{routers,models,schemas}/__init__.py leaf_edit=False;
  leaf_extension_globs models/*.py|schemas/*.py|routers/*/router.py;
  monolith backend/models.py|schemas.py foundation_contract=FALSE;
  foundation_contracts=[]; feature_owned_paths=4 ids. 3/4 features
  clean-merged (v5-afed3510769a, v5-108d3e5873b9, v5-eb1a09ec60fc),
  ZERO decomp-boundary recurrence. Decomp-boundary class robustly fixed.
- fix-5 e2e✓: 3 features passed child-verify scope; v5-8cab9bafe12b
  child-verify budget = known non-decisive fix-5-territory block.

## Finding A — genuine terminal cause (repair non-convergence)

Bounded integration-smoke repair agent did not converge on 4 hard
cross-feature journey failures in its single turn; burned ~60min/$9.70/
200 provider-turns including a rabbit-hole reverse-engineering otto's
OWN oracle harness instead of fixing product behavior. Repair-prompt/
scope defect. This is the decisive blocker to "otto builds iTracker
e2e", NOT a decomp/contract/gate/scope bug (those all worked).

Root-fix direction (consistent-by-construction, needs regression +
FRESH; do NOT patch#N reflexively): repair prompt must (1) forbid
reverse-engineering otto/the oracle/the harness — treat the oracle as a
black-box contract; (2) scope the agent to product behavior the journey
specs assert + the failure artifacts (screenshots/dom/console/network
under integration/journeys/.../<journey>/); (3) tighten the
fix→re-verify convergence loop. Regression = prompt-content assertion
(same pattern as test_lead_backend_isolation_contract.py /
test_v5_ia_contract.py).

## Finding B — latent fix-8-family coverage gap (did NOT bite here)

`oracle_invocations` (meant to bound CONTROLLER oracles) is consumed by
the AGENT's own `source:'cli'` clean-verify self-runs via
`reconcile_replayed_usage`. So a long single repair turn that self-runs
clean-verify >budget times makes the post-agent path block at line 1713
(`oracle_budget_exhausted`) BEFORE `final_oracle_then_block` can run a
final CONTROLLER oracle on the produced state. Same class as fix-8
(blocking on a stale pre-final oracle without judging the produced
state), via the oracle-budget exit fix-8 deliberately excluded.

Harmless THIS run (tree genuinely fails — a final oracle would honestly
fail anyway). Per loop discipline (only root-fix if it blocks root or
recurs ≥2 diverse runs; never weaken the oracle/repair-oracle gate;
patches-to-protocols not patch#N) → NOTE as fix-8-family candidate, do
NOT write a fix off one non-biting occurrence. The clean fix IF it
recurs: separate agent-self-invoked CLI oracle counting from the
controller oracle-invocation budget so the controller can always run
its final acceptance oracle.

## Status
- [x] Terminal fully root-caused from real artifacts (events journal,
      repair_packet, git status/diff, code trace 1300-1719).
- [x] fix-5/6/7 e2e-validated this run (banked).
- [x] fix-8 scenario did not occur (no discarded passing fix); honest
      merge_blocked = correct.
- [ ] Finding A root-fix (repair-prompt no-harness-reverse-engineering +
      product-scope) — deliberate, with regression + FRESH; NOT yet done.
- [ ] Finding B: monitor for recurrence; do not patch off 1 occurrence.
