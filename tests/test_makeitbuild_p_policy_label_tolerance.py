"""Run #6 regression: registration_isolation.policy is a descriptive label,
NOT a fixed enum the architect must guess. A self-consistent CHARTER that
declares a sound structural isolation contract (shared_registry_files
scaffold-owned + leaf_edit:false, leaf_extension_globs) must pass even when the
policy string is the architect's own word.

Run #6 (mib6-001619, --tier modular): compile✓, decomposition✓ (emitted=5),
scaffold built+passed, then the architect contract gate FAILED both retries on:

    route_registration_isolation_contract_invalid @ registration_isolation.policy
    "must be one of file_local_auto_discovery, manifest_auto_compose,
     none_needed, plugin_auto_discovery"

because the architect wrote `"policy": "auto-discover"` with a `description`
fully explaining it (pkgutil router discovery + import.meta.glob) and a fully
valid structural contract. Each retry = a ~14min scaffold rebuild; run #6 only
converged after 2 retries (~28min wasted), blowing the <45min mandate. This is
the 4th shape of one anti-pattern (run#2 may_add, run#4 leaf-glob, run#5 layer
keys, run#6 policy enum): a rigid predicate rejecting the agent's correct
output.

Fix: the policy enum hard-finding is removed; only the structure is validated.
This pins run #6's real CHARTER + run #5's (different valid label) to PASS, and
keeps the genuine structural invariants enforced.
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_capability_inventory import _validate_registration_isolation_contract

_RUN6 = Path(__file__).parent / "fixtures" / "charter_architect_real_run6_policylabel.md"
_RUN5 = Path(__file__).parent / "fixtures" / "charter_architect_real_run5_layerkeys.md"


def _ia(fixture: Path) -> dict:
    from otto.v5_capability_inventory import parse_information_architecture_contract

    return parse_information_architecture_contract(fixture.read_text())


def test_run6_auto_discover_policy_label_passes() -> None:
    ia = _ia(_RUN6)
    assert ia.get("registration_isolation", {}).get("policy") == "auto-discover"
    findings = _validate_registration_isolation_contract(ia, require=True)
    assert findings == [], [(f.kind, f.detail) for f in findings]


def test_run5_different_valid_label_still_passes() -> None:
    findings = _validate_registration_isolation_contract(_ia(_RUN5), require=True)
    assert findings == [], [(f.kind, f.detail) for f in findings]


def test_any_label_accepted_when_structure_is_sound() -> None:
    ia = {
        "registration_isolation": {
            "policy": "totally-made-up-strategy-name",
            "shared_registry_files": [
                {"path": "backend/main.py", "owner": "v5-scaffold", "leaf_edit": False}
            ],
            "leaf_extension_globs": ["backend/routers/*.py"],
        }
    }
    assert _validate_registration_isolation_contract(ia, require=True) == []


def test_structural_invariants_still_enforced() -> None:
    # leaf_edit:true on a shared registry file is a REAL isolation violation.
    bad_registry = {
        "registration_isolation": {
            "policy": "auto-discover",
            "shared_registry_files": [
                {"path": "backend/main.py", "owner": "v5-x", "leaf_edit": True}
            ],
            "leaf_extension_globs": ["backend/routers/*.py"],
        }
    }
    findings = _validate_registration_isolation_contract(bad_registry, require=True)
    assert any("leaf_edit" in f.reference for f in findings), [
        (f.kind, f.reference) for f in findings
    ]

    # A multi-leaf decomposition that declares a shared registry but NO
    # leaf_extension_globs gives leaves nowhere isolated to add modules.
    no_globs = {
        "registration_isolation": {
            "policy": "auto-discover",
            "shared_registry_files": [
                {"path": "backend/main.py", "owner": "v5-x", "leaf_edit": False}
            ],
        }
    }
    findings = _validate_registration_isolation_contract(no_globs, require=True)
    assert any(
        "leaf_extension_globs" in f.reference for f in findings
    ), [(f.kind, f.reference) for f in findings]


def test_no_registry_no_globs_is_effectively_none_needed() -> None:
    # Single-leaf / no shared route registration → nothing to isolate, pass.
    ia = {"registration_isolation": {"policy": "n/a"}}
    assert _validate_registration_isolation_contract(ia, require=True) == []
