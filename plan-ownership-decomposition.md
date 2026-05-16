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
  (default `feature`; architect/scaffold child = `foundation`) as a
  **typed first-class field**, not loose metadata.
- `foundation_contracts`: list of `{path, owner_task_id, check ∈
  {literal, semantic}, required_exports?, behavior_probes?}` — declared
  by the architect/scaffold task, stored on the **immediate
  decomposition parent task** (not hardcoded root — recursive decomp
  has sub-parents; reuse the existing `_parent_task_id_for_child`
  carrier the union guard already uses, `v5_runner.py:~869`). Root is
  just the root instance of that same carrier. Source: a
  machine-readable **Foundation Contracts** block the architect writes
  in `CHARTER.md` (extend the existing CHARTER IA /
  `v5_capability_inventory` parse path that handles
  `registration_isolation`). **CHARTER parse rejects `check=="semantic"`
  for any path that is a route registry** (registries must stay
  literal; allowlist only).
- **`record_task` must preserve unknown/extra metadata** (today it
  reconstructs from a fixed key set and drops unknowns,
  `task_graph.py:~155`) — add the typed fields AND stop dropping extra
  metadata, so a later `record_task` can't erase `task_role` /
  `foundation_contracts`.
- **`submit_subtask` duplicate idempotency** (today returns the old
  task and ignores corrected scope, `mcp_tools.py:~218`): on a
  duplicate with changed `task_role`/`owned_paths`, either update the
  compatible metadata or emit a structured stale-duplicate refusal —
  never silently keep stale bad scope (architect retry must be able to
  correct ownership).
- `submit_subtask` accepts optional `task_role`; lead/architect prompt
  (`otto/prompts/lead.md`, scaffold prompt) instructs: scaffold emits
  `task_role=foundation` and declares foundation_contracts; leaves are
  `feature`.

**Why:** validation alone is insufficient (architect-first ordering
already existed and still failed) — the system needs a first-class
ownership role to gate on.

**Verify:** `get_task`/`read_graph` round-trips `task_role` +
`foundation_contracts`; **re-recording a task preserves `task_role`,
`foundation_contracts`, and unrelated metadata**; **a duplicate
`submit_subtask` with changed role/owned_paths does not silently keep
stale metadata** (updates or structured-refuses); a CHARTER Foundation
Contracts block parses into the parent task metadata; **a registry path
declared `check=="semantic"` is rejected by the CHARTER parser**; no
existing `tests/test_v5_*`/`task_graph`/`subtask` test regresses.

## S1 — Foundation isolation gate + close create-anywhere loophole (v5 path)

1. **Scheduler-ordering enforcement** in the `_process_children`
   dispatch loop (`v5_runner.py:~2842`/`~3143`, the all-ready dispatch
   point): within a parent, do NOT dispatch any `feature` child while
   any sibling `foundation` task is pending/in-flight/unverified, or
   while the parent's `foundation_contracts` are absent/invalid — even
   if a lead forgot `depends_on` (prompts proven insufficient by the
   capstone). This is a scheduler invariant, not prompt guidance.
2. **Isolation gate** beside `check_route_registration_isolation`
   (`v5_runner.py:~2984`): before feature dispatch, assert every
   declared `foundation_contract` path is exclusively owned by its
   `owner_task_id` and no pending feature child's `owned_paths`
   overlaps/nests a foundation contract path or another active broad
   owner's tree. Violation → do NOT dispatch; re-enter the
   architect/foundation task (reuse existing bounded re-entry) with
   structured `kind=shared_foundation_not_isolated`; honest exhaustion →
   structured terminal (no crash, no silent dispatch). Bound = existing
   attempt bounds (no infinite re-enter on unsatisfiable architecture).
3. **Close the real v5 create-anywhere hole — at the v5 merge path, not
   only legacy build.py.** The capstone mechanism: `_merge_child_branch`
   calls `commit_worktree` which does `git add -A`
   (`v5_branching.py:~909`) BEFORE merge (`v5_runner.py:~4190`), so a
   feature child that *creates* `backend/auth.py` gets committed even
   though `detect_scope_violations` (legacy `build.py:~568`) never runs
   on the v5 path. Add a **v5 child pre-commit/pre-merge scope gate in
   `_merge_child_branch`**: compute the child's changed paths vs its
   `owned_paths` + the parent `foundation_contracts`; a created/modified
   file on a foundation-contract path the child doesn't own (or outside
   owned_paths when contracts are in effect) → structured block BEFORE
   `commit_worktree`/branch advance, routed per S2. Also tighten
   `detect_scope_violations` as a secondary defense (newly-created
   foundation path = violation), but the v5 gate is primary.

**Verify:** repro #1 GREEN; **a no-`depends_on` feature is held until
the sibling foundation passes and parent contracts are parsed**; **a v5
feature that creates a declared foundation path is blocked BEFORE
`commit_worktree` and the integration branch is NOT advanced**; existing
registration-isolation repro
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
machinery; emit `foundation_contract_amendment_repair`).

**Lifecycle (must be complete — not just "emit an event"):** the leaf
records `blocked_on_task_id=<amendment>` and must NOT become a terminal
non-runnable `merge_blocked` that nothing retries (terminal verdicts are
non-runnable via `_NON_RUNNABLE_VERDICTS`, `subtask.py:~140`). Define:
amendment task is scheduled & dispatched (a runnable task in the
graph), runs, lands; on amendment landing the original leaf is
re-enqueued / its block cleared and its merge retried. No graph
deadlock (leaf never waits on an amendment that is never scheduled).

**Repro adjustment (RED-first oracle fix):** repro #5's current
assertion is too weak (OR over event/task/marker). Strengthen it to
assert the **full lifecycle**: no leaf scope-gated repair call AND the
amendment task exists+is runnable AND after the amendment lands the
originally-blocked leaf is retried and merges. Adjust the repro as part
of this step (before the fix), keeping it RED now.

**Verify:** strengthened repro #5 GREEN (incl. leaf eventually merges
after amendment); **contract-amendment lifecycle test: amendment
scheduled→runs→lands→original blocked leaf retried/mergeable** (not just
an event exists); existing union-guard + seam repros
(`test_concurrency_recursion_seam_repros`, `test_critical_seam_repros`)
stay GREEN (no double-record / no new escape / no orphan task).

## S3 — Semantic union guard for foundation contracts (registries literal)

`_integration_union_missing_contributions` /
`_record_and_check_integration_union` (`otto/v5_runner.py:~722`,
contributed-line tracking `~684`): semantic mode is **narrow and
strong**, not "exports alone" (exports-superset can green-light dropped
behavior — losing reconnect/auth-token/event-fanout while still
exporting `connect`):
- Semantic check applies ONLY to contributions from the foundation
  `owner_task_id` or a `contract_amendment` task — NOT arbitrary leaf
  touches of the path (a non-owner leaf still gets exact line-union so
  it can't silently drop the owner's contribution).
- For a `check=="semantic"` foundation contract, "satisfied" requires
  `required_exports` present AND declared `behavior_probes`/invariants
  hold (a compatible superset of the owner's behavioral contract), not
  just symbol presence.
- All other paths — including route registries (parser-rejected from
  `semantic`) — keep existing exact additive line-union unchanged.

**Verify:** repro #4 GREEN incl. its registry control (registry still
requires exact line); **semantic negative test: a foundation contract
whose export exists but a required behavior/invariant is missing →
union still blocks** (no false green); **a non-owner leaf touching a
semantic contract path still gets exact line-union**; no regression in
`test_shared_route_registration_repro`.

## S4 — Split merge-conflict repair from whole-product clean-deploy

**Split "smoke" (detection) from "smoke repair" (the leaf repair
loop).** You cannot route a clean-deploy failure without detecting it,
so the contradiction is resolved by: after a scoped conflict repair,
`_merge_child_branch` may run a **non-repairing** integration smoke
(detection only) — but it must NOT enter
`_run_integration_smoke_preflight_with_repair`'s leaf repair loop
(`v5_runner.py:~4277`) for an out-of-scope/foundation failure. On such a
failure emit a correctly-owned `foundation_repair_needed` /
`integration_repair_needed` (routed per S2 lifecycle) instead of
widening the leaf into whole-product debugging / a 1799s session.
Repair-packet allowed paths stay scoped to the conflict; the
`v5_preflight_repair.py:~988` prompt must not demand the full
acceptance oracle from a leaf conflict repair.

**Repro adjustment (RED-first oracle fix):** repro #2 currently asserts
`smoke_calls == []` — wrong per the split (a non-repairing smoke is
legitimate). Change it to assert **no leaf repair loop / no
smoke-repair session is launched from the leaf conflict path** and a
correctly-owned repair-need is emitted; a detection-only smoke call is
allowed. Adjust the repro as part of this step (before the fix),
keeping it RED now.

**Verify:** adjusted repro #2 GREEN (no leaf smoke-repair loop,
correctly-owned repair-need emitted, not merge_blocked); **no
1799s-style repair session can be launched from a leaf conflict
repair**; existing conflict-repair tests stay GREEN.

## S5 — Clean-verify worktree isolation

`cli.clean_verify_command` (`otto/cli.py:~333`): resolve the project dir
as `OTTO_CLEAN_VERIFY_WORKTREE` (set by
`build_clean_verify_oracle_command`, `v5_clean_verify.py:~677`) **only
in repair/oracle context** — i.e. when `--repair-packet` /
`OTTO_REPAIR_PACKET_PATH` is present (or add an explicit CLI flag). A
plain manual `otto clean-verify` stays `Path.cwd()`-based, so a stale
`OTTO_CLEAN_VERIFY_WORKTREE` in the user's shell env (preserved by
`_serialized_oracle_env`, `v5_clean_verify.py:~316`) can't silently
verify the wrong project. The repro #3 fixture supplies a repair
context — adjust it to also pass the repair-packet/oracle signal so it
asserts the gated behavior, not bare env presence.

**Verify:** adjusted repro #3 GREEN (env honored in repair/oracle
context); **control: `OTTO_CLEAN_VERIFY_WORKTREE` set but NO repair
packet → still uses `Path.cwd()`** (manual CLI not regressed).

---

## Global verification

- **All 5** `tests/test_ownership_decomposition_repros.py` GREEN.
- Full prior seam/repair suites GREEN:
  `test_concurrency_recursion_seam_repros`, `test_critical_seam_repros`,
  `test_shared_route_registration_repro`, `test_architect_route_isolation_repro`,
  `test_runner`, `test_repair_gates`, `test_audit_loop_repair`,
  `-k "spec_compile or compile_spec"`. `ruff check otto/` clean.
- **Codex-added criteria (Plan Gate R1, merged):**
  - Re-recording a task preserves `task_role`, `foundation_contracts`,
    unrelated metadata.
  - Duplicate `submit_subtask` w/ changed role/owned_paths does not
    silently keep stale metadata.
  - A no-`depends_on` feature is held until foundation passes + parent
    contracts parsed.
  - A v5 feature creating a declared foundation path is blocked before
    `commit_worktree`; integration branch not advanced.
  - Contract-amendment lifecycle: amendment scheduled→runs→lands→
    original blocked leaf retried/mergeable (not just an event exists).
  - S4 distinguishes non-repairing smoke from a repair loop; no
    1799s-style session from a leaf conflict repair.
  - Semantic-contract negative: export present but required
    behavior/invariant missing → union still blocks.
  - Registry declared `check=="semantic"` rejected by parser.
  - Clean-verify: env without repair packet → cwd; with repair packet →
    worktree.
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

### Round 1 — Codex (REVISE) — all 8 incorporated

- [ISSUE] S1 missed the real v5 write path (`commit_worktree` `git add -A`
  before merge; legacy `detect_scope_violations` never runs on v5) —
  **fixed**: S1.3 now adds a primary v5 pre-commit/pre-merge scope gate
  in `_merge_child_branch`; legacy gate demoted to secondary.
- [ISSUE] S0 persistence: `record_task` drops unknown metadata;
  duplicate `submit_subtask` keeps stale scope — **fixed**: S0 now
  mandates typed fields + preserve-extra-metadata + duplicate
  update-or-structured-refuse.
- [ISSUE] Root-only contracts too narrow for recursive decomp —
  **fixed**: S0 stores on the immediate decomposition parent via the
  existing `_parent_task_id_for_child` carrier.
- [ISSUE] Ordering hazard (feature dispatched before foundation if lead
  forgets `depends_on`) — **fixed**: S1.1 is now a scheduler invariant
  in the dispatch loop, not prompt guidance.
- [ISSUE] S2 graph deadlock (terminal merge_blocked non-runnable;
  amendment landing doesn't retry leaf) — **fixed**: S2 now specifies
  the full lifecycle (blocked_on_task_id, runnable amendment,
  retry-leaf-after-landing) + strengthened repro #5.
- [ISSUE] S4 contradiction (no smoke vs detect post-merge failure) —
  **fixed**: S4 splits non-repairing smoke (detection, allowed) from the
  leaf smoke-repair loop (forbidden); repro #2 assertion corrected.
- [ISSUE] S3 semantic check too weak (exports-superset green-lights
  dropped behavior) — **fixed**: S3 narrows semantic to
  owner/amendment contributions + requires behavior_probes/invariants;
  non-owner leaves stay literal; registries parser-rejected from
  semantic.
- [ISSUE] S5 env regression (stale shell env mis-targets manual CLI) —
  **fixed**: S5 gates env honoring on repair/oracle context, not bare
  env presence.

Round-1 also corrected 2 banked RED repros (#2, #5) as RED-first oracle
fixes (refine correct-behavior assertion before the code) — folded into
S4/S2.

(Round 2 trail appended after re-review.)
