"""P2 regression: the CHARTER the architect ACTUALLY produces must parse.

Run #2 (mib2-224025) proved P2's prompt side works — the architect authored
`feature_owned_paths` keyed by real feature task_ids — but the persist parser
only read the literal key ``owned_paths`` while the architect naturally emits
``{task_id: {"description": ..., "may_add": [...]}}``. Result: every feature
parsed to [] → contract gate failed → architect re-dispatch loop (the exact
waste P2 was meant to kill). This pins the parser to the real architect output.
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_capability_inventory import (
    parse_feature_owned_paths_from_charter,
    parse_foundation_contracts,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "charter_architect_real.md"


def test_real_architect_charter_feature_owned_paths_parse() -> None:
    owned_by_task, findings = parse_feature_owned_paths_from_charter(_FIXTURE)
    # No findings: the architect's natural `{may_add: [...]}` shape is accepted.
    assert findings == [], findings
    # All four feature children resolved to their derived paths.
    assert set(owned_by_task) == {
        "v5-ec6a245d76bf",
        "v5-8790a6b95ead",
        "v5-5e9904afe378",
        "v5-10d588cb7026",
    }, sorted(owned_by_task)
    for tid, paths in owned_by_task.items():
        assert paths, f"{tid} parsed to empty owned_paths (the run#2 bug)"
    # Spot-check a known derived path survives extraction.
    assert any(
        p.endswith("backend/routers/issues.py")
        for p in owned_by_task["v5-ec6a245d76bf"]
    ), owned_by_task["v5-ec6a245d76bf"]


def test_real_architect_charter_foundation_contracts_parse() -> None:
    contracts, findings = parse_foundation_contracts(_FIXTURE)
    assert findings == [], findings
    assert contracts, "architect-authored Foundation Contracts block did not parse"
    paths = {str(c.get("path") or "") for c in contracts}
    assert "backend/auth.py" in paths, sorted(paths)
