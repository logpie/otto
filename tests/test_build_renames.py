"""Tests for the Slice→Group vocabulary renames in otto/build.py.

Covers progress.md A1b.1 (function-name rename) and A1b.2 (BuildBudget
field rename). Both renames are *additive*: legacy names continue to
work as kwargs / attributes / module-level callables.
"""

from __future__ import annotations

import otto.build as build_mod
from otto.build import BuildBudget


# ---------------------------------------------------------------------------
# A1b.1 — build_groups / build_slices are aliases for run_build
# ---------------------------------------------------------------------------


def test_build_groups_is_canonical_dispatch_entry() -> None:
    """`build_groups` is exposed at module level and equals `run_build`."""
    assert hasattr(build_mod, "build_groups")
    assert build_mod.build_groups is build_mod.run_build


def test_build_slices_legacy_alias_still_resolves() -> None:
    """The legacy `build_slices` name keeps resolving for back-compat."""
    assert hasattr(build_mod, "build_slices")
    assert build_mod.build_slices is build_mod.run_build


def test_aliases_are_in_public_all() -> None:
    """Both names are re-exported via __all__."""
    assert "build_groups" in build_mod.__all__
    assert "build_slices" in build_mod.__all__
    assert "run_build" in build_mod.__all__


# ---------------------------------------------------------------------------
# A1b.2 — BuildBudget canonical fields are per_group_*, legacy per_slice_*
# names still work as kwargs and attribute reads/writes.
# ---------------------------------------------------------------------------


def test_build_budget_canonical_per_group_kwargs() -> None:
    """The canonical kwargs use per_group_* names."""
    b = BuildBudget(
        per_group_retries_hard_cap=7,
        per_group_wall_s=99,
        per_group_cost_usd=12.5,
    )
    assert b.per_group_retries_hard_cap == 7
    assert b.per_group_wall_s == 99
    assert b.per_group_cost_usd == 12.5


def test_build_budget_legacy_per_slice_kwargs_still_work() -> None:
    """Passing legacy per_slice_* kwargs writes to the canonical field."""
    b = BuildBudget(
        per_slice_retries_hard_cap=4,
        per_slice_wall_s=42,
        per_slice_cost_usd=5.0,
    )
    # Canonical attributes reflect the legacy-passed values.
    assert b.per_group_retries_hard_cap == 4
    assert b.per_group_wall_s == 42
    assert b.per_group_cost_usd == 5.0
    # Legacy attribute reads also resolve to the same value.
    assert b.per_slice_retries_hard_cap == 4
    assert b.per_slice_wall_s == 42
    assert b.per_slice_cost_usd == 5.0


def test_build_budget_canonical_kwargs_visible_under_legacy_alias() -> None:
    """Setting via canonical kwarg → legacy alias attribute read sees it."""
    b = BuildBudget(per_group_cost_usd=5.0)
    assert b.per_slice_cost_usd == 5.0
    assert b.per_group_cost_usd == 5.0


def test_build_budget_legacy_attribute_setter_writes_canonical() -> None:
    """Assigning to a legacy attribute writes through to the canonical one."""
    b = BuildBudget(per_group_cost_usd=1.0)
    b.per_slice_cost_usd = 8.0  # type: ignore[misc]
    assert b.per_group_cost_usd == 8.0
    assert b.per_slice_cost_usd == 8.0


def test_build_budget_rejects_both_canonical_and_legacy_for_same_field() -> None:
    """Passing both names for the same field is a programmer error."""
    import pytest

    with pytest.raises(TypeError):
        BuildBudget(per_group_cost_usd=1.0, per_slice_cost_usd=2.0)


def test_build_budget_defaults_route_through_otto_defaults() -> None:
    """No-arg construction picks up values from otto/defaults.py.

    We assert positivity rather than exact numbers — defaults can change,
    but they must always be present and finite for the canonical fields.
    """
    b = BuildBudget()
    assert b.per_group_retries_hard_cap > 0
    assert b.per_group_wall_s > 0
    assert b.per_group_cost_usd > 0
    # Legacy attribute reads also work on a default-constructed budget.
    assert b.per_slice_retries_hard_cap == b.per_group_retries_hard_cap
    assert b.per_slice_wall_s == b.per_group_wall_s
    assert b.per_slice_cost_usd == b.per_group_cost_usd


def test_build_budget_charge_and_remaining_helpers_unchanged() -> None:
    """Renames are additive: existing helper methods still behave correctly."""
    b = BuildBudget(total_repair_s=100, total_cost_usd=10.0)
    assert b.remaining_repair_s() == 100
    assert b.remaining_total_cost_usd() == 10.0
    b.charge_repair(30)
    b.charge_cost(2.5)
    assert b.remaining_repair_s() == 70
    assert b.remaining_total_cost_usd() == 7.5
