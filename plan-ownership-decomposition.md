# Plan — Ownership-first decomposition redesign

Root fixes for the 5 defect classes from the Claude+Codex dual audit
(research.md "Ownership-first decomposition redesign" + Codex review;
task #49/#50). RED repros banked: `tests/test_ownership_decomposition_repros.py`
(commit 8dece7c93, all 5 RED). Each step flips its repro GREEN.

**Principles:** runtime task-graph primitive (NOT flat schema v4 —
`owned_paths` originate at runtime via `submit_subtask`); reuse existing
re-entry/structured-reason machinery, no parallel channels; agent-native
(fail-fast → re-enter bounded loop with structured reason, never silent
or crash); minimal diff; registries stay exact-union (no over-correction).

Fix order is dependency-driven: S0→S1 unblock 1/4/5; S2,S3,S4,S5 then
independent.

---

## S0 — Runtime ownership primitive (foundation: enables S1–S5)

Add to the task-graph task record (`otto/queue/task_graph.py`,
`otto/queue/subtask.py`, `otto/mcp_tools.py:submit_subtask`):
- `task_role ∈ {foundation, feature, contract_amendment, integration}`
  (default `feature`; architect/scaffold child = `foundation`).
- `foundation_contracts`: list of `{path, owner_task_id, check ∈
  {literal, semantic}, required_exports?}` — declared by the
  architect/scaffold task, stored on the parent (root) task metadata so
  every sibling sees it. Source: a machine-readable **Foundation
  Contracts** block the architect writes in `CHARTER.md` (extend the
  existing CHARTER IA / `v5_capability_inventory` parse path that
  already handles `registration_isolation`).
- `submit_subtask` accepts optional `task_role`; lead/architect prompt
  (`otto/prompts/lead.md`, scaffold prompt) instructs: scaffold emits
  `task_role=foundation` and declares foundation_contracts; leaves are
  `feature`.

**Why:** validation alone is insufficient (architect-first ordering
already existed and still failed) — the system needs a first-class
ownership role to gate on.

**Verify:** `get_task`/`read_graph` round-trips `task_role` +
`foundation_contracts`; a CHARTER with a Foundation Contracts block
parses into root metadata (assert via a focused unit on the parse path);
no existing `tests/test_v5_*`/`task_graph` test regresses.

## S1 — Foundation isolation gate + close create-anywhere loophole

1. In `_process_children` (`otto/v5_runner.py:~2984`, beside
   `check_route_registration_isolation`): before dispatching any
   `feature` child, assert every declared `foundation_contract` path is
   exclusively owned by its `owner_task_id` and that no pending feature
   child's `owned_paths` overlaps/nests a foundation contract path or
   another active broad owner's tree. On violation: do NOT dispatch
   feature leaves; re-enter the architect/foundation task (reuse the
   existing bounded re-entry machinery) with a structured
   `kind=shared_foundation_not_isolated` reason; honest exhaustion →
   structured terminal (no crash, no silent dispatch).
2. Close the create-anywhere loophole: `detect_scope_violations`
   (`otto/build.py:~568`) must treat a newly-created file that lands on
   a declared foundation_contract path (or outside the child's
   owned_paths when foundation_contracts are in effect) as a violation,
   not an allowed new path.

**Verify:** repro #1
(`test_shared_foundation_contracts_block_feature_dispatch_after_architect_pass`)
GREEN; a second control: a leaf that *creates* `backend/auth.py` when it
owns only `backend/routers/` is scope-flagged (not silently accepted);
existing registration-isolation repro
(`tests/test_architect_route_isolation_repro.py`) stays GREEN.

## S2 — Shared-contract repair routing

In `_merge_child_branch` union-feedback path
(`otto/v5_runner.py:~4535`, `_record_and_check_integration_union` →
`_repair_child_upward_merge_gate_once`): when the union/conflict
feedback path is a declared `foundation_contract` that the child does
NOT own, do NOT route the repair to the leaf (its scope-gate will
always reject → deadlock). Instead create/record a
`task_role=contract_amendment` repair owned by the foundation
`owner_task_id` (reuse the existing structured repair-need + re-entry
machinery; emit `foundation_contract_amendment_repair`). Leaf merge
remains blocked with a structured reason pending the amendment, not a
silent/looping failure.

**Verify:** repro #5
(`test_shared_contract_union_feedback_routes_to_foundation_owner_not_leaf_scope_gate`)
GREEN; existing union-guard + seam repros
(`test_concurrency_recursion_seam_repros`, `test_critical_seam_repros`)
stay GREEN (no double-record / no new escape).

## S3 — Semantic union guard for foundation contracts (registries literal)

`_integration_union_missing_contributions` /
`_record_and_check_integration_union` (`otto/v5_runner.py:~722`): for a
path whose `foundation_contract.check == "semantic"`, replace literal
text-containment with a semantic check (all `required_exports` present /
exported-symbol+signature-compatible superset accepted). For all other
paths — including route registries — keep the existing exact additive
line-union unchanged.

**Verify:** repro #4
(`test_semantic_foundation_contracts_do_not_require_literal_line_union_but_registries_do`)
GREEN including its registry control (registry still requires exact
line); no regression in existing union-completeness repro
(`test_shared_route_registration_repro`).

## S4 — Split merge-conflict repair from whole-product clean-deploy

`_merge_child_branch` conflict path: a scoped conflict repair that
resolves only conflicted owned paths must NOT immediately invoke
`_run_integration_smoke_preflight_with_repair` (`v5_runner.py:~4277`)
inside the leaf. If post-merge clean-deploy fails on an out-of-scope /
foundation path, emit a separate `foundation_repair_needed` /
`integration_repair_needed` (correctly-owned) instead of widening the
leaf repair into whole-product debugging. Repair-packet allowed paths
stay scoped to the conflict (`v5_preflight_repair.py:~988` prompt must
not demand full acceptance oracle from a leaf conflict repair).

**Verify:** repro #2
(`test_merge_conflict_repair_does_not_expand_leaf_scope_into_clean_deploy_repair`)
GREEN (`smoke_calls == []`, repair-need event emitted, not
merge_blocked); existing conflict-repair tests stay GREEN.

## S5 — Clean-verify worktree isolation

`cli.clean_verify_command` (`otto/cli.py:~333`): resolve the project dir
as `OTTO_CLEAN_VERIFY_WORKTREE` (set by
`build_clean_verify_oracle_command`, `v5_clean_verify.py:~677`) when
present, before falling back to `Path.cwd()`.

**Verify:** repro #3
(`test_clean_verify_cli_uses_explicit_repair_worktree_env_instead_of_ambient_cwd`)
GREEN; a control with the env unset still uses `Path.cwd()` (no
behavior change when not in a repair context).

---

## Global verification

- **All 5** `tests/test_ownership_decomposition_repros.py` GREEN.
- Full prior seam/repair suites GREEN:
  `test_concurrency_recursion_seam_repros`, `test_critical_seam_repros`,
  `test_shared_route_registration_repro`, `test_architect_route_isolation_repro`,
  `test_runner`, `test_repair_gates`, `test_audit_loop_repair`,
  `-k "spec_compile or compile_spec"`. `ruff check otto/` clean.
- **Real-world acceptance (final, not iteration):** one
  `otto v5 run` iTracker capstone (log OUTSIDE the worktree;
  `--budget 7200 --max-parallel 4 --tier modular`) reaches a root
  verdict ≥ partial with shared foundation contracts (`backend/auth.py`,
  `frontend/src/lib/ws.ts`) authored once by the foundation task, no
  add/add on them, no leaf repair widening to whole-product timeout, no
  scope-gate deadlock.

## Risks / rejected alternatives

- **Rejected:** foundation ownership in flat schema v4 — the flat
  compiler emits no groups/owned_paths/shared_contracts; ownership is a
  runtime concern (Codex-confirmed). Hence S0 is a task-graph primitive.
- **Rejected:** validation-only (no `task_role`) — architect-first
  ordering already existed and failed; need a first-class role.
- **Risk:** S3 semantic check too lenient → real missing contributions
  pass. Mitigation: semantic check only for explicitly-declared
  `check=="semantic"` foundation contracts; everything else stays
  literal; registry control test guards over-correction.
- **Risk:** S1 re-enter loop on a genuinely-unsatisfiable architecture.
  Mitigation: bounded re-entry (reuse existing attempt bounds) → honest
  structured terminal, never infinite.

## Plan Review
(Plan Gate trail appended below.)
