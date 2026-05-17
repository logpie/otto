"""Run #5 regression: the architect groups feature_owned_paths under
self-invented category keys (backend / frontend / tests) — not a single
owned_paths / paths / may_add key. The parser must collect paths from EVERY
path-like list under a feature entry, not allowlist key names.

Run #5 (mib5-235140, --tier modular) reached the contract gate with all prior
fixes in place, then failed: parse_feature_owned_paths_from_charter yielded all
three feature children but with EMPTY path lists → three
`feature ownership entries must include owned_paths` findings → persist aborted
→ contract gate re-dispatch loop (same dead-end as run #2 and run #4, one
shape-variant later).

Root cause: _feature_ownership_items allowlisted a fixed set of synonym keys
(owned_paths / may_add / paths / globs / add / new_files / files) and took the
FIRST match only. The run #5 architect authored:

    "v5-886ccb4d5f04": {
      "description": "...",
      "backend":  ["backend/routers/auth.py", ...],
      "frontend": ["frontend/src/features/auth/**", ...],
      "tests":    ["backend/tests/test_auth.py", ...]
    }

None of backend/frontend/tests were in the allowlist → zero paths. The fix
stops allowlisting and instead collects every list-of-strings value under a
feature entry, skipping only known prose/identity metadata keys. This pins:
run #5's real CHARTER parses to 3 features with non-empty paths and ZERO
findings, and the run #2 (may_add) + run #4 shapes still parse — one protocol,
not another per-key patch.
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_capability_inventory import (
    _collect_path_strings,
    _feature_ownership_items,
    parse_feature_owned_paths_from_charter,
    parse_foundation_contracts,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "charter_architect_real_run5_layerkeys.md"


def test_run5_layerkey_charter_persists_with_zero_findings() -> None:
    owned_by_task, findings = parse_feature_owned_paths_from_charter(_FIXTURE)
    assert findings == [], [(f.kind, f.detail) for f in findings]
    assert len(owned_by_task) == 3, sorted(owned_by_task)
    for tid, paths in owned_by_task.items():
        assert paths, f"{tid} parsed to EMPTY (the run#5 layer-key bug)"
    # backend/frontend/tests categories must all be collected, not just one.
    all_paths = {p for ps in owned_by_task.values() for p in ps}
    assert any(p.startswith("backend/routers/") for p in all_paths), sorted(all_paths)
    assert any("frontend/src/features/" in p for p in all_paths), sorted(all_paths)
    assert any("tests/" in p for p in all_paths), sorted(all_paths)


def test_run5_foundation_contracts_still_parse() -> None:
    contracts, findings = parse_foundation_contracts(_FIXTURE)
    assert findings == [], [(f.kind, f.detail) for f in findings]
    assert len(contracts) >= 1


def test_collect_path_strings_handles_all_shapes() -> None:
    # Flat list (architect emits value directly as a path list).
    assert _collect_path_strings(["a.py", "./b.py", " c.py "]) == ["a.py", "b.py", "c.py"]
    # Single synonym key (run #2 shape).
    assert _collect_path_strings({"may_add": ["x.ts"]}) == ["x.ts"]
    # Multiple category keys, ALL collected (run #5 shape) — not first-only.
    got = _collect_path_strings(
        {
            "description": "prose, must be skipped",
            "backend": ["api/a.py"],
            "frontend": ["ui/b.tsx"],
            "tests": ["t/c.py"],
        }
    )
    assert got == ["api/a.py", "ui/b.tsx", "t/c.py"], got
    # Identity/metadata keys never leak in as paths even if list-valued.
    assert _collect_path_strings({"id": ["v5-x"], "paths": ["real.py"]}) == ["real.py"]


def test_list_payload_shape_still_supported() -> None:
    payload = [
        {"task_id": "v5-aa", "backend": ["s/a.py"], "frontend": ["w/b.tsx"]},
        {"id": "v5-bb", "owned_paths": ["c.py"]},
    ]
    items = dict(_feature_ownership_items(payload))
    assert items["v5-aa"] == ["s/a.py", "w/b.tsx"], items
    assert items["v5-bb"] == ["c.py"], items
