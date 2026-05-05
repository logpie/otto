"""Tests for A1b.4 — Spec.shared_paths handling in scope detection.

`shared_paths` are files/globs that any Group or Component may freely
modify. The merge queue serializes lands so two units editing the same
shared path don't collide. Unlike `shared_scaffold` (which is the
older legacy field), `shared_paths` is the new design vocabulary
(research §2.6).

Coverage:
- A Group editing a path matching `Spec.shared_paths` produces no scope
  warning.
- A Group editing a peer Group's `owned_paths` still produces a scope
  warning (existing behavior unchanged).
- A Group editing a Component's `owned_paths` produces a scope warning
  (Components participate in the same scope partition).
- shared_paths overrides peer ownership: even when the path matches a
  peer's owned_paths, if it's also in shared_paths, no warning.
"""

from __future__ import annotations

from pathlib import Path

from otto.build import detect_scope_violations
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
    shared_paths: list[str] | None = None,
    shared_scaffold: list[str] | None = None,
) -> Spec:
    return Spec(
        intent="t",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=groups or [],
        components=components or [],
        shared_paths=shared_paths or [],
        shared_scaffold=shared_scaffold or [],
    )


# ---------------------------------------------------------------------------
# shared_paths positive case — no warning
# ---------------------------------------------------------------------------


def test_shared_paths_allows_modification_no_warning(tmp_path: Path) -> None:
    g1 = Group(id="g1", name="x", owned_paths=["src/a/**"])
    g2 = Group(id="g2", name="y", owned_paths=["src/b/**"])
    spec = _spec(groups=[g1, g2], shared_paths=["models.py", "app.py"])

    # Pre-existing shared file. (models.py exists on disk → "modification".)
    (tmp_path / "models.py").write_text("# existing", encoding="utf-8")

    violations = detect_scope_violations(
        g1, spec, ["models.py"], project_root=tmp_path,
    )
    assert violations == []


def test_shared_paths_glob_match(tmp_path: Path) -> None:
    g1 = Group(id="g1", name="x", owned_paths=["src/a/**"])
    spec = _spec(groups=[g1], shared_paths=["**/*.config.json"])

    (tmp_path / "x.config.json").write_text("{}", encoding="utf-8")
    violations = detect_scope_violations(
        g1, spec, ["x.config.json"], project_root=tmp_path,
    )
    assert violations == []


def test_shared_paths_overrides_peer_ownership(tmp_path: Path) -> None:
    """If a path matches both a peer's owned_paths AND shared_paths,
    shared_paths wins — no scope warning."""
    g1 = Group(id="g1", name="x", owned_paths=["src/a/**"])
    g2 = Group(id="g2", name="y", owned_paths=["app.py"])
    spec = _spec(groups=[g1, g2], shared_paths=["app.py"])

    (tmp_path / "app.py").write_text("# existing", encoding="utf-8")
    violations = detect_scope_violations(
        g1, spec, ["app.py"], project_root=tmp_path,
    )
    assert violations == []


# ---------------------------------------------------------------------------
# Peer ownership still flagged when path is NOT in shared_paths
# ---------------------------------------------------------------------------


def test_peer_owned_path_still_flagged(tmp_path: Path) -> None:
    g1 = Group(id="g1", name="x", owned_paths=["src/a/**"])
    g2 = Group(id="g2", name="y", owned_paths=["src/b/**"])
    spec = _spec(groups=[g1, g2])  # no shared_paths

    # Pre-existing peer file: g1 modifying it is a violation.
    (tmp_path / "src" / "b").mkdir(parents=True)
    peer_file = tmp_path / "src" / "b" / "peer.py"
    peer_file.write_text("# peer", encoding="utf-8")

    violations = detect_scope_violations(
        g1, spec, ["src/b/peer.py"], project_root=tmp_path,
    )
    assert violations == ["src/b/peer.py"]


# ---------------------------------------------------------------------------
# Components participate in scope partition (A1b.4)
# ---------------------------------------------------------------------------


def test_component_owned_path_flagged_when_peer(tmp_path: Path) -> None:
    """A Group modifying a peer Component's owned path triggers a scope
    warning, just like a peer Group."""
    g1 = Group(id="g1", name="x", owned_paths=["src/a/**"])
    c1 = Component(id="ws-hub", name="WS hub", owned_paths=["src/ws/**"])
    spec = _spec(groups=[g1], components=[c1])

    (tmp_path / "src" / "ws").mkdir(parents=True)
    f = tmp_path / "src" / "ws" / "hub.py"
    f.write_text("# hub", encoding="utf-8")

    violations = detect_scope_violations(
        g1, spec, ["src/ws/hub.py"], project_root=tmp_path,
    )
    assert violations == ["src/ws/hub.py"]


def test_component_dep_owned_path_allowed(tmp_path: Path) -> None:
    """If a Group declares a Component as a dependency, the Group may
    extend the Component's owned files (transitive-dep rule)."""
    c1 = Component(id="ws-hub", name="WS hub", owned_paths=["src/ws/**"])
    g1 = Group(id="g1", name="x", dependencies=["ws-hub"], owned_paths=["src/a/**"])
    spec = _spec(groups=[g1], components=[c1])

    (tmp_path / "src" / "ws").mkdir(parents=True)
    f = tmp_path / "src" / "ws" / "hub.py"
    f.write_text("# hub", encoding="utf-8")

    violations = detect_scope_violations(
        g1, spec, ["src/ws/hub.py"], project_root=tmp_path,
    )
    assert violations == []


def test_shared_paths_allows_component_to_modify(tmp_path: Path) -> None:
    """Components also benefit from shared_paths — they're units like
    Groups for scope-partition purposes."""
    g1 = Group(id="g1", name="x", owned_paths=["src/a/**"])
    c1 = Component(id="c1", name="C1", owned_paths=["src/c/**"])
    spec = _spec(groups=[g1], components=[c1], shared_paths=["app.py"])

    (tmp_path / "app.py").write_text("# existing", encoding="utf-8")

    # The Component (treated as the focus unit) modifying app.py: no warning.
    # detect_scope_violations accepts any object with id+owned_paths; we
    # adapt the Component via the same helper run_build uses.
    from otto.build import _component_as_slice

    adapter = _component_as_slice(c1)
    violations = detect_scope_violations(
        adapter, spec, ["app.py"], project_root=tmp_path,
    )
    assert violations == []
