# DEBUG: child-verify repair scope gate — product-root coordinate mismatch

Created 2026-05-18 ~01:46 (setupfix3 run, PID 27946, DST v5-itracker-setupfix3-005823)

## Symptom

Fresh `otto v5 run --tier modular` (iTracker). compile accepted attempt-1
(fix-4 working). 4 children emitted. 2 merged cleanly
(v5-3c59c1fbda31, v5-5cd03bb97688 — D1 union-merge clean). Then:

```
✓ child v5-aca5509d9361: merge_blocked
child v5-13ba9d13c4a2 blocked before upward merge: Child verify/repair oracle did not pass: repair budget exhausted
✓ child v5-13ba9d13c4a2: merge_blocked
  integration → root
preflight clean_deploy_smoke_error [block]: oracle_infra_error: ui journeys require start.sh clean deployment
```

2/4 children blocked → integrated product half-built → start.sh clean
deploy fails → journeys never run → no proof-packet. Run doomed.

## Root cause (verified from real artifacts, NOT guessed)

`v5-13ba9d13c4a2` child-verify repair: every repair attempt rejected by
the composite landing gate (`pre_commit`) with:

```
kind: scope_violation
message: repair changed paths outside the allowed conflict scope
paths: ['itracker/backend/routers/workspaces.py']
```

`repair_packet.json` → `repair_unit.allowed_paths` (what the scope gate
enforced):

```
backend/routers/auth.py, backend/routers/workspaces.py,
backend/routers/teams.py, backend/routers/users.py,
backend/routers/webhooks.py, backend/webhook_service.py,
backend/tests/test_auth_workspace.py, frontend/src/features/auth-workspace/
```
→ **product-relative, NO `itracker/` prefix.**

git `changed_paths` (computed in the worktree, repo-relative):
`itracker/backend/routers/workspaces.py` → **`itracker/`-prefixed.**

`_path_allowed` (v5_preflight_repair.py:423-430) →
`path_matches_any_ownership_pattern(..., allow_literal_prefix=True)`
(path_ownership.py:9-40): matches only on exact / dir-prefix / glob.
`itracker/backend/routers/workspaces.py` vs allowed
`backend/routers/workspaces.py` → no match → scope_violation on EVERY
changed path → repair can never pass gate → `reason: budget_exhausted`
→ child merge_blocked.

### The canonical source IS correctly prefixed — the mismatch is otto-internal

CHARTER.md (architect's canonical IA), worktree root:
- L29: "All source code lives under `itracker/`. The worktree root
  contains this CHARTER and decisions.md only."
- IA `shared_registry_files`, `leaf_extension_globs`, `foundation_contracts`
  ALL `itracker/`-prefixed.
- `feature_owned_paths["v5-13ba9d13c4a2"]` =
  `itracker/backend/routers/auth.py, .../users.py, .../workspaces.py,
   itracker/backend/tests/test_auth.py, .../test_workspaces.py,
   itracker/frontend/src/features/auth/, .../settings/`
  → **correctly `itracker/`-prefixed.**

So CHARTER + scaffold + git all agree on worktree-relative
(`itracker/`-prefixed) paths. ONLY `repair_unit.allowed_paths` is in a
different coordinate system — AND a different path set than CHARTER
`feature_owned_paths` (allowed_paths has webhooks.py / webhook_service.py
/ test_auth_workspace.py / features/auth-workspace/ which are NOT in the
CHARTER map for this child). So `repair_unit.allowed_paths` is sourced
from the per-child task `owned_paths` (architect per-child IA emission /
decomposition), which is product-relative, NOT from the canonical
`itracker/`-prefixed CHARTER `feature_owned_paths`.

## Why this is a protocol-level bug (not a one-line patch)

Explore mapped ≥6 callers in v5_runner.py passing `allowed_paths` into
`_scaffold_repair_unit` (subtree propagation 3523, scaffold-oracle 3918,
child-merge 4768, conflict-marker 5218, nested-child 6982/7316). All feed
the same scope gate. The gate compares to git (worktree-relative). If
the per-child owned_paths coordinate system is product-relative, EVERY
subdir-nested product fails child-verify repair scope. Patching one
caller = patch #1 of N. Per patches-to-protocols + feedback_decomp_
boundary_bugs: fix the coordinate reconciliation at a single seam.

## Candidate root fixes (decide after pinning the seam)

- **(A) Normalize owned_paths → worktree-relative at the single seam**
  where per-child task `owned_paths` are derived/persisted, anchored to
  the product root the architect already declares (CHARTER
  feature_owned_paths is `itracker/`-prefixed; the scaffold dir is
  `itracker/`). Make repair_unit.allowed_paths share git's coordinate
  system by construction. Does NOT weaken the gate.
- **(B) Make the scope matcher product-root-aware**: reconcile
  changed_paths and allowed_paths to a common product root before
  comparison (strip/add the known product-subdir prefix). Single
  enforcement point (v5_preflight_repair.py). Must use the KNOWN product
  root (not a brittle "strip one segment" heuristic, not suffix-match —
  that would relax the gate).

Prefer the option that is consistent-by-construction and does not relax
the scope gate. Pin the owned_paths→repair_unit seam + architect IA
emission point first.

## Seam pinned (verified from code + artifacts)

- Enforcement seam: `_evaluate_composite_gate` `otto/v5_preflight_repair.py:565-628`.
  Compares `changed_since_baseline` (git, worktree-relative, `itracker/`-pref)
  vs `effective_allowed_paths = repair_unit.allowed_paths ∪ conflict_scope_paths`
  via `_path_allowed` (exact / dir-prefix / glob). NO coordinate reconciliation.
- `task_graph.json` persists `owned_paths: []` for tasks. `persist_feature_
  owned_paths_from_charter` runs at INTEGRATION time (v5_runner.py:5484),
  AFTER children. So at child-verify time `repair_unit.allowed_paths` =
  architect's INITIAL product-relative per-child decomposition scope
  (no `itracker/`), NOT the authoritative CHARTER map.
- The packet ALREADY carries the authoritative, worktree-relative source:
  `packet.product_contract["charter"]["text"]` (built by
  `_worktree_product_contract`, v5_runner.py:2818; attached to every
  repair packet at 3039/3476/3886). CHARTER `feature_owned_paths[task_id]`
  is `itracker/`-prefixed = SAME coordinate system as git changed_paths.
- Existing parser: `parse_feature_owned_paths_from_charter`
  `otto/v5_capability_inventory.py:1008-1066` (structured: parses the
  CHARTER IA ```json block, keyed by exact task_id — NOT prose regex).

## Root fix DECISION (consistent-by-construction, single seam, not gate-weakening)

In `_evaluate_composite_gate`, when `scope_policy == "allowed_paths"`,
reconcile to the authoritative worktree-relative coordinate system before
the scope comparison:

1. Parse CHARTER `feature_owned_paths` from `packet.product_contract
   ["charter"]["text"]` (reuse `parse_feature_owned_paths_from_charter`).
   Resolve THIS packet's task id (`repair_unit.task_id`, strip the
   `-child-verify`/`-*` repair suffix to the base `v5-...` task id).
2. If the CHARTER has an authoritative entry for this task, that
   worktree-relative set is the scope basis (it is the architect's
   authoritative ownership partition — stricter & correct; same
   coordinate system as git → no false scope_violation, no weakening).
3. Independently, derive the product-root prefix from the CHARTER IA
   (the common leading dir of the authoritative worktree-relative
   paths, e.g. `itracker`) and treat a changed path as in-scope if it
   matches an allowed pattern after reconciling that known prefix
   (covers the fallback where only the stale product-relative
   `repair_unit.allowed_paths` exists and CHARTER has no entry).
   This anchors to a KNOWN product root (structured CHARTER IA), NOT a
   brittle "strip one arbitrary segment" heuristic, and NOT a
   suffix-match (which would weaken the gate).
4. Pure helper in v5_preflight_repair.py (or v5_capability_inventory),
   no behavior change when CHARTER absent / paths already aligned /
   scope_policy != allowed_paths. Idempotent; safe for the other 6
   repair callers (same gate).

Rejected: (A) make architect emit worktree-relative initial owned_paths
— product root unknown at initial-decomp time, 6+ producer seams,
multi-patch (anti patches-to-protocols). (B-naive) suffix/segment-strip
heuristic — brittle-predicate anti-pattern + relaxes the gate.

## Status
- [x] Symptom captured from run.out + repair_packet + events.jsonl
- [x] Root cause verified (coordinate-system mismatch, canonical source correct)
- [x] Pin exact seam: enforcement = _evaluate_composite_gate; authoritative
      source = product_contract.charter feature_owned_paths (worktree-rel)
- [x] Chose consistent-by-construction fix at the single enforcement seam
- [x] TDD regression red→green (tests/test_v5_preflight_scope_coordsys.py
      5/5), ruff clean, 178 no-regression (1 PRE-EXISTING unrelated
      failure: tests/smoke/test_preflight_repair_fixtures.py::
      test_startup_port_cleanup_routes_to_packet_repair — fails identically
      on baseline dba18698c without this change; orthogonal, out of scope)
- [x] Committed 725c27dce (otto/v5_preflight_repair.py + new test only;
      NO Codex; co-authored; backtick-free)
- [x] setupfix3 (PID 27946) terminated merge_blocked (integration-root
      1199s timeout = downstream symptom of half-built product; confirms
      root cause, no new investigation)
- [ ] FRESH validation: PID 1496 DST v5-itracker-scopefix-020916
      launched 02:09:42; Monitor bye7530fo persistent. Awaiting:
      compile → children (child-verify repair = where this fix bites:
      scope_ok must hold for itracker/-prefixed changed paths) →
      integrate → clean_deploy → non-cold journeys → root >=partial +
      proof-packet → INDEPENDENT verify → memory + auto/compile + P2.
