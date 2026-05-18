# DEBUG: backend shared-but-feature-EXTENDED concern has no isolation seam

Created 2026-05-18 (fcfix-024555 run, PID 27712 — killed after confirmed
post-clean_deploy hang; terminal cause fully classified from real artifacts).

## Symptom (terminal)

Fresh `otto v5 run --tier modular` (iTracker). compile accepted attempt-1
(fix-4 ✓). architect emitted=5. Foundation v5-99df862963d8 PASS+merged
44458ae (fix-6 ✓ — foundation-contracts all-or-nothing validated e2e;
fix-5 validated too: 2de0b65257b2 b95ab65 + f2ee6e943db5 be65e95 merged
before the post-merge guards fired). Then ALL 4 FEATURE children
merge_blocked → integration→root → clean_deploy_ports_not_listening
(:56176 ws owned by blocked v5-f2ee6e943db5 = DOWNSTREAM symptom) →
doomed; otto then HUNG in post-clean_deploy bookkeeping (root stuck
pending_children, run.out frozen, zero session activity 25min — a
SEPARATE downstream otto bug: post-clean_deploy-block root-verdict never
flushes when all features blocked; follow-up, NOT this fix).

## Root cause (REPRODUCED from real artifacts, not narrative)

task_graph.json structured_reason, all 4 feature children, TWO faces of
ONE class:

**Face B — foundation seeds feature-owned per-resource files.**
`v5-2de0b65257b2` AND `v5-f2ee6e943db5`: BYTE-IDENTICAL
`integration union incomplete: backend/routers/cycles/router.py missing
line contributed by v5-99df862963d8: from fastapi import APIRouter /
async def list_cycles`. `v5-1dd64ee59a39`: merge-conflict
`backend/routers/workspaces/router.py` repair budget exhausted.
- `git show 44458ae:backend/routers/__init__.py` → a CORRECT
  auto-discovery loader (rglob `**/router.py`, leaf_edit:false, "Feature
  leaves add a new routers/<domain>/router.py — they do NOT edit this
  file"). The registry seam is RIGHT. (My earlier "hardcoded imports"
  hypothesis was FALSIFIED by this artifact.)
- BUT `git show 44458ae:backend/routers/cycles/router.py` → the
  FOUNDATION seeded a full 501-stub body (`async def list_cycles ...
  return {"detail":"Not implemented"}, 501`) for ALL 12 resources
  (`git ls-tree 44458ae` → auth/comments/cycles/issues/labels/
  notifications/savedviews/search/teams/users/webhooks/workspaces/ws
  each has a foundation-seeded router.py).
- These paths are feature-owned (CHARTER `feature_owned_paths`) AND under
  `leaf_extension_globs` (`backend/routers/*/router.py`). Foundation
  seeding them makes the foundation a CONTRIBUTOR to feature-owned files
  → D1 union guard requires the feature preserve the foundation's seeded
  lines (impossible when implementing the real router) → union-incomplete;
  and foundation-stub vs feature-rewrite → merge conflict. The
  auto-discovery loader rglobs — a missing router.py is simply not
  registered; the app still boots. **Seeding is unnecessary AND harmful.**

**Face A — backend data-model/schema layer is a monolithic
foundation_contract every feature must EXTEND.**
`v5-d27f8d5eb5c5` structured_reason `foundation_contract_write_blocked`,
violations: `backend/models.py` (owner v5-99df862963d8) and
`backend/schemas.py` (owner v5-99df862963d8). CHARTER L102-103 lists both
as `check:semantic` foundation_contracts. The "Core Issues" feature must
ADD its entities' models/schemas (Issue/Team/Label/Comment/Workspace) but
`models.py`/`schemas.py` are single foundation-owned files with NO
leaf-extension seam → write-blocked.

## Why ONE class (the contract-coverage gap in otto/prompts/lead.md)

lead.md (the architect IA contract, L70-160) DOES prescribe isolation /
composition-extension for: route/API/screen registration
(`registration_isolation` + auto-discovery — architect did this RIGHT);
shared TEST/BUILD infra (scaffold-owned foundation_contract); shared
cross-feature CLIENT RUNTIME STATE ("the scaffold MUST set up a
COMPOSITION/extension point ... features add ONLY their own slice ...
never edit the central store/hook"); and consumed shared modules
(`required_exports`). It explicitly calls client runtime state "the same
hazard as route registration."

Two precise gaps:
- **Gap A:** the composition/extension-point requirement is stated ONLY
  for the FRONTEND client-state case. It is NOT generalized to the
  BACKEND data-model/schema layer, which is the EXACT same hazard ("a
  shared module EVERY feature must contribute new definitions to"). So
  the architect emits monolithic `models.py`/`schemas.py`
  foundation_contracts → any feature adding an entity is write-blocked
  (d27f8). The fix already exists in spirit for routers (auto-discovery
  package) and frontend slices — it must be GENERALIZED to backend
  models/schemas (a package with per-feature `models/<feature>.py` +
  scaffold-owned aggregator/base, NOT a monolith every feature edits).
- **Gap B:** lead.md says feature paths must be NEW files under
  leaf_extension_globs and "never assign a foundation_contract or shared
  registry file to a feature" — but states NO converse rule forbidding
  the scaffold/foundation from PRE-SEEDING files that are feature-owned
  (under leaf_extension_globs / in feature_owned_paths). So the
  foundation seeds router.py stubs → it becomes a contributor to
  feature-owned files → D1 union-incomplete + conflicts (2de0/f2ee/1dd64).

Both faces = backend shared-but-feature-EXTENDED concerns lack the
consistent leaf-extension/composition isolation the contract already
(correctly) prescribes for routes + frontend state. This is
feedback_decomp_boundary_bugs + patches-to-protocols +
project_otto_v5_hierarchy_arbitration; the registration-isolation
campaign (plan-route-registration-isolation.md, step-1 IA object
implemented) did not cover model/schema extension or anti-seed.

## Root fix DECISION (consistent-by-construction, NOT gate-weakening)

Single seam: `otto/prompts/lead.md` IA contract (the architect emission
point), with detector enforcement in `otto/v5_capability_inventory.py`
if a machine-checkable predicate is warranted.

1. **Generalize the composition/extension-point rule** so it explicitly
   covers ANY shared module every feature must EXTEND with new
   definitions — naming the BACKEND data-model & schema layer alongside
   client state: such a layer MUST be a leaf-extensible package (e.g.
   `backend/models/` with scaffold-owned `__init__.py` aggregator/base
   declared in `registration_isolation.shared_registry_files` +
   `leaf_extension_globs` `backend/models/*.py`, features add their own
   `backend/models/<feature>.py`), NOT a monolithic foundation_contract
   file that every feature must edit. Same pattern the architect already
   applied to the router package.
2. **Add the explicit anti-seed rule:** the scaffold/foundation MUST NOT
   pre-create (seed) files that are feature-owned (in
   `feature_owned_paths` / matching `leaf_extension_globs`). The
   auto-discovery/composition seam MUST tolerate absent feature files
   (loader rglobs; missing = not registered; app still boots). Foundation
   owns ONLY aggregators/loaders/base + true shared scaffolding.
3. Regression: TDD red→green unit on the architect/compile IA-emission
   or the v5_capability_inventory detector — a CHARTER whose
   foundation_contracts include a backend model/schema monolith that
   ≥1 feature must extend, OR whose foundation scaffold seeds a
   feature-owned path, is rejected/repaired; a leaf-extensible
   models/ package + no foundation-seeded feature files passes. Extend
   `tests/test_architect_route_isolation_repro.py` if present.

NOT gate-weakening: the write guard / D1 union guard / merge stay exactly
as strict; we make the architect emit a structure where features never
need to edit foundation files and the foundation never contributes to
feature-owned files — so all three hold BY CONSTRUCTION.

## Status
- [x] Prior run PID 27712 confirmed hung (4 wakes, 25min no session
      growth, foregone) → killed by exact verified PID; safe for FRESH.
- [x] Terminal cause REPRODUCED from real artifacts (task_graph
      structured_reason ×4; git show 44458ae __init__.py + cycles
      router; CHARTER L84-202; lead.md L70-160). Two earlier hypotheses
      FALSIFIED by artifacts (router NOT in leaf_extension_globs; hardcoded
      __init__.py) — verify-before-claiming.
- [ ] Pin exact lead.md insertion points (after the CLIENT RUNTIME STATE
      bullet ~L108-120; near "never assign a foundation_contract..." ~L144)
- [ ] Decide lead.md-only vs lead.md + v5_capability_inventory detector
      (read the detector first).
- [ ] Implement fix + TDD regression + ruff + FRESH otto v5 run NOT resume.
