# DEBUG: foundation-contracts all-or-nothing brittle predicate

Created 2026-05-18 ~02:40 (scopefix-020916 run, PID 1496).

## Symptom (terminal)

Fresh `otto v5 run --tier modular` (iTracker), validating fix-5
(725c27dce). compile accepted attempt-1 (fix-4 ✓). architect emitted=5.
Foundation child v5-caf5ebace8c6 build PASSED and merged (b6f64b7) — but
`architect v5-caf5ebace8c6 contract gate failed (attempt 1/2 → 2/2):
re-dispatching` then `contract gate failed after 2 retries; marking
merge_blocked`. Foundation merge_blocked → `integration → root` →
structural block (project_otto_structural_block_gap) → run doomed (no
proof-packet). fix-5 itself never refuted: child-verify scope never
exercised (caf5ebace8c6 passed cleanly; the architect *contract* gate is
a different gate).

## Root cause (REPRODUCED on the real artifact, not agent narrative)

`.venv/bin/python -c parse_foundation_contracts(<DST>/CHARTER.md)` →
`parsed_count=11, findings_count=1`. The one finding:

```
foundation_contracts_registry_semantic_rejected
foundation_contracts[8].check : frontend/src/store/index.ts is declared
in registration_isolation.shared_registry_files; route registries must
use check='literal'
```

`otto/v5_runner.py:_foundation_contracts_for_parent` (2021-2077)
resolution order: parent graph metadata (empty pre-persist) → foundation
child task metadata (empty pre-persist) → **CHARTER.md fallback parse**
(2062-2077). Line **2068-2069**:

```python
if parse_findings:
    return contracts        # contracts is still [] here
```

→ 11 valid parsed contracts DISCARDED because ONE of them has
`check='semantic'` where the validator wants `'literal'`. So
`contracts == []` → `contracts_present = bool([]) = False`
(v5_runner.py:4993/5032/5047) → architect/foundation contract gate fails
→ bounded architect-retry 1/2, 2/2 → foundation merge_blocked.

The misleading `contracts_present: false` (implying NO contracts when 11
exist) sent the re-dispatched architect chasing a red herring
("verdict.json missing top-level foundation_contracts", sessions
540bea/79161d) instead of the actual 1-line fix
(`foundation_contracts[8].check: literal`). 3 dispatches, never fixed,
merge_blocked.

This is the project_otto_brittle_predicate_campaign all-or-nothing
anti-pattern at the v5 architect/foundation contract gate (6th+ in that
campaign; the 2058-2061 comment shows prior fixes #6/#8/#10/#11 added the
CHARTER fallback for the SAME gate — patches-to-protocols: this needs the
predicate to be accurate, not patch #N).

## Root fix DECISION (consistent-by-construction, NOT gate-weakening)

`_foundation_contracts_for_parent`: when the graph/child metadata is
empty and the CHARTER fallback parse yields BOTH parsed contracts AND
findings, **return the parsed contracts** (they genuinely exist —
`contracts_present` must be truthful) instead of discarding them on
`if parse_findings`. The findings are independently surfaced for
architect feedback via `_foundation_contract_findings(contracts)` /
the gate's `contract_findings` (v5_runner.py 4994/5016/5033/5048) and
`parse_foundation_contracts`'s own finding path — so the quality nit
(`contract[8].check` should be `literal`) is STILL reported to the
architect as actionable feedback; we only stop the false "zero
contracts" that nukes the whole foundation. NOT gate-weakening: the
gate still fires on genuine emptiness / genuine findings feedback; we
make `contracts_present` reflect reality (11 contracts ARE present).

Only change behavior in the fallback-parse branch (graph/child empty);
no change when graph metadata already has contracts, when parse yields
zero parsed contracts, or when parse raises. Idempotent; safe for all
callers of `_foundation_contracts_for_parent` (the findings are still
computed downstream).

OPEN: confirm the architect-contract-gate path (v5_runner.py ~4820-4940)
consumes `_foundation_contracts_for_parent` so `contracts_present`
flows from it (it does at 4831/4865 → contracts arg → 4993/5032/5047).
Verify before coding.

## Status
- [x] Terminal cause REPRODUCED (parse → 11 parsed + 1 finding; line 2068 drops all)
- [x] Confirmed gate consumes _foundation_contracts_for_parent → contracts_present
      (v5_runner 4831/4865 + scheduler 5017/5036 `not contracts or contract_findings`);
      _foundation_contract_findings (2080) only flags missing path/owner/
      check∉{literal,semantic}/dup — does NOT re-derive the registry-semantic
      advisory, so the line-2068 fix alone is sufficient & not gate-weakening
- [x] TDD red→green: tests/test_v5_foundation_contracts_allornothing.py 5/5
      (red proved result==[]; green after removing the early-return);
      148 other tests pass; 1 PRE-EXISTING unrelated failure
      tests/test_build.py::test_run_build_records_contract_delta_without_blocking
      (fails identically on baseline 725c27dce; legacy build pipeline, orthogonal)
- [x] ruff clean; committed 1a59f651a (otto/v5_runner.py + new test only;
      NO Codex; co-authored; backtick-free)
- [x] doomed scopefix-020916 PID 1496 confirmed terminal (foundation
      merge_blocked → integration→root → clean_deploy_ports_not_listening
      WS:65279 unbound — foundation-owned WS blocked; partial product
      substantially built per project_otto_structural_block_gap); killed by
      exact PID
- [ ] FRESH validation: PID 27712 DST v5-itracker-fcfix-024555 launched
      02:46:23; Monitor bo3fnjlim persistent. Decisive: foundation/architect
      contract gate must now see contracts_present=true (a passed foundation
      child whose CHARTER has parsed contracts + advisory findings must NOT
      merge_block) → children build → child-verify scope (fix-5) → integrate
      (D1) → clean_deploy (R1) → non-cold journeys → root >=partial +
      proof-packet → INDEPENDENT verify → memory + auto/compile + P2.
