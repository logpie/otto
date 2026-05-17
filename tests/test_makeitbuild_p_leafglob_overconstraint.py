"""Run #4 regression: the architect's self-consistent feature partition must
persist even when feature_owned_paths reference real files outside the (also
architect-authored, often narrower) registration_isolation.leaf_extension_globs.

Run #4 (mib4-231403) got further than any prior run — flat-compile ok,
decomposition ok (emitted 5), foundation built+passed+merged (P1 held) — then
the contract gate failed twice and entered the architect re-dispatch loop. The
correct-probe diagnosis: `parse_feature_owned_paths_from_charter` DID parse all
four feature children (7/7/8/9 paths) and 20 foundation contracts, but raised
`feature_ownership_contract_invalid` findings like:

    frontend/src/components/ui/Sidebar.tsx is outside
        registration_isolation.leaf_extension_globs
    backend/lib/mentions.js is outside ...
    frontend/src/store/notifStore.ts is outside ...

A real feature legitimately owns files in components/ui, lib/, store/ — but the
architect's leaf_extension_globs list was narrower than the partition it itself
derived from the scaffold it built. The hard finding made `persist_*` return
early, nothing persisted, contract gate failed → re-dispatch waste.

The leaf_extension_globs membership check was a redundant deterministic
over-constraint on the agent's own correct output. The invariants that actually
matter are still enforced: a feature path must not be a shared registry file
(checked here) and must not collide with a foundation_contract (enforced by
`_foundation_isolation_feedback`). This pins: run #4's real CHARTER parses with
ZERO findings, and shared-registry rejection is still alive.
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_capability_inventory import (
    parse_feature_owned_paths_from_charter,
    parse_foundation_contracts,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "charter_architect_real_run4_leafglob.md"


def test_run4_charter_persists_with_zero_findings() -> None:
    owned_by_task, findings = parse_feature_owned_paths_from_charter(_FIXTURE)
    # The exact run#4 failure: legitimate feature files outside leaf_extension
    # globs raised findings → persist aborted. Must be empty now.
    assert findings == [], [(f.kind, f.detail) for f in findings]
    assert len(owned_by_task) == 4, sorted(owned_by_task)
    for tid, paths in owned_by_task.items():
        assert paths, f"{tid} parsed to empty owned_paths"
    # The very files run#4 wrongly rejected must now be retained, not dropped.
    all_paths = {p for paths in owned_by_task.values() for p in paths}
    assert any(p.endswith("components/ui/Sidebar.tsx") for p in all_paths) or any(
        "components/ui" in p for p in all_paths
    ), sorted(all_paths)


def test_run4_charter_foundation_contracts_still_parse() -> None:
    contracts, findings = parse_foundation_contracts(_FIXTURE)
    assert findings == [], [(f.kind, f.detail) for f in findings]
    assert len(contracts) >= 1, "foundation contracts vanished"


def test_shared_registry_rejection_still_enforced() -> None:
    """Dropping the leaf-glob check must NOT weaken the real isolation invariant:
    a feature owning a declared shared-registry file is still rejected."""
    charter = """# CHARTER

## Information Architecture Contract

```json
{
  "registration_isolation": {
    "leaf_extension_globs": ["frontend/src/features/**"],
    "shared_registry_files": ["frontend/src/routes.tsx"]
  },
  "foundation_contracts": [],
  "feature_owned_paths": {
    "v5-aaaaaaaaaaaa": {"description": "x", "paths": ["frontend/src/routes.tsx"]}
  }
}
```
"""
    fixture = _FIXTURE.parent / "_tmp_shared_registry_probe.md"
    _ = fixture.write_text(charter)
    try:
        _owned, findings = parse_feature_owned_paths_from_charter(fixture)
        assert any(
            "shared registry" in f.detail for f in findings
        ), [(f.kind, f.detail) for f in findings]
    finally:
        fixture.unlink(missing_ok=True)
