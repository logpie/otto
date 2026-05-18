"""Regression: foundation-contracts presence must not be all-or-nothing.

Root cause (scopefix-020916, 2026-05-18, foundation child v5-caf5ebace8c6
merge_blocked): the architect/foundation contract gate computes
``contracts_present = bool(_foundation_contracts_for_parent(...))``. When
graph metadata is empty (pre-persist) the function falls back to parsing
the scaffold's CHARTER.md. ``parse_foundation_contracts`` already EXCLUDES
malformed/rejected entries from its ``parsed`` list (it ``continue``s and
records a finding), so ``parsed`` holds only structurally-valid contracts.
But ``_foundation_contracts_for_parent`` did ``if parse_findings: return
contracts`` — discarding ALL successfully-parsed contracts because the
parser also emitted ONE advisory finding (e.g. a registry path using
check='semantic' that should be 'literal'). 11 valid foundation contracts
→ ``contracts_present: false`` → architect contract gate failed → bounded
retry 1/2, 2/2 → foundation merge_blocked → whole run structurally blocked.
The misleading "no contracts" signal also sent the re-dispatched architect
chasing a red herring instead of the 1-line check fix.

Fix: the fallback returns the successfully-parsed contracts even when the
parser also emitted advisory findings — ``contracts_present`` reflects
reality. NOT gate-weakening: ``parse_foundation_contracts`` never includes
malformed entries in ``parsed`` (it skips them), the gate's own
``_foundation_contract_findings`` independently re-validates the returned
contracts (and still blocks genuinely-bad ones), genuine emptiness / total
invalidity still yields ``[]`` (gate still blocks), and the advisory
findings are still produced by ``parse_foundation_contracts`` for callers
that surface architect feedback.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.v5_capability_inventory import parse_foundation_contracts
from otto.v5_runner import (
    _foundation_contract_findings,
    _foundation_contracts_for_parent,
)


def _charter(foundation_contracts: list[dict]) -> str:
    ia = {
        "registration_isolation": {
            "policy": "file-local",
            "shared_registry_files": [
                {"path": "backend/main.py", "leaf_edit": False},
                {"path": "frontend/src/store/index.ts", "leaf_edit": False},
            ],
            "leaf_extension_globs": ["backend/routers/*.py"],
        },
        "foundation_contracts": foundation_contracts,
        "feature_owned_paths": {"v5-feat": ["backend/routers/x.py"]},
    }
    return (
        "# CHARTER\n\n## Information Architecture Contract\n\n```json\n"
        + json.dumps(ia, indent=2)
        + "\n```\n"
    )


# A realistic mix: 3 structurally-valid contracts + 1 registry-semantic
# entry (path is a shared_registry_file with check='semantic') that the
# parser rejects-with-finding and EXCLUDES from `parsed`.
_VALID_PLUS_ADVISORY = [
    {"path": "backend/db.py", "owner_task_id": "v5-found", "check": "semantic"},
    {"path": "backend/auth.py", "owner_task_id": "v5-found", "check": "semantic"},
    {"path": "frontend/src/lib/api.ts", "owner_task_id": "v5-found", "check": "literal"},
    {"path": "frontend/src/store/index.ts", "owner_task_id": "v5-found", "check": "semantic"},
]


def _tasks(parent_extra: dict | None = None) -> dict:
    parent = {"task_id": "v5-parent", "task_role": "root"}
    if parent_extra:
        parent.update(parent_extra)
    return {
        "v5-parent": parent,
        "v5-found": {
            "task_id": "v5-found",
            "parent_task_id": "v5-parent",
            "task_role": "foundation",
        },
    }


def test_fixture_sanity_parse_yields_parsed_plus_finding(tmp_path: Path) -> None:
    """The fixture must reproduce the real condition: parsed non-empty AND
    an advisory finding (registry-semantic), with the bad entry excluded."""
    charter = _charter(_VALID_PLUS_ADVISORY)
    parsed, findings = parse_foundation_contracts(charter)
    assert len(parsed) == 3, [c.get("path") for c in parsed]
    assert findings, "expected the registry-semantic advisory finding"
    assert any(
        getattr(f, "kind", "") == "foundation_contracts_registry_semantic_rejected"
        for f in findings
    )
    # the rejected registry path is NOT in parsed
    assert "frontend/src/store/index.ts" not in {c.get("path") for c in parsed}


def test_parsed_contracts_present_despite_advisory_finding(tmp_path: Path) -> None:
    """The real failing case: graph empty → CHARTER fallback → parsed
    contracts MUST be returned (contracts_present true) even though the
    parser also emitted an advisory finding."""
    (tmp_path / "CHARTER.md").write_text(_charter(_VALID_PLUS_ADVISORY))
    result = _foundation_contracts_for_parent(tmp_path, "v5-parent", _tasks())
    assert len(result) == 3, [c.get("path") for c in result]
    assert {c["path"] for c in result} == {
        "backend/db.py",
        "backend/auth.py",
        "frontend/src/lib/api.ts",
    }
    # Gate's OWN validator finds these clean → gate would NOT block.
    assert _foundation_contract_findings(result) == []


def test_gate_not_weakened_total_invalid_still_empty(tmp_path: Path) -> None:
    """Every entry rejected → parsed empty → returns [] → gate still
    blocks on genuine absence (not weakened)."""
    all_rejected = [
        {"path": "frontend/src/store/index.ts", "owner_task_id": "v5-found", "check": "semantic"},
        {"path": "backend/main.py", "owner_task_id": "v5-found", "check": "semantic"},
    ]
    (tmp_path / "CHARTER.md").write_text(_charter(all_rejected))
    parsed, findings = parse_foundation_contracts(_charter(all_rejected))
    assert parsed == [] and findings  # both registry-semantic rejected
    result = _foundation_contracts_for_parent(tmp_path, "v5-parent", _tasks())
    assert result == []


def test_no_charter_returns_empty(tmp_path: Path) -> None:
    """No CHARTER.md → returns [] (genuine emptiness still blocks)."""
    result = _foundation_contracts_for_parent(tmp_path, "v5-parent", _tasks())
    assert result == []


def test_graph_metadata_takes_precedence_no_charter_consulted(tmp_path: Path) -> None:
    """When parent graph metadata already has foundation_contracts, those
    are returned as-is and the CHARTER fallback is NOT consulted (no
    behavior change for the already-persisted path)."""
    (tmp_path / "CHARTER.md").write_text(_charter(_VALID_PLUS_ADVISORY))
    persisted = [{"path": "backend/only.py", "owner_task_id": "v5-found", "check": "literal"}]
    result = _foundation_contracts_for_parent(
        tmp_path, "v5-parent", _tasks({"foundation_contracts": persisted})
    )
    assert result == persisted
