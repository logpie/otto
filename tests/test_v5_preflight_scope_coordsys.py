"""Regression: child-verify repair scope gate must reconcile the product-root
coordinate system before flagging scope violations.

Root cause (v5-itracker-setupfix3-005823, 2026-05-18, child v5-13ba9d13c4a2
child-verify): at child-verify time ``repair_unit.allowed_paths`` is the
architect's STALE *product-relative* initial-decomposition scope
(``backend/routers/auth.py``), while git ``changed_paths`` are
worktree-relative (``itracker/backend/routers/auth.py``) because the
architect scaffolds the product under a sub-directory. ``_evaluate_composite_gate``
compared the two raw coordinate systems, so EVERY repair edit was flagged
``scope_violation`` -> bounded repair budget exhausted -> 2/4 children
merge_blocked -> half-built product -> clean_deploy infra error -> no
proof-packet. The packet already carries the architect's AUTHORITATIVE
worktree-relative ``feature_owned_paths`` in ``product_contract.charter``.

Fix: reconcile the scope into git's worktree-relative coordinate system at
the single enforcement seam (``_reconcile_scope_allowed_paths``), using the
authoritative CHARTER IA in the packet. Consistent-by-construction, NOT
gate-weakening (a path genuinely outside the task's ownership still
violates), no behavior change when CHARTER is absent / scopes already
align / scope_policy != allowed_paths.
"""

from __future__ import annotations

import types

from otto.v5_preflight_repair import (
    RepairBudget,
    RepairPacket,
    _evaluate_composite_gate,
    _path_allowed,
    _reconcile_scope_allowed_paths,
)

# A realistic CHARTER with the architect's authoritative, worktree-relative
# (``itracker/``-prefixed) Information Architecture Contract.
CHARTER = """# ITracker — Project Charter

## Project Layout

All source code lives under `itracker/`. The worktree root contains this
CHARTER and decisions.md only.

## Information Architecture Contract

```json
{
  "registration_isolation": {
    "policy": "file-local",
    "shared_registry_files": [
      {"path": "itracker/backend/main.py", "leaf_edit": false}
    ],
    "leaf_extension_globs": ["itracker/backend/routers/*.py"]
  },
  "feature_owned_paths": {
    "v5-13ba9d13c4a2": [
      "itracker/backend/routers/auth.py",
      "itracker/backend/routers/users.py",
      "itracker/frontend/src/features/auth/"
    ],
    "v5-5cd03bb97688": [
      "itracker/backend/routers/issues.py"
    ]
  }
}
```
"""

# What the stale product-relative repair_unit.allowed_paths looked like.
STALE_PRODUCT_RELATIVE_ALLOWED = [
    "backend/routers/auth.py",
    "backend/routers/users.py",
    "frontend/src/features/auth/",
]


def _product_contract(charter_text: str | None) -> dict:
    if charter_text is None:
        return {}
    return {"charter": {"exists": True, "path": "CHARTER.md", "text": charter_text}}


def test_reconcile_admits_worktree_relative_changed_path() -> None:
    """The real failing case: stale product-relative allowed_paths +
    itracker/-prefixed git changed path. After reconciliation the changed
    path is in scope (no false scope_violation)."""
    effective = _reconcile_scope_allowed_paths(
        product_contract=_product_contract(CHARTER),
        task_id="v5-13ba9d13c4a2",
        allowed_paths=STALE_PRODUCT_RELATIVE_ALLOWED,
        conflict_scope_paths=[],
    )
    # git changed_paths are worktree-relative
    assert _path_allowed("itracker/backend/routers/workspaces.py".replace(
        "workspaces", "auth"), effective)
    assert _path_allowed("itracker/backend/routers/users.py", effective)
    # authoritative CHARTER dir entry covers nested files
    assert _path_allowed("itracker/frontend/src/features/auth/Login.tsx", effective)


def test_reconcile_does_not_weaken_gate() -> None:
    """A path genuinely outside the task's ownership is STILL a violation
    after reconciliation (gate not weakened)."""
    effective = _reconcile_scope_allowed_paths(
        product_contract=_product_contract(CHARTER),
        task_id="v5-13ba9d13c4a2",
        allowed_paths=STALE_PRODUCT_RELATIVE_ALLOWED,
        conflict_scope_paths=[],
    )
    # owned by a DIFFERENT task per the authoritative CHARTER
    assert not _path_allowed("itracker/backend/routers/issues.py", effective)
    # not owned by anyone / not under the stale allowed scope
    assert not _path_allowed("itracker/backend/secret_exfil.py", effective)
    assert not _path_allowed("itracker/backend/main.py", effective)


def test_no_charter_is_unchanged_behavior() -> None:
    """CHARTER absent/unparseable -> effective == sorted(dedupe(allowed ∪
    conflict)) exactly (no behavior change, safe fallback)."""
    base_allowed = ["backend/routers/auth.py"]
    conflict = ["decisions.md"]
    effective = _reconcile_scope_allowed_paths(
        product_contract=_product_contract(None),
        task_id="v5-13ba9d13c4a2",
        allowed_paths=base_allowed,
        conflict_scope_paths=conflict,
    )
    assert effective == sorted(dict.fromkeys([*base_allowed, *conflict]))


def test_charter_authoritative_entry_used_even_if_stale_allowed_empty() -> None:
    """If the stale allowed_paths is empty/missing but the CHARTER has an
    authoritative worktree-relative entry, that entry IS the scope basis."""
    effective = _reconcile_scope_allowed_paths(
        product_contract=_product_contract(CHARTER),
        task_id="v5-13ba9d13c4a2",
        allowed_paths=[],
        conflict_scope_paths=[],
    )
    assert _path_allowed("itracker/backend/routers/auth.py", effective)
    assert not _path_allowed("itracker/backend/routers/issues.py", effective)


def _packet(*, allowed: list[str], charter: str | None, task_id: str) -> RepairPacket:
    return RepairPacket(
        repair_unit={
            "id": f"{task_id}-child-verify",
            "task_id": task_id,
            "allowed_paths": allowed,
            "scope_policy": "allowed_paths",
            "worktree": "/tmp/does-not-matter",
        },
        acceptance_oracle={},
        latest_oracle_result={},
        product_contract=_product_contract(charter),
        integration_context={},
        attempt_history=[],
        current_state={},
        budget=RepairBudget(),
        packet_dir=__import__("pathlib").Path("/tmp/does-not-matter"),
    )


def test_evaluate_composite_gate_uses_reconciliation(monkeypatch) -> None:
    """End-to-end at the enforcement seam: the gate must NOT report a
    scope_violation for an itracker/-prefixed changed path when the stale
    allowed_paths is product-relative but the CHARTER pins ownership."""
    import otto.v5_preflight_repair as pr

    changed = ["itracker/backend/routers/auth.py"]
    monkeypatch.setattr(pr, "_changed_paths_since_repair_start",
                         lambda *a, **k: list(changed))
    monkeypatch.setattr(pr, "_modified_paths_since_baseline",
                         lambda *a, **k: [])
    monkeypatch.setattr(pr, "_has_conflict_markers", lambda *a, **k: False)
    monkeypatch.setattr(pr, "_unmerged_path_names", lambda *a, **k: [])

    packet = _packet(
        allowed=STALE_PRODUCT_RELATIVE_ALLOWED,
        charter=CHARTER,
        task_id="v5-13ba9d13c4a2",
    )
    oracle_result = types.SimpleNamespace(passed=True)
    gate = _evaluate_composite_gate(
        packet, oracle_result, require_clean_worktree=False
    )
    assert gate["scope_ok"] is True, gate["scope_violations"]
    assert gate["scope_violations"] == []

    # Control: a foreign path must still violate (gate not weakened).
    changed[:] = ["itracker/backend/routers/issues.py"]
    gate2 = _evaluate_composite_gate(
        packet, oracle_result, require_clean_worktree=False
    )
    assert gate2["scope_ok"] is False
    assert "itracker/backend/routers/issues.py" in gate2["scope_violations"]
