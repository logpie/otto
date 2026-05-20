# Architect Route Registration Isolation Plan

Written: 2026-05-16T05:44:08Z

## Goal

Stop newly generated multi-leaf webapp decompositions from dispatching into
known route-registration collisions. The architect scaffold must expose
file-local, auto-discovered registration so a feature leaf adds its own file
instead of editing a shared central registry.

## Plan

1. Add the contract vocabulary.
   - Update `otto/prompts/lead.md` so architect tasks require isolated
     backend/frontend route registration and a machine-readable
     `registration_isolation` object in the CHARTER IA JSON.
   - Add parser/validator support in `otto/v5_capability_inventory.py`.
   - Verify: focused tests assert the prompt contains the isolation
     requirement and malformed/missing IA isolation clauses are machine-visible.

2. Add the deterministic detector.
   - From the task graph, find multiple non-architect route-like leaves under
     the architect's parent.
   - Expand their `owned_paths` against the architect scaffold and find
     concrete files editable by more than one leaf.
   - Classify a shared file as a registry generically by CHARTER declaration,
     path/content route-registry signals, or route-like task context.
   - Verify: unit assertions show a monolithic shared registry produces
     `kind=shared_registry_not_isolated`; isolated per-feature route files pass.

3. Route violations through the existing architect repair path.
   - Run the check after architect clean-oracle success and before leaf
     dispatch/toolchain propagation.
   - On violation and remaining architect retry budget, call
     `clear_verdict_for_retry` and emit `architect_retry` with the structured
     reason.
   - On exhaustion, use `_record_task_merge_blocked_reason` with
     `origin=architect_contract` and the same structured reason.
   - Verify: the new repro observes `architect_retry`, the structured reason,
     and zero feature leaf dispatches for the monolithic case.

4. Add a fast repro.
   - `tests/test_architect_route_isolation_repro.py` creates a deterministic
     root decomposition with one architect and two route leaves.
   - The monolithic scaffold/CHARTER case must be rejected before leaves run.
   - The isolated scaffold/CHARTER case must allow both leaves to run and have
     no shared owned file overlap.
   - Verify: run the new test file, Pass 1 route-registration tests, critical
     seam tests, and the requested tier/lint/type gates.

## Gate Review Note

The project instruction requires `/codex-gate`, but no Codex MCP gate tool is
available in this session. This plan proceeds with deterministic red/green
tests and records that limitation here rather than faking the review trail.
