"""Tests for A1c.1 / A1c.2 — eligibility ordering across Groups + Components.

Coverage:
- `Group.dependencies` is a working alias for `Group.deps` (back-compat).
- A Group whose `dependencies` references a Component id waits until the
  Component lands.
- A Component whose `dependencies` references another Component id is
  topologically ordered.
- A mixed dep chain Group -> Component -> Group orders correctly.
- `eligible_components` excludes already-landed and blocked Components.
"""

from __future__ import annotations

from otto.merge_queue import (
    eligible_candidates,
    eligible_components,
)
from otto.spec_compile import (
    Component,
    Group,
    Spec,
    StructureDecisions,
)


def _spec(
    *,
    groups: list[Group] | None = None,
    components: list[Component] | None = None,
) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=groups or [],
        components=components or [],
    )


# ---------------------------------------------------------------------------
# A1c.1 — Group.dependencies / Group.deps back-compat
# ---------------------------------------------------------------------------


def test_group_dependencies_alias_reads_deps() -> None:
    g = Group(id="g1", title="x", deps=["g0"])
    assert g.dependencies == ["g0"]
    # Same identity — no copy — so reads always reflect live edits.
    assert g.dependencies is g.deps


def test_group_dependencies_setter_replaces_deps() -> None:
    g = Group(id="g1", title="x", deps=["g0"])
    g.dependencies = ["gA", "gB"]
    assert g.deps == ["gA", "gB"]
    # Reads still reflect the same list.
    assert g.dependencies == ["gA", "gB"]


def test_eligible_candidates_reads_dependencies_alias() -> None:
    """Constructing a Group via `deps=` and reading via `.dependencies`
    inside `eligible_candidates` produces the right ordering."""
    spec = _spec(
        groups=[
            Group(id="g1", title="x", deps=[]),
            Group(id="g2", title="y", deps=["g1"]),
        ],
    )
    # g2 depends on g1 — g2 not eligible until g1 has landed.
    eligible = eligible_candidates(
        spec, passing_ids={"g1", "g2"}, landed_ids=set(),
    )
    assert [s.id for s in eligible] == ["g1"]

    eligible = eligible_candidates(
        spec, passing_ids={"g1", "g2"}, landed_ids={"g1"},
    )
    assert [s.id for s in eligible] == ["g2"]


# ---------------------------------------------------------------------------
# A1c.2 — Component dependencies threaded into eligibility ordering
# ---------------------------------------------------------------------------


def test_group_depends_on_component_waits_for_component_to_land() -> None:
    spec = _spec(
        groups=[
            # g1 depends on a Component (c1) — must wait until c1 lands.
            Group(id="g1", title="feat", deps=["c1"]),
        ],
        components=[
            Component(id="c1", name="ws-hub"),
        ],
    )
    # Both passing, nothing landed yet → g1 NOT eligible (c1 not landed).
    eligible = eligible_candidates(
        spec, passing_ids={"g1", "c1"}, landed_ids=set(),
    )
    assert eligible == []

    # Once c1 lands, g1 becomes eligible.
    eligible = eligible_candidates(
        spec, passing_ids={"g1", "c1"}, landed_ids={"c1"},
    )
    assert [s.id for s in eligible] == ["g1"]


def test_component_depends_on_component_topological_order() -> None:
    spec = _spec(
        components=[
            Component(id="c1", name="base"),
            Component(id="c2", name="indexer", dependencies=["c1"]),
        ],
    )
    # c2 not eligible until c1 lands.
    eligible = eligible_components(
        spec, passing_ids={"c1", "c2"}, landed_ids=set(),
    )
    assert [c.id for c in eligible] == ["c1"]

    eligible = eligible_components(
        spec, passing_ids={"c1", "c2"}, landed_ids={"c1"},
    )
    assert [c.id for c in eligible] == ["c2"]


def test_component_depends_on_group_waits_for_group_to_land() -> None:
    spec = _spec(
        groups=[Group(id="g1", title="auth", deps=[])],
        components=[
            Component(id="c1", name="search-index", dependencies=["g1"]),
        ],
    )
    eligible = eligible_components(
        spec, passing_ids={"g1", "c1"}, landed_ids=set(),
    )
    assert eligible == []

    eligible = eligible_components(
        spec, passing_ids={"g1", "c1"}, landed_ids={"g1"},
    )
    assert [c.id for c in eligible] == ["c1"]


def test_mixed_chain_group_component_group() -> None:
    """g1 -> c1 -> g2 — must land in order g1, c1, g2 even when all pass."""
    spec = _spec(
        groups=[
            Group(id="g1", title="auth", deps=[]),
            Group(id="g2", title="feed", deps=["c1"]),
        ],
        components=[
            Component(id="c1", name="search", dependencies=["g1"]),
        ],
    )
    landed: set[str] = set()

    # Round 1: only g1 is eligible.
    grp_eligible = eligible_candidates(
        spec, passing_ids={"g1", "g2"}, landed_ids=landed,
    )
    cmp_eligible = eligible_components(
        spec, passing_ids={"c1"}, landed_ids=landed,
    )
    assert [s.id for s in grp_eligible] == ["g1"]
    assert cmp_eligible == []

    # Land g1.
    landed.add("g1")

    # Round 2: c1 eligible (its dep g1 landed); g2 still blocked on c1.
    grp_eligible = eligible_candidates(
        spec, passing_ids={"g1", "g2"}, landed_ids=landed,
    )
    cmp_eligible = eligible_components(
        spec, passing_ids={"c1"}, landed_ids=landed,
    )
    assert grp_eligible == []
    assert [c.id for c in cmp_eligible] == ["c1"]

    # Land c1.
    landed.add("c1")

    # Round 3: g2 eligible.
    grp_eligible = eligible_candidates(
        spec, passing_ids={"g1", "g2"}, landed_ids=landed,
    )
    assert [s.id for s in grp_eligible] == ["g2"]


def test_eligible_components_excludes_landed_and_blocked() -> None:
    spec = _spec(
        components=[
            Component(id="c1", name="a"),
            Component(id="c2", name="b"),
            Component(id="c3", name="c"),
        ],
    )
    # c1 already landed, c2 blocked, c3 still passing.
    eligible = eligible_components(
        spec,
        passing_ids={"c1", "c2", "c3"},
        landed_ids={"c1"},
        blocked_ids={"c2"},
    )
    assert [c.id for c in eligible] == ["c3"]


def test_eligible_components_fifo_within_eligible_per_spec_order() -> None:
    spec = _spec(
        components=[
            Component(id="c1", name="a"),
            Component(id="c2", name="b"),
        ],
    )
    eligible = eligible_components(
        spec, passing_ids={"c1", "c2"}, landed_ids=set(),
    )
    assert [c.id for c in eligible] == ["c1", "c2"]
