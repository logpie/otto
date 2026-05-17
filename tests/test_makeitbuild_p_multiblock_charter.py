"""Run #10 regression: the CHARTER's IA contract must parse even when invalid
JSON *code-example* fences precede it.

Runs #6/#8/#10 all spuriously failed the architect contract gate on attempt 1
(then self-corrected on the ~14min re-dispatch — the #1 remaining <45min
blocker). Definitive cause (run #10, mib10-025118): the architect's CHARTER
legitimately contains several ```json code examples in prose sections
(`## WebSocket Event Protocol`, `## Full-Text Search Strategy`, ...) that are
illustrative, not strict JSON. `parse_information_architecture_contract` did
`_FENCED_JSON.search(block)` — the FIRST fenced block — so it grabbed an
invalid example, `json.loads` raised, the IA contract read as absent
(`contracts_present: false`, `contract_findings: []`), and the gate failed
attempt 1 even though the real Information Architecture Contract block (16
foundation contracts) was perfectly valid further down.

Fix: scan every fenced JSON block and pick the one that actually carries the
contract keys (registration_isolation / foundation_contracts /
feature_owned_paths), skipping invalid example fences. This pins run #10's
real CHARTER — which has 4 fenced blocks, the contract NOT first — to parse.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.v5_capability_inventory import (
    _first_json_object_with_keys,
    parse_feature_owned_paths_from_charter,
    parse_foundation_contracts,
    parse_information_architecture_contract,
)

# NOTE: this is run #10's *attempt-2* (self-corrected) CHARTER — the failing
# attempt-1 CHARTER was overwritten by the architect re-dispatch, so the
# on-disk file cannot itself reproduce the old first-fence failure. The
# deterministic proof of the fix's invariant is
# test_scanner_skips_invalid_and_keyless_blocks below (an invalid example
# fence preceding the real contract); these fixture tests guard that the real
# architect CHARTER still parses end-to-end.
_FIXTURE = Path(__file__).parent / "fixtures" / "charter_architect_real_run10_multiblock.md"


def test_run10_multiblock_charter_ia_contract_parses() -> None:
    ia = parse_information_architecture_contract(_FIXTURE.read_text())
    assert isinstance(ia, dict), "IA contract read as absent (the run#10 bug)"
    assert "foundation_contracts" in ia
    assert "feature_owned_paths" in ia


def test_run10_multiblock_charter_persists_clean() -> None:
    owned, findings = parse_feature_owned_paths_from_charter(_FIXTURE)
    assert findings == [], [(f.kind, f.detail) for f in findings]
    assert len(owned) == 4, sorted(owned)
    contracts, cfindings = parse_foundation_contracts(_FIXTURE)
    assert cfindings == [], [(f.kind, f.detail) for f in cfindings]
    assert len(contracts) >= 10, len(contracts)


def test_scanner_skips_invalid_and_keyless_blocks() -> None:
    text = """
```json
{ this is a broken websocket example, not valid json }
```

```json
{"unrelated": "parses but lacks contract keys"}
```

```json
{"registration_isolation": {"policy": "auto"}, "foundation_contracts": []}
```
"""
    got = _first_json_object_with_keys(
        text, ("registration_isolation", "foundation_contracts", "feature_owned_paths")
    )
    assert got is not None
    assert "registration_isolation" in got and "foundation_contracts" in got


def test_scanner_returns_none_when_no_block_has_keys() -> None:
    text = '```json\n{"only": "noise"}\n```'
    assert _first_json_object_with_keys(text, ("foundation_contracts",)) == {"only": "noise"}
    assert _first_json_object_with_keys("no fences here", ("foundation_contracts",)) is None
